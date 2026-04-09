import re
import signal
import unittest
from unittest.mock import patch

import pexpect

from dog.runner import PatternWatcher, Runner


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

    def interact(self, escape_character=None, output_filter=None) -> None:
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

        self.watcher._do_retry(rule)

        self.assertEqual(self.child.sent, ["retry\r"])
        self.assertEqual(self.watcher._retries, 1)
        self.assertEqual(self.watcher._buf, "")

    @patch("dog.runner.console.print")
    @patch("dog.runner.time.sleep", return_value=None)
    def test_do_permission_resets_retry_budget(self, _sleep, _print) -> None:
        rule = {"label": "approve", "response": "y\r", "delay": 0}
        self.watcher._retries = 2

        self.watcher._do_permission(rule)

        self.assertEqual(self.child.sent, ["y\r"])
        self.assertEqual(self.watcher._retries, 0)

    @patch("dog.runner.console.print")
    @patch("dog.runner.time.sleep", return_value=None)
    @patch("dog.runner.os._exit", side_effect=SystemExit(3))
    def test_do_retry_exits_when_retry_budget_is_exhausted(self, _exit, _sleep, _print) -> None:
        self.watcher._retries = 2

        with self.assertRaises(SystemExit) as ctx:
            self.watcher._do_retry({"label": "network", "response": "retry\r", "delay": 0})

        self.assertEqual(ctx.exception.code, 3)
        self.assertTrue(self.child.closed)


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
