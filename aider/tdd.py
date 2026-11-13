import copy
import json
import re

from aider.run_cmd import run_cmd
from aider.tdd_red import RedProposal


class TDDController:
    """Run a bounded sequence of red/green cycles using Aider's existing editor."""

    max_cycles = 10

    def __init__(self, coder, max_cycles=None):
        self.coder = coder
        self.test_count = None
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
        try:
            # Prompt instances are shared by coders. Override only this session's copy.
            coder.gpt_prompts = copy.copy(original_prompts)
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
            completed = []
            for cycle in range(1, self.max_cycles + 1):
                coder.io.tool_output(f"TDD: cycle {cycle}/{self.max_cycles} — {behavior}")
                output = self.run_cycle(intention, behavior)
                if output is None:
                    return
                completed.append(behavior)
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
            coder.gpt_prompts = original_prompts
            for name, value in settings.items():
                setattr(coder, name, value)

    def run_cycle(self, intention, behavior):
        coder = self.coder
        previous_test_count = self.test_count
        coder.io.tool_output("TDD: red — adding one test.")
        if not RedProposal(coder).run(intention, behavior):
            return
        red_response = coder.partial_response_content
        status, failure = self.run_tests()
        if previous_test_count is not None and self.test_count is not None:
            added = self.test_count - previous_test_count
            if added != 1:
                coder.io.tool_error(
                    f"TDD: red added {added} tests ({previous_test_count} → {self.test_count}); "
                    "exactly one new test is required. Review the test edits before continuing."
                )
                return
        if status == 0:
            coder.io.tool_error("TDD: the new test must fail before the green phase.")
            return

        coder.io.tool_output("TDD: reviewing the reason for the failure.")
        verdict = coder.main_model.simple_send_with_retries(
            [
                {
                    "role": "system",
                    "content": (
                        "Review a TDD red step. Reply exactly TDD_RED_OK only if the test "
                        "output shows an assertion failing because the intended behavior "
                        "is missing. Syntax errors, missing imports, undefined names and "
                        "environment errors do not qualify. Check that the assertion exercises "
                        "the actual production function imported from its module. A local "
                        "definition or mock that shadows the function under test is not a valid "
                        "red step, even if the assertion fails. Otherwise explain the problem. "
                        "Do not propose or output edits."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Original intention: {intention}\nSelected behavior: {behavior}\n"
                        f"Red step:\n{red_response}\nActual test output:\n{failure}"
                    ),
                },
            ]
        )
        if (verdict or "").strip() != "TDD_RED_OK":
            coder.io.tool_error(f"TDD: red needs review. {verdict or 'No verdict returned.'}")
            return

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
        coder.io.tool_output("TDD: green. One cycle complete.")
        return output or ""

    def run_refactor(self, intention, reason):
        self.coder.io.tool_output(f"TDD: refactor — {reason}")
        self.coder.run(
            f"TDD REFACTOR phase. Original intention: {intention}\n"
            f"Identified duplication or code smell: {reason}\n"
            "The suite is green. Remove this duplication with the smallest appropriate "
            "production-code refactoring. Repeated hardcoded answers in production duplicate "
            "the examples in tests: replace them with a rule where appropriate. Generalizing "
            "to remove duplication is allowed in this phase. Keep the concrete examples in "
            "the tests. Do not modify tests or project configuration. Preserve the public API "
            "and existing observable behavior; do not add unrelated behavior or abstractions. "
            "Output each changed file exactly once. Stop after this refactor; the controller "
            "will run the tests.",
            preproc=False,
        )
        status, output = self.run_tests()
        if status != 0:
            self.coder.io.tool_error("TDD: refactor left the suite red; review before continuing.")
            return
        self.coder.io.tool_output("TDD: refactor complete; suite remains green.")
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
                        "intention, actual code, tests and test output. First consider removing "
                        "duplication: hardcoded answers in production may duplicate concrete "
                        "examples in tests. Keep test examples and replace production answers "
                        "with a rule when appropriate. Refactor only for an identified smell, "
                        "preserving the public API and behavior. At most one refactor is "
                        "allowed per green cycle. A single passing example "
                        "does not satisfy a request for general behavior. Select exactly one "
                        "uncovered concrete input/output example that should currently fail; "
                        "do not select an already covered or already implemented behavior. "
                        "Return only one JSON object, without markdown or edits: "
                        '{"action":"next","behavior":"one concrete example"}, '
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
