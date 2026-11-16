import json
import shlex
import sys
from unittest.mock import Mock

import pytest

from aider.io import InputOutput
from aider.tdd import TDDController
from aider.tdd_edit import EditProposal
from aider.tdd_red import RedProposal


def context_coder(tmp_path):
    coder = Mock()
    coder.root = str(tmp_path)
    coder.test_cmd = "node scripts/check.mjs"
    coder.io = InputOutput(yes=True, pretty=False, fancy_input=False)
    coder.abs_read_only_fnames = set()
    coder.abs_fnames = set()
    coder.aider_edited_files = set()
    coder.cur_messages = []
    coder.get_abs_fnames_content.return_value = []
    return coder


@pytest.mark.parametrize(
    "path",
    [
        "package.json",
        "nested/package.json",
        "pyproject.toml",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "Makefile",
        "conftest.py",
        "vitest.config.ts",
        ".git/config",
        ".github/workflows/check.yml",
        "scripts/check.mjs",
    ],
)
@pytest.mark.parametrize("phase", ["production", "red", "stub"])
def test_test_configuration_is_protected_before_any_write(tmp_path, path, phase):
    coder = context_coder(tmp_path)
    target = tmp_path / path
    if path == "scripts/check.mjs":
        target.parent.mkdir()
        target.write_text("process.exit(1);")
    if phase == "production":

        def propose():
            return EditProposal(coder).get_edits(
                {"files": [{"path": path, "content": "process.exit(0);"}]}
            )

    else:
        proposal = {
            "tests": [
                {
                    "path": path if phase == "red" else "calc.test.js",
                    "imports": [],
                    "code": "test('case', () => {});",
                }
            ]
        }
        if phase == "stub":
            proposal["stubs"] = [{"path": path, "content": "process.exit(0);"}]

        def propose():
            return RedProposal(coder).get_edits(json.dumps(proposal))

    with pytest.raises(ValueError, match="protected"):
        propose()
    coder.prepare_to_edit.assert_not_called()
    if target.exists():
        assert target.read_text() == "process.exit(1);"


def test_red_can_extend_a_test_named_explicitly_in_the_test_command(tmp_path):
    coder = context_coder(tmp_path)
    coder.test_cmd = "node --test calc.test.js"
    original = "test('ready', () => {});\n"
    (tmp_path / "calc.test.js").write_text(original)
    edits = RedProposal(coder).get_edits(
        json.dumps(
            {
                "tests": [
                    {
                        "path": "calc.test.js",
                        "code": "test('two', () => assert.equal(double(2), 4));",
                    }
                ]
            }
        )
    )
    assert original in "".join(edits[0][2])
    assert "double(2)" in "".join(edits[0][2])


@pytest.mark.parametrize("review", ["refactor_step", "next_step"])
def test_decision_reviews_keep_read_only_references(tmp_path, review):
    coder = context_coder(tmp_path)
    reference = tmp_path / "requirements.md"
    reference.write_text("Only positive integers are supported; 1 returns '1 = 1'.")
    coder.abs_read_only_fnames = {str(reference)}
    coder.main_model.simple_send_with_retries.return_value = json.dumps(
        {
            "action": "complete" if review == "refactor_step" else "next",
            "behavior": "primeFactors(1) returns '1 = 1'",
            "reason": "Check the base case",
        }
    )
    getattr(TDDController(coder), review)("Prime factors", [], "green")
    messages = coder.main_model.simple_send_with_retries.call_args.args[0]
    assert "Only positive integers" in messages[1]["content"]
    assert "(read-only)" in messages[1]["content"]


def factor_context(tmp_path):
    coder = context_coder(tmp_path)
    production = "export function primeFactors(n) {\n  if (n === 9) return '9 = 3*3';\n}\n"
    test = "test('nine', () => assert.equal(primeFactors(9), '9 = 3*3'));\n"
    (tmp_path / "prime.js").write_text(production)
    (tmp_path / "prime.test.js").write_text(test)
    coder.get_abs_fnames_content.return_value = [("prime.js", production), ("prime.test.js", test)]
    return coder


def refactor_proposal():
    return {
        "action": "refactor",
        "kind": "duplication",
        "reason": "The answer is copied",
        "preserves": "The expression still produces 9 = 3*3 for the tested input",
        "evidence": [
            {"path": "prime.js", "fragment": "'9 = 3*3'"},
            {"path": "prime.test.js", "fragment": "'9 = 3*3'"},
        ],
        "changes": [{"path": "prime.js", "before": "'9 = 3*3'", "after": "`${n} = 3*3`"}],
    }


@pytest.mark.parametrize(
    "bad_evidence",
    [
        [],
        [{"path": "prime.js", "fragment": "const product = factors.reduce"}],
        [{"path": "prime.js", "fragment": "'9 = 3*3'"}] * 2,
        [{"path": "unseen.js", "fragment": "'9 = 3*3'"}],
    ],
)
def test_invented_or_reused_duplication_evidence_is_rejected(tmp_path, bad_evidence):
    coder = factor_context(tmp_path)
    proposal = refactor_proposal()
    proposal["evidence"] = bad_evidence
    with pytest.raises(ValueError, match="evidence"):
        TDDController(coder).validate_refactor_decision(proposal)
    assert (tmp_path / "prime.js").read_text().count("'9 = 3*3'") == 1


def test_refactor_and_repair_allow_larger_readable_formatting(tmp_path):
    coder = context_coder(tmp_path)
    target = tmp_path / "sequences.py"
    source = "def numbers():\n    return [" + ", ".join(map(str, range(1, 16))) + "]\n"
    replacement = (
        "def numbers():\n    result = [\n"
        + "".join(f"        {value},\n" for value in range(1, 16))
        + "    ]\n    return result\n"
    )
    target.write_text(source)
    (tmp_path / "test_sequences.py").write_text(
        "import unittest\nfrom sequences import numbers\n"
        "class NumbersTest(unittest.TestCase):\n"
        "    def test_numbers(self):\n"
        "        self.assertEqual(numbers(), list(range(1, 16)))\n"
    )
    coder.abs_fnames = {str(target)}
    coder.abs_root_path.side_effect = lambda path: str(tmp_path / path)
    coder.prepare_to_edit.side_effect = lambda edits: edits
    coder.get_abs_fnames_content.side_effect = lambda: [(str(target), target.read_text())]
    coder.test_cmd = f"{shlex.quote(sys.executable)} -B -m unittest discover"
    controller = TDDController(coder)
    proposal = {
        "action": "refactor",
        "kind": "simplification",
        "reason": "Name and format the sequence",
        "preserves": "The same ordered numbers are returned",
        "evidence": [{"path": "sequences.py", "fragment": source}],
        "changes": [{"path": "sequences.py", "before": source, "after": replacement}],
    }
    accepted = controller.validate_refactor_decision(proposal)
    assert (
        controller.run_refactor("Return the sequence", accepted["reason"], accepted["changes"])
        is not None
    )
    assert target.read_text() == replacement
    # Full-file refactor repairs use the same permissive candidate gate.
    target.write_text(source)
    coder.main_model.simple_send_with_retries.return_value = json.dumps(
        {
            "files": [{"path": "sequences.py", "content": replacement}],
        }
    )
    assert controller.run_refactor("Return the sequence", accepted["reason"]) is not None
    assert target.read_text() == replacement


def test_completion_requires_test_evidence_and_discloses_assumptions(tmp_path):
    coder = factor_context(tmp_path)
    controller = TDDController(coder)
    with pytest.raises(ValueError, match="evidence"):
        controller.validate_decision({"action": "done", "reason": "All edge cases work"})
    decision = {
        "action": "done",
        "reason": "The specifically requested example passes",
        "evidence": [
            {"path": "prime.test.js", "fragment": "assert.equal(primeFactors(9), '9 = 3*3')"}
        ],
        "assumptions": ["Only the example 9 was requested"],
        "unverified": [],
    }
    assert controller.validate_decision(decision) == decision
    decision["unverified"] = ["Prime inputs"]
    with pytest.raises(ValueError, match="next"):
        controller.validate_decision(decision)
    decision["unverified"] = []
    decision["evidence"] = [{"path": "prime.js", "fragment": "'9 = 3*3'"}]
    with pytest.raises(ValueError, match="test evidence"):
        controller.validate_decision(decision)


def test_green_repairs_cannot_accumulate_changes_across_files(tmp_path):
    coder = context_coder(tmp_path)
    original = "\n".join(f"const x{i} = 0;" for i in range(5))
    changed = original.replace("= 0", "= 1")
    baseline = {"a.js": original, "b.js": original}
    coder.get_abs_fnames_content.return_value = [("a.js", changed), ("b.js", original)]
    with pytest.raises(ValueError, match="small-change"):
        TDDController(coder).refactor_candidates.check(
            [("b.js", "block", changed.splitlines(True))], baseline=baseline
        )


def test_duplication_can_describe_knowledge_expressed_differently(tmp_path):
    coder = context_coder(tmp_path)
    source = "def double(n):\n    return 2 + 2\n"
    (tmp_path / "calc.py").write_text(source)
    coder.get_abs_fnames_content.return_value = [
        ("calc.py", source),
        ("test_calc.py", "self.assertEqual(double(2), 4)"),
    ]
    decision = {
        "action": "refactor",
        "kind": "duplication",
        "reason": "Both expressions encode doubling two",
        "preserves": "For the tested input two, n * 2 still produces four",
        "evidence": [
            {"path": "calc.py", "fragment": "2 + 2"},
            {"path": "test_calc.py", "fragment": "double(2), 4"},
        ],
        "changes": [{"path": "calc.py", "before": "2 + 2", "after": "n * 2"}],
    }
    assert TDDController(coder).validate_refactor_decision(decision)["action"] == "refactor"
    assert (tmp_path / "calc.py").read_text() == source


def test_simplification_may_name_an_intermediate_result(tmp_path):
    coder = factor_context(tmp_path)
    proposal = refactor_proposal()
    proposal["kind"] = "simplification"
    proposal["evidence"] = [proposal["evidence"][0]]
    proposal["changes"] = [
        {
            "path": "prime.js",
            "before": "return '9 = 3*3';",
            "after": "const result = '9 = 3*3'; return result;",
        }
    ]
    assert TDDController(coder).validate_refactor_decision(proposal)["action"] == "refactor"


def test_next_selection_can_request_already_passing_coverage(tmp_path):
    coder = factor_context(tmp_path)

    def choose(messages, **kwargs):
        prompt = messages[0]["content"]
        assert "already pass" in prompt
        assert "why the current code fails it" not in prompt
        return json.dumps(
            {
                "action": "next",
                "behavior": "primeFactors(2) returns '2 = 2'",
                "reason": "A prime is an untested category",
            }
        )

    coder.main_model.simple_send_with_retries.side_effect = choose
    assert TDDController(coder).next_step("Prime factors", [], "green")["action"] == "next"


def test_refactoring_prompt_separates_judgment_from_contract_without_a_quota(tmp_path):
    coder = factor_context(tmp_path)
    coder.main_model.simple_send_with_retries.return_value = json.dumps(
        {
            "action": "complete",
            "reason": "The existing implementation is clear",
        }
    )
    TDDController(coder).refactor_step("Prime factors", [], "green", refactored=2)
    messages = coder.main_model.simple_send_with_retries.call_args.args[0]
    prompt, context = messages[0]["content"], messages[1]["content"]
    assert "repeated knowledge" in prompt
    assert "Missing behavior belongs to the next test" in prompt
    assert "Response contract:" in prompt
    assert "Remaining refactor budget" not in context
    assert "Refactors attempted this cycle" not in context
    assert "non-whitespace" not in prompt
    assert "12" not in prompt
