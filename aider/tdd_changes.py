import difflib
from pathlib import Path


class FileChanges:
    def __init__(self, coder, paths):
        self.coder = coder
        self.before = {path: self.read(path) for path in paths}

    def read(self, path):
        target = Path(self.coder.root) / path
        return self.coder.io.read_text(str(target)) if target.exists() else None

    def show(self, action="edit"):
        for path, before in self.before.items():
            after = self.read(path)
            if before == after:
                self.coder.io.tool_output(f"TDD: {action} — {path}: no changes.")
                continue
            state = "created" if before is None else "removed" if after is None else "modified"
            self.coder.io.tool_output(f"TDD: {action} — {path}: {state}.")
            lines = difflib.unified_diff(
                (before or "").splitlines(keepends=True),
                (after or "").splitlines(keepends=True),
                fromfile="/dev/null" if before is None else f"a/{path}",
                tofile="/dev/null" if after is None else f"b/{path}",
            )
            diff = "".join(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                for line in lines
            )
            if diff:
                self.coder.io.tool_output(diff.rstrip("\n"))
