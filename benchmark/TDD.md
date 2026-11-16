# Live TDD evaluation

Run from the repository root using the development environment, with Node and Git
installed and the selected model available:

```sh
python -m benchmark.tdd --model ollama_chat/qwen3-coder:30b --runs 3 --output /tmp/tdd-evaluation
```

The model receives the same controller prompts and JSON transport as `/tdd`.
Each trial creates a disposable Git repository. The generated project has no access
to your working files through Aider's edit interface. The model's generated tests
and production code execute locally, just as they do during an ordinary `/tdd` run.
Use a model and execution environment you trust.

Four scenarios distinguish judgment quality from controller correctness:

* **hardcoded-answer:** ask for the next move when only 9 is implemented and tested;
  expect another example.
* **coverage-gap:** ask for the next move with a general implementation but only 9
  tested; expect useful coverage even though the example might already pass.
* **no-duplication:** review a straightforward general implementation; expect the
  refactor phase to complete rather than invent duplicated calculations.
* **end-to-end:** start with only a passing readiness test and run up to ten TDD
  cycles. Check the generated implementation independently against eleven inputs,
  including 1, primes, powers, mixed factors and a larger composite. A passing
  trial requires both a completion decision and a passing independent oracle.

`report.json` retains every model response, decisions, rejection counts, refactor
attempts, completion claims and oracle results. Each scenario also has a terminal
log. Static literal inputs found in generated tests are reported as `tested_inputs`;
this is an observation, not a claim to parse all dynamic or parameterized tests.
The generated test command has a 30-second timeout and the independent oracle a
10-second timeout. A failed scenario exits nonzero; all trials still run and the
report is saved after each trial. Choose one scenario with `--scenario NAME`.

These evaluations are deliberately separate from CI: they require a real model
and measure nondeterministic decisions. CI runs deterministic contract tests and
verifies that the independent oracle catches overfitting and nontermination.
Compare repeated runs for the same model and revision; a single passing run is
not a reliability estimate. Keep the report and logs together when sharing results.
