# 🐕 dog-cli

**dog** is a resilient wrapper for interactive AI CLIs like **Claude Code** and **OpenAI Codex**.  
It uses [`pexpect`](https://pexpect.readthedocs.io/) to transparently proxy the process while silently watching for API errors, timeouts, or certificate failures — and **automatically sends retry commands** to keep the session alive.

```
dog claude --model claude-opus-4-5 "refactor auth module"
```

---

## Features

| Feature | Details |
|---|---|
| **Auto-retry** | Detects 10+ error patterns (SSL, timeout, rate-limit, network…) |
| **Interactive passthrough** | Your keystrokes reach the child process normally |
| **Custom patterns** | Add `--retry-on "pattern"` at runtime |
| **Fatal detection** | Stops retrying on auth/billing errors — no infinite loops |
| **Max retry budget** | `--max-retries` (default 10) prevents runaway sessions |
| **Any CLI** | `dog run mycommand` wraps *any* interactive tool |

---

## Installation

```bash
# From the repo root
pip install -e .

# Verify
dog --version
```

## Usage

### Claude Code

```bash
# All flags after `claude` are forwarded verbatim
dog claude -- --model claude-opus-4-5 --dangerously-skip-permissions

# Increase retry budget for long tasks
dog claude -r 20 -t 60 -- "migrate the database schema"
```

### OpenAI Codex

```bash
dog codex --full-auto "write unit tests for utils.py"
dog codex -r 5 -- --model o4-mini "refactor auth module"
```

### Generic wrapper

```bash
dog run npx claude-code --model opus
```

### Custom retry patterns

```bash
# Fire on any custom message, send Enter to continue
dog claude --retry-on "Service Unavailable" --retry-cmd $'\n'

# Multiple patterns
dog claude \
  --retry-on "Gateway Timeout" \
  --retry-on "overloaded" \
  --retry-cmd "/retry"
```

---

## Retry Rules (Built-in)

| Label | Matched pattern | Response sent |
|---|---|---|
| Certificate / SSL error | `UNKNOWN_CERTIFICATE_VERIFICATION_ERROR`, `SSL.*Error` | `/retry\n` |
| API connection error | `Unable to connect to API`, `ConnectionError` | `/retry\n` |
| API timeout | `Request timed out`, `504`, `ETIMEDOUT` | `/retry\n` |
| Rate limit / quota | `RateLimitError`, `429` | `/retry\n` (after 5 s) |
| Network error | `ECONNRESET`, `fetch failed` | `/retry\n` |
| Claude retry prompt | `(y to retry)` | `y\n` |
| Codex connection error | `openai.APIConnectionError` | `continue\n` |

Add more in `dog/patterns.py` → `RETRY_RULES`.

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Clean exit |
| `1` | Failed to spawn process |
| `2` | Fatal error detected (auth/billing) |
| `3` | Max retries exhausted |
| child code | Propagated from the wrapped CLI |

---

## Project Layout

```
dog-cli/
├── pyproject.toml
├── README.md
└── dog/
    ├── __init__.py
    ├── __main__.py
    ├── cli.py        ← Click entry points
    ├── runner.py     ← pexpect engine
    └── patterns.py   ← all retry/fatal patterns
```
