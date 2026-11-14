from collections import namedtuple


PhaseResult = namedtuple("PhaseResult", "accepted output attempts error status")


class PhaseLoop:
    """Shared propose, execute, evaluate and repair loop; callers own recovery policy."""

    def __init__(self, io, name, max_attempts):
        self.io = io
        self.name = name
        self.max_attempts = max_attempts

    def run(self, propose, run_tests, evaluate):
        feedback = None
        output = ""
        status = None
        for attempt in range(1, self.max_attempts + 1):
            if propose(feedback) is False:
                return PhaseResult(False, output, attempt - 1, "proposal was not approved", status)
            status, output = run_tests()
            error = evaluate(status, output)
            if error is None:
                return PhaseResult(True, output, attempt, None, status)
            if attempt < self.max_attempts:
                self.io.tool_output(
                    f"TDD: {self.name} rejected — {error}. "
                    f"Requesting a repair ({attempt + 1}/{self.max_attempts})."
                )
                feedback = f"{error}\nActual test output:\n{output}"
        return PhaseResult(False, output, self.max_attempts, error, status)
