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

import os
import re
import signal
import sys
import time
import threading
import hashlib
import itertools
from typing import Optional

import pexpect
from rich.console import Console

from dog.patterns import (
    COMMON_RETRY_RULES,
    TOOL_RETRY_RULES,
    PERMISSION_RULES,
    FATAL_PATTERNS,
    SUCCESS_PATTERNS,
)

console = Console(stderr=True)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_STATUS_LOCK = threading.Lock()
_STATUS_VISIBLE = False
_LAST_STATUS_MESSAGE = ""


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
        rf"(^|[\r\n])[ \t]*(?:[›>][ \t]*)?{escaped}(?=($|[\r\n]))",
        re.IGNORECASE,
    )


def _status_write(message: str = "", *, newline: bool = False, clear: bool = False) -> None:
    global _STATUS_VISIBLE, _LAST_STATUS_MESSAGE
    with _STATUS_LOCK:
        if sys.stderr.isatty():
            if clear or _STATUS_VISIBLE:
                sys.stderr.write("\r\033[2K")
            if message:
                sys.stderr.write(message)
                _STATUS_VISIBLE = not newline
                _LAST_STATUS_MESSAGE = message
            elif clear:
                _STATUS_VISIBLE = False
                _LAST_STATUS_MESSAGE = ""
            if newline:
                sys.stderr.write("\n")
                _STATUS_VISIBLE = False
                _LAST_STATUS_MESSAGE = ""
        else:
            if clear and not message and not newline:
                return
            if message and message == _LAST_STATUS_MESSAGE and not newline:
                return
            if message:
                sys.stderr.write(message)
            if newline or message:
                sys.stderr.write("\n")
            _STATUS_VISIBLE = False
            _LAST_STATUS_MESSAGE = message if not newline else ""
        sys.stderr.flush()


def _status_clear() -> None:
    _status_write(clear=True)


def _status_show(message: str) -> None:
    _status_write(message)


def _status_log(message: str) -> None:
    _status_write(message, newline=True, clear=True)


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
        success_re: re.Pattern,
        max_retries: int,
        auto_permission: bool,
    ) -> None:
        self._child            = child
        self._rule_patterns    = rule_patterns
        self._perm_patterns    = permission_patterns
        self._fatal_re         = fatal_re
        self._success_re       = success_re
        self._max_retries      = max_retries
        self._auto_permission  = auto_permission

        self._buf      = ""
        self._lock     = threading.Lock()
        self._notify   = threading.Event()
        self._stop_event = threading.Event()
        self._retry_counts: dict[str, int] = {}
        self._success_seen = False
        self._input_buf = ""
        self._last_triggered_at: dict[str, float] = {}
        self._pending_echo_suppression: list[tuple[re.Pattern, float]] = []
        self._active_actions = 0
        self._idle_event = threading.Event()
        self._idle_event.set()
        # Prevent rapid re-firing on the same chunk
        self._last_action_time = 0.0

    # ── Called from output_filter (main thread) ───────────────────────────────

    def feed(self, data: bytes) -> bytes:
        text = data.decode("utf-8", errors="replace")
        visible_text = self._suppress_auto_echo(text)
        if visible_text:
            _status_clear()
        with self._lock:
            self._buf += visible_text
            if len(self._buf) > 8192:
                self._buf = self._buf[-8192:]
        self._notify.set()
        return visible_text.encode("utf-8", errors="replace")

    def stop(self) -> None:
        self._stop_event.set()
        self._notify.set()

    def wait_for_idle(self, start_timeout: float = 0.25, finish_timeout: float = 0.0) -> bool:
        deadline = time.monotonic() + max(start_timeout, 0.0)
        while time.monotonic() < deadline:
            with self._lock:
                active_actions = self._active_actions
                notify_pending = self._notify.is_set()
            if active_actions > 0:
                return self._idle_event.wait(timeout=max(finish_timeout, 0.0))
            if not notify_pending:
                return True
            time.sleep(0.01)

        with self._lock:
            active_actions = self._active_actions
        if active_actions > 0:
            return self._idle_event.wait(timeout=max(finish_timeout, 0.0))
        return True

    def _suppress_auto_echo(self, text: str) -> str:
        now = time.monotonic()
        with self._lock:
            rules = [(pattern, expires_at) for pattern, expires_at in self._pending_echo_suppression if expires_at > now]
            self._pending_echo_suppression = rules

        if not rules:
            return text

        filtered = text
        for pattern, _expires_at in rules:
            filtered = pattern.sub(lambda m: m.group(1), filtered)
        return filtered

    def note_user_input(self, data: bytes) -> bytes:
        text = data.decode("utf-8", errors="replace")
        with self._lock:
            for ch in text:
                if ch in ("\r", "\n"):
                    if self._input_buf.strip():
                        self._success_seen = False
                        self._buf = ""
                        self._last_triggered_at = {}
                        self._retry_counts = {}
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
                _status_clear()
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
                self._retry_counts = {}
                _status_clear()
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

            # 3. Permission auto-approve
            if self._auto_permission:
                matched = self._match(buf, self._perm_patterns)
                rule = matched[0] if matched else None
                if rule:
                    self._do_permission(rule, matched[1])
                    continue

            # 4. Retry
            matched = self._match(buf, self._rule_patterns)
            rule = matched[0] if matched else None
            if rule:
                self._success_seen = False
                self._do_retry(rule, matched[1])

    def _wait_with_progress(
        self,
        delay: float,
        *,
        response: str,
        label: str,
        retry_count: int,
    ) -> bool:
        delay = max(float(delay), 0.0)
        if delay <= 0:
            return True

        response_preview = repr(response.strip()) or "<Enter>"
        spinner = itertools.cycle("|/-\\")
        deadline = time.monotonic() + delay
        last_rendered = None

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _status_clear()
                return True
            if self._stop_event.wait(min(0.2, remaining)):
                _status_clear()
                return False
            if self._user_is_typing():
                if not sys.stderr.isatty():
                    _status_log("dog auto-retry cancelled: local input detected")
                else:
                    _status_clear()
                return False
            frame = next(spinner)
            message = (
                f"dog waiting {frame} {remaining:4.1f}s before {response_preview} "
                f"({label}, {retry_count}/{self._max_retries})"
            )
            if message != last_rendered:
                _status_show(message)
                last_rendered = message

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
                return None
            return rule, matched_text
        return None

    def _do_permission(self, rule: dict, matched_text: str) -> None:
        with self._lock:
            self._active_actions += 1
            self._idle_event.clear()
        try:
            self._retry_counts = {}
            delay    = rule.get("delay", 0.3)
            label    = rule.get("label", "permission")
            response = rule.get("response", "y\n")

            if delay <= 0 and not sys.stderr.isatty():
                _status_log(
                    f"dog auto-approve: {label} -> {repr(response.strip()) or '<Enter>'}"
                )
            if not self._wait_with_progress(delay, response=response, label=label, retry_count=1):
                return
            self._safe_send(response)
            _status_clear()
            with self._lock:
                self._buf = ""
                self._last_triggered_at[_signature_id(label, matched_text)] = time.monotonic()
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
            delay    = rule.get("delay", 1.0)
            response = rule.get("response", "retry\n")

            if delay <= 0 and not sys.stderr.isatty():
                _status_log(
                    f"dog retry {retry_count}/{self._max_retries}: {label}; "
                    f"sending {repr(response.strip()) or '<Enter>'} now"
                )
            if not self._wait_with_progress(
                delay,
                response=response,
                label=label,
                retry_count=retry_count,
            ):
                return
            self._safe_send(response)
            _status_clear()
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
            pattern = _build_echo_pattern(text)
            if pattern:
                with self._lock:
                    self._pending_echo_suppression.append((pattern, time.monotonic() + 2.5))
            self._child.send(text)
        except pexpect.exceptions.ExceptionPexpect as e:
            _status_clear()
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
        self.command        = command
        self.max_retries    = max_retries
        self.echo           = echo
        self.timeout        = timeout
        self.auto_permission = auto_permission

        all_rules = list(COMMON_RETRY_RULES)
        if profile:
            all_rules.extend(TOOL_RETRY_RULES.get(profile, []))
        if extra_rules:
            all_rules.extend(extra_rules)
        all_rules = sorted(all_rules, key=lambda r: r.get("priority", 50))

        self._rule_patterns = [
            (re.compile(r["pattern"], re.IGNORECASE), r)
            for r in all_rules
        ]
        self._perm_patterns = [
            (re.compile(r["pattern"], re.IGNORECASE), r)
            for r in PERMISSION_RULES
        ]
        self._fatal_re  = _compile(FATAL_PATTERNS)
        self._success_re = _compile(SUCCESS_PATTERNS)
        self._child: Optional[pexpect.spawn] = None
        self._watcher: Optional[PatternWatcher] = None

    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> int:
        # Get actual terminal dimensions so Claude Code renders correctly
        import shutil
        size = shutil.get_terminal_size(fallback=(80, 24))
        cols, rows = size.columns, size.lines

        console.print(
            f"[bold cyan]🐕 dog[/] launching: [yellow]{self.command}[/]"
        )
        console.print(
            "[dim]  Ctrl+C = cancel current task in Claude  │  "
            "auto-permission: %s  │  auto-retry: ON (max %d)[/]"
            % ("ON" if self.auto_permission else "OFF", self.max_retries)
        )

        try:
            self._child = pexpect.spawn(
                self.command,
                encoding=None,       # bytes mode — cleaner for PTY passthrough
                timeout=self.timeout,
                echo=False,
                dimensions=(rows, cols),
            )
        except pexpect.exceptions.ExceptionPexpect as e:
            console.print(f"[red]Failed to spawn process:[/] {e}")
            return 1

        # Forward SIGWINCH (terminal resize) to child
        def _handle_winch(sig, frame):
            try:
                import shutil
                size = shutil.get_terminal_size(fallback=(80, 24))
                self._child.setwinsize(size.lines, size.columns)
            except Exception:
                pass

        old_winch = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, _handle_winch)

        # Start pattern watcher in background thread
        self._watcher = PatternWatcher(
            child=self._child,
            rule_patterns=self._rule_patterns,
            permission_patterns=self._perm_patterns,
            fatal_re=self._fatal_re,
            success_re=self._success_re,
            max_retries=self.max_retries,
            auto_permission=self.auto_permission,
        )
        watcher_thread = threading.Thread(
            target=self._watcher.run, daemon=True, name="dog-watcher"
        )
        watcher_thread.start()

        def _output_filter(data: bytes) -> bytes:
            self._watcher.feed(data)
            return data if self.echo else b""

        # interact() — pexpect handles raw mode, escape sequences, Ctrl+C, etc.
        # output_filter captures output into the watcher buffer
        try:
            self._child.interact(
                escape_character=None,              # no special escape char
                input_filter=self._watcher.note_user_input,
                output_filter=_output_filter,
            )
        except Exception:
            pass
        finally:
            if self._watcher is not None:
                self._watcher.wait_for_idle(
                    start_timeout=0.35,
                    finish_timeout=0.0 if not self._child.isalive() else 31.0,
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

        _status_clear()
        if code == 0 or self._watcher._success_seen:
            console.print("\n[bold green]✓ dog: session finished cleanly.[/]")
        else:
            console.print(f"\n[bold red]✗ dog: process exited with code {code}.[/]")

        return code
