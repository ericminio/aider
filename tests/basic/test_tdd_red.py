import json
from unittest.mock import Mock

import pytest

from aider.io import InputOutput
from aider.tdd_red import RedProposal


def test_absolute_proposal_paths_inside_repository_are_normalized(tmp_path):
    coder = Mock()
    coder.root = str(tmp_path)
    coder.abs_read_only_fnames = set()
    proposal = {
        "tests": [{"path": str(tmp_path / "src/test.js"), "imports": [],
                   "code": "test('example', () => {});"}],
        "stubs": [{"path": str(tmp_path / "src/calc.js"), "content": "export const f = () => 0;"}],
    }
    edits = RedProposal(coder).get_edits(json.dumps(proposal))
    assert [edit[0] for edit in edits] == ["src/test.js", "src/calc.js"]
    assert not (tmp_path / "src").exists()


@pytest.mark.parametrize("absolute", [False, True])
def test_normalization_keeps_paths_inside_repository(tmp_path, absolute):
    coder = Mock()
    coder.root = str(tmp_path)
    coder.abs_read_only_fnames = set()
    path = str(tmp_path.parent / "outside.js") if absolute else "../outside.js"
    with pytest.raises(ValueError, match="stay within the repository"):
        RedProposal(coder).validate_path(path)


def test_absolute_path_cannot_bypass_read_only_reference(tmp_path):
    coder = Mock()
    coder.root = str(tmp_path)
    reference = tmp_path / "ready.test.js"
    coder.abs_read_only_fnames = {str(reference)}
    with pytest.raises(ValueError, match="read-only"):
        RedProposal(coder).validate_path(str(reference))


def test_rejected_multi_test_proposal_never_reaches_disk(tmp_path):
    coder = Mock()
    coder.root = str(tmp_path)
    coder.io = InputOutput(yes=True, pretty=False, fancy_input=False)
    coder.test_cmd = "npm test"
    coder.abs_read_only_fnames = set()
    coder.get_abs_fnames_content.return_value = []
    coder.cur_messages = []
    coder.abs_root_path.side_effect = lambda path: str(tmp_path / path)
    test_file = tmp_path / "test.js"
    original = "// existing test\ntest('ready', () => {});\n"
    test_file.write_text(original)
    first = {
        "tests": [
            {"path": "test.js", "imports": [], "code": "test('a', () => {});"},
            {"path": "test.js", "imports": [], "code": "test('b', () => {});"},
        ],
        "stubs": [{"path": "calc.js", "content": "export const f = () => 0;\n"}],
    }
    accepted = {
        "tests": [
            {"path": "test.js", "imports": [], "code": "test('a', () => {});"}
        ],
        "stubs": [],
    }

    def answer(messages):
        assert test_file.read_text() == original
        assert not (tmp_path / "calc.js").exists()
        if coder.main_model.simple_send_with_retries.call_count == 1:
            return json.dumps(first)
        assert "2 tests" in messages[-1]["content"]
        return json.dumps(accepted)

    coder.main_model.simple_send_with_retries.side_effect = answer
    coder.prepare_to_edit.side_effect = lambda edits: edits
    assert RedProposal(coder).run("Add a test", "a")
    assert coder.main_model.simple_send_with_retries.call_count == 2
    assert test_file.read_text() == original + "\ntest('a', () => {});\n"
    assert not (tmp_path / "calc.js").exists()


def test_repair_replaces_the_proposed_test_and_preserves_earlier_tests(tmp_path):
    coder = Mock()
    coder.root = str(tmp_path)
    coder.io = InputOutput(yes=True, pretty=False, fancy_input=False)
    coder.abs_read_only_fnames = set()
    coder.get_abs_fnames_content.return_value = []
    coder.cur_messages = []
    coder.abs_root_path.side_effect = lambda path: str(tmp_path / path)
    coder.prepare_to_edit.side_effect = lambda edits: edits
    test_file = tmp_path / "test.js"
    original = "// earlier test\ntest('ready', () => {});\n"
    test_file.write_text(original)
    proposal = RedProposal(coder)
    first = {"tests": [{"path": "test.js", "imports": [], "code": "brokenTest();"}]}
    fixed = {
        "tests": [{"path": "test.js", "imports": ["import test from 'node:test';"],
                   "code": "test('new behavior', () => {});"}]
    }
    coder.main_model.simple_send_with_retries.side_effect = [json.dumps(first), json.dumps(fixed)]
    assert proposal.run("Add one behavior", "new behavior")
    assert "brokenTest();" in test_file.read_text()
    assert proposal.run("Add one behavior", "new behavior", feedback="ReferenceError: brokenTest")
    assert test_file.read_text() == (
        "import test from 'node:test';\n" + original + "\ntest('new behavior', () => {});\n"
    )
    assert "ReferenceError: brokenTest" in (
        coder.main_model.simple_send_with_retries.call_args.args[0][-1]["content"]
    )
