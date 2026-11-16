import json

PLACEHOLDERS = {
    "one concrete input/output example",
    "one concrete example",
    "duplication present now and the small change that removes it",
    "specific duplication and how to remove it",
    "specific duplication to remove",
    "evidence in the current code",
    "code evidence of missing behavior",
    "how the code satisfies the full intention",
    "why the full intention is satisfied",
    "no useful duplication removal remains",
    "why no useful refactoring remains",
    "relative/production-file",
    "relative/source-file",
    "relative/test-file",
}


def reject_placeholders(value):
    if isinstance(value, dict):
        for field, item in value.items():
            if field in ("reason", "behavior", "path") and isinstance(item, str):
                if item.strip().casefold() in PLACEHOLDERS:
                    raise ValueError(
                        f"{field} contains a prompt placeholder; supply a concrete value"
                    )
            reject_placeholders(item)
    elif isinstance(value, list):
        for item in value:
            reject_placeholders(item)


def parse_object(response):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(response or "", object_pairs_hook=unique_keys)
    except (TypeError, ValueError) as err:
        raise ValueError(f"Return one valid JSON object: {err}") from err
    if not isinstance(value, dict):
        raise ValueError("Return one JSON object")
    reject_placeholders(value)
    return value


def request_json(coder, messages, validate, label, max_attempts=3, log_response=False):
    messages = [dict(message) for message in messages]
    messages[0]["content"] += " Return only JSON, without markdown fences or surrounding prose."
    for attempt in range(1, max_attempts + 1):
        coder.io.tool_output(f"TDD: {label} JSON response {attempt}/{max_attempts}.")
        response = coder.main_model.simple_send_with_retries(messages, json_mode=True)
        if log_response:
            coder.io.tool_output(
                f"TDD: {label} proposal {attempt}/{max_attempts}:\n"
                + str(response if response else "<empty response>")
            )
        try:
            return validate(parse_object(response))
        except ValueError as err:
            coder.io.tool_output(f"TDD: rejected {label} before applying changes: {err}")
            reassess = (
                " Reassess whether a useful refactor exists; choose complete if no grounded "
                "improvement remains, rather than invent evidence for the rejected idea."
                if label == "refactoring"
                else ""
            )
            messages += [
                {"role": "assistant", "content": response or ""},
                {
                    "role": "user",
                    "content": (
                        f"Rejected: {err}. Return a corrected JSON object. "
                        "No changes from this rejected response were applied."
                        + reassess
                    ),
                },
            ]
    coder.io.tool_error(f"TDD: no valid {label} JSON after {max_attempts} attempts.")
    return None


def file_context(coder):
    files = dict(coder.get_abs_fnames_content())
    for name in sorted(coder.abs_read_only_fnames):
        files[f"{name} (read-only)"] = coder.io.read_text(name)
    return "\n\n".join(f"File: {name}\n{content}" for name, content in files.items())
