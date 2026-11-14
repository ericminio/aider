import json
import shlex
import shutil
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
    results = iter([
        (0, "ℹ tests 1\nℹ fail 0\n"),
        (1, "AssertionError [ERR_ASSERTION]: Expected [] to equal [3, 3]\nℹ tests 2\nℹ fail 1\n"),
        (0, "passed"),
    ])

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
        pytest.fail("Unexpected model call for red review")

    coder.main_model.simple_send_with_retries.side_effect = review

    def red(intention, behavior):
        edit(intention, False)
        return True

    with (
        patch("aider.tdd.run_cmd", side_effect=run_tests),
        patch("aider.tdd.RedProposal.run", side_effect=red),
    ):
        TDDController(coder).run("Decompose 9 into prime factors")

    assert events == ["tests", "edit", "tests", "edit", "tests", "decision"]
    green = coder.run.call_args.args[0]
    assert "Decompose 9 into prime factors" in green
    assert "Expected [] to equal [3, 3]" in green
    assert "Do not modify tests" in green
    assert coder.auto_test is True
    assert coder.auto_lint is True
    assert coder.auto_commits is True
    assert coder.dirty_commits is True
    assert coder.suggest_shell_commands is True
    coder.io.tool_output.assert_any_call("TDD: green. Cycles completed: 1.")
    coder.io.tool_output.assert_any_call(expected_message)
    coder.io.tool_output.assert_any_call("TDD: finished — cycles completed: 1; cycles started: 1.")


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
        patch("aider.tdd.run_cmd", side_effect=[(0, baseline)] + [(1, red)] * 3) as tests,
        patch("aider.tdd.RedProposal.run", return_value=True),
    ):
        TDDController(coder).run("Prime decomposition as a string")

    # Only the red edit was requested; neither review nor green may accept the batch.
    assert coder.run.call_count == 0
    assert tests.call_count == 4
    coder.main_model.simple_send_with_retries.assert_not_called()
    assert "red added 3 tests (1 → 4)" in coder.io.tool_error.call_args.args[0]
    assert "after 3 attempts" in coder.io.tool_error.call_args.args[0]


def test_undefined_test_function_is_repaired_before_green_without_a_model_review():
    coder = Mock()
    coder.test_cmd = "npm test"
    output = (
        "ReferenceError: test is not defined\n"
        "✖ src/__tests__/primeFactors.test.js (180ms)\n"
        "  'test failed'\nℹ tests 2\nℹ pass 1\nℹ fail 1\n"
    )
    assertion = "AssertionError [ERR_ASSERTION]\nℹ tests 2\nℹ fail 1\n"
    with (
        patch("aider.tdd.RedProposal.run", return_value=True) as proposals,
        patch("aider.tdd.run_cmd", side_effect=[
            (0, "ℹ tests 1\nℹ fail 0\n"), (1, output), (1, assertion), (0, "green")
        ]),
        patch.object(TDDController, "next_step", return_value={"action": "done", "reason": "Done"}),
    ):
        TDDController(coder).run("Prime decomposition")

    assert proposals.call_count == 2
    assert "ReferenceError: test is not defined" in proposals.call_args.kwargs["feedback"]
    coder.run.assert_called_once()
    assert assertion in coder.run.call_args.args[0]
    coder.main_model.simple_send_with_retries.assert_not_called()
    coder.io.tool_output.assert_any_call("TDD: finished — cycles completed: 1; cycles started: 1.")


def test_assertion_red_can_advance_to_green_without_a_model_review():
    coder = Mock()
    coder.test_cmd = "npm test"
    controller = TDDController(coder)
    controller.test_count = 1
    red_output = "AssertionError [ERR_ASSERTION]\nℹ tests 2\nℹ fail 1\nℹ cancelled 0\n"
    green_output = "ℹ tests 2\nℹ fail 0\n"
    with (
        patch("aider.tdd.RedProposal.run", return_value=True),
        patch("aider.tdd.run_cmd", side_effect=[(1, red_output), (0, green_output)]),
    ):
        assert controller.run_cycle("Prime decomposition", "9 = 3*3") == green_output

    coder.run.assert_called_once()
    coder.main_model.simple_send_with_retries.assert_not_called()


def test_repairs_missing_node_imports_then_reaches_assertion_red_and_green():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the red repair integration check")
    with GitTemporaryDirectory():
        ready = "import test from 'node:test';\ntest('ready', () => {});\n"
        Path("ready.test.mjs").write_text(ready)
        coder = Coder.create(
            Model("gpt-3.5-turbo"), "whole",
            io=InputOutput(yes=True, pretty=False, fancy_input=False),
            read_only_fnames=["ready.test.mjs"],
            test_cmd=f"{shlex.quote(node)} --test --test-reporter=tap",
            auto_commits=False, map_tokens=0, stream=False,
        )
        coder.summarizer.max_tokens = 100000
        results = []

        def run_tests(*args, **kwargs):
            result = run_cmd(*args, **kwargs)
            results.append(result)
            return result

        def propose(messages):
            imports = ["import { primeFactors } from './prime.mjs';"]
            stubs = [{"path": "prime.mjs", "content": "export const primeFactors = () => '';"}]
            if len(results) == 2:
                assert "ReferenceError" in messages[-1]["content"]
                assert Path("prime.mjs").read_text() == "export const primeFactors = () => '';\n"
                imports += [
                    "import test from 'node:test';", "import assert from 'node:assert/strict';"
                ]
                stubs = []
            else:
                assert len(results) == 1
            return json.dumps({
                "tests": [{
                    "path": "prime.test.mjs", "imports": imports,
                    "code": "test('nine', () => assert.equal(primeFactors(9), '9 = 3*3'));"
                }],
                "stubs": stubs,
            })

        def green(*args, **kwargs):
            assert len(results) == 3
            assert "ERR_ASSERTION" in results[-1][1]
            coder.partial_response_content = (
                "prime.mjs\n```javascript\nexport const primeFactors = () => '9 = 3*3';\n```\n"
            )
            coder.partial_response_function_call = {}
            return []

        with (
            patch("aider.tdd.run_cmd", side_effect=run_tests),
            patch.object(
                coder.main_model, "simple_send_with_retries", side_effect=propose
            ) as proposals,
            patch.object(coder, "send", side_effect=green) as greens,
            patch.object(
                TDDController, "next_step", return_value={"action": "done", "reason": "Done"}
            ),
        ):
            coder.run("/tdd Return 9 = 3*3 for input 9")

        assert [result[0] for result in results] == [0, 1, 1, 0]
        assert proposals.call_count == 2
        assert greens.call_count == 1
        assert Path("prime.test.mjs").read_text().count("test('nine'") == 1
        assert Path("ready.test.mjs").read_text() == ready


def test_already_green_new_test_is_kept_as_coverage_without_repairs_or_production_edits():
    coder = Mock()
    coder.test_cmd = "npm test"
    coder.get_abs_fnames_content.return_value = []
    controller = TDDController(coder)
    baseline = "ℹ tests 3\nℹ fail 0\n"
    green = "ℹ tests 4\nℹ fail 0\n"
    with (
        patch("aider.tdd.RedProposal.run", return_value=True) as proposals,
        patch("aider.tdd.run_cmd", side_effect=[(0, baseline), (0, green)]),
        patch.object(
            coder.main_model, "simple_send_with_retries",
            return_value=json.dumps({"action": "done", "reason": "Primes are covered"}),
        ) as decisions,
    ):
        controller.run("Cover prime input 7")

    proposals.assert_called_once()
    coder.run.assert_not_called()
    decisions.assert_called_once()
    assert "Cover prime input 7" in decisions.call_args.args[0][-1]["content"]
    assert "coverage" in decisions.call_args.args[0][-1]["content"].lower()
    coder.io.tool_output.assert_any_call("TDD: finished — cycles completed: 1; cycles started: 1.")
    coder.io.tool_output.assert_any_call("TDD: coverage-only cycles: 1.")


def test_passing_experiment_returns_its_finding_to_completion_decision():
    coder = Mock()
    coder.test_cmd = "npm test"
    coder.get_abs_fnames_content.return_value = [("prime.js", "// general factorization rule")]
    calls = []

    def decide(messages):
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps({
                "action": "experiment", "question": "Does the rule handle prime inputs?",
                "behavior": "primeFactors(7) returns '7 = 7'",
            })
        context = messages[-1]["content"]
        assert "Does the rule handle prime inputs?" in context
        assert "already green" in context.lower()
        assert "without production changes" in context
        return json.dumps({"action": "done", "reason": "The general rule handles primes too"})

    with (
        patch("aider.tdd.RedProposal.run", return_value=True) as proposals,
        patch("aider.tdd.run_cmd", side_effect=[
            (0, "ℹ tests 1\nℹ fail 0\n"),
            (0, "ℹ tests 2\nℹ fail 0\n"),
            (0, "ℹ tests 3\nℹ fail 0\n"),
        ]),
        patch.object(coder.main_model, "simple_send_with_retries", side_effect=decide),
    ):
        TDDController(coder).run("Prime factorization as a string")

    assert len(calls) == 2
    assert proposals.call_count == 2
    coder.run.assert_not_called()
    coder.io.tool_output.assert_any_call("TDD: experiment — Does the rule handle prime inputs?")
    coder.io.tool_output.assert_any_call("TDD: done. The general rule handles primes too")


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
    coder.io.tool_output.assert_any_call("TDD: green. Cycles completed: 3.")
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
            pytest.fail("Unexpected model call for red review")

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
        expected_calls = 4 if continue_cycle else 2
        if continue_cycle == "refactor":
            expected_calls = 5
            expected_edits = 3
        assert model_turns.call_count == expected_edits
        assert review.call_count == expected_calls
        expected_return = "n * 2" if continue_cycle else "4"
        assert Path("calc.py").read_text() == f"def double(n):\n    return {expected_return}\n"
        assert "self.assertEqual(double(2), 4)" in Path("test_calc.py").read_text()
        assert Path("test_ready.py").read_text() == ready_test
        assert coder.gpt_prompts is original_prompts
        assert "then stop so I can add" in coder.gpt_prompts.files_no_full_files_with_repo_map
