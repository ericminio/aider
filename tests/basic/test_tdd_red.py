import json
from unittest.mock import Mock

from aider.io import InputOutput
from aider.tdd_red import RedProposal


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
