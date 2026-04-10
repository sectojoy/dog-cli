import unittest
from unittest.mock import patch

from click.testing import CliRunner

from dog.cli import _build_extra_rules, main


class BuildExtraRulesTests(unittest.TestCase):
    def test_appends_carriage_return_when_retry_command_has_no_line_ending(self) -> None:
        rules = _build_extra_rules(("Gateway Timeout",), "continue")

        self.assertEqual(
            rules,
            [
                {
                    "label": "custom: Gateway Timeout",
                    "pattern": "Gateway Timeout",
                    "response": "continue\r",
                    "delay": 1.0,
                }
            ],
        )

    def test_preserves_existing_newline_in_retry_command(self) -> None:
        rules = _build_extra_rules(("busy",), "continue\n")

        self.assertEqual(rules[0]["response"], "continue\n")


class CliCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    @patch("dog.cli._run")
    def test_claude_forwards_arguments_and_flags(self, run_mock) -> None:
        result = self.runner.invoke(
            main,
            [
                "claude",
                "-r",
                "5",
                "-t",
                "60",
                "--retry-on",
                "Service Unavailable",
                "--retry-cmd",
                "continue",
                "--no-auto-permission",
                "--model",
                "claude-opus-4-5",
                "fix flaky tests",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        run_mock.assert_called_once_with(
            "claude --model claude-opus-4-5 'fix flaky tests'",
            5,
            60.0,
            False,
            ("Service Unavailable",),
            "continue",
            False,
        )

    @patch("dog.cli._run")
    def test_run_accepts_unknown_options_for_wrapped_command(self, run_mock) -> None:
        result = self.runner.invoke(
            main,
            ["run", "npx", "claude-code", "--model", "opus", "--full-auto"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        run_mock.assert_called_once_with(
            "npx claude-code --model opus --full-auto",
            360,
            30.0,
            False,
            (),
            "continue",
            True,
        )

    @patch("dog.cli._run")
    def test_opencode_forwards_arguments_and_flags(self, run_mock) -> None:
        result = self.runner.invoke(
            main,
            [
                "opencode",
                "-r",
                "7",
                "--retry-on",
                "stream disconnected",
                "run",
                "--continue",
                "--model",
                "openai/gpt-5",
                "fix flaky tests",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        run_mock.assert_called_once_with(
            "opencode run --continue --model openai/gpt-5 'fix flaky tests'",
            7,
            30.0,
            False,
            ("stream disconnected",),
            "continue",
            True,
        )
