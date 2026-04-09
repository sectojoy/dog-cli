"""
cli.py — Click entry points for the `dog` command.

Usage examples
--------------
# Wrap Claude Code (pass all args through)
dog claude -- --model claude-opus-4-5 --prompt "refactor main.py"

# Wrap Codex
dog codex -- --full-auto "write unit tests for utils.py"

# Wrap any arbitrary command
dog run --max-retries 5 -- npx claude-code --model opus

# Custom patterns from a JSON / inline flag
dog claude --retry-on "Service Unavailable" --retry-cmd "continue"
"""
from __future__ import annotations

import shlex
import sys

import click
from rich.console import Console

from dog.runner import Runner
from dog import __version__

console = Console(stderr=True)


# ──────────────────────────────────────────────────────────────────────────────
# Shared options used by multiple sub-commands
# ──────────────────────────────────────────────────────────────────────────────
_SHARED_OPTIONS = [
    click.option(
        "--max-retries", "-r",
        default=360,
        show_default=True,
        help="Maximum number of automatic retries before giving up.",
    ),
    click.option(
        "--timeout", "-t",
        default=30.0,
        show_default=True,
        help="Seconds to wait for output before assuming a hang.",
    ),
    click.option(
        "--no-echo",
        is_flag=True,
        default=False,
        help="Suppress child output (useful for scripting).",
    ),
    click.option(
        "--retry-on",
        multiple=True,
        metavar="PATTERN",
        help="Extra regex pattern(s) to trigger a retry (can repeat).",
    ),
    click.option(
        "--retry-cmd",
        default="continue",
        show_default=True,
        help="Command sent to the CLI when --retry-on matches.",
    ),
    click.option(
        "--no-auto-permission",
        is_flag=True,
        default=False,
        help="Disable automatic approval of Claude Code permission prompts.",
    ),
]


def _add_shared(func):
    """Decorator: attach all shared options to a Click command."""
    for option in reversed(_SHARED_OPTIONS):
        func = option(func)
    return func


def _build_extra_rules(retry_on: tuple[str, ...], retry_cmd: str) -> list[dict]:
    if not retry_on:
        return []
    cmd = retry_cmd if retry_cmd.endswith(("\n", "\r")) else retry_cmd + "\r"
    return [
        {"label": f"custom: {p}", "pattern": p, "response": cmd, "delay": 1.0}
        for p in retry_on
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Root group
# ──────────────────────────────────────────────────────────────────────────────
@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=False,
)
@click.version_option(__version__, "-V", "--version")
def main() -> None:
    """🐕 dog — resilient wrapper for Claude Code, Codex, and other AI CLIs.

    Automatically retries on API errors, timeouts, and certificate failures
    while transparently forwarding your keystrokes for normal interaction.
    """


# ──────────────────────────────────────────────────────────────────────────────
# dog claude [args...]
# ──────────────────────────────────────────────────────────────────────────────
@main.command(
    name="claude",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@_add_shared
def cmd_claude(
    args: tuple[str, ...],
    max_retries: int,
    timeout: float,
    no_echo: bool,
    retry_on: tuple[str, ...],
    retry_cmd: str,
    no_auto_permission: bool,
) -> None:
    """Wrap **claude** (Claude Code) with auto-retry.

    All unrecognised flags are passed directly to `claude`.

    \b
    Examples:
      dog claude --model claude-opus-4-5 --prompt "fix tests"
      dog claude -r 20 -- --dangerously-skip-permissions
    """
    extra_args = " ".join(shlex.quote(a) for a in args)
    command = f"claude {extra_args}".strip()
    _run(command, max_retries, timeout, no_echo, retry_on, retry_cmd, not no_auto_permission)


# ──────────────────────────────────────────────────────────────────────────────
# dog codex [args...]
# ──────────────────────────────────────────────────────────────────────────────
@main.command(
    name="codex",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@_add_shared
def cmd_codex(
    args: tuple[str, ...],
    max_retries: int,
    timeout: float,
    no_echo: bool,
    retry_on: tuple[str, ...],
    retry_cmd: str,
    no_auto_permission: bool,
) -> None:
    """Wrap **codex** (OpenAI Codex CLI) with auto-retry.

    All unrecognised flags are passed directly to `codex`.

    \b
    Examples:
      dog codex --full-auto "write tests for utils.py"
      dog codex -r 5 -t 60 -- --model o4-mini "refactor auth module"
    """
    extra_args = " ".join(shlex.quote(a) for a in args)
    command = f"codex {extra_args}".strip()
    _run(command, max_retries, timeout, no_echo, retry_on, retry_cmd, not no_auto_permission)


# ──────────────────────────────────────────────────────────────────────────────
# dog run <full command>   — generic wrapper for any CLI
# ──────────────────────────────────────────────────────────────────────────────
@main.command(
    name="run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@_add_shared
def cmd_run(
    command: tuple[str, ...],
    max_retries: int,
    timeout: float,
    no_echo: bool,
    retry_on: tuple[str, ...],
    retry_cmd: str,
    no_auto_permission: bool,
) -> None:
    """Wrap **any** CLI command with auto-retry.

    \b
    Examples:
      dog run npx claude-code --model opus
      dog run --retry-on "Gateway Timeout" --retry-cmd "\\n" -- my-ai-tool
    """
    cmd_str = " ".join(shlex.quote(a) for a in command)
    _run(cmd_str, max_retries, timeout, no_echo, retry_on, retry_cmd, not no_auto_permission)


# ──────────────────────────────────────────────────────────────────────────────
# Shared runner helper
# ──────────────────────────────────────────────────────────────────────────────
def _run(
    command: str,
    max_retries: int,
    timeout: float,
    no_echo: bool,
    retry_on: tuple[str, ...],
    retry_cmd: str,
    auto_permission: bool = True,
) -> None:
    extra_rules = _build_extra_rules(retry_on, retry_cmd)
    runner = Runner(
        command=command,
        max_retries=max_retries,
        echo=not no_echo,
        timeout=timeout,
        extra_rules=extra_rules,
        auto_permission=auto_permission,
    )
    exit_code = runner.run()
    sys.exit(exit_code)
