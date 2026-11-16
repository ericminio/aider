import shlex
from pathlib import Path


def protect_test_environment(coder, path):
    """Keep TDD proposals from changing the machinery that judges their work."""
    relative = Path(path)
    name = relative.name.lower()
    protected = {
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "pyproject.toml",
        "poetry.lock",
        "uv.lock",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "setup.py",
        "conftest.py",
        "makefile",
        "gnumakefile",
        "justfile",
        "pipfile",
        "pipfile.lock",
        "requirements.txt",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "gemfile",
        "gemfile.lock",
    }
    if (
        name in protected
        or ".config." in name
        or name.startswith((".aider", ".env", ".npmrc", ".yarnrc"))
        or any(part in (".git", ".github", "node_modules", ".venv") for part in relative.parts)
    ):
        raise ValueError(f"TDD test environment is protected: {path}")
    command = coder.test_cmd
    if not isinstance(command, str):
        return
    root = Path(coder.root).resolve()
    try:
        tokens = shlex.split(command)
    except ValueError as err:
        raise ValueError("Cannot protect files in an invalid test command") from err
    target = (root / relative).resolve()
    for token in tokens:
        candidate = (root / token).resolve()
        if candidate == target and candidate.is_file():
            raise ValueError(f"TDD test command file is protected: {path}")
