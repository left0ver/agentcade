import pytest

from robot_agents.config import LLMSettings


@pytest.mark.parametrize("provider", ["claude_code", "codex"])
def test_local_provider_requires_no_api_credentials(provider):
    settings = LLMSettings(provider=provider)
    assert settings.api_key == ""
    assert settings.model_name == ""


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        LLMSettings(provider="unknown")


@pytest.mark.parametrize("timeout", [0, -1])
def test_nonpositive_timeout_is_rejected(timeout):
    with pytest.raises(ValueError, match="LLM_TIMEOUT_SECONDS"):
        LLMSettings(provider="codex", timeout_seconds=timeout)


@pytest.mark.parametrize("value", [None, [], "", 1, True])
def test_extra_body_requires_an_object(value):
    with pytest.raises(TypeError, match="JSON 对象"):
        LLMSettings(provider="codex", extra_body=value)


@pytest.mark.parametrize("key", ["model", "messages", "stream", "response_format"])
def test_extra_body_cannot_override_shared_model_or_output_format(key):
    with pytest.raises(ValueError, match="不能覆盖"):
        LLMSettings(extra_body={key: None})


def test_local_provider_rejects_http_request_parameters():
    with pytest.raises(ValueError, match="仅用于 OpenAI"):
        LLMSettings(provider="codex", extra_body={"temperature": 0.5})


@pytest.mark.parametrize("url", [
    "", "file:///model", "https://", "https://user:password@example.com/v1",
    "https://example.com/v1?key=value", "https://example.com/v1#fragment",
])
def test_invalid_api_urls_are_rejected(url):
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        LLMSettings(base_url=url)


@pytest.mark.parametrize("url", ["https://api.deepseek.com", "http://localhost:8000/v1"])
def test_valid_api_urls_still_require_key_and_model(url):
    with pytest.raises(ValueError, match="LLM_API_KEY 和 LLM_MODEL_NAME"):
        LLMSettings(base_url=url)
