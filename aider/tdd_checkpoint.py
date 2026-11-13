from pathlib import Path


class FileCheckpoint:
    """Capture exact pre-edit file bytes without resetting the user's Git state."""

    def __init__(self, coder):
        self.coder = coder
        self.files = {}
        self.restored = False

    def __enter__(self):
        self.prepare_to_edit = self.coder.prepare_to_edit
        self.editable = set(self.coder.abs_fnames)
        edited = self.coder.aider_edited_files
        self.edited = set(edited) if edited is not None else None
        self.coder.prepare_to_edit = self.capture_edits
        return self

    def capture_edits(self, edits):
        root = Path(self.coder.root).resolve()
        for edit in edits:
            if edit[0] is None:
                continue
            path = Path(self.coder.abs_root_path(edit[0])).resolve()
            if not path.is_relative_to(root) or path == root:
                raise ValueError("Refactor edits must stay within the repository")
            if path not in self.files:
                self.files[path] = (
                    (path.read_bytes(), path.stat().st_mode) if path.exists() else None
                )
        return self.prepare_to_edit(edits)

    def restore(self):
        if self.restored:
            return
        for path, original in self.files.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                content, mode = original
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                path.chmod(mode)
        self.coder.abs_fnames = self.editable
        self.coder.aider_edited_files = self.edited
        self.restored = True

    def __exit__(self, exc_type, exc_value, traceback):
        self.coder.prepare_to_edit = self.prepare_to_edit
        if exc_type is not None:
            self.restore()
