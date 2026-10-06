"""Named LLM presets for setup wizard (not used on the hot path)."""

from __future__ import annotations

from dataclasses import dataclass

from .types import Capabilities


@dataclass(frozen=True)
class LlmPreset:
    name: str
    description: str
    provider: str
    model: str
    capabilities: Capabilities
    thinking_level: str | None = None


PRESETS: dict[str, LlmPreset] = {
    "crusher-budget": LlmPreset(
        name="crusher-budget",
        description="Native Gemini Flash — free tier friendly, vision + audio + schema.",
        provider="gemini",
        model="gemini-3.6-flash",
        capabilities=Capabilities(
            supports_vision=True,
            supports_audio=True,
            supports_video_upload=True,
            structured_output="schema",
        ),
        thinking_level="LOW",
    ),
    "crusher-quality": LlmPreset(
        name="crusher-quality",
        description="Native Gemini Flash (newer) — higher quality crush summaries.",
        provider="gemini",
        model="gemini-3.8-flash",
        capabilities=Capabilities(
            supports_vision=True,
            supports_audio=True,
            supports_video_upload=True,
            structured_output="schema",
        ),
        thinking_level="LOW",
    ),
}
