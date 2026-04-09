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
from typing import Optional

import pexpect
from rich.console import Console
from rich.text import Text

from dog.patterns import RETRY_RULES, PERMISSION_RULES, FATAL_PATTERNS, SUCCESS_PATTERNS

console = Console(stderr=True)


def _compile(patterns: list[str]) -> re.Pattern:
    combined = "|".join(f"(?:{p})" for p in patterns)
    return re.compile(combined, re.IGNORECASE)


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
        self._retries  = 0
        self._success_seen = False
        self._stop     = False
        # Prevent rapid re-firing on the same chunk
        self._last_action_time = 0.0

    # ── Called from output_filter (main thread) ───────────────────────────────

    def feed(self, data: bytes) -> bytes:
        text = data.decode("utf-8", errors="replace")
        with self._lock:
            self._buf += text
            if len(self._buf) > 8192:
                self._buf = self._buf[-8192:]
        self._notify.set()
        return data                 # pass through unchanged to stdout

    def stop(self) -> None:
        self._stop = True
        self._notify.set()

    # ── Background thread entry point ─────────────────────────────────────────

    def run(self) -> None:
        while not self._stop:
            fired = self._notify.wait(timeout=1.0)
            if not fired:
                continue
            self._notify.clear()

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
                self._child.close(force=True)
                os._exit(2)

            # 2. Success
            if self._success_re.search(buf) and not self._success_seen:
                self._success_seen = True
                console.print(
                    "\n[bold green]🎉 dog: task completed — "
                    "waiting for your next input.[/]"
                )
                with self._lock:
                    self._buf = ""
                self._last_action_time = time.monotonic()
                continue

            # 3. Permission auto-approve
            if self._auto_permission:
                rule = self._match(buf, self._perm_patterns)
                if rule:
                    self._do_permission(rule)
                    continue

            # 4. Retry
            rule = self._match(buf, self._rule_patterns)
            if rule:
                self._success_seen = False
                self._do_retry(rule)

    def _match(self, text: str, patterns: list) -> Optional[dict]:
        for pat, rule in patterns:
            if pat.search(text):
                return rule
        return None

    def _do_permission(self, rule: dict) -> None:
        delay    = rule.get("delay", 0.3)
        label    = rule.get("label", "permission")
        response = rule.get("response", "y\n")

        console.print(
            Text.assemble(
                "\n[bold blue]🔑 dog:[/] ",
                ("auto-approve", "bold blue"),
                f"  ({label})  →  ",
                (repr(response.strip()) or "<Enter>", "cyan"),
            )
        )
        time.sleep(delay)
        self._safe_send(response)
        with self._lock:
            self._buf = ""
        self._last_action_time = time.monotonic()

    def _do_retry(self, rule: dict) -> None:
        if self._retries >= self._max_retries:
            console.print(
                f"\n[bold red]dog: max retries ({self._max_retries}) reached — giving up.[/]"
            )
            self._child.close(force=True)
            os._exit(3)

        self._retries += 1
        delay    = rule.get("delay", 1.0)
        label    = rule.get("label", "error")
        response = rule.get("response", "/retry\n")

        console.print(
            Text.assemble(
                "\n[bold yellow]⚡ dog:[/] ",
                ("auto-retry", "bold yellow"),
                f" #{self._retries}/{self._max_retries}  ",
                (f"({label})", "dim"),
                f"  — waiting {delay}s then sending: ",
                (repr(response.strip()), "cyan"),
            )
        )
        time.sleep(delay)
        self._safe_send(response)
        with self._lock:
            self._buf = ""
        self._last_action_time = time.monotonic()

    def _safe_send(self, text: str) -> None:
        try:
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
    echo            : (unused in v2; interact() always echoes)
    timeout         : pexpect spawn timeout (not used for interact)
    extra_rules     : additional RETRY_RULES injected at runtime
    auto_permission : auto-answer Claude Code permission prompts
    """

    def __init__(
        self,
        command: str,
        max_retries: int = 10,
        echo: bool = True,
        timeout: float = 30.0,
        extra_rules: Optional[list[dict]] = None,
        auto_permission: bool = True,
    ) -> None:
        self.command        = command
        self.max_retries    = max_retries
        self.timeout        = timeout
        self.auto_permission = auto_permission

        all_rules = sorted(RETRY_RULES, key=lambda r: r.get("priority", 50))
        if extra_rules:
            all_rules.extend(extra_rules)

        self._rule_patterns = [
            (re.compile(r["pattern"], re.IGNORECASE | re.DOTALL), r)
            for r in all_rules
        ]
        self._perm_patterns = [
            (re.compile(r["pattern"], re.IGNORECASE | re.DOTALL), r)
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

        # interact() — pexpect handles raw mode, escape sequences, Ctrl+C, etc.
        # output_filter captures output into the watcher buffer
        try:
            self._child.interact(
                escape_character=None,              # no special escape char
                output_filter=self._watcher.feed,  # bytes → bytes passthrough
            )
        except Exception:
            pass
        finally:
            self._watcher.stop()
            signal.signal(signal.SIGWINCH, old_winch)

        # Collect exit code
        try:
            self._child.wait()
        except Exception:
            pass
        code = self._child.exitstatus if self._child.exitstatus is not None else 0

        if code == 0 or self._watcher._success_seen:
            console.print("\n[bold green]✓ dog: session finished cleanly.[/]")
        else:
            console.print(f"\n[bold red]✗ dog: process exited with code {code}.[/]")

        return code
