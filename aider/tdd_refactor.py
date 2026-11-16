import difflib
import hashlib
import json
from pathlib import Path

from aider.tdd_red import RedProposal


class RefactorCandidates:
    max_changed_lines = 12

    def __init__(self, coder):
        self.coder = coder
        self.failed = {}

    def state(self, edits=()):
        root = Path(self.coder.root).resolve()
        files = {
            str((root / name).resolve().relative_to(root)): content
            for name, content in self.coder.get_abs_fnames_content()
        }
        for path, _, lines in edits:
            files[path] = "".join(lines)
        return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()

    def check(self, edits, baseline=None):
        root = Path(self.coder.root).resolve()
        current = dict(self.coder.get_abs_fnames_content())
        originals = current if baseline is None else baseline
        originals = {str((root / path).resolve()): content for path, content in originals.items()}
        candidate = {str((root / path).resolve()): content for path, content in current.items()}
        for path, _, lines in edits:
            candidate[str((root / path).resolve())] = "".join(lines)
        changed = 0
        for path in originals.keys() | candidate.keys():
            before, after = originals.get(path) or "", candidate.get(path) or ""
            diff = difflib.ndiff(before.splitlines(), after.splitlines())
            changed += sum(line[:2] in ("+ ", "- ") and bool(line[2:].strip()) for line in diff)
        if changed > self.max_changed_lines:
            raise ValueError(
                f"Production edit exceeds the {self.max_changed_lines}-line small-change budget "
                f"({changed} added/deleted nonblank lines). Choose a smaller production change."
            )
        self.check_failed(edits)

    def check_failed(self, edits):
        failure = self.failed.get(self.state(edits))
        if failure is not None:
            raise ValueError(
                f"This candidate previously failed; choose a different change.\n{failure}"
            )

    def record_failure(self, output):
        self.failed[self.state()] = output or "The test suite failed"

    def plan(self, changes):
        files = {}
        originals = {}
        validator = RedProposal(self.coder)
        for change in changes:
            path, full_path = validator.validate_path(change.get("path"))
            before, after = change.get("before"), change.get("after")
            if not isinstance(before, str) or not before.strip() or not isinstance(after, str):
                raise ValueError("Each change needs exact before and after strings")
            if before == after:
                raise ValueError("A refactor must change the source fragment")
            if path not in files:
                if not full_path.is_file():
                    raise ValueError(f"Refactor target must exist: {path}")
                files[path] = self.coder.io.read_text(str(full_path))
                originals[path] = files[path]
            content = files[path]
            if content is None or content.count(before) != 1:
                raise ValueError(f"before must match exactly one current source fragment in {path}")
            files[path] = content.replace(before, after, 1)
        if files == originals:
            raise ValueError("The refactor plan makes no change")
        return [{"path": path, "content": content} for path, content in files.items()]
