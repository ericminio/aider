import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model
from aider.run_cmd import run_cmd
from aider.tdd import TDDController
from aider.utils import GitTemporaryDirectory


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for green recovery")
@pytest.mark.parametrize(
    "scenario,expressions,expected_statuses,repair_succeeds",
    [
        ("repair", ["originalN", "'9 = 3*3'"], [0, 1, 1, 0], True),
        ("exhausted", ["originalN", "missing2", "missing3"], [0, 1, 1, 1, 1], False),
        ("repeated", ["originalN", "originalN", "'9 = 3*3'"], [0, 1, 1, 0], True),
        ("oversized", ["oversized", "'9 = 3*3'"], [0, 1, 0], True),
    ],
)
def test_green_repairs_use_real_failures_without_changing_the_accepted_test(
    scenario, expressions, expected_statuses, repair_succeeds
):
    with GitTemporaryDirectory():
        Path("package.json").write_text('{"type":"module"}')
        Path("ready.test.js").write_text("import test from 'node:test'; test('ready', () => {});")
        coder = Coder.create(
            Model("gpt-3.5-turbo"),
            "whole",
            io=InputOutput(yes=True, pretty=False, fancy_input=False),
            read_only_fnames=["package.json", "ready.test.js"],
            test_cmd="node --test --test-reporter=tap *.test.js",
            auto_commits=False,
            dirty_commits=False,
            auto_test=False,
            auto_lint=False,
            suggest_shell_commands=False,
            stream=False,
            map_tokens=0,
        )
        statuses = []
        production_calls = []
        accepted_test = []

        def tests(*args, **kwargs):
            result = run_cmd(*args, **kwargs)
            statuses.append(result[0])
            return result

        def answer(messages, **kwargs):
            if messages[0]["content"].startswith("TDD RED proposal"):
                return json.dumps(
                    {
                        "tests": [
                            {
                                "path": "prime.test.js",
                                "imports": [
                                    "import test from 'node:test';",
                                    "import assert from 'node:assert/strict';",
                                    "import { primeFactors } from './prime.js';",
                                ],
                                "code": (
                                    "test('nine', () => assert.equal(primeFactors(9), '9 = 3*3'));"
                                ),
                            }
                        ],
                        "stubs": [
                            {"path": "prime.js", "content": "export function primeFactors(n) {}"}
                        ],
                    }
                )
            production_calls.append(messages)
            if not accepted_test:
                accepted_test.append(Path("prime.test.js").read_bytes())
            assert Path("prime.test.js").read_bytes() == accepted_test[0]
            content = messages[-1]["content"]
            context = "\n".join(message["content"] for message in messages)
            if len(production_calls) > 1:
                if scenario == "oversized":
                    assert "small-change" in content
                    assert Path("prime.js").read_text() == "export function primeFactors(n) {}\n"
                else:
                    assert "is not defined" in content
                    assert "Do not modify tests" in context
                    assert "GREEN REPAIR" in context
                if scenario == "repeated" and len(production_calls) == 3:
                    assert "previously failed" in content
            expression = expressions[len(production_calls) - 1]
            production = f"export function primeFactors(n) {{ return {expression}; }}"
            if expression == "oversized":
                production = (
                    "export function primeFactors(n) {\n"
                    + "\n".join(f"const unused{i} = n + {i};" for i in range(15))
                    + "\nreturn '9 = 3*3';\n}"
                )
            return json.dumps(
                {
                    "files": [
                        {
                            "path": "prime.js",
                            "content": production,
                        }
                    ]
                }
            )

        controller = TDDController(coder)
        with (
            patch.object(coder.main_model, "simple_send_with_retries", side_effect=answer),
            patch("aider.tdd.run_cmd", side_effect=tests),
        ):
            assert controller.run_tests()[0] == 0
            output = controller.run_cycle("Prime factors", "9 = 3*3")

        assert (output is not None) is repair_succeeds
        assert statuses == expected_statuses
        assert len(production_calls) == len(expressions)
        assert Path("prime.test.js").read_bytes() == accepted_test[0]
        assert Path("package.json").read_text() == '{"type":"module"}'
