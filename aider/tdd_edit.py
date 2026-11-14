from pathlib import Path

from aider.coders.wholefile_coder import WholeFileCoder
from aider.tdd_json import file_context, request_json
from aider.tdd_red import RedProposal


class EditProposal:
    def __init__(self, coder):
        self.coder = coder

    def run(self, prompt):
        coder = self.coder
        messages = [
            {
                "role": "system",
                "content": (
                    "TDD production edit. Return a JSON object with this structure: "
                    '{"files":[{"path":"relative/source-file",'
                    '"content":"complete final content"}]}. '
                    "Each file must appear exactly once. Return only the final intended version, "
                    "not intermediate alternatives. Include full contents without diff syntax, "
                    "markdown fences, omissions or narrative. JSON strings must escape newlines. "
                    "Do not edit test files or read-only references. Preserve the public API and "
                    "all existing tests. Do not add code comments. Do not run commands. "
                    "The controller will apply accepted edits and run the tests."
                ),
            },
            {"role": "user", "content": f"{prompt}\nCurrent files:\n{file_context(coder)}"},
        ]
        edits = request_json(coder, messages, self.get_edits, "production edit")
        if edits is None:
            return False
        approved = coder.prepare_to_edit(edits)
        if len(approved) != len(edits):
            coder.io.tool_error("TDD: production edits were not fully approved; stopping.")
            return False
        WholeFileCoder.apply_edits(coder, approved)
        if coder.aider_edited_files is None:
            coder.aider_edited_files = set()
        for path, _, _ in approved:
            coder.io.tool_output(f"Applied edit to {path}")
            coder.aider_edited_files.add(path)
        coder.cur_messages += [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "Applied the validated JSON production edit."},
        ]
        return True

    def get_edits(self, proposal):
        files = proposal.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("files must be a nonempty collection")
        paths = set()
        edits = []
        validator = RedProposal(self.coder)
        for file in files:
            if not isinstance(file, dict):
                raise ValueError("Each file needs path and content")
            path, resolved = validator.validate_path(file.get("path"))
            if resolved in paths:
                raise ValueError(f"Duplicate file {path}; return one final version per file")
            paths.add(resolved)
            name = Path(path).name
            if (
                any(
                    part in ("tests", "test", "__tests__", "spec", "specs")
                    for part in Path(path).parts[:-1]
                )
                or name.startswith("test_") or name.startswith("test.")
                or ".test." in name or ".spec." in name or name.endswith("_test.py")
            ):
                raise ValueError(f"Production phases cannot edit tests: {path}")
            content = file.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"File {path} needs complete content")
            edits.append((path, "block", content.splitlines(keepends=True)))
        return edits
