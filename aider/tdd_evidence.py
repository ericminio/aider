from pathlib import Path


def validate_evidence(coder, evidence, minimum=1, tests_only=False):
    """Ground model claims in distinct fragments of files actually supplied to it."""
    if not isinstance(evidence, list) or len(evidence) < minimum:
        raise ValueError(f"Provide at least {minimum} source evidence fragment(s)")
    root = Path(coder.root).resolve()
    sources = {(root / path).resolve(): content for path, content in coder.get_abs_fnames_content()}
    sources.update(
        {Path(path).resolve(): coder.io.read_text(path) for path in coder.abs_read_only_fnames}
    )
    locations = []
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("Each evidence item needs path and fragment strings")
        path, fragment = item.get("path"), item.get("fragment")
        if not isinstance(path, str) or not isinstance(fragment, str) or not fragment.strip():
            raise ValueError("Each evidence item needs path and fragment strings")
        resolved = (root / path).resolve()
        content = sources.get(resolved)
        if content is None or content.count(fragment) != 1:
            raise ValueError("Source evidence must match one unique fragment in current context")
        if tests_only:
            from aider.tdd_edit import EditProposal

            if not EditProposal.is_test_path(path):
                raise ValueError("Completion needs test evidence, not production code")
        start = content.index(fragment)
        end = start + len(fragment)
        if any(p == resolved and start < b and end > a for p, a, b in locations):
            raise ValueError("Source evidence must identify distinct, nonoverlapping occurrences")
        locations.append((resolved, start, end))


def validate_string_list(decision, field):
    values = decision.get(field)
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise ValueError(f"{field} must be a list of nonempty strings (or [])")
    return values


def validate_duplication(coder, decision, proposed):
    duplicate = decision.get("duplicate")
    if not isinstance(duplicate, str) or not duplicate.strip():
        raise ValueError("Duplication evidence needs duplicate: the exact repeated source string")
    if any(duplicate not in item["fragment"] for item in decision["evidence"]):
        raise ValueError("The exact duplicate must occur in every cited evidence fragment")
    before = after = 0
    for file in proposed:
        original = coder.io.read_text(str(Path(coder.root) / file["path"]))
        before += original.count(duplicate)
        after += file["content"].count(duplicate)
    if after >= before:
        raise ValueError("A duplication refactor must reduce occurrences of the exact duplicate")


def validate_simplification(coder, proposed):
    before = after = 0
    for file in proposed:
        original = coder.io.read_text(str(Path(coder.root) / file["path"]))
        before += len("".join(original.split()))
        after += len("".join(file["content"].split()))
    if after >= before:
        raise ValueError(
            "A simplification must reduce non-whitespace production source. "
            "Choose complete for equivalent style preferences without a concrete reduction."
        )
