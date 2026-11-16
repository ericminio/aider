"""Repeatable live-model TDD evaluations; all generated edits run in temporary repos."""

import argparse
import contextlib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model
from aider.repo import GitRepo
from aider.tdd import TDDController

INTENTION = (
    'Create primeFactors(n) returning its prime decomposition as a string, for example "9 = 3*3". '
    'Support positive integers including 1, represented as "1 = 1". '
    "Put the exported function in prime.js."
)
GENERIC = """export function primeFactors(n) {
  const original = n;
  const factors = [];
  for (let divisor = 2; divisor * divisor <= n; divisor++) {
    while (n % divisor === 0) {
      factors.push(divisor);
      n /= divisor;
    }
  }
  if (n > 1) factors.push(n);
  return `${original} = ${factors.join('*') || '1'}`;
}
"""
NINE_TEST = """import test from 'node:test';
import assert from 'node:assert/strict';
import { primeFactors } from './prime.js';
test('nine', () => assert.equal(primeFactors(9), '9 = 3*3'));
"""


def probe_prime_factors(root, timeout=10):
    cases = {
        1: "1 = 1",
        2: "2 = 2",
        3: "3 = 3",
        4: "4 = 2*2",
        8: "8 = 2*2*2",
        9: "9 = 3*3",
        12: "12 = 2*2*3",
        25: "25 = 5*5",
        49: "49 = 7*7",
        97: "97 = 97",
        30030: "30030 = 2*3*5*7*11*13",
    }
    code = (
        "import assert from 'node:assert/strict'; import { primeFactors } from './prime.js';\n"
        f"for (const [n, expected] of Object.entries({json.dumps(cases)})) "
        "assert.equal(primeFactors(Number(n)), expected, `input ${n}`);"
    )
    try:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", code],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "passed": result.returncode == 0,
            "cases": len(cases),
            "output": result.stdout + result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"passed": False, "cases": len(cases), "output": "Oracle timed out"}


def timed_tests(command, cwd, error_print):
    try:
        result = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=30
        )
        output = result.stdout + result.stderr
        print(output)
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return 124, "Evaluation test command timed out after 30 seconds"


def evaluate(model_name, scenario, directory, max_cycles):
    log_path = directory / f"{scenario}.log"
    responses = []
    with tempfile.TemporaryDirectory(prefix="aider-tdd-eval-") as temporary:
        root = Path(temporary)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "package.json").write_text('{"type":"module"}\n')
        ready = root / "ready.test.js"
        ready.write_text("import test from 'node:test'; test('ready', () => {});\n")
        references = [str(root / "package.json"), str(ready)]
        editable = []
        if scenario != "end-to-end":
            production = root / "prime.js"
            production.write_text(
                "export function primeFactors(n) { return '9 = 3*3'; }\n"
                if scenario == "hardcoded-answer"
                else GENERIC
            )
            test = root / "prime.test.js"
            test.write_text(NINE_TEST)
            editable = [str(production), str(test)]
        with (
            log_path.open("w") as log,
            contextlib.redirect_stdout(log),
            contextlib.redirect_stderr(log),
        ):
            io = InputOutput(yes=True, pretty=False, fancy_input=False)
            coder = Coder.create(
                Model(model_name),
                "whole",
                repo=GitRepo(io, [], str(root)),
                fnames=editable,
                read_only_fnames=references,
                io=io,
                test_cmd="node --test --test-reporter=tap *.test.js",
                auto_commits=False,
                dirty_commits=False,
                auto_test=False,
                auto_lint=False,
                suggest_shell_commands=False,
                stream=False,
                map_tokens=0,
            )
            send = coder.main_model.simple_send_with_retries

            def record(messages, **kwargs):
                response = send(messages, **kwargs)
                print("Model response:", response, flush=True)
                responses.append(
                    {"phase": messages[0]["content"].split("\n")[0], "response": response}
                )
                return response

            controller = TDDController(coder, max_cycles=max_cycles)
            with (
                patch.object(coder.main_model, "simple_send_with_retries", side_effect=record),
                patch("aider.tdd.run_cmd", side_effect=timed_tests),
            ):
                if scenario == "end-to-end":
                    controller.run(INTENTION)
                    decision = None
                else:
                    status, output = controller.run_tests()
                    if status != 0:
                        raise RuntimeError("Evaluation fixture must start green")
                    review = (
                        controller.refactor_step
                        if scenario == "no-duplication"
                        else controller.next_step
                    )
                    decision = review(INTENTION, ["primeFactors(9) returns '9 = 3*3'"], output)
            oracle = probe_prime_factors(root) if scenario == "end-to-end" else None
            tested_inputs = sorted(
                {
                    int(value)
                    for path in root.glob("*.test.js")
                    for value in re.findall(r"primeFactors\(\s*(\d+)\s*\)", path.read_text())
                }
            )
            files = {path.name: path.read_text() for path in root.glob("*.js")}
    transcript = log_path.read_text()
    expected = "complete" if scenario == "no-duplication" else "next"
    passed = (
        oracle["passed"] and "TDD: done." in transcript
        if oracle
        else decision.get("action") == expected
    )
    result = {
        "scenario": scenario,
        "passed": passed,
        "decision": decision,
        "oracle": oracle,
        "tested_inputs": tested_inputs,
        "model_responses": len(responses),
        "rejected_responses": transcript.count("before applying changes:") + transcript.count(
            "before writing files:"
        ),
        "refactor_attempts": transcript.count("TDD: refactor —"),
        "completion_claimed": "TDD: done." in transcript,
        "log": str(log_path),
        "responses": responses,
        "files": files,
    }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="ollama_chat/qwen3-coder:30b")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--scenario",
        choices=["all", "hardcoded-answer", "coverage-gap", "no-duplication", "end-to-end"],
        default="all",
    )
    args = parser.parse_args(argv)
    if args.runs < 1 or args.cycles < 1:
        parser.error("runs and cycles must be positive")
    if not shutil.which("node") or not shutil.which("git"):
        parser.error("Node and Git are required")
    args.output.mkdir(parents=True, exist_ok=True)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    report = {
        "model": args.model,
        "runs": args.runs,
        "trials": [],
        "revision": revision.stdout.strip(),
        "dirty": bool(status.stdout.strip()),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    scenarios = ["hardcoded-answer", "coverage-gap", "no-duplication", "end-to-end"]
    if args.scenario != "all":
        scenarios = [args.scenario]
    for run in range(1, args.runs + 1):
        directory = args.output / f"run-{run}"
        directory.mkdir(exist_ok=True)
        for scenario in scenarios:
            print(f"Run {run}/{args.runs}: {scenario}", flush=True)
            try:
                trial = evaluate(args.model, scenario, directory, args.cycles)
            except Exception as err:
                trial = {"scenario": scenario, "passed": False, "error": str(err)}
            trial["run"] = run
            report["trials"].append(trial)
            (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(f"  {'PASS' if trial['passed'] else 'FAIL'}", flush=True)
    return 0 if all(trial["passed"] for trial in report["trials"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
