import copy
import json
import re

from aider.run_cmd import run_cmd
from aider.tdd_checkpoint import FileCheckpoint
from aider.tdd_phase import PhaseLoop
from aider.tdd_red import RedProposal
from aider.tdd_test_output import assertion_red_error


class TDDController:
    """Run a bounded sequence of red/green cycles using Aider's existing editor."""

    max_cycles = 10
    max_red_attempts = 3
    max_refactor_attempts = 3

    def __init__(self, coder, max_cycles=None):
        self.coder = coder
        self.test_count = None
        self.refactor_history = []
        self.coverage_history = []
        self.experiment_history = []
        self.last_cycle_was_coverage = False
        if max_cycles is not None:
            if not isinstance(max_cycles, int) or max_cycles < 1:
                raise ValueError("The cycle budget must be a positive integer")
            self.max_cycles = max_cycles

    def run(self, intention):
        coder = self.coder
        original_prompts = coder.gpt_prompts
        settings = {
            name: getattr(coder, name)
            for name in (
                "auto_test", "auto_lint", "auto_commits", "dirty_commits", "suggest_shell_commands"
            )
        }
        completed = []
        started = 0
        try:
            # Prompt instances are shared by coders. Override only this session's copy.
            coder.gpt_prompts = copy.copy(original_prompts)
            coder.gpt_prompts.main_system = (
                f"{original_prompts.main_system}\n"
                "Do not add comments to generated code, including stubs and repairs.\n"
            )
            new_files_prompt = (
                "No existing files are currently editable. Create new files needed for the "
                "current TDD phase using the normal file-edit format. Choose paths consistent "
                "with the read-only examples and test discovery command. Existing read-only "
                "files must remain unchanged. If an existing file needs editing, ask to add "
                "that file; do not ask to add files that do not exist yet."
            )
            coder.gpt_prompts.files_no_full_files = new_files_prompt
            coder.gpt_prompts.files_no_full_files_with_repo_map = new_files_prompt
            coder.gpt_prompts.files_no_full_files_with_repo_map_reply = (
                "I will create the new files required for this TDD phase."
            )
            for name in settings:
                setattr(coder, name, False)
            coder.io.tool_output("TDD: checking the green baseline.")
            status, _ = self.run_tests()
            if status != 0:
                coder.io.tool_error("TDD: start with a passing test suite.")
                return

            behavior = intention
            experiment = None
            for cycle in range(1, self.max_cycles + 1):
                started = cycle
                coder.io.tool_output(f"TDD: cycle {cycle}/{self.max_cycles} — {behavior}")
                output = self.run_cycle(intention, behavior)
                if output is None:
                    return
                completed.append(behavior)
                outcome = "coverage already green" if self.last_cycle_was_coverage else "green"
                coder.io.tool_output(f"TDD: {outcome}. Cycles completed: {len(completed)}.")
                if experiment is not None:
                    finding = (
                        "already green: behavior confirmed without production changes"
                        if self.last_cycle_was_coverage
                        else "assertion failed first; behavior was implemented and verified green"
                    )
                    self.experiment_history.append({
                        "question": experiment, "behavior": behavior, "finding": finding
                    })
                    coder.io.tool_output(f"TDD: experiment finding — {finding}.")
                refactored = False
                while True:
                    decision = self.next_step(intention, completed, output, refactored)
                    action = decision.get("action")
                    if action != "refactor":
                        break
                    if refactored:
                        coder.io.tool_output("TDD: stopped at the one-refactor-per-cycle limit.")
                        return
                    reason = decision.get("reason")
                    if not isinstance(reason, str) or not reason.strip():
                        coder.io.tool_error(
                            "TDD: refactor needs an identified duplication or smell."
                        )
                        return
                    output = self.run_refactor(intention, reason)
                    if output is None:
                        return
                    refactored = True

                if action == "done":
                    coder.io.tool_output(f"TDD: done. {decision.get('reason', '')}")
                    return
                if action == "clarify":
                    question = decision.get("question", "")
                    coder.io.tool_output(f"TDD: clarification needed. {question}")
                    return
                if action in ("next", "experiment") and cycle == self.max_cycles:
                    coder.io.tool_output(
                        f"TDD: stopped at the {self.max_cycles}-cycle limit. "
                        "The full intention may still need more work."
                    )
                    return

                behavior = decision.get("behavior")
                if (
                    action not in ("next", "experiment")
                    or not isinstance(behavior, str)
                    or not behavior.strip()
                ):
                    coder.io.tool_error("TDD: no next behavior selected; review before continuing.")
                    return
                experiment = None
                if action == "experiment":
                    experiment = decision.get("question")
                    if not isinstance(experiment, str) or not experiment.strip():
                        coder.io.tool_error("TDD: an experiment needs a specific question.")
                        return
                    coder.io.tool_output(f"TDD: experiment — {experiment}")
        finally:
            coder.gpt_prompts = original_prompts
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
        coder.run(
            f"TDD GREEN phase. Original intention: {intention}\n"
            f"Selected behavior: {behavior}\nActual failing test output:\n{failure}\n"
            "Make the smallest production-code change that passes this test and all "
            "existing tests. Do not modify tests or project configuration. Do not generalize "
            "for untested cases. Output each changed file exactly once. Stop after this "
            "green step; the controller will run the tests.",
            preproc=False,
        )
        status, output = self.run_tests()
        if status != 0:
            coder.io.tool_error("TDD: suite is still red; review the output before continuing.")
            return
        return output or ""

    def run_refactor(self, intention, reason):
        self.coder.io.tool_output(f"TDD: refactor — {reason}")
        prompt = (
            f"TDD REFACTOR phase. Original intention: {intention}\n"
            f"Identified duplication or code smell: {reason}\n"
            "The suite is green. Remove this duplication with the smallest appropriate "
            "production-code refactoring. Repeated hardcoded answers in production duplicate "
            "the examples in tests: replace them with a rule where appropriate. Generalizing "
            "to remove duplication is allowed in this phase. Keep the concrete examples in "
            "the tests. Do not modify tests or project configuration. Preserve the public API "
            "and existing observable behavior; do not add unrelated behavior or abstractions. "
            "Output each changed file exactly once. Stop after this refactor; the controller "
            "will run the tests."
        )

        def propose(feedback):
            message = prompt
            if feedback is not None:
                message = (
                    f"TDD REFACTOR REPAIR. Original intention: {intention}\n"
                    f"Refactor attempted: {reason}\n{feedback}\n"
                    "Fix the refactor so the existing suite is green again. Do not modify tests "
                    "or project configuration. Preserve the public API and existing behavior. "
                    "Keep the change small; do not add behavior or pursue another refactor. "
                    "Output each changed file exactly once, then stop for the controller's tests."
                )
            self.coder.run(message, preproc=False)
            return True

        def evaluate(status, output):
            return "the existing suite is red" if status != 0 else None

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
            self.refactor_history.append({
                "reason": reason,
                "outcome": "abandoned; restored green checkpoint",
                "attempts": result.attempts,
                "failure": result.output,
            })
            self.coder.cur_messages += [
                {
                    "role": "user",
                    "content": f"Abandoned refactor: {reason}. "
                    "Restored the exact pre-refactor files and verified the suite is green. "
                    "The attempted edits are no longer on disk. Choose a different next move.",
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

    def next_step(self, intention, completed, output, refactored=False):
        self.coder.io.tool_output("TDD: deciding the next move.")
        files = "\n\n".join(
            f"File: {name}\n{content}"
            for name, content in self.coder.get_abs_fnames_content()
        )
        response = self.coder.main_model.simple_send_with_retries(
            [
                {
                    "role": "system",
                    "content": (
                        "Decide the next step after a passing TDD cycle. Review the original "
                        "intention, actual code, tests, experiment findings and test output. "
                        "Inspect production code for nongeneric pieces: branches tied to example "
                        "inputs, hardcoded answers, duplication or incomplete rules. Treat these "
                        "as suspicious evidence to investigate, not automatic proof of missing "
                        "behavior. Guards, validation, base cases and legitimate domain-specific "
                        "rules are often nongeneric and may be correct. Explain their role "
                        "against the intention before deciding to change them. "
                        "Choose next only for a concrete missing behavior, explaining from the "
                        "code why its example should fail. If unsure, choose experiment with "
                        "a specific question and concrete input/output example to resolve it. "
                        "A passing experiment resolves that uncertainty and is evidence toward "
                        "done; do not keep adding examples merely because possibilities remain. "
                        "Choose done when code and evidence satisfy the original intention, "
                        "explaining why relevant rules and guards are legitimate. Do not invent "
                        "requirements for unspecified inputs. Choose clarify if a missing "
                        "requirement matters. Refactor only to remove an identified smell, "
                        "preserving the public API and behavior. Keep test examples and replace "
                        "duplicated production answers with a rule where appropriate. At most "
                        "one refactor is allowed per green cycle. Review abandoned refactors: "
                        "their changes were rolled back. Do not immediately repeat the same "
                        "failed refactor; choose a different next move. A general requirement "
                        "needs a general rule, not just a hardcoded example answer. "
                        "An experiment may pass after generalization and becomes coverage. "
                        "Do not repeat already covered examples or force a passing test to fail. "
                        "Return only one JSON object, without markdown or edits: "
                        '{"action":"next","behavior":"one concrete example",'
                        '"reason":"code evidence of missing behavior"}, '
                        '{"action":"experiment","question":"specific uncertainty",'
                        '"behavior":"one concrete input/output example"}, '
                        '{"action":"refactor","reason":"specific duplication to remove"}, '
                        '{"action":"done","reason":"why the full intention is satisfied"}, '
                        'or {"action":"clarify","question":"one focused question"}.'
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Original intention: {intention}\nCompleted cycles: {completed}\n"
                        f"Already refactored this cycle: {refactored}\n"
                        f"Refactor history: {json.dumps(self.refactor_history)}\n"
                        f"Already-green coverage examples: {json.dumps(self.coverage_history)}\n"
                        f"Experiment findings: {json.dumps(self.experiment_history)}\n"
                        f"Latest passing test output:\n{output}\nCurrent files:\n{files}"
                    ),
                },
            ]
        )
        try:
            decision = json.loads(response or "")
        except (ValueError, TypeError):
            decision = None
        if not isinstance(decision, dict):
            self.coder.io.tool_error("TDD: could not read the next-step decision; stopping.")
            return {}
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
