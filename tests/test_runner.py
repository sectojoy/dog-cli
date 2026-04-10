import re
import signal
import unittest
from unittest.mock import patch

import pexpect

from dog.runner import PatternWatcher, Runner, _build_echo_pattern, _signature_id
from dog.patterns import RETRY_RULES, SUCCESS_PATTERNS


class FakeChild:
    def __init__(self, exitstatus: int = 0) -> None:
        self.exitstatus = exitstatus
        self.sent: list[str] = []
        self.closed = False
        self.wait_called = False
        self.filtered_output: bytes | None = None
        self.window_size: tuple[int, int] | None = None

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self, force: bool = False) -> None:
        self.closed = force

    def interact(self, escape_character=None, input_filter=None, output_filter=None) -> None:
        if input_filter is not None:
            input_filter(b"")
        if output_filter is not None:
            self.filtered_output = output_filter(b"hello from child")

    def wait(self) -> None:
        self.wait_called = True

    def setwinsize(self, rows: int, cols: int) -> None:
        self.window_size = (rows, cols)


class PatternWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.child = FakeChild()
        self.watcher = PatternWatcher(
            child=self.child,
            rule_patterns=[],
            permission_patterns=[],
            fatal_re=re.compile(r"fatal"),
            success_re=re.compile(r"done"),
            max_retries=2,
            auto_permission=True,
        )

    @patch("dog.runner.console.print")
    @patch("dog.runner.time.sleep", return_value=None)
    def test_do_retry_sends_response_and_increments_counter(self, _sleep, _print) -> None:
        rule = {"label": "network", "response": "retry\r", "delay": 0}
        self.watcher._buf = "retry me"

        self.watcher._do_retry(rule, "network error")

        self.assertEqual(self.child.sent, ["retry\r"])
        self.assertEqual(len(self.watcher._retry_counts), 1)
        self.assertEqual(next(iter(self.watcher._retry_counts.values())), 1)
        self.assertEqual(self.watcher._buf, "")

    @patch("dog.runner.console.print")
    @patch("dog.runner.time.sleep", return_value=None)
    def test_do_permission_resets_retry_budget(self, _sleep, _print) -> None:
        rule = {"label": "approve", "response": "y\r", "delay": 0}
        self.watcher._retry_counts = {"sig123456": 2}

        self.watcher._do_permission(rule, "approve prompt")

        self.assertEqual(self.child.sent, ["y\r"])
        self.assertEqual(self.watcher._retry_counts, {})

    @patch("dog.runner.console.print")
    @patch("dog.runner.time.sleep", return_value=None)
    @patch("dog.runner.os._exit", side_effect=SystemExit(3))
    def test_do_retry_exits_when_retry_budget_is_exhausted(self, _exit, _sleep, _print) -> None:
        signature_id = _signature_id("network", "network error")
        self.watcher._retry_counts = {signature_id: 2}

        with self.assertRaises(SystemExit) as ctx:
            self.watcher._do_retry({"label": "network", "response": "retry\r", "delay": 0}, "network error")

        self.assertEqual(ctx.exception.code, 3)
        self.assertTrue(self.child.closed)

    def test_note_user_input_clears_success_state_after_submitted_prompt(self) -> None:
        self.watcher._success_seen = True
        self.watcher._buf = "old output"
        self.watcher._retry_counts = {"sig123456": 1}

        self.watcher.note_user_input(b"review current changes")
        self.watcher.note_user_input(b"\r")

        self.assertFalse(self.watcher._success_seen)
        self.assertEqual(self.watcher._buf, "")
        self.assertEqual(self.watcher._retry_counts, {})

    @patch("dog.runner.console.print")
    @patch("dog.runner.time.sleep", return_value=None)
    def test_success_state_blocks_followup_retry_until_user_submits_again(self, _sleep, _print) -> None:
        self.watcher._success_seen = True
        self.watcher._buf = "stream disconnected before completion"
        self.watcher._rule_patterns = [
            (re.compile(r"stream disconnected", re.IGNORECASE), {"response": "continue\r", "label": "codex", "delay": 0})
        ]

        self.watcher.note_user_input(b"\r")
        rule = self.watcher._match(self.watcher._buf, self.watcher._rule_patterns)

        self.assertIsNotNone(rule)
        self.assertTrue(self.watcher._success_seen)
        self.assertEqual(self.child.sent, [])

    def test_match_skips_same_trigger_until_state_resets(self) -> None:
        self.watcher._rule_patterns = [
            (re.compile(r"network error", re.IGNORECASE), {"response": "continue\r", "label": "codex"})
        ]

        first = self.watcher._match("network error", self.watcher._rule_patterns)
        self.assertIsNotNone(first)
        self.watcher._last_triggered_at[_signature_id("codex", "network error")] = 10_000.0

        with patch("dog.runner.time.monotonic", return_value=10_001.0):
            second = self.watcher._match("network error", self.watcher._rule_patterns)
        self.assertIsNone(second)

        self.watcher.note_user_input(b"new task\r")
        third = self.watcher._match("network error", self.watcher._rule_patterns)
        self.assertIsNotNone(third)

    @patch("dog.runner.console.print")
    @patch("dog.runner.time.sleep", return_value=None)
    def test_do_retry_counts_per_failure_signature(self, _sleep, print_mock) -> None:
        rule = {"label": "network", "response": "continue\r", "delay": 0}

        self.watcher._do_retry(rule, "network error")
        self.watcher._do_retry(rule, "network error")
        self.watcher._do_retry(rule, "timeout error")

        self.assertEqual(sorted(self.watcher._retry_counts.values()), [1, 2])
        rendered = "".join(str(call.args[0]) for call in print_mock.call_args_list)
        self.assertIn("sig", rendered)

    def test_match_allows_same_signature_again_after_cooldown(self) -> None:
        rule = {"response": "continue\r", "label": "codex", "delay": 1.0}
        self.watcher._rule_patterns = [
            (re.compile(r"stream disconnected", re.IGNORECASE), rule)
        ]
        signature_id = _signature_id("codex", "stream disconnected")
        self.watcher._last_triggered_at[signature_id] = 100.0

        with patch("dog.runner.time.monotonic", return_value=101.0):
            blocked = self.watcher._match("stream disconnected", self.watcher._rule_patterns)
        self.assertIsNone(blocked)

        with patch("dog.runner.time.monotonic", return_value=102.1):
            allowed = self.watcher._match("stream disconnected", self.watcher._rule_patterns)
        self.assertIsNotNone(allowed)

    def test_feed_suppresses_recent_auto_echo_line(self) -> None:
        pattern = _build_echo_pattern("continue\r")
        self.assertIsNotNone(pattern)
        self.watcher._pending_echo_suppression = [(pattern, 999.0)]

        with patch("dog.runner.time.monotonic", return_value=100.0):
            visible = self.watcher.feed(b"\n\n\xe2\x80\xba continue\n")

        self.assertEqual(visible, b"\n\n\n")
        self.assertEqual(self.watcher._buf, "\n\n\n")

    def test_feed_keeps_normal_output_when_no_echo_suppression_matches(self) -> None:
        with patch("dog.runner.time.monotonic", return_value=100.0):
            visible = self.watcher.feed(b"regular output\n")

        self.assertEqual(visible, b"regular output\n")
        self.assertEqual(self.watcher._buf, "regular output\n")


class RunnerTests(unittest.TestCase):
    @patch("dog.runner.console.print")
    @patch("dog.runner.signal.signal")
    @patch("dog.runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("dog.runner.PatternWatcher.run", return_value=None)
    @patch("dog.runner.pexpect.spawn")
    def test_run_respects_echo_enabled(
        self,
        spawn_mock,
        _watcher_run,
        _getsignal,
        _signal,
        _print,
    ) -> None:
        child = FakeChild()
        spawn_mock.return_value = child

        exit_code = Runner("echo hello", echo=True).run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(child.filtered_output, b"hello from child")
        self.assertTrue(child.wait_called)

    @patch("dog.runner.console.print")
    @patch("dog.runner.signal.signal")
    @patch("dog.runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("dog.runner.PatternWatcher.run", return_value=None)
    @patch("dog.runner.pexpect.spawn")
    def test_run_suppresses_child_output_when_echo_disabled(
        self,
        spawn_mock,
        _watcher_run,
        _getsignal,
        _signal,
        _print,
    ) -> None:
        child = FakeChild()
        spawn_mock.return_value = child

        exit_code = Runner("echo hello", echo=False).run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(child.filtered_output, b"")

    @patch("dog.runner.console.print")
    @patch(
        "dog.runner.pexpect.spawn",
        side_effect=pexpect.exceptions.ExceptionPexpect("boom"),
    )
    def test_run_returns_one_when_spawn_fails(self, _spawn, _print) -> None:
        exit_code = Runner("missing-command").run()

        self.assertEqual(exit_code, 1)


class PatternRulesTests(unittest.TestCase):
    def test_codex_stream_disconnect_prefers_continue_over_generic_retry(self) -> None:
        rules = sorted(RETRY_RULES, key=lambda rule: rule.get("priority", 50))
        text = "stream disconnected before completion: Transport error: network error: error decoding response body"

        matched = None
        for rule in rules:
            if re.search(rule["pattern"], text, re.IGNORECASE):
                matched = rule
                break

        self.assertIsNotNone(matched)
        self.assertEqual(matched["label"], "Codex stream disconnected before completion")
        self.assertEqual(matched["response"], "continue\r")

    def test_codex_chinese_completion_summary_matches_success_patterns(self) -> None:
        success_re = re.compile("|".join(f"(?:{pattern})" for pattern in SUCCESS_PATTERNS), re.IGNORECASE)
        text = "• 已按 docs 的 Stage 1 文档完成第一版落地。现在工程从模板改成了 iOS 15 基线的 Stage 1 结构。"

        self.assertIsNotNone(success_re.search(text))
