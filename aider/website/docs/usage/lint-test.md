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

This fork provides `/tdd <intention>` to perform up to two red/green cycles.
Start aider with the project's test command and relevant files in context:

```bash
aider-dev --read package.json --read src/__tests__/ready.test.js --test-cmd "npm test"
```

Then give it an intention:

```text
/tdd Create primeFactors(n) returning its prime decomposition, such as "9 = 3*3"
```

The controller checks that the baseline passes, asks the model for exactly one
failing test, applies the edit and runs the suite. A separate model request reviews
the real failure output. If the model judges it to be the intended assertion
failure, the controller requests the smallest production change and runs the suite
again. Each editing response should contain only one version of each file.

After the first green, another model request reviews the original intention,
current editable-file contents, tests and passing test output. It either selects
one new concrete behavior, explains why the intention is complete, or asks a
focused clarification question. The controller prints each cycle's selected
behavior before requesting the next test. A second cycle starts only after the
first suite passes. After two cycles it stops at the experiment's limit, without
claiming the whole intention is complete.

During the cycle, the controller temporarily disables automatic lint/test repair,
commits and suggested shell commands so they do not skip over the red phase. It
restores those settings afterward. Existing read-only files remain read-only;
the red step can create a new test file and a minimal production stub.

Phase boundaries, completion and failure assessment rely on model instructions
and judgment. Automatic recovery, enforced file boundaries, longer runs,
refactoring and PR delivery are future work. On an
unexpected result, it stops and leaves the files available for inspection.

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


