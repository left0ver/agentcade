import os

import pytest

from games.guess_number.config import Settings as NumberSettings
from robot_agents.config import LLMSettings, Settings, read_environment


@pytest.mark.parametrize("timeout", [0, -1])
def test_nonpositive_timeout_is_rejected(timeout):
    with pytest.raises(ValueError, match="LLM_TIMEOUT_SECONDS"):
        LLMSettings(timeout_seconds=timeout)


@pytest.mark.parametrize("value", [None, [], "", 1, True])
def test_extra_body_requires_an_object(value):
    with pytest.raises(TypeError, match="JSON 对象"):
        LLMSettings(extra_body=value)


@pytest.mark.parametrize("key", ["model", "messages", "stream", "response_format",
                                 "tools", "tool_choice", "parallel_tool_calls"])
def test_extra_body_cannot_override_shared_model_or_output_format(key):
    with pytest.raises(ValueError, match="不能覆盖"):
        LLMSettings(extra_body={key: None})


@pytest.mark.parametrize("url", [
    "", "file:///model", "https://", "https://user:password@example.com/v1",
    "https://example.com/v1?key=value", "https://example.com/v1#fragment",
])
def test_invalid_api_urls_are_rejected(url):
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        LLMSettings(base_url=url)


@pytest.mark.parametrize("url", ["https://api.deepseek.com", "http://localhost:8000/v1"])
@pytest.mark.parametrize("api_key, model_name", [
    ("", "model"), ("key", ""), ("   ", "model"), ("key", "   "),
])
def test_valid_api_urls_require_key_and_model(url, api_key, model_name):
    with pytest.raises(ValueError, match="LLM_API_KEY 和 LLM_MODEL_NAME"):
        LLMSettings(base_url=url, api_key=api_key, model_name=model_name)


def test_agent_files_are_independent_and_do_not_change_environment(run_directory):
    original = dict(os.environ)
    first = run_directory / "first.env"
    second = run_directory / "second.env"
    credentials = "LLM_BASE_URL=https://api.deepseek.com\nLLM_API_KEY=config-validation\n"
    first.write_text(credentials + "LLM_MODEL_NAME=first-model\nLLM_TIMEOUT_SECONDS=15\n")
    second.write_text(credentials + "LLM_MODEL_NAME=second-model\nLLM_TIMEOUT_SECONDS=45\n")
    one = NumberSettings.from_environment(first)
    two = NumberSettings.from_environment(second)
    assert (one.llm.model_name, one.llm.timeout_seconds) == ("first-model", 15)
    assert (two.llm.model_name, two.llm.timeout_seconds) == ("second-model", 45)
    second.write_text(credentials + "LLM_MODEL_NAME=updated-model\nLLM_TIMEOUT_SECONDS=90\n")
    assert NumberSettings.from_environment(first) == one
    assert NumberSettings.from_environment(second).llm.timeout_seconds == 90
    assert dict(os.environ) == original


def test_missing_agent_file_fails_without_loading_other_configuration(run_directory):
    with pytest.raises(FileNotFoundError):
        NumberSettings.from_environment(run_directory / "missing.env")


def test_environment_file_values_do_not_expand_process_variables(run_directory):
    path = run_directory / "literal.env"
    path.write_text("GAME_CATALOG='${HOME}/games.json'\n")
    assert read_environment(path)["GAME_CATALOG"] == "${HOME}/games.json"


def test_main_agent_configuration_uses_independent_llm_and_speech(run_directory):
    path = run_directory / "main.env"
    path.write_text(
        "DASHSCOPE_API_KEY=config-validation\n"
        "DASHSCOPE_BASE_URL=https://workspace.example.com/compatible-mode/v1\n"
        "GAME_CATALOG=games.json\n"
        "LLM_API_KEY=main-config-validation\n"
        "LLM_BASE_URL=https://api.deepseek.com\nLLM_MODEL_NAME=deepseek-flash\n"
    )
    settings = Settings.from_environment(path)
    assert settings.asr_model == "qwen-audio-3.1-asr-flash-streaming"
    assert settings.tts_model == "qwen-audio-3.1-tts-flash"
    assert settings.tts_voice == "yuxiaoyun_v3.1"
    assert settings.websocket_url == (
        "wss://workspace.example.com/api-ws/v1/inference"
    )
    assert settings.game_catalog == run_directory / "games.json"
    assert settings.llm.api_key == "main-config-validation"
    assert settings.llm.base_url == "https://api.deepseek.com"
    assert settings.llm.model_name == "deepseek-flash"
    game_path = run_directory / "game.env"
    game_path.write_text(
        "LLM_BASE_URL=https://api.deepseek.com\nLLM_API_KEY=game-config-validation\n"
        "LLM_MODEL_NAME=game-model\n"
    )
    game = NumberSettings.from_environment(game_path)
    assert game.llm.api_key == "game-config-validation"
    assert game.llm.model_name == "game-model"
    assert Settings.from_environment(path) == settings


def test_main_agent_requires_its_own_llm_configuration(run_directory):
    path = run_directory / "main.env"
    path.write_text("DASHSCOPE_API_KEY=validation\nTYPESAFE_API_KEY=validation\n")
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        Settings.from_environment(path)


@pytest.mark.parametrize("parameters", [
    {"asr_silence_ms": 100}, {"speech_timeout_seconds": 0},
    {"input_gain": 0}, {"input_gain": 9}, {"tts_voice": ""}, {"asr_model": ""},
])
def test_main_agent_rejects_invalid_speech_parameters(parameters):
    llm = LLMSettings(api_key="config-validation", base_url="https://api.deepseek.com",
                      model_name="deepseek-flash")
    with pytest.raises(ValueError):
        Settings(dashscope_api_key="config-validation", llm=llm, **parameters)
