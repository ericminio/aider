import shutil
import subprocess

import pytest

from aider.tdd_test_output import assertion_red_error


@pytest.mark.parametrize("reporter", ["spec", "tap"])
@pytest.mark.parametrize(
    "source, accepted",
    [
        (
            "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
            "test('red', () => assert.equal(0, 1));\n",
            True,
        ),
        ("test('red', () => {});\n", False),
        ("import test from 'node:test';\ntest('red', () => {\n", False),
        ("import { f } from './missing.mjs';\n", False),
        (
            "import test from 'node:test';\n"
            "test('red', () => { throw new Error('broken fixture'); });\n",
            False,
        ),
    ],
)
def test_classifies_real_node_test_output(tmp_path, reporter, source, accepted):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the test-runner integration check")
    test_file = tmp_path / "example.test.mjs"
    test_file.write_text(source)
    result = subprocess.run(
        [node, "--test", f"--test-reporter={reporter}", str(test_file)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=20,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert (assertion_red_error(output) is None) is accepted, output


def test_multiple_assertion_failures_do_not_qualify_as_one_red_test():
    error = assertion_red_error("AssertionError [ERR_ASSERTION]\nℹ tests 3\nℹ fail 2\n")
    assert error == "expected exactly one failing test"


def test_cancelled_test_does_not_qualify_as_assertion_red():
    error = assertion_red_error("AssertionError [ERR_ASSERTION]\n# fail 1\n# cancelled 1\n")
    assert error == "cancelled tests are not assertion failures"
