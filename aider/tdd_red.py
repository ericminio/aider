import ast
import json
from pathlib import Path

from grep_ast.tsl import get_parser

from aider.coders.wholefile_coder import WholeFileCoder


class RedProposal:
    """Validate a collection containing one new test before writing any files."""

    max_attempts = 3

    def __init__(self, coder):
        self.coder = coder
        self.test_path = None
        self.original_test_content = None

    def run(self, intention, behavior, feedback=None):
        coder = self.coder
        files = dict(coder.get_abs_fnames_content())
        for name in sorted(coder.abs_read_only_fnames):
            files[f"{name} (read-only)"] = coder.io.read_text(name)
        messages = [
            {
                "role": "system",
                "content": (
                    "TDD RED proposal. Return only a JSON object, without markdown. "
                    "Use this structure: "
                    '{"tests":[{"path":"relative/test-file", "imports":["import line"], '
                    '"code":"one new test definition"}], '
                    '"stubs":[{"path":"relative/source-file", "content":"minimal stub"}]}. '
                    "The tests collection must contain exactly one item. Each item represents "
                    "one test, not a whole test file or suite. Put only the new test definition "
                    "in code; never repeat existing tests. Give the test a unique descriptive "
                    "name in its target file, "
                    "including its specific input and expected behavior rather than repeating "
                    "a generic title. Imports is a list of required import "
                    "lines; existing imports will be retained. The tool appends the new test "
                    "to its file. The test MUST exercise the actual production function. "
                    "Include its import from the production module in imports when absent. "
                    "Never define, shadow, mock or reimplement the function under test inside "
                    "the test code. Put any missing implementation stub in a separate production "
                    "file in stubs and import that exported function. Follow the visible test "
                    "framework, directory and discovery "
                    "command. Preserve the exact expected output from the user's intention. "
                    "Create new test files directly when needed. Read-only files cannot be "
                    "edited. Stubs may create only NEW production files; use an empty list if "
                    "the implementation exists. A stub must make the test fail on its assertion, "
                    "not on an import or undefined name. Do not implement the behavior, add "
                    "extra examples, or run commands. Do not add comments to generated code, "
                    "including tests, stubs and repairs. JSON string values must escape newlines."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Original intention: {intention}\nSelected behavior: {behavior}\n"
                    f"Test command: {coder.test_cmd}\nCurrent files:\n"
                    + "\n\n".join(f"{name}\n{content}" for name, content in files.items())
                ),
            },
        ]
        if feedback:
            messages[-1]["content"] += (
                f"\nThe red gate rejected the current test:\n{feedback}\n"
                f"Repair the SAME test in {self.test_path}. Return the one-test JSON proposal "
                "again, with ALL imports needed by this test. The tool replaces your previous "
                "proposal; it will not append another test. Preserve the intended behavior and "
                "expected result. Fix test setup, imports or syntax so the real production "
                "function is exercised and fails an assertion. Do not weaken the assertion or "
                "implement production behavior. Return stubs: []; "
                "production files must stay unchanged."
            )
        for attempt in range(1, self.max_attempts + 1):
            coder.io.tool_output(f"TDD: red JSON proposal {attempt}/{self.max_attempts}.")
            response = coder.main_model.simple_send_with_retries(messages)
            try:
                edits = self.get_edits(response)
            except ValueError as err:
                coder.io.tool_output(f"TDD: rejected red proposal before writing files: {err}")
                messages += [
                    {"role": "assistant", "content": response or ""},
                    {
                        "role": "user",
                        "content": (
                            f"Rejected: {err}. Return a corrected JSON proposal with exactly "
                            "one tests item. No files have been written. "
                            "Keep only the next smallest test."
                        ),
                    },
                ]
                continue

            # All collection and path validation happens before normal Aider edit approval.
            coder.io.tool_output("TDD: accepted a proposal containing one new test.")
            test_path = edits[0][0]
            full_path = Path(coder.root) / test_path
            original = coder.io.read_text(str(full_path)) if full_path.exists() else ""
            approved = coder.prepare_to_edit(edits)
            if len(approved) != len(edits):
                coder.io.tool_error("TDD: red proposal was not fully approved; stopping.")
                return False
            WholeFileCoder.apply_edits(coder, approved)
            if self.test_path is None:
                self.test_path = test_path
                self.original_test_content = original
            for path, _, _ in approved:
                coder.io.tool_output(f"Applied edit to {path}")
                coder.aider_edited_files.add(path)
            coder.partial_response_content = response
            coder.cur_messages += [
                {"role": "user", "content": f"TDD red: {behavior}"},
                {"role": "assistant", "content": "Added exactly one test and any needed new stub."},
            ]
            return True
        coder.io.tool_error("TDD: no valid single-test proposal after three attempts.")
        return False

    def get_edits(self, response):
        try:
            proposal = json.loads(response or "")
        except (ValueError, TypeError) as err:
            raise ValueError("Return valid JSON") from err
        if not isinstance(proposal, dict) or not isinstance(proposal.get("tests"), list):
            raise ValueError("Provide a tests collection")
        if len(proposal["tests"]) != 1:
            raise ValueError(f"Received {len(proposal['tests'])} tests; exactly one is required")
        test = proposal["tests"][0]
        if not isinstance(test, dict):
            raise ValueError("The test must be an object with path, imports and code")
        path, full_path = self.validate_path(test.get("path"))
        if self.test_path is not None and path != self.test_path:
            raise ValueError(
                f"Repair the existing proposal in {self.test_path}; do not create a new test"
            )
        imports = test.get("imports", [])
        code = test.get("code")
        if not isinstance(imports, list) or any(not isinstance(line, str) for line in imports):
            raise ValueError("imports must be a list of strings")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must contain the single new test definition")
        if self.test_path is not None:
            existing = self.original_test_content
        else:
            existing = self.coder.io.read_text(str(full_path)) if full_path.exists() else ""
        if existing is None:
            raise ValueError(f"Cannot read {path}")
        self.validate_unique_names(path, existing, code)
        missing = list(dict.fromkeys(line for line in imports if line not in existing.splitlines()))
        prefix = "\n".join(missing)
        if prefix:
            prefix += "\n"
        content = prefix + existing
        if content:
            content = content.rstrip("\n") + "\n\n"
        content += code.rstrip("\n") + "\n"
        edits = [(path, "block", content.splitlines(keepends=True))]
        stubs = proposal.get("stubs", [])
        if not isinstance(stubs, list):
            raise ValueError("stubs must be a list")
        if self.test_path is not None and stubs:
            raise ValueError("A red repair may change only the proposed test; return stubs: []")
        paths = {full_path}
        for stub in stubs:
            if not isinstance(stub, dict):
                raise ValueError("Each stub must have path and content")
            path, full_path = self.validate_path(stub.get("path"))
            content = stub.get("content")
            if full_path in paths or full_path.exists():
                raise ValueError(f"Stub {path} must be a new, distinct file")
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"Stub {path} needs content")
            paths.add(full_path)
            edits.append((path, "block", (content.rstrip("\n") + "\n").splitlines(True)))
        return edits

    def validate_unique_names(self, path, existing, code):
        if Path(path).suffix not in (".js", ".mjs", ".cjs", ".jsx"):
            return
        seen = set(self.node_test_names(existing))
        for name in self.node_test_names(code):
            if name in seen:
                raise ValueError(
                    f"Duplicate test name {name!r} in {path}; choose a unique case-specific name"
                )
            seen.add(name)

    def node_test_names(self, code):
        root = get_parser("javascript").parse(code.encode("utf-8")).root_node
        pending = [root]
        names = []
        functions = {"test", "it", "test.only", "test.skip", "test.todo", "it.only", "it.skip"}
        while pending:
            node = pending.pop()
            pending.extend(reversed(node.named_children))
            if node.type != "call_expression":
                continue
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if function is None or function.text.decode("utf-8") not in functions:
                continue
            if arguments is None or not arguments.named_children:
                continue
            title = arguments.named_children[0]
            if title.type == "string":
                try:
                    names.append(ast.literal_eval(title.text.decode("utf-8")))
                except (ValueError, SyntaxError):
                    continue
            elif title.type == "template_string" and not any(
                child.type == "template_substitution" for child in title.named_children
            ):
                names.append(title.text.decode("utf-8")[1:-1])
        return names

    def validate_path(self, path):
        if not isinstance(path, str) or not path.strip():
            raise ValueError("File paths must be non-empty strings")
        root = Path(self.coder.root).resolve()
        full_path = (root / path).resolve()
        if not full_path.is_relative_to(root) or full_path == root:
            raise ValueError("File paths must stay within the repository")
        if full_path in {Path(name).resolve() for name in self.coder.abs_read_only_fnames}:
            raise ValueError(f"{path} is read-only")
        return str(full_path.relative_to(root)), full_path
