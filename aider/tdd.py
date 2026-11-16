import difflib
import json
import re

from aider.run_cmd import run_cmd
from aider.tdd_checkpoint import FileCheckpoint
from aider.tdd_edit import EditProposal
from aider.tdd_evidence import (
    validate_duplication,
    validate_evidence,
    validate_simplification,
    validate_string_list,
)
from aider.tdd_json import file_context, request_json
from aider.tdd_phase import PhaseLoop
from aider.tdd_red import RedProposal
from aider.tdd_refactor import RefactorCandidates
from aider.tdd_test_output import assertion_red_error


class TDDController:
    """Run a bounded sequence of red/green cycles using Aider's existing editor."""

    max_cycles = 10
    max_red_attempts = 3
    max_green_attempts = 3
    max_refactor_attempts = 3
    max_refactors_per_cycle = 3

    def __init__(self, coder, max_cycles=None):
        self.coder = coder
        self.test_count = None
        self.refactor_history = []
        self.refactor_candidates = RefactorCandidates(coder)
        self.coverage_history = []
        self.last_cycle_was_coverage = False
        if max_cycles is not None:
            if not isinstance(max_cycles, int) or max_cycles < 1:
                raise ValueError("The cycle budget must be a positive integer")
            self.max_cycles = max_cycles

    def run(self, intention):
        coder = self.coder
        settings = {
            name: getattr(coder, name)
            for name in (
                "auto_test",
                "auto_lint",
                "auto_commits",
                "dirty_commits",
                "suggest_shell_commands",
            )
        }
        completed = []
        started = 0
        try:
            for name in settings:
                setattr(coder, name, False)
            coder.io.tool_output("TDD: checking the green baseline.")
            status, _ = self.run_tests()
            if status != 0:
                coder.io.tool_error("TDD: start with a passing test suite.")
                return

            behavior = intention
            for cycle in range(1, self.max_cycles + 1):
                started = cycle
                coder.io.tool_output(f"TDD: cycle {cycle}/{self.max_cycles} — {behavior}")
                output = self.run_cycle(intention, behavior)
                if output is None:
                    return
                completed.append(behavior)
                outcome = "coverage already green" if self.last_cycle_was_coverage else "green"
                coder.io.tool_output(f"TDD: {outcome}. Cycles completed: {len(completed)}.")
                refactored = 0
                while True:
                    if refactored >= self.max_refactors_per_cycle:
                        coder.io.tool_output(
                            f"TDD: reached the {self.max_refactors_per_cycle}"
                            "-refactors-per-cycle limit; moving to next behavior or done."
                        )
                        break
                    decision = self.refactor_step(intention, completed, output, refactored)
                    action = decision.get("action")
                    if action == "complete":
                        break
                    if action == "cancelled":
                        coder.io.tool_output(f"TDD: cancelled refactor — {decision['reason']}")
                        self.refactor_history.append(
                            {
                                "reason": decision["reason"],
                                "outcome": "cancelled; test refactoring is disabled",
                                "files": decision["files"],
                            }
                        )
                        refactored += 1
                        continue
                    if action != "refactor":
                        return
                    reason = decision.get("reason")
                    if not isinstance(reason, str) or not reason.strip():
                        coder.io.tool_error(
                            "TDD: refactor needs an identified duplication or smell."
                        )
                        return
                    if decision.get("changes"):
                        output = self.run_refactor(intention, reason, decision["changes"])
                    else:
                        output = self.run_refactor(intention, reason)
                    if output is None:
                        return
                    refactored += 1

                decision = self.next_step(intention, completed, output, refactored)
                action = decision.get("action")
                if action == "done":
                    coder.io.tool_output(f"TDD: done. {decision.get('reason', '')}")
                    coder.io.tool_output(
                        "TDD: completion is the model's assessment of the evidence."
                    )
                    for item in decision.get("evidence", []):
                        coder.io.tool_output(
                            f"TDD: test evidence — {item['path']}: {item['fragment']}"
                        )
                    coder.io.tool_output(
                        "TDD: assumptions — " + "; ".join(decision.get("assumptions", []))
                        if decision.get("assumptions")
                        else "TDD: assumptions — none reported."
                    )
                    return
                if action == "next" and cycle == self.max_cycles:
                    coder.io.tool_output(
                        f"TDD: stopped at the {self.max_cycles}-cycle limit. "
                        "The full intention may still need more work."
                    )
                    return

                behavior = decision.get("behavior")
                if action != "next" or not isinstance(behavior, str) or not behavior.strip():
                    coder.io.tool_error("TDD: no next behavior selected; review before continuing.")
                    return
        finally:
            for name, value in settings.items():
                setattr(coder, name, value)
            coder.io.tool_output(
                f"TDD: finished — cycles completed: {len(completed)}; cycles started: {started}."
            )
            if self.coverage_history:
                coder.io.tool_output(f"TDD: coverage-only cycles: {len(self.coverage_history)}.")

    def run_cycle(self, intention, behavior):
        coder = self.coder
        self.last_cycle_was_coverage = False
        previous_test_count = self.test_count
        coder.io.tool_output("TDD: red — adding one test.")
        proposal = RedProposal(coder)

        def propose(feedback):
            if feedback is None:
                return proposal.run(intention, behavior)
            return proposal.run(intention, behavior, feedback=feedback)

        def evaluate(status, failure):
            coder.io.tool_output("TDD: checking for an assertion failure (no model call).")
            if previous_test_count is not None and self.test_count is not None:
                added = self.test_count - previous_test_count
                if added != 1:
                    return (
                        f"red added {added} tests ({previous_test_count} → {self.test_count}); "
                        "exactly one new test is required"
                    )
            if status == 0:
                if previous_test_count is None or self.test_count is None:
                    return "cannot verify that the passing suite includes exactly one new test"
                return None
            return assertion_red_error(failure)

        result = PhaseLoop(coder.io, "red", self.max_red_attempts).run(
            propose, self.run_tests, evaluate
        )
        if not result.accepted:
            coder.io.tool_error(
                f"TDD: no assertion-red test after {result.attempts} attempts. "
                f"Last rejection: {result.error}. Green phase not started."
            )
            return
        failure = result.output
        if result.status == 0:
            self.last_cycle_was_coverage = True
            self.coverage_history.append(behavior)
            coder.io.tool_output(
                "TDD: new test already passes; kept as coverage. No green production edit needed."
            )
            return result.output or ""

        coder.io.tool_output("TDD: green — implementing the smallest change.")
        original_files = dict(coder.get_abs_fnames_content())
        prompt = (
            f"TDD GREEN phase. Original intention: {intention}\n"
            f"Selected behavior: {behavior}\nActual failing test output:\n{failure}\n"
            "Pass the failing test with a small production change. Prefer a simple rule "
            "over another hardcoded answer when it is a small step. Let generality grow "
            "from the tested examples. Do not modify tests or project configuration."
            " A hardcoded answer for the first example is acceptable. One example does not "
            "justify a full algorithm for untested cases. Across files and repairs, add/delete "
            "at most 12 nonblank lines relative to the accepted red files."
        )

        def implement(feedback):
            message = prompt
            if feedback is not None:
                message += (
                    "\nTDD GREEN REPAIR. The accepted test remains the contract. "
                    "Diagnose the production failure using the actual test output below. "
                    "Repair its cause in the smallest step; do not broaden the implementation. "
                    "Do not modify tests or project configuration.\n"
                    + feedback
                )
            return EditProposal(
                coder, lambda edits: self.refactor_candidates.check(edits, baseline=original_files)
            ).run(message)

        def verify(status, output):
            if status == 0:
                return None
            self.refactor_candidates.record_failure(output)
            return "the existing suite is red"

        result = PhaseLoop(coder.io, "green", self.max_green_attempts).run(
            implement,
            self.run_tests,
            verify,
        )
        if not result.accepted:
            coder.io.tool_error(
                "TDD: green repairs exhausted or unapproved; suite not verified green. "
                "Review the latest files and test output before continuing."
            )
            return
        return result.output or ""

    def run_refactor(self, intention, reason, changes=None):
        self.coder.io.tool_output(f"TDD: refactor — {reason}")
        original_files = dict(self.coder.get_abs_fnames_content())
        failed_attempts = []
        prompt = (
            f"TDD REFACTOR phase. Original intention: {intention}\n"
            f"Identified duplication or code smell: {reason}\n"
            "Remove this duplication in one small production change supported by the "
            "tested examples. Preserve behavior and the public API. Do not modify tests "
            "or project configuration."
        )

        def propose(feedback):
            editor = EditProposal(
                self.coder,
                lambda edits: self.refactor_candidates.check(edits, baseline=original_files),
            )
            if feedback is None and changes is not None:
                files = self.refactor_candidates.plan(changes)
                return editor.apply(editor.get_edits({"files": files}))
            message = prompt
            if feedback is not None:
                message = (
                    f"TDD REFACTOR REPAIR. Original intention: {intention}\n"
                    f"Refactor attempted: {reason}\n{feedback}\n"
                    f"Pre-refactor green files:\n{json.dumps(original_files)}\n"
                    "Previous failed refactor attempts (each diff is against the green files):\n"
                    f"{json.dumps(failed_attempts)}\n"
                    "Current files contain the latest failed attempt. Compare its failure "
                    "with earlier attempts and the green baseline. Repair the cause in a "
                    "smaller step; do not repeat a failed candidate or expand the rewrite. "
                    "Preserve behavior and the public API. Do not modify tests or configuration."
                )
            return editor.run(message)

        def evaluate(status, output):
            if status == 0:
                return None
            self.refactor_candidates.record_failure(output)
            current_files = dict(self.coder.get_abs_fnames_content())
            changes = "".join(
                "".join(
                    difflib.unified_diff(
                        (original_files.get(name) or "").splitlines(keepends=True),
                        (current_files.get(name) or "").splitlines(keepends=True),
                        fromfile=f"green/{name}",
                        tofile=f"candidate/{name}",
                    )
                )
                for name in sorted(original_files.keys() | current_files.keys())
            )
            failed_attempts.append(
                {
                    "attempt": len(failed_attempts) + 1,
                    "changes": changes,
                    "test_output": output,
                }
            )
            return "the existing suite is red"

        with FileCheckpoint(self.coder) as checkpoint:
            result = PhaseLoop(self.coder.io, "refactor", self.max_refactor_attempts).run(
                propose, self.run_tests, evaluate
            )
            if result.accepted:
                self.coder.io.tool_output("TDD: refactor complete; suite remains green.")
                return result.output or ""

            self.coder.io.tool_output(
                "TDD: refactor repairs exhausted; abandoning this attempt and restoring green."
            )
            checkpoint.restore()
            status, output = self.run_tests()
            if status != 0:
                self.coder.io.tool_error("TDD: checkpoint restored but tests are red; stopping.")
                return
            self.refactor_history.append(
                {
                    "reason": reason,
                    "outcome": "abandoned; restored green checkpoint",
                    "attempts": result.attempts,
                    "failure": result.output,
                    "failed_attempts": failed_attempts,
                }
            )
            self.coder.cur_messages += [
                {
                    "role": "user",
                    "content": (
                        f"Abandoned refactor: {reason}. "
                        "Restored the exact pre-refactor files and verified the suite is green. "
                        "The attempted edits are no longer on disk. Choose a different next move."
                    ),
                },
                {
                    "role": "assistant",
                    "content": "I will use the restored files as the current state.",
                },
            ]
            self.coder.io.tool_output(
                "TDD: green checkpoint restored and verified; deciding again."
            )
            return output or ""

    def decision_context(self, intention, completed, output, refactored):
        files = file_context(self.coder)
        return (
            f"Original intention: {intention}\nCompleted cycles: {completed}\n"
            f"Refactors attempted this cycle: {refactored}\n"
            f"Remaining refactor budget: {self.max_refactors_per_cycle - refactored}\n"
            f"Refactor history: {json.dumps(self.refactor_history)}\n"
            f"Already-green coverage examples: {json.dumps(self.coverage_history)}\n"
            f"Latest passing test output:\n{output}\nCurrent files:\n{files}"
        )

    def refactor_step(self, intention, completed, output, refactored=0):
        self.coder.io.tool_output("TDD: looking for the next duplication to remove.")
        messages = [
            {
                "role": "system",
                "content": (
                    "Review refactoring after green. Compare tests and production for repeated "
                    "knowledge, including example values copied into production. No useful "
                    "refactor is a normal outcome; do not invent duplication to fill the budget. "
                    "Remove one small duplication or simplify production; leave tests unchanged. "
                    "Do not introduce a full algorithm for untested categories. Guards, base "
                    "cases and algorithm seeds may be legitimate. Use exact current source "
                    "fragments and their replacement. Aider applies these changes directly.\n"
                    "Return JSON with action refactor, kind (duplication or simplification), "
                    "reason (the repeated knowledge or unnecessary complexity), preserves "
                    "(why the tested behavior is preserved), evidence, and changes. "
                    "Each evidence item has path and fragment, an exact unique source quote "
                    "from current context. Duplication needs at least two distinct occurrences; "
                    "also supply duplicate, the exact repeated source string present in every "
                    "citation. The edit must reduce its occurrences in production. "
                    "Simplification needs one citation and must reduce non-whitespace source. "
                    "Equivalent style preferences are not a useful refactor. "
                    "Moving a single calculation to a "
                    "variable is not duplication removal. Explain the concrete benefit. "
                    "At most 12 added/deleted nonblank lines across all files, including repairs. "
                    "Each change has "
                    "path (repository-relative), before (one unique current fragment), and "
                    "after (its replacement). Supply actual strings.\n"
                    "If no useful change remains, return action complete and a reason. "
                    "Complete ends this phase only."
                ),
            },
            {
                "role": "user",
                "content": self.decision_context(intention, completed, output, refactored),
            },
        ]
        return (
            request_json(self.coder, messages, self.validate_refactor_decision, "refactor review")
            or {}
        )

    def next_step(self, intention, completed, output, refactored=0):
        self.coder.io.tool_output("TDD: choosing the next behavior or done.")
        messages = [
            {
                "role": "system",
                "content": (
                    "Decide the next behavior or done. Compare the intention with the current "
                    "code and tests. Select missing behavior OR a useful untested category, "
                    "even if it may already pass after generalization. Return JSON with action "
                    "next, behavior (a string describing one actual input/output example, "
                    "not an object), and reason (a string explaining the gap in "
                    "behavior or coverage). Passing examples alone do not establish a general "
                    "requirement. Compare materially different cases implied by the intention, "
                    "rather than imposing a fixed test count. Stay within scope and avoid repeats. "
                    "Only when no useful in-scope gap remains, return action done, reason "
                    "(separate tested facts from reasoning about generality), evidence "
                    "(path and fragment quotes from actual current tests), assumptions "
                    "(a list of input-domain assumptions, or []), and unverified "
                    "(a list of remaining in-scope gaps, or []). Do not claim untested cases "
                    "were verified. If unverified is nonempty, choose next instead."
                ),
            },
            {
                "role": "user",
                "content": self.decision_context(intention, completed, output, refactored),
            },
        ]
        return request_json(self.coder, messages, self.validate_decision, "next move") or {}

    def validate_refactor_decision(self, decision):
        decision = self.validate_action(
            decision,
            {
                "refactor": ("reason",),
                "complete": ("reason",),
            },
        )
        if decision["action"] == "complete":
            return decision
        changes = decision.get("changes")
        if (
            not isinstance(changes, list)
            or not changes
            or any(not isinstance(change, dict) for change in changes)
        ):
            raise ValueError("A refactor needs changes with path, before and after strings")
        files = [change.get("path") for change in changes]
        if any(not isinstance(path, str) or not path.strip() for path in files):
            raise ValueError("Refactor files must be nonempty path strings")
        if any(EditProposal.is_test_path(path) for path in files):
            return {
                "action": "cancelled",
                "reason": f"Test refactoring is disabled. Proposed: {decision['reason']}",
                "files": files,
            }
        kind = decision.get("kind")
        if kind not in ("duplication", "simplification"):
            raise ValueError("Refactor kind must be duplication or simplification")
        validate_evidence(self.coder, decision.get("evidence"), 2 if kind == "duplication" else 1)
        if not isinstance(decision.get("preserves"), str) or not decision["preserves"].strip():
            raise ValueError("Refactor needs preserves explaining why tested behavior is preserved")
        proposed = self.refactor_candidates.plan(changes)
        if kind == "duplication":
            validate_duplication(self.coder, decision, proposed)
        else:
            validate_simplification(self.coder, proposed)
        editor = EditProposal(self.coder, self.refactor_candidates.check)
        edits = editor.get_edits({"files": proposed})
        return dict(decision, files=[path for path, _, _ in edits])

    def validate_decision(self, decision):
        decision = self.validate_action(
            decision,
            {
                "next": ("behavior", "reason"),
                "done": ("reason",),
            },
        )
        if decision["action"] == "done":
            validate_evidence(self.coder, decision.get("evidence"), tests_only=True)
            validate_string_list(decision, "assumptions")
            if validate_string_list(decision, "unverified"):
                raise ValueError("Unverified in-scope gaps remain; choose next for one example")
        return decision

    def validate_action(self, decision, fields):
        action = decision.get("action")
        if not isinstance(action, str) or action not in fields:
            raise ValueError("Choose " + " or ".join(fields))
        for field in fields[action]:
            if not isinstance(decision.get(field), str) or not decision[field].strip():
                raise ValueError(
                    f"Decision {action} needs a nonempty {field} string, not an object"
                )
        return decision

    def run_tests(self):
        result = run_cmd(
            self.coder.test_cmd,
            cwd=self.coder.root,
            error_print=self.coder.io.tool_error,
        )
        output = re.sub(r"\x1b\[[0-9;]*m", "", result[1] or "")
        # Node's spec/TAP reporters and Python unittest expose an executed-test total.
        counts = re.findall(r"(?m)^(?:ℹ|#)\s*tests\s+(\d+)\s*$", output)
        if not counts:
            counts = re.findall(r"(?m)^Ran (\d+) tests? in ", output)
        self.test_count = int(counts[-1]) if counts else None
        return result
