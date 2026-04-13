"""
Error / prompt patterns and the responses dog sends to recover.

Each entry in RETRY_RULES is a dict with:
  pattern  : str   — pexpect regex matched against subprocess output
  response : str   — text sent to the subprocess stdin on match
  label    : str   — human-readable label shown in the UI
  delay    : float — seconds to wait before responding (default 1.0)
  priority : int   — lower = checked first (default 50)
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# AUTO-RETRY RULES — triggered on error output, sends retry command
# ---------------------------------------------------------------------------
COMMON_RETRY_RULES: list[dict] = [

    # ── Real-world API error strings seen in Claude Code ─────────────────────
    {
        "label": "Certificate / SSL error (UNKNOWN_CERTIFICATE_VERIFICATION_ERROR)",
        "pattern": r"UNKNOWN_CERTIFICATE_VERIFICATION_ERROR|certificate verify failed|SSL.*[Ee]rror|CERT_|ERR_CERT",
        "response": "retry\r",
        "delay": 30.0,
        "priority": 10,
    },
    {
        "label": "API Error: Unable to connect to API",
        "pattern": r"API Error:.*Unable to connect to API|Unable to connect to API",
        "response": "retry\r",
        "delay": 30.0,
        "priority": 10,
    },
    {
        "label": "API Error: Network error (generic / Chinese gateway)",
        # Matches: API Error: 400 {"error":{"message":"Network error ...
        "pattern": r"API Error:.*[Nn]etwork error|Network error.*error id:",
        "response": "retry\r",
        "delay": 30.0,
        "priority": 10,
    },
    {
        "label": "API connection refused / reset",
        "pattern": r"Connection(?:Error| refused|Reset|Timeout)|ECONNREFUSED|ECONNRESET|ENOTFOUND",
        "response": "retry\r",
        "delay": 30.0,
        "priority": 20,
    },
    {
        "label": "API timeout / gateway timeout",
        "pattern": r"Request timed out|Timeout(?:Error)?|timed? out|ETIMEDOUT|504 Gateway|408",
        "response": "retry\r",
        "delay": 30.0,
        "priority": 20,
    },
    {
        "label": "Rate limit / quota exceeded",
        "pattern": r"rate.?limit|RateLimitError|quota.?exceeded|429",
        "response": "retry\r",
        "delay": 30.0,
        "priority": 20,
    },
    {
        "label": "Fetch / network failed",
        "pattern": r"fetch failed|NetworkError|network error",
        "response": "retry\r",
        "delay": 30.0,
        "priority": 30,
    },
    {
        "label": "Unexpected error / crash",
        "pattern": r"Unexpected error|UnhandledPromiseRejection|Something went wrong",
        "response": "retry\r",
        "delay": 30.0,
        "priority": 30,
    },
    {
        "label": "dev server kill-others error",
        # e.g. "handling the dev event returned with error code 1"
        "pattern": r"handling the dev event returned with error code",
        "response": "retry\r",
        "delay": 30.0,
        "priority": 30,
    },
]

TOOL_RETRY_RULES: dict[str, list[dict]] = {
    "claude": [
        {
            "label": "Claude explicit (y to retry) prompt",
            "pattern": r"\(y to retry\)",
            "response": "y\r",
            "delay": 0.5,
            "priority": 5,
        },
        {
            "label": "Claude 'Press Enter to continue'",
            "pattern": r"Press Enter to continue",
            "response": "\r",
            "delay": 0.3,
            "priority": 5,
        },
    ],
    "codex": [
        {
            "label": "Codex stream disconnected before completion",
            "pattern": (
                r"stream disconnected before completion"
                r"|stream closed before response\.completed"
                r"|Transport error:\s*network error:\s*error decoding response body"
            ),
            "response": "continue\r",
            "allow_plain_prompt": True,
            "delay": 3.0,
            "priority": 8,
        },
        {
            "label": "Codex 429 Too Many Requests",
            "pattern": r"exceeded retry limit, last status:\s*429 Too Many Requests|429 Too Many Requests",
            "response": "continue\r",
            "allow_plain_prompt": True,
            "delay": 30.0,
            "priority": 9,
        },
        {
            "label": "Codex APIConnectionError",
            "pattern": r"openai\.APIConnectionError|openai\.APITimeoutError",
            "response": "continue\r",
            "allow_plain_prompt": True,
            "delay": 30.0,
            "priority": 10,
        },
        {
            "label": "Codex RateLimitError",
            "pattern": r"openai\.RateLimitError",
            "response": "continue\r",
            "allow_plain_prompt": True,
            "delay": 30.0,
            "priority": 10,
        },
    ],
    "opencode": [
        {
            "label": "Opencode highlighted continue after recoverable failure",
            "pattern": (
                r"(?:429 Too Many Requests|rate.?limit|quota.?exceeded"
                r"|stream disconnected|stream closed|fetch failed|network error"
                r"|Request timed out|ETIMEDOUT)(?:.|\n){0,240}?[›❯>]\s*continue\b"
            ),
            "response": "\r",
            "delay": 0.3,
            "priority": 6,
        },
        {
            "label": "Opencode stream interruption",
            "pattern": r"stream disconnected|stream closed",
            "response": "continue\r",
            "allow_plain_prompt": True,
            "delay": 3.0,
            "priority": 9,
        },
        {
            "label": "Opencode 429 / rate limit",
            "pattern": r"429 Too Many Requests|rate.?limit|quota.?exceeded",
            "response": "continue\r",
            "allow_plain_prompt": True,
            "delay": 30.0,
            "priority": 10,
        },
    ],
}

RETRY_RULES: list[dict] = (
    COMMON_RETRY_RULES
    + TOOL_RETRY_RULES["claude"]
    + TOOL_RETRY_RULES["codex"]
    + TOOL_RETRY_RULES["opencode"]
)

# ---------------------------------------------------------------------------
# PERMISSION AUTO-APPROVE RULES
# These match Claude Code's terminal permission prompts and auto-answer them.
# Each rule sends the appropriate key/text to approve the action.
# ---------------------------------------------------------------------------
PERMISSION_RULES: list[dict] = [
    {
        "label": "Permission: Claude execution plan menu → choose bypass permissions",
        "pattern": (
            r"Claude has written up a plan and is ready to execute\.\s*Would you like to proceed\?"
            r"(?:.|\n){0,400}?\b1\.\s*Yes,\s*and\s*bypass permissions\b"
        ),
        "response": "1\r",
        "delay": 0.3,
    },
    # Claude Code standard approval prompts
    # The terminal shows options like:
    #   ❯ Yes   No   Always allow   Always deny
    # or
    #   Do you want to proceed? [y/n]
    {
        "label": "Permission: Yes/No prompt → approve with 'y'",
        "pattern": r"\bDo you want to proceed\?.*\[y[/|]n\]|\bProceed\? \[Y/n\]",
        "response": "y\r",
        "delay": 0.3,
    },
    {
        "label": "Permission: Claude Code tool-use approval (arrow-key menu)",
        # Claude Code shows a menu: ❯ Yes  No  Always allow
        # The first option is already highlighted — pressing Enter accepts it
        "pattern": r"❯\s+Yes\b|❯\s+Allow\b",
        "response": "\r",   # Enter = select highlighted option
        "delay": 0.3,
    },
    {
        "label": "Permission: 'Allow this time' / 'Yes, allow' prompt",
        "pattern": r"\bAllow this time\b|\bYes, allow\b|\ballow once\b",
        "response": "y\r",
        "delay": 0.3,
    },
    {
        "label": "Permission: bash/shell approve with 'y'",
        "pattern": r"\bRun this command\?|\bExecute\b.*\?|\bApprove\b.*\bcommand\b",
        "response": "y\r",
        "delay": 0.3,
    },
    {
        "label": "Permission: file write/edit approve",
        "pattern": r"\bWrite to\b|\bEdit\b.*\bfile\b|\bCreate\b.*\bfile\b.*\bapprove\b|\bAllow file\b",
        "response": "y\r",
        "delay": 0.3,
    },
]

# ---------------------------------------------------------------------------
# SUCCESS PATTERNS — clean task completion, no action needed by dog
# (dog will print a green notice and stop intercepting)
# ---------------------------------------------------------------------------
SUCCESS_PATTERNS: list[str] = [
    # English — explicit "all done" / "summary" messages
    r"All done\.\s+Here'?s? a summary",
    r"All done\.",
    r"Task(?:s)? completed",
    r"No changes needed",
    r"✓ Done",
    r"Here'?s? a summary of (?:what|the work)",
    r"Here'?s?\s+(?:what I|a summary of)",
    r"(?:all )?tasks? (?:are )?complete[d.]",
    # «Already completed» / «Summary of completed work» style
    r"[Aa]lready completed",
    r"[Ss]ummary of (?:completed|the) work",
    r"[Hh]ere(?:'s|\s+is) (?:a |the )?(?:summary|overview|recap)",
    r"[Ww]ork(?:s)? (?:is |are )?(?:done|finished|complete)",
    # Chinese
    r"所有任务完成[。.]",
    r"已完成[。.\s]?\s*(?:以下是|这是|总结|摘要)",
    r"已完成的工作[:：]",
    r"任务完成[。.]",
    r"以下是(?:已完成工作的)?摘要",
    r"已全部完成",
    r"完成了所有",
    r"(?:^|\n)[•*-]\s*已按.*完成",
    r"(?:^|\n)[•*-]\s*已.*完成第?[一二三四五六七八九0-9]+版",
    r"(?:^|\n)[•*-]\s*验证也已?完成",
]

# ---------------------------------------------------------------------------
# INTERRUPTION PATTERNS — user cancelled / interrupted the current turn
# dog should hand control back to the user instead of auto-retrying.
# ---------------------------------------------------------------------------
INTERRUPTION_PATTERNS: list[str] = [
    r"Conversation interrupted\s*-\s*tell the model what to do differently",
    r"\bInterrupted\b.*tell the model what to do differently",
]

# ---------------------------------------------------------------------------
# FATAL PATTERNS — stop immediately, do NOT retry
# ---------------------------------------------------------------------------
FATAL_PATTERNS: list[str] = [
    r"Invalid API key",
    r"AuthenticationError",
    r"Permission denied",
    r"billing.*hard.?limit",
    r"Your account has been disabled",
    r"Maximum context length exceeded",  # can't simply retry this
]
