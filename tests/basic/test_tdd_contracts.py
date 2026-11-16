import json
from unittest.mock import Mock, patch

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
        "duplicate": "'9 = 3*3'",
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


def test_large_algorithm_cannot_be_smuggled_through_refactoring_or_repair(tmp_path):
    coder = factor_context(tmp_path)
    proposal = refactor_proposal()
    replacement = "\n".join(f"  const factor{i} = n / {i + 1};" for i in range(15))
    proposal["changes"] = [
        {"path": "prime.js", "before": coder.get_abs_fnames_content()[0][1], "after": replacement}
    ]
    controller = TDDController(coder)
    with pytest.raises(ValueError, match="small-change"):
        controller.validate_refactor_decision(proposal)
    coder.main_model.simple_send_with_retries.return_value = json.dumps(
        {"files": [{"path": "prime.js", "content": replacement}]}
    )
    with patch("aider.tdd.run_cmd", return_value=(0, "green")) as tests:
        controller.run_refactor("Prime factors", "Remove copied answer")
    coder.prepare_to_edit.assert_not_called()
    assert tests.call_count == 1  # Only verify the restored green checkpoint.


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


def test_refactor_repairs_cannot_accumulate_changes_across_files(tmp_path):
    coder = context_coder(tmp_path)
    original = "\n".join(f"const x{i} = 0;" for i in range(5))
    changed = original.replace("= 0", "= 1")
    baseline = {"a.js": original, "b.js": original}
    coder.get_abs_fnames_content.return_value = [("a.js", changed), ("b.js", original)]
    with pytest.raises(ValueError, match="small-change"):
        TDDController(coder).refactor_candidates.check(
            [("b.js", "block", changed.splitlines(True))], baseline=baseline
        )


def test_duplication_plan_must_reduce_the_cited_repetition(tmp_path):
    coder = factor_context(tmp_path)
    proposal = refactor_proposal()
    proposal["duplicate"] = "'9 = 3*3'"
    proposal["changes"] = [
        {
            "path": "prime.js",
            "before": "return '9 = 3*3';",
            "after": "const result = '9 = 3*3'; return result;",
        }
    ]
    with pytest.raises(ValueError, match="reduce"):
        TDDController(coder).validate_refactor_decision(proposal)


def test_simplification_cannot_expand_a_single_calculation(tmp_path):
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
    with pytest.raises(ValueError, match="simplification must reduce"):
        TDDController(coder).validate_refactor_decision(proposal)


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
