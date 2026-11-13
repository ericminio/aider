from aider.run_cmd import run_cmd


class TDDController:
    """Run one explicit red/green cycle using Aider's existing editor."""

    def __init__(self, coder):
        self.coder = coder

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
        try:
            for name in settings:
                setattr(coder, name, False)

            coder.io.tool_output("TDD: checking the green baseline.")
            status, _ = self.run_tests()
            if status != 0:
                coder.io.tool_error("TDD: start with a passing test suite.")
                return

            coder.io.tool_output("TDD: red — adding one test.")
            coder.run(
                "TDD RED phase. Intention: " + intention + "\n"
                "Add exactly one test for the next smallest behavior. Follow the existing "
                "project test conventions, including the test directory and discovery command. "
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
                            f"Intention: {intention}\nRed step:\n{red_response}\n"
                            f"Actual test output:\n{failure}"
                        ),
                    },
                ]
            )
            if (verdict or "").strip() != "TDD_RED_OK":
                coder.io.tool_error(f"TDD: red needs review. {verdict or 'No verdict returned.'}")
                return

            coder.io.tool_output("TDD: green — implementing the smallest change.")
            coder.run(
                f"TDD GREEN phase. Intention: {intention}\n"
                f"Actual failing test output:\n{failure}\n"
                "Make the smallest production-code change that passes this test. "
                "Do not modify tests or project configuration. Do not generalize for untested "
                "cases. Output each changed file exactly once. Stop after this green step; "
                "the controller will run the tests.",
                preproc=False,
            )
            status, _ = self.run_tests()
            if status == 0:
                coder.io.tool_output("TDD: green. One cycle complete.")
            else:
                coder.io.tool_error("TDD: suite is still red; review the output before continuing.")
        finally:
            for name, value in settings.items():
                setattr(coder, name, value)

    def run_tests(self):
        return run_cmd(
            self.coder.test_cmd,
            cwd=self.coder.root,
            error_print=self.coder.io.tool_error,
        )
