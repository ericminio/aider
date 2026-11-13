import copy
import json

from aider.run_cmd import run_cmd


class TDDController:
    """Run up to two explicit red/green cycles using Aider's existing editor."""

    max_cycles = 2

    def __init__(self, coder):
        self.coder = coder

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
                if cycle == self.max_cycles:
                    coder.io.tool_output(
                        f"TDD: stopped at the {self.max_cycles}-cycle limit. "
                        "The full intention may still need more work."
                    )
                    return

                decision = self.next_step(intention, completed, output)
                action = decision.get("action")
                if action == "done":
                    coder.io.tool_output(f"TDD: done. {decision.get('reason', '')}")
                    return
                if action == "clarify":
                    question = decision.get("question", "")
                    coder.io.tool_output(f"TDD: clarification needed. {question}")
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
        coder.io.tool_output("TDD: red — adding one test.")
        coder.run(
            f"TDD RED phase. Original intention: {intention}\n"
            f"Selected behavior for this cycle: {behavior}\n"
            "Add exactly one test for this next smallest behavior. Preserve all existing "
            "tests and previously passing behavior. Follow the existing project test "
            "conventions, including the test directory and discovery command. "
            "You are authorized to create new test and production files. If no relevant "
            "implementation exists, choose sensible filenames and create the new test "
            "and its imported stub directly using your normal file-edit format. "
            "Do not ask the user to create or add nonexistent files to the chat. "
            "Read-only reference files must stay unchanged; they do not prevent you "
            "from creating new files. Do not implement the behavior. If needed, add only "
            "a minimal production stub so the test fails on an assertion rather than a "
            "missing import or undefined function. Output each file exactly once. "
            "Stop after this red step; the controller will run the tests.",
            preproc=False,
        )
        red_response = coder.partial_response_content
        status, failure = self.run_tests()
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
                        "environment errors do not qualify. Otherwise explain the problem. "
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

    def next_step(self, intention, completed, output):
        self.coder.io.tool_output("TDD: deciding whether another behavior is needed.")
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
                        "intention, actual code, tests and test output. A single passing example "
                        "does not satisfy a request for general behavior. Select exactly one "
                        "uncovered concrete input/output example that should currently fail; "
                        "do not select an already covered or already implemented behavior. "
                        "Return only one JSON object, without markdown or edits: "
                        '{"action":"next","behavior":"one concrete example"}, '
                        '{"action":"done","reason":"why the full intention is satisfied"}, '
                        'or {"action":"clarify","question":"one focused question"}.'
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Original intention: {intention}\nCompleted cycles: {completed}\n"
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
        return run_cmd(
            self.coder.test_cmd,
            cwd=self.coder.root,
            error_print=self.coder.io.tool_error,
        )
