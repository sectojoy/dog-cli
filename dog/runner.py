"""
runner.py — pexpect-based resilient subprocess runner.

Key responsibilities:
  1. Spawn the target CLI (claude / codex / any command)
  2. Stream all output to the terminal in real-time (passthrough)
  3. Scan output for:
       a) FATAL patterns   → abort immediately
       b) SUCCESS patterns → print green notice, let process finish naturally
       c) PERMISSION rules → auto-approve by sending the configured key/text
       d) RETRY rules      → wait delay, inject recovery command
  4. Respect a maximum retry budget (--max-retries)
  5. Allow the user to type normally when no pattern is matched (interactive passthrough)
"""
from __future__ import annotations

import re
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


class Runner:
    """
    Wraps a CLI command with pexpect and handles error/permission recovery.

    Parameters
    ----------
    command         : full command string, e.g. "claude --model opus ..."
    max_retries     : maximum auto-retry attempts before giving up
    echo            : whether to echo child output to our stdout
    timeout         : pexpect read timeout in seconds
    extra_rules     : additional RETRY_RULES injected at runtime
    auto_permission : if True, auto-answer Claude Code permission prompts
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
        self.command = command
        self.max_retries = max_retries
        self.echo = echo
        self.timeout = timeout
        self.auto_permission = auto_permission

        # Sort retry rules by priority (lower number = higher priority)
        all_rules = sorted(RETRY_RULES, key=lambda r: r.get("priority", 50))
        if extra_rules:
            all_rules.extend(extra_rules)

        self._rule_patterns = [
            (re.compile(r["pattern"], re.IGNORECASE | re.DOTALL), r)
            for r in all_rules
        ]
        self._permission_patterns = [
            (re.compile(r["pattern"], re.IGNORECASE | re.DOTALL), r)
            for r in PERMISSION_RULES
        ]
        self._fatal_re = _compile(FATAL_PATTERNS)
        self._success_re = _compile(SUCCESS_PATTERNS)

        self._retries = 0
        self._success_seen = False
        self._child: Optional[pexpect.spawn] = None
        # Lock to prevent stdin-forwarder and main loop from colliding on writes
        self._send_lock = threading.Lock()

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def run(self) -> int:
        """Spawn and manage the child process. Returns the exit code."""
        console.print(
            f"[bold cyan]🐕 dog[/] launching: [yellow]{self.command}[/]"
        )
        if self.auto_permission:
            console.print(
                "[dim]  auto-permission: ON  |  "
                "auto-retry: ON  (max %d)[/]" % self.max_retries
            )

        try:
            self._child = pexpect.spawn(
                self.command,
                encoding="utf-8",
                timeout=self.timeout,
                echo=False,
                dimensions=(50, 220),
            )
        except pexpect.exceptions.ExceptionPexpect as e:
            console.print(f"[red]Failed to spawn process:[/] {e}")
            return 1

        stdin_thread = threading.Thread(
            target=self._forward_stdin, daemon=True
        )
        stdin_thread.start()

        exit_code = self._read_loop()

        stdin_thread.join(timeout=1.0)
        return exit_code

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _read_loop(self) -> int:
        child = self._child
        # Rolling buffer — we keep trailing 8 KB to catch multi-line patterns
        buf = ""

        while True:
            try:
                chunk = child.read_nonblocking(size=2048, timeout=self.timeout)
                if not chunk:
                    continue
            except pexpect.exceptions.TIMEOUT:
                continue
            except pexpect.exceptions.EOF:
                child.wait()
                code = child.exitstatus if child.exitstatus is not None else 0
                if code == 0 or self._success_seen:
                    console.print("\n[bold green]✓ dog: session finished cleanly.[/]")
                else:
                    console.print(
                        f"\n[bold red]✗ dog: process exited with code {code}.[/]"
                    )
                return code

            if self.echo:
                sys.stdout.write(chunk)
                sys.stdout.flush()

            buf += chunk
            if len(buf) > 8192:
                buf = buf[-8192:]

            # ── 1. Fatal check ─────────────────────────────────────────────
            if self._fatal_re.search(buf):
                console.print(
                    "\n[bold red]💀 dog: FATAL error — aborting (no retry).[/]"
                )
                child.close(force=True)
                return 2

            # ── 2. Success check ───────────────────────────────────────────
            if self._success_re.search(buf) and not self._success_seen:
                self._success_seen = True
                console.print(
                    "\n[bold green]🎉 dog: task completed — "
                    "waiting for your next input.[/]"
                )
                # Don't return; let process stay alive for next user prompt
                buf = ""
                continue

            # ── 3. Permission auto-approve ─────────────────────────────────
            if self.auto_permission:
                perm_rule = self._match_permission(buf)
                if perm_rule:
                    self._handle_permission(perm_rule)
                    buf = ""
                    continue

            # ── 4. Retry rule check ────────────────────────────────────────
            matched_rule = self._match_rule(buf)
            if matched_rule:
                self._success_seen = False   # reset — we're retrying
                self._handle_retry(matched_rule)
                buf = ""

    def _match_rule(self, text: str) -> Optional[dict]:
        for pattern, rule in self._rule_patterns:
            if pattern.search(text):
                return rule
        return None

    def _match_permission(self, text: str) -> Optional[dict]:
        for pattern, rule in self._permission_patterns:
            if pattern.search(text):
                return rule
        return None

    def _handle_permission(self, rule: dict) -> None:
        delay = rule.get("delay", 0.3)
        label = rule.get("label", "permission")
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
        with self._send_lock:
            try:
                self._child.send(response)
            except pexpect.exceptions.ExceptionPexpect as e:
                console.print(f"[red]dog: failed to send permission response:[/] {e}")

    def _handle_retry(self, rule: dict) -> None:
        if self._retries >= self.max_retries:
            console.print(
                f"\n[bold red]dog: max retries ({self.max_retries}) reached — giving up.[/]"
            )
            self._child.close(force=True)
            sys.exit(3)

        self._retries += 1
        delay = rule.get("delay", 1.0)
        label = rule.get("label", "error")
        response = rule.get("response", "/retry\n")

        console.print(
            Text.assemble(
                "\n[bold yellow]⚡ dog:[/] ",
                ("auto-retry", "bold yellow"),
                f" #{self._retries}/{self.max_retries}  ",
                (f"({label})", "dim"),
                f"  — waiting {delay}s then sending: ",
                (repr(response.strip()), "cyan"),
            )
        )

        time.sleep(delay)
        with self._send_lock:
            try:
                self._child.send(response)
            except pexpect.exceptions.ExceptionPexpect as e:
                console.print(f"[red]dog: failed to send retry command:[/] {e}")

    def _forward_stdin(self) -> None:
        """
        Read user keystrokes and forward them to the child process.
        Runs in a daemon thread; exits when child dies.
        """
        child = self._child
        raw_mode = False
        old_settings = None
        fd = None

        try:
            import termios
            import tty
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setraw(fd)
            raw_mode = True
        except Exception:
            pass

        try:
            while child.isalive():
                try:
                    ch = sys.stdin.read(1)
                    if not ch:
                        break
                    with self._send_lock:
                        child.send(ch)
                except (OSError, EOFError):
                    break
        finally:
            if raw_mode and fd is not None and old_settings is not None:
                import termios
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
