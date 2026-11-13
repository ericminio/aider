import json
import shlex
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

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


def test_tdd_command_accepts_a_custom_cycle_budget():
    coder = Mock()
    coder.test_cmd = "npm test"
    commands = Commands(Mock(), coder)
    with patch("aider.tdd.TDDController") as controller:
        commands.run('/tdd --cycles 5 Return "9 = 3*3"')
    controller.assert_called_once_with(coder, max_cycles=5)
    controller.return_value.run.assert_called_once_with('Return "9 = 3*3"')


@pytest.mark.parametrize(
    "decision, expected_message",
    [
        (
            {"action": "done", "reason": "The requested example passes."},
            "TDD: done. The requested example passes.",
        ),
        (
            {"action": "clarify", "question": "What should happen for zero?"},
            "TDD: clarification needed. What should happen for zero?",
        ),
    ],
)
def test_one_cycle_uses_real_test_results_between_model_turns(tmp_path, decision, expected_message):
    coder = Mock()
    coder.root = str(tmp_path)
    coder.test_cmd = "npm test"
    coder.auto_test = True
    coder.auto_lint = True
    coder.auto_commits = True
    coder.dirty_commits = True
    coder.suggest_shell_commands = True
    coder.get_abs_fnames_content.return_value = [("calc.js", "return '9 = 3*3';")]
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
        if "Decide" in messages[0]["content"]:
            events.append("decision")
            assert "return '9 = 3*3';" in messages[-1]["content"]
            return json.dumps(decision)
        events.append("review")
        assert "Expected [] to equal [3, 3]" in messages[-1]["content"]
        return "TDD_RED_OK"

    coder.main_model.simple_send_with_retries.side_effect = review

    def red(intention, behavior):
        edit(intention, False)
        return True

    with (
        patch("aider.tdd.run_cmd", side_effect=run_tests),
        patch("aider.tdd.RedProposal.run", side_effect=red),
    ):
        TDDController(coder).run("Decompose 9 into prime factors")

    assert events == ["tests", "edit", "tests", "review", "edit", "tests", "decision"]
    green = coder.run.call_args.args[0]
    assert "Decompose 9 into prime factors" in green
    assert "Expected [] to equal [3, 3]" in green
    assert "Do not modify tests" in green
    assert coder.auto_test is True
    assert coder.auto_lint is True
    assert coder.auto_commits is True
    assert coder.dirty_commits is True
    assert coder.suggest_shell_commands is True
    coder.io.tool_output.assert_any_call("TDD: green. One cycle complete.")
    coder.io.tool_output.assert_any_call(expected_message)


@pytest.mark.parametrize("summary", ["ℹ tests {count}", "# tests {count}"])
def test_red_with_three_new_tests_cannot_advance_to_green(summary):
    coder = Mock()
    coder.test_cmd = "npm test"
    coder.get_abs_fnames_content.return_value = []
    coder.partial_response_content = "Three new tests: 1, 2, and 9"
    coder.main_model.simple_send_with_retries.return_value = "TDD_RED_OK"
    baseline = summary.format(count=1)
    red = "AssertionError\n" + summary.format(count=4)
    with (
        patch("aider.tdd.run_cmd", side_effect=[(0, baseline), (1, red)]) as tests,
        patch("aider.tdd.RedProposal.run", return_value=True),
    ):
        TDDController(coder).run("Prime decomposition as a string")

    # Only the red edit was requested; neither review nor green may accept the batch.
    assert coder.run.call_count == 0
    assert tests.call_count == 2
    coder.main_model.simple_send_with_retries.assert_not_called()
    coder.io.tool_error.assert_called_once_with(
        "TDD: red added 3 tests (1 → 4); exactly one new test is required. "
        "Review the test edits before continuing."
    )


def test_default_budget_allows_a_third_cycle():
    coder = Mock()
    controller = TDDController(coder)
    with (
        patch.object(controller, "run_tests", return_value=(0, "baseline passed")),
        patch.object(controller, "run_cycle", return_value="green") as cycles,
        patch.object(
            controller,
            "next_step",
            side_effect=[
                {"action": "next", "behavior": "double(3) == 6"},
                {"action": "next", "behavior": "double(4) == 8"},
                {"action": "done", "reason": "Intention satisfied"},
            ],
        ),
    ):
        controller.run("Double an integer")

    assert cycles.call_count == 3
    assert cycles.call_args.args == ("Double an integer", "double(4) == 8")
    coder.io.tool_output.assert_any_call("TDD: done. Intention satisfied")


def test_refactor_can_return_to_test_selection_without_consuming_a_cycle():
    coder = Mock()
    controller = TDDController(coder)
    with (
        patch.object(controller, "run_tests", return_value=(0, "baseline passed")),
        patch.object(
            controller, "run_cycle", side_effect=["first green", "second green"]
        ) as cycles,
        patch.object(controller, "run_refactor", return_value="refactor green") as refactor,
        patch.object(
            controller,
            "next_step",
            side_effect=[
                {"action": "refactor", "reason": "Remove the duplicated answer"},
                {"action": "next", "behavior": "double(3) == 6"},
                {"action": "done", "reason": "Intention satisfied"},
            ],
        ) as decisions,
    ):
        controller.run("Double an integer")

    assert cycles.call_count == 2
    assert cycles.call_args.args == ("Double an integer", "double(3) == 6")
    refactor.assert_called_once_with("Double an integer", "Remove the duplicated answer")
    assert decisions.call_args_list[1].args[2:] == ("refactor green", True)
    assert decisions.call_args_list[2].args[2:] == ("second green", False)


@pytest.mark.parametrize("continue_cycle", [False, True, "refactor"])
def test_command_creates_files_from_read_only_context_with_real_test_runs(continue_cycle):
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
        coder.summarizer.max_tokens = 100000
        original_prompts = coder.gpt_prompts
        responses = [
            "calc.py\n```python\ndef double(n):\n    return 4\n```\n",
        ]
        if continue_cycle:
            responses += [
                "calc.py\n```python\ndef double(n):\n"
                "    if n == 3:\n        return 6\n    return 4\n```\n"
                if continue_cycle == "refactor"
                else "calc.py\n```python\ndef double(n):\n    return n * 2\n```\n",
            ]
        if continue_cycle == "refactor":
            responses += ["calc.py\n```python\ndef double(n):\n    return n * 2\n```\n"]
        responses = iter(responses)

        def send(*args, **kwargs):
            coder.partial_response_content = next(responses)
            coder.partial_response_function_call = {}
            return []

        statuses = []
        tests_before_refactor = []
        assert not coder.abs_fnames

        def record_tests(*args, **kwargs):
            result = run_cmd(*args, **kwargs)
            statuses.append(result[0])
            return result

        def review_or_decide(messages):
            if messages[0]["content"].startswith("TDD RED proposal"):
                assert "Double an integer" in messages[-1]["content"]
                assert "test_ready.py" in messages[-1]["content"]
                if len(statuses) == 1:
                    return json.dumps(
                        {
                            "tests": [{
                                "path": "test_calc.py",
                                "imports": ["import unittest", "from calc import double"],
                                "code": "class TestDouble(unittest.TestCase):\n"
                                "    def test_two(self):\n        self.assertEqual(double(2), 4)",
                            }],
                            "stubs": [
                                {"path": "calc.py", "content": "def double(n):\n    return 0"}
                            ],
                        }
                    )
                assert statuses == [0, 1, 0]
                assert "double(3) == 6" in messages[-1]["content"]
                return json.dumps(
                    {
                        "tests": [{
                            "path": "test_calc.py",
                            "imports": [],
                            "code": "class TestThree(unittest.TestCase):\n"
                            "    def test_three(self):\n        self.assertEqual(double(3), 6)",
                        }],
                        "stubs": [],
                    }
                )
            if "Decide" in messages[0]["content"]:
                if continue_cycle and len(statuses) == 5:
                    assert statuses == [0, 1, 0, 1, 0]
                    if continue_cycle == "refactor":
                        assert "return 6" in messages[-1]["content"]
                        tests_before_refactor.append(Path("test_calc.py").read_text())
                        return json.dumps(
                            {"action": "refactor", "reason": "Replace duplicated example answers"}
                        )
                    return json.dumps({"action": "next", "behavior": "double(4) == 8"})
                if len(statuses) == 6:
                    assert "return n * 2" in messages[-1]["content"]
                    return json.dumps({"action": "done", "reason": "Duplication removed."})
                assert statuses == [0, 1, 0]
                assert "return 4" in messages[-1]["content"]
                assert "self.assertEqual(double(2), 4)" in messages[-1]["content"]
                if continue_cycle:
                    return json.dumps({"action": "next", "behavior": "double(3) == 6"})
                return json.dumps({"action": "done", "reason": "Example implemented."})
            assert statuses[-1] == 1
            return "TDD_RED_OK"

        with (
            patch.object(coder, "get_repo_map", return_value="test_ready.py: TestReady"),
            patch.object(coder, "send", side_effect=send) as model_turns,
            patch.object(
                coder.main_model, "simple_send_with_retries", side_effect=review_or_decide
            ) as review,
            patch("aider.tdd.run_cmd", side_effect=record_tests),
        ):
            coder.run("/tdd --cycles 2 Double an integer")

        expected_statuses = [0, 1, 0, 1, 0] if continue_cycle else [0, 1, 0]
        if continue_cycle == "refactor":
            expected_statuses += [0]
            assert Path("test_calc.py").read_text() == tests_before_refactor[0]
            refactor_prompt = [
                m for m in model_turns.call_args.args[0] if m["role"] == "user"
            ][-1]["content"]
            assert "Replace duplicated example answers" in refactor_prompt
            assert "Do not modify tests" in refactor_prompt
        assert statuses == expected_statuses
        expected_edits = 2 if continue_cycle else 1
        expected_calls = 6 if continue_cycle else 3
        if continue_cycle == "refactor":
            expected_calls = 7
            expected_edits = 3
        assert model_turns.call_count == expected_edits
        assert review.call_count == expected_calls
        expected_return = "n * 2" if continue_cycle else "4"
        assert Path("calc.py").read_text() == f"def double(n):\n    return {expected_return}\n"
        assert "self.assertEqual(double(2), 4)" in Path("test_calc.py").read_text()
        assert Path("test_ready.py").read_text() == ready_test
        assert coder.gpt_prompts is original_prompts
        assert "then stop so I can add" in coder.gpt_prompts.files_no_full_files_with_repo_map
