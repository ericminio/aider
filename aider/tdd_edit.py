from pathlib import Path

from aider.coders.wholefile_coder import WholeFileCoder
from aider.tdd_changes import FileChanges
from aider.tdd_json import file_context, request_json
from aider.tdd_red import RedProposal


class EditProposal:
    def __init__(self, coder, validate_candidate=None):
        self.coder = coder
        self.validate_candidate = validate_candidate

    def run(self, prompt):
        coder = self.coder
        messages = [
            {
                "role": "system",
                "content": (
                    "TDD production edit. Return JSON with a files array. Each item has path "
                    "(repository-relative) and content (complete final file text). Use actual "
                    "strings and one version per file, not diffs. Preserve the public API; "
                    "leave tests, configuration and read-only files unchanged. Add no comments. "
                    "Aider applies the edits and runs the tests."
                ),
            },
            {"role": "user", "content": f"{prompt}\nCurrent files:\n{file_context(coder)}"},
        ]
        edits = request_json(coder, messages, self.get_edits, "production edit")
        if edits is None:
            return False
        return self.apply(edits)

    def apply(self, edits):
        coder = self.coder
        changes = FileChanges(coder, [path for path, _, _ in edits])
        approved = coder.prepare_to_edit(edits)
        if len(approved) != len(edits):
            coder.io.tool_error("TDD: production edits were not fully approved; stopping.")
            return False
        WholeFileCoder.apply_edits(coder, approved)
        if coder.aider_edited_files is None:
            coder.aider_edited_files = set()
        for path, _, _ in approved:
            coder.aider_edited_files.add(path)
        changes.show()
        coder.cur_messages += [
            {"role": "user", "content": "Apply the accepted TDD production change."},
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
            if self.is_test_path(path):
                raise ValueError(f"Production phases cannot edit tests: {path}")
            content = file.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"File {path} needs complete content")
            edits.append((path, "block", content.splitlines(keepends=True)))
        if self.validate_candidate is not None:
            self.validate_candidate(edits)
        return edits

    @staticmethod
    def is_test_path(path):
        name = Path(path).name
        return (
            any(
                part in ("tests", "test", "__tests__", "spec", "specs")
                for part in Path(path).parts[:-1]
            )
            or name.startswith("test_")
            or name.startswith("test.")
            or ".test." in name
            or ".spec." in name
            or name.endswith("_test.py")
        )
