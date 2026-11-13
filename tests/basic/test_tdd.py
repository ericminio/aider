import shlex
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from aider.coders import Coder
from aider.commands import Commands
from aider.io import InputOutput
from aider.models import Model
from aider.run_cmd import run_cmd
from aider.tdd import TDDController
from aider.utils import GitTemporaryDirectory


def test_tdd_command_starts_one_cycle_with_the_users_intention():
    coder = Mock()
    coder.test_cmd = "npm test"
    commands = Commands(Mock(), coder)

    with patch("aider.tdd.TDDController") as controller:
        commands.run('/tdd Decompose 9 as "9 = 3*3"')

    controller.assert_called_once_with(coder)
    controller.return_value.run.assert_called_once_with('Decompose 9 as "9 = 3*3"')


def test_one_cycle_uses_real_test_results_between_model_turns(tmp_path):
    coder = Mock()
    coder.root = str(tmp_path)
    coder.test_cmd = "npm test"
    coder.auto_test = True
    coder.auto_lint = True
    coder.auto_commits = True
    coder.dirty_commits = True
    coder.suggest_shell_commands = True
    events = []
    results = iter([(0, "baseline passed"), (1, "Expected [] to equal [3, 3]"), (0, "passed")])

    def run_tests(*args, **kwargs):
        assert args == ("npm test",)
        assert kwargs["cwd"] == str(tmp_path)
        events.append("tests")
        return next(results)

    def edit(message, preproc):
        assert preproc is False
        assert not coder.auto_test
        assert not coder.auto_lint
        assert not coder.auto_commits
        assert not coder.dirty_commits
        assert not coder.suggest_shell_commands
        events.append("edit")
        coder.partial_response_content = "one test for prime factors of 9"

    coder.run.side_effect = edit

    def review(messages):
        events.append("review")
        assert "Expected [] to equal [3, 3]" in messages[-1]["content"]
        return "TDD_RED_OK"

    coder.main_model.simple_send_with_retries.side_effect = review
    with patch("aider.tdd.run_cmd", side_effect=run_tests):
        TDDController(coder).run("Decompose 9 into prime factors")

    assert events == ["tests", "edit", "tests", "review", "edit", "tests"]
    red, green = [call.args[0] for call in coder.run.call_args_list]
    assert "Decompose 9 into prime factors" in red
    assert "exactly one" in red
    assert "Expected [] to equal [3, 3]" in green
    assert "Do not modify tests" in green
    assert coder.auto_test is True
    assert coder.auto_lint is True
    assert coder.auto_commits is True
    assert coder.dirty_commits is True
    assert coder.suggest_shell_commands is True
    coder.io.tool_output.assert_any_call("TDD: green. One cycle complete.")


def test_command_creates_files_from_read_only_context_with_real_test_runs():
    with GitTemporaryDirectory():
        ready_test = (
            "import unittest\n\nclass TestReady(unittest.TestCase):\n"
            "    def test_ready(self):\n        self.assertEqual(1 + 1, 2)\n"
        )
        Path("test_ready.py").write_text(ready_test)
        coder = Coder.create(
            Model("gpt-3.5-turbo"),
            "whole",
            io=InputOutput(yes=True, pretty=False, fancy_input=False),
            read_only_fnames=["test_ready.py"],
            test_cmd=f"{shlex.quote(sys.executable)} -B -m unittest discover",
            auto_test=True,
            auto_commits=False,
            map_tokens=0,
            stream=False,
        )
        responses = iter(
            [
                "calc.py\n```python\ndef double(n):\n    return 0\n```\n\n"
                "test_calc.py\n```python\nimport unittest\nfrom calc import double\n\n"
                "class TestDouble(unittest.TestCase):\n"
                "    def test_two(self):\n        self.assertEqual(double(2), 4)\n```\n",
                "calc.py\n```python\ndef double(n):\n    return 4\n```\n",
            ]
        )

        def send(*args, **kwargs):
            coder.partial_response_content = next(responses)
            coder.partial_response_function_call = {}
            return []

        statuses = []
        assert not coder.abs_fnames

        def record_tests(*args, **kwargs):
            result = run_cmd(*args, **kwargs)
            statuses.append(result[0])
            return result

        with (
            patch.object(coder, "send", side_effect=send) as model_turns,
            patch.object(
                coder.main_model, "simple_send_with_retries", return_value="TDD_RED_OK"
            ) as review,
            patch("aider.tdd.run_cmd", side_effect=record_tests),
        ):
            coder.run("/tdd Double the number 2")

        assert statuses == [0, 1, 0]
        assert model_turns.call_count == 2
        assert "AssertionError: 0 != 4" in review.call_args.args[0][-1]["content"]
        assert Path("calc.py").read_text() == "def double(n):\n    return 4\n"
        assert "self.assertEqual(double(2), 4)" in Path("test_calc.py").read_text()
        assert Path("test_ready.py").read_text() == ready_test
