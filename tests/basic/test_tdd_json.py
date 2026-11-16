from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aider.models import Model


@pytest.mark.parametrize("supported", [True, False])
def test_json_mode_uses_provider_capability_without_affecting_ordinary_chat(supported):
    model = Model("ollama_chat/qwen3-coder:30b")
    model.extra_params = {"num_ctx": 8192}
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])
    with (
        patch(
            "aider.models.litellm.get_supported_openai_params",
            return_value=["response_format"] if supported else [],
        ) as capability,
        patch("aider.models.litellm.completion", return_value=response) as completion,
    ):
        assert model.simple_send_with_retries([], json_mode=True) == "{}"
        if supported:
            assert completion.call_args.kwargs["response_format"] == {"type": "json_object"}
        else:
            assert "response_format" not in completion.call_args.kwargs
        capability.assert_called_once_with(model=model.name)
        model.simple_send_with_retries([])
        assert "response_format" not in completion.call_args.kwargs
        assert model.extra_params == {"num_ctx": 8192}


def test_advertised_but_unsupported_json_mode_falls_back_once():
    from aider.llm import litellm

    model = Model("ollama_chat/qwen3-coder:30b")
    model.extra_params = {"num_ctx": 8192}
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])
    with (
        patch("aider.models.litellm.get_supported_openai_params", return_value=["response_format"]),
        patch(
            "aider.models.litellm.completion",
            side_effect=[
                litellm.UnsupportedParamsError("response_format unsupported", model=model.name),
                response,
            ],
        ) as completion,
    ):
        assert model.simple_send_with_retries([], json_mode=True) == "{}"
        assert completion.call_count == 2
        assert "response_format" in completion.call_args_list[0].kwargs
        assert "response_format" not in completion.call_args_list[1].kwargs
