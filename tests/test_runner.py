import re
import signal
import threading
import time
import unittest
import io
from unittest.mock import patch

import pexpect

import dog.runner as runner_mod
from dog.runner import PatternWatcher, Runner, _build_echo_bytes_pattern, _infer_profile, _signature_id
from dog.patterns import RETRY_RULES, SUCCESS_PATTERNS, TOOL_RETRY_RULES


class FakeChild:
    def __init__(
        self,
        exitstatus: int | None = 0,
        interact_output: bytes = b"hello from child",
        tail_output: bytes = b"",
        interact_delay: float = 0.0,
        preserve_exitstatus_after_interact: bool = False,
    ) -> None:
        self.exitstatus = exitstatus
        self.sent: list[str] = []
        self.closed = False
        self.wait_called = False
        self.filtered_output: bytes | None = None
        self.window_size: tuple[int, int] | None = None
        self.interact_output = interact_output
        self.tail_output = tail_output
        self.interact_delay = interact_delay
        self.preserve_exitstatus_after_interact = preserve_exitstatus_after_interact

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self, force: bool = False) -> None:
        self.closed = force

    def interact(self, escape_character=None, input_filter=None, output_filter=None) -> None:
        if input_filter is not None:
            input_filter(b"")
        if output_filter is not None:
            self.filtered_output = output_filter(self.interact_output)
        if self.interact_delay:
            time.sleep(self.interact_delay)
        if self.exitstatus is None and not self.preserve_exitstatus_after_interact:
            self.exitstatus = 0

    def wait(self) -> None:
        self.wait_called = True

    def setwinsize(self, rows: int, cols: int) -> None:
        self.window_size = (rows, cols)

    def isalive(self) -> bool:
        return self.exitstatus is None

    def read_nonblocking(self, size: int = 1, timeout: int | float = 0) -> bytes:
        if self.tail_output:
            chunk = self.tail_output[:size]
            self.tail_output = self.tail_output[size:]
            return chunk
        raise pexpect.exceptions.TIMEOUT("no more buffered output")


class FakeTTYStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.buffer = io.BytesIO()

    def isatty(self) -> bool:
        return True


class PatternWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        runner_mod._LAST_STATUS_MESSAGE = ""
        self.child = FakeChild(exitstatus=None)
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
    def test_stop_cancels_pending_retry_before_send(self, _print) -> None:
        self.child.exitstatus = None
        done = threading.Thread(
            target=self.watcher._do_retry,
            args=({"label": "network", "response": "retry\r", "delay": 5.0}, "network error"),
        )

        done.start()
        time.sleep(0.05)
        self.watcher.stop()
        done.join(timeout=1.0)

        self.assertFalse(done.is_alive())
        self.assertEqual(self.child.sent, [])

    def test_safe_send_skips_dead_child(self) -> None:
        self.child.exitstatus = 1

        self.watcher._safe_send("continue\r")

        self.assertEqual(self.child.sent, [])

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

    @patch("dog.runner.console.print")
    @patch("dog.runner.time.sleep", return_value=None)
    def test_do_retry_selects_highlighted_retry_menu_with_enter(self, _sleep, _print) -> None:
        rule = {"label": "rate limit", "response": "retry\r", "delay": 0}
        self.watcher._buf = (
            "exceeded retry limit, last status: 429 Too Many Requests\n"
            "› retry"
        )

        self.watcher._do_retry(rule, "429 too many requests")

        self.assertEqual(self.child.sent, ["\r"])

    @patch("dog.runner.console.print")
    @patch("dog.runner.time.sleep", return_value=None)
    def test_do_retry_selects_highlighted_continue_menu_with_enter(self, _sleep, _print) -> None:
        rule = {"label": "stream disconnected", "response": "continue\r", "delay": 0}
        self.watcher._buf = "stream disconnected before completion\n❯ continue"

        self.watcher._do_retry(rule, "stream disconnected before completion")

        self.assertEqual(self.child.sent, ["\r"])

    @patch("dog.runner.console.print")
    @patch("dog.runner.time.sleep", return_value=None)
    def test_do_retry_selects_highlighted_continue_menu_with_ansi_word_styling(self, _sleep, _print) -> None:
        rule = {"label": "Codex 429 Too Many Requests", "response": "continue\r", "delay": 0}
        self.watcher._buf = (
            "exceeded retry limit, last status: 429 Too Many Requests\n"
            "❯ \x1b[7mcontinue\x1b[0m"
        )

        self.watcher._do_retry(rule, "429 too many requests")

        self.assertEqual(self.child.sent, ["\r"])

    @patch("dog.runner.console.print")
    def test_do_retry_waits_for_prompt_before_sending_continue(self, _print) -> None:
        rule = {"label": "Codex 429 Too Many Requests", "response": "continue\r", "delay": 30.0}

        def finish_wait(*args, **kwargs) -> bool:
            self.watcher._buf = (
                "exceeded retry limit, last status: 429 Too Many Requests\n"
                "› "
            )
            return True

        with patch.object(self.watcher, "_wait_with_progress", side_effect=finish_wait):
            self.watcher._do_retry(rule, "429 too many requests")

        self.assertEqual(self.child.sent, ["continue\r"])

    @patch("dog.runner.console.print")
    def test_do_retry_rechecks_screen_after_wait_before_sending(self, _print) -> None:
        rule = {"label": "Codex 429 Too Many Requests", "response": "continue\r", "delay": 30.0}

        def finish_wait(*args, **kwargs) -> bool:
            self.watcher._buf = (
                "exceeded retry limit, last status: 429 Too Many Requests\n"
                "❯ continue"
            )
            return True

        with patch.object(self.watcher, "_wait_with_progress", side_effect=finish_wait):
            self.watcher._do_retry(rule, "429 too many requests")

        self.assertEqual(self.child.sent, ["\r"])

    @patch("dog.runner.console.print")
    def test_wait_for_input_prompt_requires_quiet_period(self, _print) -> None:
        self.watcher._profile = "codex"
        self.watcher._buf = "exceeded retry limit, last status: 429 Too Many Requests\n> "
        self.watcher._last_output_at = 100.0

        with (
            patch("dog.runner.time.monotonic", side_effect=[100.0, 100.1, 100.1, 100.5]),
            patch.object(self.watcher._stop_event, "wait", side_effect=[False, False]),
        ):
            response = self.watcher._wait_for_input_prompt(
                "continue\r",
                action="retry",
                label="Codex 429 Too Many Requests",
                retry_count=1,
                quiet_period=0.35,
            )

        self.assertEqual(response, "continue\r")

    @patch("dog.runner.console.print")
    def test_wait_for_input_prompt_supports_opencode_ready_prompt(self, _print) -> None:
        watcher = PatternWatcher(
            child=self.child,
            rule_patterns=[],
            permission_patterns=[],
            fatal_re=re.compile(r"fatal"),
            success_re=re.compile(r"done"),
            max_retries=2,
            auto_permission=True,
            profile="opencode",
        )
        watcher._buf = "temporary failure\n> "
        watcher._last_output_at = 100.0

        with (
            patch("dog.runner.time.monotonic", side_effect=[100.0, 100.5, 100.7, 100.7, 100.9]),
            patch.object(watcher._stop_event, "wait", side_effect=[False]),
        ):
            response = watcher._wait_for_input_prompt(
                "continue\r",
                action="retry",
                label="opencode retry",
                retry_count=1,
            )

        self.assertEqual(response, "continue\r")

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
        pattern = _build_echo_bytes_pattern("continue\r")
        self.assertIsNotNone(pattern)
        self.watcher._pending_echo_suppression = [(pattern, 999.0)]

        with patch("dog.runner.time.monotonic", return_value=100.0):
            visible = self.watcher.feed(b"\n\n\xe2\x80\xba continue\n")

        self.assertEqual(visible, b"\n\n\n")
        self.assertEqual(self.watcher._buf, "\n\n\n")

    def test_feed_suppresses_plain_auto_echo_line_without_prompt_marker(self) -> None:
        pattern = _build_echo_bytes_pattern("continue\r")
        self.assertIsNotNone(pattern)
        self.watcher._pending_echo_suppression = [(pattern, 999.0)]

        with patch("dog.runner.time.monotonic", return_value=100.0):
            visible = self.watcher.feed(b"\ncontinue\n")

        self.assertEqual(visible, b"\n\n")
        self.assertEqual(self.watcher._buf, "\n\n")

    def test_feed_suppresses_recent_auto_echo_line_with_unicode_prompt_marker(self) -> None:
        pattern = _build_echo_bytes_pattern("continue\r")
        self.assertIsNotNone(pattern)
        self.watcher._pending_echo_suppression = [(pattern, 999.0)]

        with patch("dog.runner.time.monotonic", return_value=100.0):
            visible = self.watcher.feed(b"\n\xe2\x9d\xaf continue")

        self.assertEqual(visible, b"\n")
        self.assertEqual(self.watcher._buf, "\n")

    def test_feed_suppresses_recent_auto_echo_line_without_trailing_newline(self) -> None:
        pattern = _build_echo_bytes_pattern("continue\r")
        self.assertIsNotNone(pattern)
        self.watcher._pending_echo_suppression = [(pattern, 999.0)]

        with patch("dog.runner.time.monotonic", return_value=100.0):
            visible = self.watcher.feed(b"\ncontinue")

        self.assertEqual(visible, b"\n")
        self.assertEqual(self.watcher._buf, "\n")

    def test_feed_keeps_normal_output_when_no_echo_suppression_matches(self) -> None:
        with patch("dog.runner.time.monotonic", return_value=100.0):
            visible = self.watcher.feed(b"regular output\n")

        self.assertEqual(visible, b"regular output\n")
        self.assertEqual(self.watcher._buf, "regular output\n")

    def test_feed_preserves_split_utf8_bytes_without_replacement_garbling(self) -> None:
        border = "─".encode("utf-8")

        first = self.watcher.feed(border[:1])
        second = self.watcher.feed(border[1:])

        self.assertEqual(first + second, border)
        self.assertEqual(self.watcher._buf, "─")

    def test_wait_with_progress_cancels_silently_when_user_types_on_tty(self) -> None:
        stderr = io.StringIO()
        stderr.isatty = lambda: True  # type: ignore[attr-defined]
        self.watcher._input_buf = "d"

        with (
            patch("dog.runner.sys.stderr", stderr),
            patch("dog.runner.time.monotonic", side_effect=[0.0, 0.1]),
        ):
            cancelled = self.watcher._wait_with_progress(
                1.0,
                action="retry",
                response="continue\r",
                label="Codex 429 Too Many Requests",
                retry_count=1,
            )

        self.assertFalse(cancelled)
        self.assertEqual(stderr.getvalue(), "")

    def test_wait_with_progress_shows_countdown_on_interactive_tty(self) -> None:
        stderr = FakeTTYStream()

        with (
            patch("dog.runner.sys.stdout", FakeTTYStream()),
            patch("dog.runner.sys.stderr", stderr),
            patch.object(self.watcher._stop_event, "wait", side_effect=[False, False, False, False, False]),
            patch("dog.runner.time.monotonic", side_effect=[100.0, 100.0, 100.6, 101.2, 101.8, 102.1]),
        ):
            finished = self.watcher._wait_with_progress(
                2.0,
                action="retry",
                response="continue\r",
                label="Codex 429 Too Many Requests",
                retry_count=1,
            )

        self.assertTrue(finished)
        self.assertIn("dog retry 1/2: Codex 429 Too Many Requests; sending 'continue' in 2.0s", stderr.getvalue())
        self.assertIn("dog retry 1/2: Codex 429 Too Many Requests; sending 'continue' in 1.4s", stderr.getvalue())
        self.assertTrue(stderr.getvalue().endswith("\r\033[2K"))

    def test_wait_for_idle_waits_for_pending_notify_to_drain(self) -> None:
        self.watcher._notify.set()

        def clear_pending() -> None:
            time.sleep(0.05)
            self.watcher._notify.clear()

        thread = threading.Thread(target=clear_pending)
        thread.start()
        try:
            drained = self.watcher.wait_for_idle(start_timeout=0.01, finish_timeout=0.20)
        finally:
            thread.join(timeout=1.0)

        self.assertTrue(drained)

    def test_do_retry_logs_single_line_without_terminal_rewrite_codes(self) -> None:
        stderr = io.StringIO()
        stderr.isatty = lambda: False  # type: ignore[attr-defined]
        rule = {"label": "Codex 429 Too Many Requests", "response": "continue\r", "delay": 30.0}

        with (
            patch("dog.runner.sys.stderr", stderr),
            patch.object(self.watcher, "_wait_with_progress", return_value=True),
            patch.object(self.watcher, "_wait_for_input_prompt", return_value="continue\r"),
        ):
            self.watcher._do_retry(rule, "429 too many requests")

        self.assertIn("dog retry 1/2: Codex 429 Too Many Requests; sending 'continue' in 30.0s", stderr.getvalue())
        self.assertNotIn("\r\033[2K", stderr.getvalue())

    def test_do_retry_shows_transient_status_on_interactive_tty(self) -> None:
        stdout = FakeTTYStream()
        stderr = FakeTTYStream()
        rule = {"label": "Codex 429 Too Many Requests", "response": "continue\r", "delay": 30.0}

        with (
            patch("dog.runner.sys.stdout", stdout),
            patch("dog.runner.sys.stderr", stderr),
            patch.object(self.watcher, "_wait_with_progress", return_value=True),
            patch.object(self.watcher, "_wait_for_input_prompt", return_value="continue\r"),
        ):
            self.watcher._do_retry(rule, "429 too many requests")

        self.assertIn("\r\033[2Kdog retry 1/2: Codex 429 Too Many Requests; sending 'continue' in 30.0s", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")


class RunnerTests(unittest.TestCase):
    def test_infer_profile_recognizes_supported_wrappers(self) -> None:
        self.assertEqual(_infer_profile("codex --full-auto", None), "codex")
        self.assertEqual(_infer_profile("claude --model opus", None), "claude")
        self.assertEqual(_infer_profile("opencode run", None), "opencode")
        self.assertIsNone(_infer_profile("python tool.py", None))

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
    @patch("dog.runner.signal.signal")
    @patch("dog.runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("dog.runner.pexpect.spawn")
    def test_run_waits_for_pending_retry_before_stopping_watcher(
        self,
        spawn_mock,
        _getsignal,
        _signal,
        _print,
    ) -> None:
        child = FakeChild(
            exitstatus=None,
            interact_output=b"stream disconnected before completion\n> ",
            interact_delay=0.05,
            preserve_exitstatus_after_interact=True,
        )
        spawn_mock.return_value = child

        exit_code = Runner(
            "codex",
            max_retries=2,
            extra_rules=[{
                "label": "test retry",
                "pattern": r"stream disconnected before completion",
                "response": "continue\r",
                "delay": 0.05,
                "priority": 1,
            }],
        ).run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(child.sent, ["continue\r"])
        self.assertTrue(child.wait_called)

    @patch("dog.runner.console.print")
    @patch("dog.runner.signal.signal")
    @patch("dog.runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("dog.runner.pexpect.spawn")
    def test_run_restarts_command_when_retryable_failure_exits_child(
        self,
        spawn_mock,
        _getsignal,
        _signal,
        _print,
    ) -> None:
        first = FakeChild(
            exitstatus=1,
            interact_output=b"retryable failure",
        )
        second = FakeChild(
            exitstatus=0,
            interact_output=b"hello from child",
        )
        spawn_mock.side_effect = [first, second]

        exit_code = Runner(
            "codex",
            max_retries=2,
            extra_rules=[{
                "label": "test retry",
                "pattern": r"retryable failure",
                "response": "continue\r",
                "delay": 0.05,
                "priority": 1,
            }],
        ).run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(spawn_mock.call_count, 2)
        self.assertEqual(first.sent, [])
        self.assertTrue(first.wait_called)
        self.assertTrue(second.wait_called)

    @patch("dog.runner.console.print")
    @patch("dog.runner.signal.signal")
    @patch("dog.runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("dog.runner.pexpect.spawn")
    def test_run_processes_final_retryable_output_even_when_watcher_is_late(
        self,
        spawn_mock,
        _getsignal,
        _signal,
        _print,
    ) -> None:
        first = FakeChild(
            exitstatus=1,
            interact_output=b"retryable failure",
        )
        second = FakeChild(
            exitstatus=0,
            interact_output=b"hello from child",
        )
        spawn_mock.side_effect = [first, second]

        original_run = PatternWatcher.run

        def delayed_run(watcher_self) -> None:
            if watcher_self._child is first:
                time.sleep(0.5)
            return original_run(watcher_self)

        with patch("dog.runner.PatternWatcher.run", new=delayed_run):
            exit_code = Runner(
                "codex",
                max_retries=2,
                extra_rules=[{
                    "label": "test retry",
                    "pattern": r"retryable failure",
                    "response": "continue\r",
                    "delay": 0.0,
                    "priority": 1,
                }],
            ).run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(spawn_mock.call_count, 2)

    @patch("dog.runner.console.print")
    @patch("dog.runner.signal.signal")
    @patch("dog.runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("dog.runner.pexpect.spawn")
    def test_run_retries_when_429_only_arrives_in_tail_output(
        self,
        spawn_mock,
        _getsignal,
        _signal,
        _print,
    ) -> None:
        first = FakeChild(
            exitstatus=1,
            interact_output=b"",
            tail_output=b"exceeded retry limit, last status: 429 Too Many Requests\n",
        )
        second = FakeChild(
            exitstatus=0,
            interact_output=b"hello from child",
        )
        spawn_mock.side_effect = [first, second]

        with patch("dog.runner.PatternWatcher._wait_with_progress", return_value=True):
            exit_code = Runner(
                "codex",
                max_retries=2,
                profile="codex",
            ).run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(spawn_mock.call_count, 2)

    @patch("dog.runner.console.print")
    @patch(
        "dog.runner.pexpect.spawn",
        side_effect=pexpect.exceptions.ExceptionPexpect("boom"),
    )
    def test_run_returns_one_when_spawn_fails(self, _spawn, _print) -> None:
        exit_code = Runner("missing-command").run()

        self.assertEqual(exit_code, 1)

    def test_drain_pending_output_skips_terminal_echo_on_interactive_tty(self) -> None:
        child = FakeChild(exitstatus=1, tail_output=b"tail fragment\n")
        runner = Runner("echo hello")
        runner._child = child
        runner._watcher = PatternWatcher(
            child=child,
            rule_patterns=[],
            permission_patterns=[],
            fatal_re=re.compile(r"fatal"),
            success_re=re.compile(r"done"),
            max_retries=2,
            auto_permission=True,
        )
        stdout = FakeTTYStream()
        stderr = FakeTTYStream()

        with (
            patch("dog.runner.sys.stdout", stdout),
            patch("dog.runner.sys.stderr", stderr),
        ):
            runner._drain_pending_output(runner._watcher.feed)

        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stdout.buffer.getvalue(), b"")
        self.assertIn("tail fragment", runner._watcher._buf)


class PatternRulesTests(unittest.TestCase):
    def test_opencode_highlighted_continue_rule_prefers_enter(self) -> None:
        rules = TOOL_RETRY_RULES["opencode"]
        text = "429 Too Many Requests\n> continue"

        matched = None
        for rule in sorted(rules, key=lambda rule: rule.get("priority", 50)):
            if re.search(rule["pattern"], text, re.IGNORECASE):
                matched = rule
                break

        self.assertIsNotNone(matched)
        self.assertEqual(matched["label"], "Opencode highlighted continue after recoverable failure")
        self.assertEqual(matched["response"], "\r")

    def test_opencode_429_uses_continue_resume(self) -> None:
        rules = TOOL_RETRY_RULES["opencode"]
        text = "429 Too Many Requests"

        matched = None
        for rule in sorted(rules, key=lambda rule: rule.get("priority", 50)):
            if re.search(rule["pattern"], text, re.IGNORECASE):
                matched = rule
                break

        self.assertIsNotNone(matched)
        self.assertEqual(matched["label"], "Opencode 429 / rate limit")
        self.assertEqual(matched["response"], "continue\r")

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

    def test_codex_direct_stream_closed_message_uses_continue(self) -> None:
        rules = TOOL_RETRY_RULES["codex"]
        text = "stream closed before response.completed"

        matched = None
        for rule in sorted(rules, key=lambda rule: rule.get("priority", 50)):
            if re.search(rule["pattern"], text, re.IGNORECASE):
                matched = rule
                break

        self.assertIsNotNone(matched)
        self.assertEqual(matched["label"], "Codex stream disconnected before completion")
        self.assertEqual(matched["response"], "continue\r")

    def test_codex_429_uses_continue_instead_of_generic_retry(self) -> None:
        rules = TOOL_RETRY_RULES["codex"] + RETRY_RULES
        text = "exceeded retry limit, last status: 429 Too Many Requests"

        matched = None
        for rule in sorted(rules, key=lambda rule: rule.get("priority", 50)):
            if re.search(rule["pattern"], text, re.IGNORECASE):
                matched = rule
                break

        self.assertIsNotNone(matched)
        self.assertEqual(matched["label"], "Codex 429 Too Many Requests")
        self.assertEqual(matched["response"], "continue\r")

    def test_codex_chinese_completion_summary_matches_success_patterns(self) -> None:
        success_re = re.compile("|".join(f"(?:{pattern})" for pattern in SUCCESS_PATTERNS), re.IGNORECASE)
        text = "• 已按 docs 的 Stage 1 文档完成第一版落地。现在工程从模板改成了 iOS 15 基线的 Stage 1 结构。"

        self.assertIsNotNone(success_re.search(text))
