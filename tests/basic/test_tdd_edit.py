import json
from unittest.mock import Mock

import pytest

from aider.io import InputOutput
from aider.tdd import TDDController
from aider.tdd_edit import EditProposal
from aider.tdd_json import parse_object


@pytest.mark.parametrize("missing_slash", [False, True])
@pytest.mark.parametrize("unchanged", [False, True])
def test_conflicting_json_file_entries_are_corrected_before_writing(
    tmp_path, missing_slash, unchanged
):
    coder = Mock()
    coder.root = str(tmp_path)
    history = tmp_path / "chat.md"
    coder.io = InputOutput(
        yes=True, pretty=False, fancy_input=False, chat_history_file=str(history)
    )
    coder.abs_read_only_fnames = set()
    coder.get_abs_fnames_content.return_value = []
    coder.cur_messages = []
    coder.aider_edited_files = set()
    coder.abs_root_path.side_effect = lambda path: str(tmp_path / path)
    coder.prepare_to_edit.side_effect = lambda edits: edits
    target = tmp_path / "calc.js"
    original = f"export const f = () => {2 if unchanged else 0};\n"
    target.write_text(original)
    calls = []

    def answer(messages):
        calls.append(messages)
        assert target.read_text() == original
        if len(calls) == 1:
            return json.dumps({"files": [
                {"path": "calc.js", "content": "export const f = () => 1;\n"},
                {"path": str(target).lstrip("/") if missing_slash else str(target),
                 "content": "export const f = () => 2;\n"},
            ]})
        expected = "leading slash" if missing_slash else "Duplicate file"
        assert expected in messages[-1]["content"]
        return json.dumps({"files": [
            {"path": "calc.js", "content": "export const f = () => 2;\n"}
        ]})

    coder.main_model.simple_send_with_retries.side_effect = answer
    assert EditProposal(coder).run("TDD GREEN phase: make f return 2")
    assert len(calls) == 2
    assert target.read_text() == "export const f = () => 2;\n"
    coder.run.assert_not_called()
    log = history.read_text()
    if unchanged:
        assert "calc.js: no changes." in log
        assert "--- a/calc.js" not in log
    else:
        assert "--- a/calc.js" in log
        assert "+++ b/calc.js" in log
        assert "-export const f = () => 0;" in log
        assert "+export const f = () => 2;" in log
    assert "+export const f = () => 1;" not in log


@pytest.mark.parametrize("response", [
    'Here are the edits: {"files": []}',
    '```json\n{"files": []}\n```',
    '{"files": [], "files": []}',
    '{"files": [{"path": "calc.js", "content": "first", "content": "second"}]}',
    '[]',
])
def test_non_json_or_ambiguous_objects_are_rejected(response):
    with pytest.raises(ValueError):
        parse_object(response)


@pytest.mark.parametrize("path", [
    "../outside.js", "src/__tests__/calc.js", "calc.test.js", "test_calc.py", "reference.js",
])
def test_invalid_file_in_collection_prevents_every_write(tmp_path, path):
    coder = Mock()
    coder.root = str(tmp_path)
    coder.abs_read_only_fnames = {str(tmp_path / "reference.js")}
    coder.get_abs_fnames_content.return_value = []
    coder.main_model.simple_send_with_retries.return_value = json.dumps({"files": [
        {"path": "calc.js", "content": "export const f = () => 2;\n"},
        {"path": path, "content": "invalid edit"},
    ]})

    assert not EditProposal(coder).run("Make f return 2")

    coder.prepare_to_edit.assert_not_called()
    coder.io.write_text.assert_not_called()
    assert coder.main_model.simple_send_with_retries.call_count == 3


def test_invalid_decisions_are_corrected_before_selecting_a_move():
    coder = Mock()
    coder.get_abs_fnames_content.return_value = []
    coder.main_model.simple_send_with_retries.side_effect = [
        'I think we are done.',
        '{"action":"next","behavior":"double(3) == 6"}',
        '{"action":"done","reason":"The rule satisfies the intention"}',
    ]

    decision = TDDController(coder).next_step("Double integers", ["double(2) == 4"], "green")

    assert decision == {"action": "done", "reason": "The rule satisfies the intention"}
    messages = coder.main_model.simple_send_with_retries.call_args.args[0]
    assert "nonempty reason" in messages[-1]["content"]
    coder.run.assert_not_called()


def test_invalid_json_never_reaches_the_file_writer(tmp_path):
    coder = Mock()
    coder.root = str(tmp_path)
    coder.abs_read_only_fnames = set()
    coder.get_abs_fnames_content.return_value = []
    coder.main_model.simple_send_with_retries.return_value = (
        'calc.js\n```javascript\nexport const f = () => 2;\n```'
    )

    assert not EditProposal(coder).run("Make f return 2")

    coder.prepare_to_edit.assert_not_called()
    coder.io.write_text.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("behavior", "one concrete input/output example"),
    ("reason", "duplication present now and the small change that removes it"),
])
def test_copied_prompt_placeholders_are_rejected(field, value):
    with pytest.raises(ValueError, match="placeholder"):
        parse_object(json.dumps({field: value}))
