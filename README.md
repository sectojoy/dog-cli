# dog-cli

`dog` is a resilient wrapper around interactive AI CLIs such as Claude Code and OpenAI Codex.
It proxies the child process through `pexpect`, watches the terminal output for retryable failures,
and automatically sends recovery commands so the session can keep moving.

```bash
dog claude --model claude-opus-4-5 --prompt "refactor auth module"
dog codex --full-auto "write unit tests for utils.py"
```

## What It Does

- Wraps `claude`, `codex`, or any other terminal command.
- Detects built-in retryable failures such as SSL issues, network failures, timeouts, and rate limits.
- Auto-approves common Claude Code permission prompts unless you disable it.
- Stops immediately on fatal errors such as invalid API keys or billing hard limits.
- Lets you add runtime retry patterns with `--retry-on`.
- Preserves the child process exit code when no dog-specific fatal condition is triggered.

## Installation

### Quick setup

```bash
./install.sh
```

That creates `.venv/`, installs the package in editable mode, and prints the resulting `dog` binary path.

### Manual setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/dog --version
```

## Usage

### Claude Code

All arguments after `dog claude` are forwarded to `claude`.

```bash
dog claude --model claude-opus-4-5 --prompt "fix the flaky tests"
dog claude -r 20 -t 60 --dangerously-skip-permissions
dog claude --no-auto-permission --model claude-sonnet-4
```

### OpenAI Codex

All arguments after `dog codex` are forwarded to `codex`.

```bash
dog codex --full-auto "write unit tests for utils.py"
dog codex -r 5 -t 60 --model o4-mini "refactor auth module"
```

### Generic wrapper

Use `dog run` to wrap any command, including tools that expose their own flags.

```bash
dog run npx claude-code --model opus
dog run uv run my-agent --profile prod
dog run --retry-on "Gateway Timeout" --retry-cmd $'\n' -- my-ai-tool --interactive
```

## Common Options

| Option | Default | Meaning |
|---|---:|---|
| `-r, --max-retries` | `360` | Max automatic retries before `dog` exits with code `3` |
| `-t, --timeout` | `30.0` | Spawn timeout passed to `pexpect` |
| `--no-echo` | off | Suppress child output while still watching for retry/fatal patterns |
| `--retry-on PATTERN` | none | Add one or more extra regex triggers at runtime |
| `--retry-cmd TEXT` | `continue` | Command sent when a custom retry pattern matches |
| `--no-auto-permission` | off | Disable Claude permission auto-approval |

## Built-in Behavior

### Retry patterns

`dog` ships with retry rules for cases such as:

- certificate / SSL failures
- API connection failures
- generic network errors
- request timeouts and gateway timeouts
- rate limit / quota responses
- Codex `APIConnectionError` and `RateLimitError`
- Claude prompts like `(y to retry)` and `Press Enter to continue`

Most network-style retries wait `30s` before sending `retry` or `continue`.
Interactive confirmation prompts use shorter delays such as `0.3s` or `0.5s`.

The built-in definitions live in [`dog/patterns.py`](/Users/striver/workspace/sectojoy/dog-cli/dog/patterns.py).

### Fatal patterns

These stop the session immediately without retrying:

- `Invalid API key`
- `AuthenticationError`
- `Permission denied`
- billing hard limit failures
- disabled account errors
- maximum context length exceeded

### Success patterns

`dog` also watches for common completion messages and resets the retry counter once a task is considered complete.

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Wrapped process exited cleanly |
| `1` | Failed to spawn the wrapped process |
| `2` | Fatal pattern detected by `dog` |
| `3` | Retry budget exhausted |
| child exit code | Propagated from the wrapped CLI |

## Development

### Run tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

### Project layout

```text
dog-cli/
├── dog/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── patterns.py
│   └── runner.py
├── tests/
├── install.sh
├── pyproject.toml
└── README.md
```
