import re


def assertion_red_error(output):
    """Return why output is not one assertion-red test, or None when it qualifies."""
    output = re.sub(r"\x1b\[[0-9;]*m", "", output or "")
    counts = dict(re.findall(r"(?m)^(?:ℹ|#)\s+(fail|cancelled)\s+(\d+)\s*$", output))
    if "fail" in counts:
        if int(counts["fail"]) != 1:
            return "expected exactly one failing test"
        if int(counts.get("cancelled", "0")):
            return "cancelled tests are not assertion failures"
        if re.search(
            r"(?m)^\s*(?:name:\s*['\"])?"
            r"(?:SyntaxError|ReferenceError|TypeError|RangeError|ImportError|"
            r"ModuleNotFoundError)\b",
            output,
        ):
            return "a setup or runtime error is not an assertion failure"
        if not re.search(r"\[ERR_ASSERTION\]|code:\s*['\"]ERR_ASSERTION['\"]", output):
            return "expected an assertion failure with ERR_ASSERTION"
        return None

    # Preserve the existing Python unittest workflow, used by controller integration tests.
    if re.search(r"(?m)^Ran \d+ tests? in ", output):
        if not re.search(r"(?m)^FAILED \(failures=1\)\s*$", output):
            return "expected one assertion failure and no unittest errors"
        if not re.search(r"(?m)^AssertionError\b", output):
            return "expected an AssertionError"
        return None

    return "cannot verify an assertion failure from this test runner's output"
