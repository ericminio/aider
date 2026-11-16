---
parent: Usage
nav_order: 900
description: Automatically fix linting and testing errors.
---

# Linting and testing

Aider can automatically lint and test your code
every time it makes changes.
This helps identify and repair any problems introduced
by the AI edits.

## Linting

Aider comes with built in linters for 
[most popular languages](/docs/languages.html)
and will automatically lint code in these languages.

Or you can specify your favorite linter
with the `--lint-cmd <cmd>` switch.
The lint command should accept the filenames
of the files to lint. 
If there are linting errors, aider expects the
command to print them on stdout/stderr
and return a non-zero exit code.
This is how most linters normally operate.

By default, aider will lint any files which it edits.
You can disable this with the `--no-auto-lint` switch.

### Per-language linters

To specify different linters based on the code language, use `--lint "language: cmd"`.

### Code formatting "linters"

Many people use code formatters as linters, to format and pretty their code.
These tools sometimes return non-zero exit codes if they make changes, which will
confuse aider into thinking there's an actual lint error that needs to be fixed.

You can use formatters by wrapping them in a shell script like this and setting
the script as your linter.

```bash
#!/bin/bash

# Run it twice.
#
# First attempt may reformat/modify files, and therefore exit with non-zero status.
#
# Second attempt will not do anything and exit 0 unless there's a real problem beyond
# the code formatting that was completed.

pre-commit run --files "$@" >/dev/null \
    || pre-commit run --files "$@"
```

## Testing

You can run tests with `/test <test-command>`.
Aider will run the test command without any arguments.
If there are test errors, aider expects the
command to print them on stdout/stderr
and return a non-zero exit code.

Aider will try and fix any errors
if the command returns a non-zero exit code.

You can configure aider to run your test suite
after each time the AI edits your code
using the `--test-cmd <test-command>` and
`--auto-test` switch.



## Experimental TDD cycle

This fork provides `/tdd <intention>` to perform up to ten red/green cycles.
Start aider with the project's test command and relevant files in context:

```bash
aider-dev --read package.json --read src/__tests__/ready.test.js --test-cmd "npm test"
```

Then give it an intention:

```text
/tdd Create primeFactors(n) returning its prime decomposition, such as "9 = 3*3"
```

Override the cycle budget for a run with `/tdd --cycles 5 <intention>`.
The budget limits red/green cycles; the model may stop earlier with completion.

The controller checks that the baseline passes, asks the model for exactly one
failing test as JSON, validates the proposal, applies the edit and runs the suite.
The proposal has a `tests` collection with exactly one item (`path`, `imports`,
and `code` containing the new test only), plus optional new production `stubs`.
The controller adds any missing import lines and appends that one test; the model
does not replace the existing test file. A rejected collection is sent back to the
model for correction, with up to three proposals and no writes for rejected ones.
Absolute paths that resolve inside the repository are normalized to relative
paths automatically. A path that repeats the absolute repository path without its
leading slash is rejected for correction, preventing accidental nested `Users/...`
copies. Paths outside the repository and read-only files remain rejected.
Test names should describe the specific case rather than reuse a generic title.
For JavaScript files, static string names in direct `test`/`it` calls (including
common modifiers) are checked against the target file before writing. Duplicate
names trigger a corrected proposal; existing kata files are not renamed.
This checks the collection size; it does not parse test definitions inside `code`.
Node spec/TAP and unittest output counts provide an additional check after execution.
The red gate classifies the real test output locally, without a model call. A
failing proposal requires exactly one failing test and an assertion failure (`ERR_ASSERTION` for
Node, or `AssertionError` and one failure without errors for unittest). Syntax,
import, undefined-name, cancelled-test and unrecognized failures cannot advance
to green. This verifies an assertion failure, not whether the assertion correctly
captures the user's intention. Once accepted, the controller requests the smallest
production change and runs the suite again. Green changes should stay small and
focused, preferring a simple rule over another hardcoded answer when that rule is
a small change. Generality can grow incrementally from the tested examples.

Every `/tdd` model call now requests a JSON object: red proposals and repairs,
green production edits, refactors and repairs, and next-move decisions. Production
edits use `{"files":[{"path":"relative/source-file","content":"complete final content"}]}`.
The controller validates the entire collection before applying it. Duplicate file
paths (including aliases), duplicate JSON keys, test-file edits and read-only or
outside-repository paths are rejected. Each file contains one final version; the
whole-file markdown parser is not used for TDD responses. Next-move decisions
also require the fields for their chosen action. Invalid JSON or an invalid
proposal triggers a correction request, up to three responses, without applying
rejected edits. This uses prompts plus local validation and retries, rather than
provider-enforced structured generation. Ordinary Aider chat keeps its configured
edit format.

If exactly one new test is discovered and the entire suite already passes, the
controller keeps the new test as coverage and skips the green production edit.
This can happen after a refactor generalizes the implementation. It does not
request a repair to force a passing example to fail; it returns to deciding the
next move and includes the already-green example in that decision's context.

When the red gate rejects a test, the controller sends the reason and actual
test output back to the model for a corrected JSON proposal. It replaces only
the current cycle's proposed test, preserving all tests from earlier cycles and
leaving production files unchanged. There are up to three execution attempts
(the initial proposal and two repairs). It proceeds to green only after the
gate accepts an assertion failure. If repairs are exhausted, it stops and leaves
the latest files for inspection. JSON-format retries remain a separate limit.

After each green, the first model request asks what small refactoring can remove
duplication now. It inspects production code and tests, including hardcoded
production answers that duplicate test examples. A `refactor` JSON response
contains a concrete reason and a `changes` collection. Each
change contains a repository-relative `path`, an exact `before` fragment that
occurs once in the current file, and its `after` replacement. Aider validates and
applies these transformations directly; a separate model does not reinterpret
the initial plan. Absent or ambiguous fragments and unchanged plans are rejected
before writing. If no useful refactoring remains, the reviewer returns `complete`
with a reason. `complete` ends the refactoring phase, not the overall intention. Guards, base
cases and algorithm seeds may remain specific when legitimate. Refactoring should
remove one small piece of duplication supported by the current examples, rather
than introduce a complete algorithm for untested cases. A broken suite prompts
repairs to reconsider the size of the step and reduce toward the green baseline
when appropriate.

Test refactoring is disabled. Aider cancels proposals naming conventional test
paths before requesting edits, records the cancellation, and asks for another
production refactoring. Cancelled proposals consume a refactor-budget slot.

After a refactor and its passing test run, the controller asks the same question
again against the updated files. It does not move to behavior selection after
just one refactor. Up to three proposals may be attempted per cycle. At that
limit the controller moves directly to choosing the next behavior or done,
without another refactor-review call. Abandoned refactors also count and are
included in review context so the model can choose a different approach.

After `complete` or the refactor-budget limit, a separate model call will
choose `next` or `done`. It reviews the original intention, current code, tests
and test output, selecting one missing behavior with code evidence or explaining
why the full intention is satisfied. This call cannot request refactoring, and
the refactoring call cannot select another behavior or declare the intention done.
Invalid actions are returned for JSON correction. Already-passing new tests still
become coverage without a production edit and proceed through the same review.
Known prompt placeholder values are rejected locally. Prompts describe the JSON
fields without example filler values. Code assessment and completion remain model
judgments; checking an exact fragment does not prove the change is a good refactor.

Red and refactor share the same bounded propose/test/evaluate/repair loop, with
different acceptance conditions. A refactor that breaks tests gets the failure
output and up to two repair turns. Each repair receives the original green file
contents and all earlier failed attempts in that refactor, including their diffs
against green and corresponding test output. The latest candidate remains visible
as the current files; the prompt asks the model to diagnose the failure rather
than rebuild an earlier unsuccessful candidate. Aider remembers failed editable-file
states (including the tests) for the current `/tdd` run. An identical candidate is
rejected before writing or retesting, including after rollback; the model receives
the previous failure and can correct the proposal within the JSON retry budget.
This checks exact text states, not semantic equivalence. Repairs still return full
production files as JSON. If all three execution attempts fail, the controller
abandons that refactor and restores the exact file state from before its edits,
including any uncommitted changes. Files created by that refactor are removed;
the Git index and unrelated files are left alone. Restoration is verified by a
fresh passing test run before continuing.

The next decision receives a record of the abandoned refactor and its failing
test output alongside the restored code, so it can select another move without
immediately repeating the same rabbit hole. If the restored suite still fails,
the controller stops rather than treating it as green.

The controller prints each selected behavior or refactoring before editing.
Accepted edits show a unified diff for each file, including tests and new stubs.
Unchanged responses are explicitly logged as no changes. Refactor rollback shows
the reverse changes, including removal of newly created files. These diffs also
appear in Aider's chat-history file. Rejected proposals produce no edit diff. It
permits the configured number of red/green cycles and up to three refactors per cycle.
Refactoring does not consume a test cycle and can occur after the final green too.
If more tests are selected after the budget is exhausted, it stops without claiming the
whole intention is complete.

During the cycle, the controller temporarily disables automatic lint/test repair,
commits and suggested shell commands so they do not skip over the red phase. It
restores those settings afterward. Existing read-only files remain read-only;
the red step can create a new test file and a minimal production stub.

Phase boundaries and completion still rely on model instructions and judgment;
assertion-failure classification is deterministic. Green-phase repair, enforced
file boundaries and PR delivery are future work. Other unexpected results stop
the controller and leave the files available for inspection.

Whenever `/tdd` ends, it prints completed and started cycle counts. A cycle is
completed after its green test run passes or a new coverage test already passes;
refactoring does not add a cycle. Coverage-only cycles are also reported separately. A red
step stopped by a setup error counts as started but not completed.

## Compiled languages

If you want to have aider compile code after each edit, you
can use the lint and test commands to achieve this.

- You might want to recompile each file which was modified
to check for compile errors.
To do this,
provide a `--lint-cmd` which both lints and compiles the file.
You could create a small shell script for this.
- You might want to rebuild the entire project after files
are edited to check for build errors.
To do this,
provide a `--test-cmd` which both builds and tests the project.
You could create a small shell script for this.
Or you may be able to do something as simple as
`--test-cmd "dotnet build && dotnet test"`.

## Manually running code

You can use the `/run` command in the chat to run your code
and optionally share the output with aider.
This can be useful to share error messages or to show aider
the code's output before asking for changes or corrections.

<div class="chat-transcript" markdown="1">
> Aider v0.43.5-dev  

#### /run python myscript.py

```
Traceback (most recent call last):  
 File "myscript.py", line 22, in \<module\ 
   raise ValueError("something bad happened")  
ValueError: something bad happened  
```

> Add the output to the chat? y  

</div>


