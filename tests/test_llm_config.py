"""LLM config synthesis and validation."""

from __future__ import annotations

import pytest

from second_brain.config import ConfigError, settings_from_dict


def test_synthesizes_llm_from_gemini_and_crusher(tmp_path):
    settings = settings_from_dict(
        {
            "vault_path": "v",
            "gemini": {"model": "gemini-3.8-flash"},
            "crusher": {"api_key_env_vars": ["GEMINI_API_KEY"], "thinking_level": "LOW"},
        },
        base_dir=tmp_path,
    )
    assert settings.llm.provider == "gemini"
    assert settings.llm.model == settings.crusher.model
    assert settings.llm.api_key_env_vars == ("GEMINI_API_KEY",)


def test_llm_api_key_field_rejected(tmp_path):
    with pytest.raises(ConfigError, match="Unknown key"):
        settings_from_dict({"vault_path": "v", "llm": {"provider": "gemini", "api_key": "secret"}}, base_dir=tmp_path)


def test_explicit_llm_openai_provider(tmp_path):
    settings = settings_from_dict(
        {
            "vault_path": "v",
            "llm": {
                "provider": "openai",
                "model": "meta-llama/llama-3.1-8b-instruct",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env_vars": ["OPENROUTER_API_KEY"],
                "supports_video_upload": False,
            },
        },
        base_dir=tmp_path,
    )
    assert settings.llm.provider == "openai"
    assert settings.llm.supports_video_upload is False
