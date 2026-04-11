# dog-cli

Resilient wrapper for Claude Code, OpenAI Codex, and Opencode.

`dog` sits in front of your AI CLI, watches for common recoverable failures, and sends the right follow-up input so the session can keep moving.

[中文说明 / Chinese README](./README.zh-CN.md)

## Install

### Clone

```bash
git clone <your-repo-url>
cd dog-cli
```

### Install locally

```bash
./install.sh
```

That will:

- create `.venv`
- install `dog` in editable mode
- print the full path to the local binary

Manual install:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/dog --version
```

### Make `dog` available globally

Add the local venv to your shell:

```bash
echo 'export PATH="$(pwd)/.venv/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Or create a symlink:

```bash
sudo ln -sf "$(pwd)/.venv/bin/dog" /usr/local/bin/dog
```

Verify:

```bash
dog --version
dog --help
```

## Quick Start

Start with the shortest possible command:

```bash
dog claude
dog codex
dog opencode
```

If the wrapped CLI supports launching without extra arguments, `dog` will attach to the interactive session and handle automatic recovery in the background.

## Common Usage

Once the basic flow works, pass through the original CLI arguments as usual.

### Claude Code

```bash
dog claude --model claude-opus-4-5 --prompt "fix the flaky tests"
dog claude -r 20 -t 60 --dangerously-skip-permissions
```

### OpenAI Codex

```bash
dog codex --full-auto "write unit tests for utils.py"
dog codex -r 5 -t 60 --model o4-mini "refactor auth module"
```

### Opencode

```bash
dog opencode run "write unit tests for utils.py"
dog opencode run --continue --model openai/gpt-5 "fix flaky tests"
```

### Wrap any command

```bash
dog run npx claude-code --model opus
dog run uv run my-agent --profile prod
```

## What It Does

- wraps `claude`, `codex`, `opencode`, or any other terminal command
- detects common recoverable failures such as SSL issues, network errors, timeouts, and rate limits
- auto-sends `retry`, `continue`, `y`, or Enter for supported prompts
- uses one recovery loop with tool-specific prompt-ready checks instead of assuming every CLI redraws the terminal the same way
- stops immediately on fatal conditions to avoid useless loops
- keeps the child process exit code unless `dog` itself aborts

## Advanced Usage

### Common options

| Option | Default | Meaning |
|---|---:|---|
| `-r, --max-retries` | `360` | Maximum automatic retries before exiting with code `3` |
| `-t, --timeout` | `30.0` | Spawn timeout passed to `pexpect` |
| `--no-echo` | off | Hide child output while still matching patterns |
| `--retry-on PATTERN` | none | Add one or more extra regex triggers |
| `--retry-cmd TEXT` | `continue` | Command sent when a custom retry rule matches |
| `--no-auto-permission` | off | Disable automatic permission approval |

### Custom retry rules

```bash
dog codex --retry-on "stream disconnected" --retry-cmd continue
dog run --retry-on "Gateway Timeout" --retry-cmd $'\n' -- my-ai-tool --interactive
```

### Passthrough behavior

- `dog claude ...` forwards remaining arguments to `claude`
- `dog codex ...` forwards remaining arguments to `codex`
- `dog opencode ...` forwards remaining arguments to `opencode`
- `dog run ...` wraps any command line

## Built-in Behavior

### Retry handling

Built-in rules cover cases like:

- certificate and SSL failures
- API connection failures
- generic network errors
- request timeouts and gateway timeouts
- rate limits and quota responses
- Codex `APIConnectionError` and `RateLimitError`
- Codex stream disconnect and response decode errors
- Claude prompts such as `(y to retry)` and `Press Enter to continue`

Most network-style recoveries wait `30s` before sending `retry` or `continue`.
Interactive confirmation prompts use shorter delays such as `0.3s` to `1.0s`.

The outer recovery model is shared across tools, but the terminal state checks are not identical:

- `claude` is mostly prompt-driven, so short explicit prompts are usually enough
- `codex` and `opencode` are treated more like TUIs, so `dog` waits for the input area or highlighted action to settle before sending `retry` or `continue`
- `dog run ...` uses the generic path unless the wrapped command is recognised as one of the built-in tool profiles

Rule definitions live in [`dog/patterns.py`](/Users/striver/workspace/sectojoy/dog-cli/dog/patterns.py).

### Success handling

`dog` detects common completion messages and pauses automatic recovery once a task is done.
Automatic recovery resumes only after you submit a new prompt.

### Fatal handling

These stop immediately without retrying:

- `Invalid API key`
- `AuthenticationError`
- `Permission denied`
- billing hard limit failures
- disabled account errors
- maximum context length exceeded

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Wrapped process exited cleanly |
| `1` | Failed to spawn the wrapped process |
| `2` | Fatal pattern detected by `dog` |
| `3` | Retry budget exhausted |
| child exit code | Propagated from the wrapped CLI |

## Development

Run tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Project layout:

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
├── README.md
└── README.zh-CN.md
```
