"""
runner.py — pexpect-based resilient subprocess runner.

Architecture (v2 — correct PTY passthrough)
============================================
OLD (broken):
  Our raw mode + manual stdin-forward thread  ←→  pexpect PTY
  Result: two layers fighting each other → garbled escape sequences

NEW (correct):
  child.interact(output_filter=fn)
    pexpect handles ALL raw-mode / PTY passthrough natively.
    We hook into output_filter to buffer child output.
    A separate watcher thread scans the buffer and calls child.send()
    when an error / permission pattern is matched.

Signal handling
===============
  Ctrl+C:  pexpect's interact() is in raw mode → \x03 is forwarded to child
           child's PTY slave has ISIG → child gets SIGINT → handles it itself
           (This is correct: Ctrl+C in Claude Code cancels the current task)
  SIGWINCH: we forward the new terminal size to the child PTY
  SIGTERM / close tab: SIGHUP propagates naturally through the PTY chain
"""

from __future__ import annotations

import codecs
import os
import errno
import re
import signal
import sys
import time
import threading
import hashlib
from typing import Optional

import pexpect
from rich.console import Console

from dog.patterns import (
    COMMON_RETRY_RULES,
    TOOL_RETRY_RULES,
    PERMISSION_RULES,
    FATAL_PATTERNS,
    INTERRUPTION_PATTERNS,
    SUCCESS_PATTERNS,
)

console = Console(stderr=True)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_HIGHLIGHTED_ACTION_PREFIX_RE = r"(?:\x1b\[[0-9;?]*[ -/]*[@-~])*[ \t]*[›❯>][ \t]*"
_STATUS_LOCK = threading.Lock()
_LAST_STATUS_MESSAGE = ""


def _interactive_tty() -> bool:
    stdout_tty = getattr(sys.stdout, "isatty", lambda: False)()
    stderr_tty = getattr(sys.stderr, "isatty", lambda: False)()
    return bool(stdout_tty and stderr_tty)


def _compile(patterns: list[str]) -> re.Pattern:
    combined = "|".join(f"(?:{p})" for p in patterns)
    return re.compile(combined, re.IGNORECASE)


def _normalize_signature(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = re.sub(r"\s+", " ", text.strip().lower())
    return text[:240]


def _signature_id(label: str, matched_text: str) -> str:
    normalized = f"{label}|{_normalize_signature(matched_text)}"
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]


def _build_echo_pattern(command: str) -> Optional[re.Pattern]:
    normalized = command.strip()
    if not normalized:
        return None
    escaped = re.escape(normalized)
    return re.compile(
        rf"(^|[\r\n])(?:\x1b\[[0-9;?]*[ -/]*[@-~])*[ \t]*"
        rf"(?:(?:[›❯>])[ \t]*)?(?:\x1b\[[0-9;?]*[ -/]*[@-~])*"
        rf"{escaped}(?:\x1b\[[0-9;?]*[ -/]*[@-~])*[ \t]*(?=($|[\r\n]|\x1b))",
        re.IGNORECASE,
    )


def _build_echo_bytes_pattern(command: str) -> Optional[re.Pattern[bytes]]:
    normalized = command.strip()
    if not normalized:
        return None
    escaped = re.escape(normalized.encode("utf-8"))
    return re.compile(
        rb"(^|[\r\n])(?:\x1b\[[0-9;?]*[ -/]*[@-~])*[ \t]*"
        rb"(?:(?:\xe2\x80\xba|\xe2\x9d\xaf|>)[ \t]*)?(?:\x1b\[[0-9;?]*[ -/]*[@-~])*"
        + escaped
        + rb"(?:\x1b\[[0-9;?]*[ -/]*[@-~])*[ \t]*(?=($|[\r\n]|\x1b))",
        re.IGNORECASE,
    )


def _select_highlighted_response(response: str, screen_text: str) -> str:
    stripped = response.strip().lower()
    if stripped not in {"retry", "continue"}:
        return response

    normalized_screen = _ANSI_RE.sub("", screen_text)
    pattern = re.compile(
        rf"(?:^|[\r\n]){_HIGHLIGHTED_ACTION_PREFIX_RE}{re.escape(stripped)}(?=[ \t]*(?:$|[\r\n]))",
        re.IGNORECASE,
    )
    if pattern.search(normalized_screen):
        return "\r"
    return response


def _screen_has_input_prompt(screen_text: str) -> bool:
    normalized_screen = _ANSI_RE.sub("", screen_text)
    for raw_line in reversed(normalized_screen.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        if re.fullmatch(r"[─━\-_=]{3,}", line):
            continue
        if re.fullmatch(r"[›❯>]", line):
            return True
        if re.fullmatch(r"[›❯>][ \t]+.+", line):
            return False
    return False


def _infer_profile(command: str, profile: Optional[str]) -> Optional[str]:
    if profile:
        return profile
    first = command.strip().split(maxsplit=1)[0].lower() if command.strip() else ""
    return first if first in {"claude", "codex", "opencode"} else None


def _build_retry_rules(
    profile: Optional[str], extra_rules: Optional[list[dict]] = None
) -> list[dict]:
    rules: list[dict] = []
    if profile != "claude":
        rules.extend(COMMON_RETRY_RULES)
    if profile:
        rules.extend(TOOL_RETRY_RULES.get(profile, []))
    if extra_rules:
        rules.extend(extra_rules)
    return sorted(rules, key=lambda r: r.get("priority", 50))


def _status_write(
    message: str = "", *, newline: bool = False, clear: bool = False
) -> None:
    global _LAST_STATUS_MESSAGE
    with _STATUS_LOCK:
        if _interactive_tty():
            if clear:
                sys.stderr.write("\r\033[2K")
                if not message and newline:
                    sys.stderr.write("\n")
                if not message:
                    _LAST_STATUS_MESSAGE = ""
                    sys.stderr.flush()
                    return
            if message and message == _LAST_STATUS_MESSAGE and not newline:
                return
            sys.stderr.write("\r\033[2K")
            if message:
                sys.stderr.write(message)
            if newline:
                sys.stderr.write("\n")
            _LAST_STATUS_MESSAGE = message if message else ""
            sys.stderr.flush()
            return
        if clear and not message and not newline:
            return
        if message and message == _LAST_STATUS_MESSAGE:
            return
        if message:
            sys.stderr.write(message)
        if newline or message:
            sys.stderr.write("\n")
        _LAST_STATUS_MESSAGE = message if message else ""
        sys.stderr.flush()


def _status_clear() -> None:
    _status_write(clear=True)


def _status_show(message: str) -> None:
    _status_write(message)


def _status_log(message: str) -> None:
    _status_write(message, newline=True, clear=True)


def _format_wait_status(
    *,
    action: str,
    retry_count: int,
    max_retries: int,
    label: str,
    response: str,
    remaining: float,
) -> str:
    response_preview = repr(response.strip()) or "<Enter>"
    return (
        f"dog {action} {retry_count}/{max_retries}: {label}; "
        f"sending {response_preview} in {remaining:.1f}s"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pattern watcher — runs in a background daemon thread
# ─────────────────────────────────────────────────────────────────────────────


class PatternWatcher:
    """
    Consumes child output (fed via .feed()), scans for patterns,
    and calls child.send() to inject recovery commands.
    Designed to run in a dedicated daemon thread.
    """

    def __init__(
        self,
        child: pexpect.spawn,
        rule_patterns: list,
        permission_patterns: list,
        fatal_re: re.Pattern,
        interrupt_re: re.Pattern,
        success_re: re.Pattern,
        max_retries: int,
        auto_permission: bool,
        profile: Optional[str] = None,
        retry_counts: Optional[dict[str, int]] = None,
    ) -> None:
        self._child = child
        self._rule_patterns = rule_patterns
        self._perm_patterns = permission_patterns
        self._fatal_re = fatal_re
        self._interrupt_re = interrupt_re
        self._success_re = success_re
        self._max_retries = max_retries
        self._auto_permission = auto_permission
        self._profile = profile

        self._buf = ""
        self._lock = threading.Lock()
        self._notify = threading.Event()
        self._stop_event = threading.Event()
        self._retry_counts = retry_counts if retry_counts is not None else {}
        self._success_seen = False
        self._interrupted_seen = False
        self._input_buf = ""
        self._last_triggered_at: dict[str, float] = {}
        self._pending_echo_suppression: list[tuple[re.Pattern, float]] = []
        self._last_output_at = 0.0
        self._active_actions = 0
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._restart_request: Optional[dict[str, str | int]] = None
        self._output_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._input_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        # Prevent rapid re-firing on the same chunk
        self._last_action_time = 0.0

    # ── Called from output_filter (main thread) ───────────────────────────────

    def feed(self, data: bytes) -> bytes:
        visible_bytes = self._suppress_auto_echo_bytes(data)
        text = self._output_decoder.decode(visible_bytes, final=False)
        with self._lock:
            self._buf += text
            if text:
                self._last_output_at = time.monotonic()
            if len(self._buf) > 8192:
                self._buf = self._buf[-8192:]
        self._notify.set()
        return visible_bytes

    def stop(self) -> None:
        self._stop_event.set()
        self._notify.set()

    def consume_restart_request(self) -> Optional[dict[str, str | int]]:
        with self._lock:
            request = self._restart_request
            self._restart_request = None
            return request

    def wait_for_idle(
        self, start_timeout: float = 0.25, finish_timeout: float = 0.0
    ) -> bool:
        timeout = max(start_timeout, 0.0) + max(finish_timeout, 0.0)
        deadline = time.monotonic() + timeout

        while True:
            with self._lock:
                active_actions = self._active_actions
                notify_pending = self._notify.is_set()

            if active_actions == 0 and not notify_pending:
                return True

            if time.monotonic() >= deadline:
                return False

            if active_actions > 0:
                remaining = max(deadline - time.monotonic(), 0.0)
                self._idle_event.wait(timeout=min(remaining, 0.1))
            else:
                time.sleep(0.01)

    def _suppress_auto_echo_bytes(self, data: bytes) -> bytes:
        now = time.monotonic()
        with self._lock:
            rules = [
                (pattern, expires_at)
                for pattern, expires_at in self._pending_echo_suppression
                if expires_at > now
            ]
            self._pending_echo_suppression = rules

        if not rules:
            return data

        filtered = data
        for pattern, _expires_at in rules:
            filtered = pattern.sub(lambda m: m.group(1), filtered)
        return filtered

    def note_user_input(self, data: bytes) -> bytes:
        text = self._input_decoder.decode(data, final=False)
        with self._lock:
            for ch in text:
                if ch in ("\r", "\n"):
                    if self._input_buf.strip():
                        self._success_seen = False
                        self._interrupted_seen = False
                        self._buf = ""
                        self._last_triggered_at = {}
                        self._retry_counts = {}
                    self._input_buf = ""
                elif ch == "\x1b":
                    self._input_buf = ""
                elif ch in ("\x7f", "\b"):
                    self._input_buf = self._input_buf[:-1]
                elif ch.isprintable():
                    self._input_buf += ch
                    if len(self._input_buf) > 512:
                        self._input_buf = self._input_buf[-512:]
        return data

    # ── Background thread entry point ─────────────────────────────────────────

    def run(self) -> None:
        while not self._stop_event.is_set():
            fired = self._notify.wait(timeout=1.0)
            if not fired:
                continue
            self._notify.clear()
            if self._stop_event.is_set():
                break

            with self._lock:
                buf = self._buf

            # Throttle: don't act more than once per 0.5 s
            if time.monotonic() - self._last_action_time < 0.5:
                continue

            # 1. Fatal
            if self._fatal_re.search(buf):
                console.print(
                    "\n[bold red]💀 dog: FATAL error — aborting (no retry).[/]"
                )
                try:
                    self._child.close(force=True)
                except Exception:
                    pass
                os._exit(2)

            # 2. Success
            if self._success_re.search(buf) and not self._success_seen:
                self._success_seen = True
                self._interrupted_seen = False
                self._retry_counts = {}
                console.print(
                    "\n[bold green]🎉 dog: task completed — "
                    "waiting for your next input.[/]"
                )
                with self._lock:
                    self._buf = ""
                    self._last_triggered_at = {}
                self._last_action_time = time.monotonic()
                continue

            if self._success_seen:
                continue

            # 3. User interruption / cancel
            if self._interrupt_re.search(buf):
                self._interrupted_seen = True
                self._success_seen = False
                self._retry_counts = {}
                with self._lock:
                    self._buf = ""
                    self._last_triggered_at = {}
                self._last_action_time = time.monotonic()
                continue

            if self._interrupted_seen:
                continue

            # 4. Permission auto-approve
            if self._auto_permission:
                matched = self._match(buf, self._perm_patterns)
                rule = matched[0] if matched else None
                if rule:
                    self._do_permission(rule, matched[1])
                    continue

            # 5. Retry
            matched = self._match(buf, self._rule_patterns)
            rule = matched[0] if matched else None
            if rule:
                self._success_seen = False
                self._do_retry(rule, matched[1])

    def _wait_with_progress(
        self,
        delay: float,
        *,
        action: str,
        response: str,
        label: str,
        retry_count: int,
    ) -> bool:
        delay = max(float(delay), 0.0)
        if delay <= 0:
            return True

        deadline = time.monotonic() + delay
        last_shown_second: Optional[int] = None

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _status_clear()
                return True
            if self._user_is_typing():
                _status_clear()
                return False
            if _interactive_tty():
                whole_seconds = int(remaining)
                if whole_seconds != last_shown_second:
                    _status_show(
                        _format_wait_status(
                            action=action,
                            retry_count=retry_count,
                            max_retries=self._max_retries,
                            label=label,
                            response=response,
                            remaining=remaining,
                        )
                    )
                    last_shown_second = whole_seconds
            if self._stop_event.wait(min(0.2, remaining)):
                _status_clear()
                return False

    def _response_needs_settled_prompt(
        self, response: str, rule: Optional[dict] = None
    ) -> bool:
        stripped = response.strip().lower()
        if stripped not in {"retry", "continue"}:
            return False
        if rule and rule.get("allow_plain_prompt"):
            return True
        return self._profile in {"codex", "opencode"}

    def _ready_response_for_profile(
        self, rule: dict, response: str, screen_text: str
    ) -> tuple[bool, str]:
        selected = _select_highlighted_response(response, screen_text)
        if selected == "\r":
            return True, selected
        if rule.get("allow_plain_prompt") and _screen_has_input_prompt(screen_text):
            return True, selected
        return False, selected

    def _rule_is_actionable(self, rule: dict, screen_text: str) -> bool:
        response = rule.get("response", "retry\n")
        if not self._response_needs_settled_prompt(response, rule):
            return True
        if not self._child_is_alive():
            return True

        ready, _selected = self._ready_response_for_profile(rule, response, screen_text)
        return ready

    def _wait_for_input_prompt(
        self,
        rule: dict,
        response: str,
        *,
        action: str,
        label: str,
        retry_count: int,
        timeout: float = 8.0,
        quiet_period: float = 0.35,
        stable_period: float = 0.15,
    ) -> Optional[str]:
        if not self._response_needs_settled_prompt(response, rule):
            return response

        deadline = time.monotonic() + max(timeout, 0.0)
        ready_signature: Optional[str] = None
        ready_since: Optional[float] = None
        while True:
            with self._lock:
                screen_text = self._buf
                last_output_at = self._last_output_at

            now = time.monotonic()
            prompt_ready, selected = self._ready_response_for_profile(
                rule, response, screen_text
            )
            screen_signature = _normalize_signature(screen_text)
            if prompt_ready:
                if screen_signature != ready_signature:
                    ready_signature = screen_signature
                    ready_since = now
            else:
                ready_signature = None
                ready_since = None

            output_quiet = (now - last_output_at) >= max(quiet_period, 0.0)
            prompt_stable = ready_since is not None and (now - ready_since) >= max(
                stable_period, 0.0
            )
            if prompt_ready and output_quiet and prompt_stable:
                _status_clear()
                return selected

            if now >= deadline:
                _status_clear()
                return selected if prompt_ready or not self._child_is_alive() else None
            if self._user_is_typing():
                _status_clear()
                return None
            if not self._child_is_alive():
                return selected

            _status_show(
                f"dog {action} {retry_count}/{self._max_retries}: {label}; "
                f"waiting for prompt to settle before sending {repr(response.strip()) or '<Enter>'}"
            )
            if self._stop_event.wait(0.1):
                _status_clear()
                return None

    def _child_is_alive(self) -> bool:
        isalive = getattr(self._child, "isalive", None)
        if callable(isalive):
            try:
                return bool(isalive())
            except Exception:
                return False
        return getattr(self._child, "exitstatus", None) is None

    def _user_is_typing(self) -> bool:
        with self._lock:
            return bool(self._input_buf.strip())

    def _match(self, text: str, patterns: list) -> Optional[tuple[dict, str]]:
        for pat, rule in patterns:
            match = pat.search(text)
            if not match:
                continue
            matched_text = match.group(0).strip().lower()
            label = rule.get("label", "pattern")
            signature_id = _signature_id(label, matched_text)
            cooldown = max(float(rule.get("delay", 1.0)), 2.0)
            last_triggered_at = self._last_triggered_at.get(signature_id, 0.0)
            if time.monotonic() - last_triggered_at < cooldown:
                continue
            if not self._rule_is_actionable(rule, text):
                continue
            return rule, matched_text
        return None

    def _do_permission(self, rule: dict, matched_text: str) -> None:
        with self._lock:
            self._active_actions += 1
            self._idle_event.clear()
        try:
            self._retry_counts = {}
            delay = rule.get("delay", 0.3)
            label = rule.get("label", "permission")
            response = rule.get("response", "y\n")

            _status_log(
                f"dog auto-approve: {label}; "
                f"sending {repr(response.strip()) or '<Enter>'} in {delay:.1f}s"
            )
            if not self._wait_with_progress(
                delay,
                action="auto-approve",
                response=response,
                label=label,
                retry_count=1,
            ):
                return
            self._safe_send(response)
            with self._lock:
                self._buf = ""
                self._last_triggered_at[_signature_id(label, matched_text)] = (
                    time.monotonic()
                )
            self._last_action_time = time.monotonic()
        finally:
            with self._lock:
                self._active_actions -= 1
                if self._active_actions == 0:
                    self._idle_event.set()

    def _do_retry(self, rule: dict, matched_text: str) -> None:
        with self._lock:
            self._active_actions += 1
            self._idle_event.clear()
        try:
            label = rule.get("label", "error")
            signature_id = _signature_id(label, matched_text)
            retry_count = self._retry_counts.get(signature_id, 0)

            if retry_count >= self._max_retries:
                _status_clear()
                console.print(
                    "\n[bold red]dog: max retries (%d) reached for failure sig %s — giving up.[/]"
                    % (self._max_retries, signature_id)
                )
                try:
                    self._child.close(force=True)
                except Exception:
                    pass
                os._exit(3)

            retry_count += 1
            self._retry_counts[signature_id] = retry_count
            delay = rule.get("delay", 1.0)
            response = rule.get("response", "retry\n")
            with self._lock:
                screen_text = self._buf
            response = _select_highlighted_response(response, screen_text)

            _status_log(
                f"dog retry {retry_count}/{self._max_retries}: {label}; "
                f"sending {repr(response.strip()) or '<Enter>'} in {delay:.1f}s"
            )
            if not self._wait_with_progress(
                delay,
                action="retry",
                response=response,
                label=label,
                retry_count=retry_count,
            ):
                return
            if float(delay) > 0 and self._response_needs_settled_prompt(response, rule):
                response = self._wait_for_input_prompt(
                    rule,
                    response,
                    action="retry",
                    label=label,
                    retry_count=retry_count,
                )
                if response is None:
                    return
            else:
                with self._lock:
                    screen_text = self._buf
                response = _select_highlighted_response(response, screen_text)
            if self._child_is_alive():
                self._safe_send(response)
            else:
                with self._lock:
                    self._restart_request = {
                        "label": label,
                        "signature_id": signature_id,
                        "retry_count": retry_count,
                    }
            with self._lock:
                self._buf = ""
                self._last_triggered_at[signature_id] = time.monotonic()
            self._last_action_time = time.monotonic()
        finally:
            with self._lock:
                self._active_actions -= 1
                if self._active_actions == 0:
                    self._idle_event.set()

    def _safe_send(self, text: str) -> None:
        if self._stop_event.is_set() or not self._child_is_alive():
            return
        try:
            pattern = _build_echo_bytes_pattern(text)
            if pattern:
                with self._lock:
                    self._pending_echo_suppression.append(
                        (pattern, time.monotonic() + 2.5)
                    )
            self._child.send(text)
        except pexpect.exceptions.ExceptionPexpect as e:
            console.print(f"[red]dog: send failed:[/] {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


class Runner:
    """
    Wraps a CLI command with pexpect and handles error/permission recovery.

    Parameters
    ----------
    command         : full command string
    max_retries     : maximum auto-retry attempts before giving up
    echo            : whether child output should be shown to the terminal
    timeout         : pexpect spawn timeout (not used for interact)
    extra_rules     : additional RETRY_RULES injected at runtime
    auto_permission : auto-answer Claude Code permission prompts
    """

    def __init__(
        self,
        command: str,
        max_retries: int = 360,
        echo: bool = True,
        timeout: float = 30.0,
        extra_rules: Optional[list[dict]] = None,
        auto_permission: bool = True,
        profile: Optional[str] = None,
    ) -> None:
        self.command = command
        self.max_retries = max_retries
        self.echo = echo
        self.timeout = timeout
        self.auto_permission = auto_permission
        self.profile = _infer_profile(command, profile)

        all_rules = _build_retry_rules(self.profile, extra_rules)

        self._rule_patterns = [
            (re.compile(r["pattern"], re.IGNORECASE), r) for r in all_rules
        ]
        self._perm_patterns = [
            (re.compile(r["pattern"], re.IGNORECASE), r) for r in PERMISSION_RULES
        ]
        self._fatal_re = _compile(FATAL_PATTERNS)
        self._interrupt_re = _compile(INTERRUPTION_PATTERNS)
        self._success_re = _compile(SUCCESS_PATTERNS)
        self._child: Optional[pexpect.spawn] = None
        self._watcher: Optional[PatternWatcher] = None
        self._retry_counts: dict[str, int] = {}
        self._max_action_delay = max(
            [0.3]
            + [float(rule.get("delay", 0.0)) for rule in all_rules]
            + [float(rule.get("delay", 0.0)) for rule in PERMISSION_RULES]
        )

    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> int:
        # Get actual terminal dimensions so Claude Code renders correctly
        import shutil

        size = shutil.get_terminal_size(fallback=(80, 24))
        cols, rows = size.columns, size.lines

        console.print(f"[bold cyan]🐕 dog[/] launching: [yellow]{self.command}[/]")
        console.print(
            "[dim]  Ctrl+C = cancel current task in Claude  │  "
            "auto-permission: %s  │  auto-retry: ON (max %d)[/]"
            % ("ON" if self.auto_permission else "OFF", self.max_retries)
        )
        old_winch = signal.getsignal(signal.SIGWINCH)

        while True:
            try:
                self._child = pexpect.spawn(
                    self.command,
                    encoding=None,  # bytes mode — cleaner for PTY passthrough
                    timeout=self.timeout,
                    echo=False,
                    dimensions=(rows, cols),
                )
            except pexpect.exceptions.ExceptionPexpect as e:
                console.print(f"[red]Failed to spawn process:[/] {e}")
                signal.signal(signal.SIGWINCH, old_winch)
                return 1

            # Forward SIGWINCH (terminal resize) to child
            def _handle_winch(sig, frame):
                try:
                    import shutil

                    size = shutil.get_terminal_size(fallback=(80, 24))
                    self._child.setwinsize(size.lines, size.columns)
                except Exception:
                    pass

            signal.signal(signal.SIGWINCH, _handle_winch)

            # Start pattern watcher in background thread
            self._watcher = PatternWatcher(
                child=self._child,
                rule_patterns=self._rule_patterns,
                permission_patterns=self._perm_patterns,
                fatal_re=self._fatal_re,
                interrupt_re=self._interrupt_re,
                success_re=self._success_re,
                max_retries=self.max_retries,
                auto_permission=self.auto_permission,
                profile=self.profile,
                retry_counts=self._retry_counts,
            )
            watcher_thread = threading.Thread(
                target=self._watcher.run, daemon=True, name="dog-watcher"
            )
            watcher_thread.start()

            def _output_filter(data: bytes) -> bytes:
                visible = self._watcher.feed(data)
                return visible if self.echo else b""

            # interact() — pexpect handles raw mode, escape sequences, Ctrl+C, etc.
            # output_filter captures output into the watcher buffer
            try:
                self._child.interact(
                    escape_character=None,  # no special escape char
                    input_filter=self._watcher.note_user_input,
                    output_filter=_output_filter,
                )
            except Exception:
                pass
            finally:
                self._drain_pending_output(_output_filter)
                if self._watcher is not None:
                    self._watcher.wait_for_idle(
                        start_timeout=0.35,
                        finish_timeout=self._max_action_delay + 1.0,
                    )
                self._watcher.stop()
                watcher_thread.join(timeout=1.0)
                signal.signal(signal.SIGWINCH, old_winch)

            # Collect exit code
            try:
                self._child.wait()
            except Exception:
                pass
            code = self._child.exitstatus if self._child.exitstatus is not None else 0
            restart_request = (
                self._watcher.consume_restart_request()
                if self._watcher is not None
                else None
            )

            if restart_request:
                if not _interactive_tty():
                    console.print(
                        "\n[yellow]dog: retryable failure persisted after the tool exited; restarting command "
                        f"({restart_request['label']}, {restart_request['retry_count']}/{self.max_retries}).[/]"
                    )
                continue

            if code == 0 or self._watcher._success_seen:
                console.print("\n[bold green]✓ dog: session finished cleanly.[/]")
            else:
                console.print(f"\n[bold red]✗ dog: process exited with code {code}.[/]")

            return code

    def _drain_pending_output(self, output_filter) -> None:
        if self._child is None or self._watcher is None:
            return

        while True:
            try:
                chunk = self._child.read_nonblocking(size=4096, timeout=0)
            except pexpect.exceptions.TIMEOUT:
                break
            except pexpect.exceptions.EOF:
                break
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                raise

            if not chunk:
                break

            visible = output_filter(chunk)
            if self.echo and visible and not _interactive_tty():
                try:
                    sys.stdout.buffer.write(visible)
                except AttributeError:
                    sys.stdout.write(visible.decode("utf-8", errors="replace"))
                sys.stdout.flush()
