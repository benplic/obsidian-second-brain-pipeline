"""Interactive LLM setup and health checks (not imported by the crush/categorize hot path)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import yaml

from .config import ConfigError, find_config_path, load_settings
from .llm.factory import build_runtime
from .llm.presets import PRESETS
from .llm.types import CompletionRequest, ImageInput, UnsupportedFeatureError

logger = logging.getLogger(__name__)

# Tiny 1x1 PNG for vision probe.
_PROBE_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05"
    b"\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _prompt(line: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{line}{suffix}: ").strip()
    return value or (default or "")


def _write_env_var(env_path: Path, var_name: str, secret: str) -> None:
    lines: list[str] = []
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    updated = False
    out: list[str] = []
    for line in lines:
        if line.startswith(f"{var_name}="):
            out.append(f"{var_name}={secret}")
            updated = True
        else:
            out.append(line)
    if not updated:
        out.append(f"{var_name}={secret}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _merge_llm_block(config_path: Path, llm_block: dict) -> None:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must be a YAML mapping.")
    raw["llm"] = llm_block
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False, default_flow_style=False), encoding="utf-8")


def run_setup(config_path: Path | None = None) -> int:
    """Wizard: provider, preset/model, env var, test call, write config."""
    path = find_config_path(config_path)
    if not path.is_file():
        print(f"Config not found: {path}. Copy config.example.yaml first.", file=sys.stderr)
        return 2

    print("Choose provider: 1) Gemini (free tier friendly)  2) OpenAI-compatible (base_url + model)")
    choice = _prompt("Enter 1 or 2", "1")
    provider = "gemini" if choice != "2" else "openai"

    preset_name = _prompt("Preset (crusher-budget, crusher-quality) or 'custom'", "crusher-budget")
    llm_block: dict = {"provider": provider}
    if preset_name in PRESETS:
        preset = PRESETS[preset_name]
        llm_block.update(
            {
                "model": preset.model,
                "thinking_level": preset.thinking_level,
                "supports_vision": preset.capabilities.supports_vision,
                "supports_audio": preset.capabilities.supports_audio,
                "supports_video_upload": preset.capabilities.supports_video_upload,
                "structured_output": preset.capabilities.structured_output,
            }
        )
        print(f"Using preset {preset_name}: {preset.description}")
    else:
        llm_block["model"] = _prompt("Model id")

    if provider == "openai":
        llm_block["base_url"] = _prompt("Base URL", "https://api.openai.com/v1")
        llm_block["supports_video_upload"] = False

    default_var = "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"
    env_var = _prompt("Environment variable name for the API key", default_var)
    llm_block["api_key_env_vars"] = [env_var]

    if not os.environ.get(env_var):
        if _prompt(f"{env_var} is not set. Paste key now? (y/N)", "n").lower() == "y":
            secret = _prompt("API key (not stored in config.yaml)")
            env_path = path.parent / ".env"
            _write_env_var(env_path, env_var, secret)
            os.environ[env_var] = secret
            print(f"Wrote {env_var} to {env_path} (gitignored).")
        else:
            print(f"Set {env_var} in your environment or .env, then run: second-brain doctor")

    print("Running test call...")
    code = run_doctor(config_path=path, quiet=True)
    if code != 0:
        print("Test call failed; config was NOT updated.", file=sys.stderr)
        return code

    _merge_llm_block(path, llm_block)
    print(f"Updated llm section in {path}")
    print("Tip: add GEMINI_API_KEY_2, GEMINI_API_KEY_3, ... for extra free-tier quotas.")
    return 0


def run_doctor(config_path: Path | None = None, *, quiet: bool = False) -> int:
    """Verify key, text JSON, and optional vision."""
    try:
        settings = load_settings(config_path)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    runtime = build_runtime(settings)
    keys = runtime.key_pool.available_keys()
    if not keys:
        print("No API keys found for: " + ", ".join(settings.llm.api_key_env_vars), file=sys.stderr)
        return 2
    if not quiet:
        print(f"Provider: {settings.llm.provider} ({runtime.adapter.provider_label})")
        print(f"Model: {settings.llm.model}")
        print(f"Using key env: {keys[0]}")

    text_req = CompletionRequest(
        prompt='Reply with JSON only: {"ok": true}',
        response_schema=None,
    )
    try:
        result = runtime.complete(text_req)
    except Exception as exc:
        print(f"Text call failed: {exc}", file=sys.stderr)
        return 1
    if not quiet:
        print(f"Text call OK ({result.input_tokens}+{result.output_tokens} tokens).")

    if settings.llm.supports_vision:
        img_req = CompletionRequest(
            prompt="Describe this image in one word.",
            images=[ImageInput(data=_PROBE_PNG, mime_type="image/png")],
        )
        try:
            runtime.complete(img_req)
            if not quiet:
                print("Vision probe OK.")
        except UnsupportedFeatureError as exc:
            print(f"Vision not available: {exc}", file=sys.stderr)
            print("Crush will still work with text + captions; keyframe escalation may fail.", file=sys.stderr)
        except Exception as exc:
            print(f"Vision probe failed: {exc}", file=sys.stderr)
            return 1
    elif not quiet:
        print("supports_vision is false; skipping image probe.")

    if not settings.llm.supports_vision and not quiet:
        print("Warning: model may not accept images; crush relies on frames for list/slide content.")

    return 0
