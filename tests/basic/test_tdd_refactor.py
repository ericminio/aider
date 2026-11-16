import json
import shlex
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model
from aider.run_cmd import run_cmd
from aider.tdd import TDDController
from aider.utils import GitTemporaryDirectory


def make_coder():
    Path("calc.py").write_text("# user's uncommitted work\ndef double(n):\n    return n * 2\n")
    Path("test_calc.py").write_text(
        "import unittest\nfrom calc import double\n\n"
        "class TestDouble(unittest.TestCase):\n"
        "    def test_two(self):\n        self.assertEqual(double(2), 4)\n"
    )
    coder = Coder.create(
        Model("gpt-3.5-turbo"), "whole",
        io=InputOutput(yes=True, pretty=False, fancy_input=False),
        fnames=["calc.py", "test_calc.py"],
        test_cmd=f"{shlex.quote(sys.executable)} -B -m unittest discover",
        auto_commits=False, dirty_commits=False, auto_test=False, auto_lint=False,
        suggest_shell_commands=False, stream=False, map_tokens=0,
    )
    coder.summarizer.max_tokens = 100000
    return coder


def test_failed_refactor_is_repaired_before_returning_to_next_step():
    with GitTemporaryDirectory():
        coder = make_coder()
        original_test = Path("test_calc.py").read_bytes()
        responses = iter([
            "def double(n):\n    return 0\n",
            "def double(n):\n    return 1\n",
            "def double(n):\n    return n + n\n",
        ])
        statuses = []

        def send(messages, **kwargs):
            if statuses:
                user_message = [m for m in messages if m["role"] == "user"][-1]["content"]
                assert "0 != 4" in user_message
                assert "Do not modify tests" in user_message
                assert "Pre-refactor green files" in user_message
                assert "return n * 2" in user_message
                assert "Previous failed refactor attempts" in user_message
                assert "+    return 0" in user_message
                if len(statuses) == 2:
                    assert "+    return 1" in user_message
                    assert "1 != 4" in user_message
                    assert '"attempt": 1' in user_message
                    assert '"attempt": 2' in user_message
            return json.dumps({"files": [{"path": "calc.py", "content": next(responses)}]})

        def tests(*args, **kwargs):
            result = run_cmd(*args, **kwargs)
            statuses.append(result[0])
            return result

        with (
            patch.object(coder.main_model, "simple_send_with_retries", side_effect=send) as turns,
            patch("aider.tdd.run_cmd", side_effect=tests),
        ):
            output = TDDController(coder).run_refactor("Double numbers", "Simplify calculation")

        assert output is not None
        assert statuses == [1, 1, 0]
        assert turns.call_count == 3
        assert "return n + n" in Path("calc.py").read_text()
        assert Path("test_calc.py").read_bytes() == original_test


def test_exhausted_refactor_restores_dirty_green_files_and_informs_next_decision():
    with GitTemporaryDirectory():
        coder = make_coder()
        controller = TDDController(coder)
        original = Path("calc.py").read_bytes()
        original_tests = Path("test_calc.py").read_bytes()
        original_files = set(coder.abs_fnames)
        Path("unrelated.txt").write_text("user's other work")
        statuses = []

        def tests(*args, **kwargs):
            result = run_cmd(*args, **kwargs)
            statuses.append(result[0])
            return result

        def decide(messages):
            if messages[0]["content"].startswith("TDD production edit"):
                return json.dumps({"files": [
                    {"path": "calc.py", "content": (
                        f"def double(n):\n    return {len(statuses) - 1}\n"
                    )},
                    {"path": "helper.py", "content": "value = 0\n"},
                ]})
            if messages[0]["content"].startswith("Review refactoring"):
                if not controller.refactor_history:
                    return json.dumps({
                        "action": "refactor", "reason": "Extract a helper",
                        "changes": [{"path": "calc.py", "before": "return n * 2",
                                     "after": "return 0"}]
                    })
                assert "0 != 4" in messages[-1]["content"]
                return json.dumps({"action": "complete", "reason": "No other useful refactor"})
            context = messages[-1]["content"]
            assert "Extract a helper" in context
            assert "0 != 4" in context
            assert "restored" in context.lower()
            assert "# user's uncommitted work" in context
            if cycles.call_count == 1:
                return json.dumps({
                    "action": "next", "behavior": "Double 3 to get 6", "reason": "Missing three"
                })
            return json.dumps({"action": "done", "reason": "Finished after abandoning refactor"})

        with (
            patch.object(coder, "send") as legacy_send,
            patch.object(coder.io, "tool_output", wraps=coder.io.tool_output) as log,
            patch("aider.tdd.run_cmd", side_effect=tests),
            patch.object(
                controller, "run_cycle", side_effect=["first green", "next green"]
            ) as cycles,
            patch.object(coder.main_model, "simple_send_with_retries", side_effect=decide),
        ):
            controller.run("Double numbers")

        assert statuses == [0, 1, 1, 1, 0]
        legacy_send.assert_not_called()
        assert cycles.call_count == 2
        assert cycles.call_args.args == ("Double numbers", "Double 3 to get 6")
        assert Path("calc.py").read_bytes() == original
        assert Path("test_calc.py").read_bytes() == original_tests
        assert not Path("helper.py").exists()
        assert Path("unrelated.txt").read_text() == "user's other work"
        assert coder.abs_fnames == original_files
        messages = "\n".join(call.args[0] for call in log.call_args_list if call.args)
        assert "TDD: rollback — calc.py: modified." in messages
        assert "TDD: rollback — helper.py: removed." in messages
        assert "+# user's uncommitted work" in messages
        assert "-value = 0" in messages


@pytest.mark.parametrize("before,after", [
    ("return 99", "return n + n"), ("return n * 2", "return n * 2"), ("n", "value")
])
def test_refactor_requires_a_current_fragment_and_real_transformation(before, after):
    with GitTemporaryDirectory():
        coder = make_coder()
        controller = TDDController(coder)
        original = Path("calc.py").read_bytes()
        with pytest.raises(ValueError):
            controller.validate_refactor_decision({
                "action": "refactor", "reason": "Replace multiplication with addition",
                "changes": [{"path": "calc.py", "before": before, "after": after}],
            })
        assert Path("calc.py").read_bytes() == original


def test_failed_candidate_is_rejected_before_writing_or_retesting():
    with GitTemporaryDirectory():
        coder = make_coder()
        controller = TDDController(coder)
        statuses = []
        proposals = iter([0, 0, 1, 0, 2])

        def answer(messages):
            value = next(proposals)
            if len(statuses) == 1:
                assert Path("calc.py").read_text().endswith("return 0\n")
            if len(statuses) == 2:
                assert Path("calc.py").read_text().endswith("return 1\n")
            return json.dumps({"files": [{
                "path": "calc.py", "content": f"def double(n):\n    return {value}\n"
            }]})

        def tests(*args, **kwargs):
            result = run_cmd(*args, **kwargs)
            statuses.append(result[0])
            return result

        with (
            patch.object(coder.main_model, "simple_send_with_retries", side_effect=answer),
            patch("aider.tdd.run_cmd", side_effect=tests),
            patch.object(coder.io, "tool_output", wraps=coder.io.tool_output) as log,
        ):
            assert controller.run_refactor("Double numbers", "Simplify calculation") is not None

        assert statuses == [1, 1, 1, 0]
        messages = "\n".join(call.args[0] for call in log.call_args_list if call.args)
        assert "previously failed" in messages
        assert Path("calc.py").read_text().endswith("return n * 2\n")
        with pytest.raises(ValueError, match="previously failed"):
            controller.validate_refactor_decision({
                "action": "refactor", "reason": "Try zero again after rollback",
                "changes": [{"path": "calc.py", "before": Path("calc.py").read_text(),
                             "after": "def double(n):\n    return 0\n"}],
            })
