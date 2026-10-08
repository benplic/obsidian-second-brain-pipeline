"""Structured Gemini output for the crusher."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..taxonomy import DEFAULT_FOLDER_MAP

# Categories the model may return; unknown values are routed to Inbox at write time.
CategoryName = Literal[tuple(DEFAULT_FOLDER_MAP.keys())]  # type: ignore[valid-type]

MediaShape = Literal[
    "standard_video",
    "slideshow_video",
    "carousel_images",
    "photo_slideshow",
    "silent_video",
    "music_only",
    "speech_heavy",
    "text_on_screen",
    "unavailable",
]

ItemKind = Literal[
    "destination",
    "album",
    "song",
    "movie",
    "recipe",
    "product",
    "tool",
    "workout",
    "health_tip",
    "study_tip",
    "tip",
    "other",
]

ItemSource = Literal["speech", "on_screen_text", "caption", "visual", "subtitle"]


class EvidenceFlags(BaseModel):
    had_speech: bool = False
    had_on_screen_text: bool = False
    had_music: bool = False
    slides_seen: int = 0


class ExtractedItem(BaseModel):
    kind: ItemKind = "other"
    name: str = Field(description="Primary label (place, album, song, etc.).")
    artist: str | None = None
    location_name: str | None = None
    country: str | None = None
    position: int | None = Field(default=None, description="Slide index or list rank when known.")
    source: ItemSource = "visual"
    notes: str | None = None


class CrusherAnalysis(BaseModel):
    media_shape: MediaShape = "standard_video"
    category: str = Field(description="Best-fit taxonomy category name.")
    secondary_categories: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    title: str = Field(description="Short actionable title for the card.")
    summary: str = Field(description="One or two sentence summary in English.")
    findings: list[str] = Field(default_factory=list, description="Markdown bullet strings.")
    language: str | None = None
    evidence: EvidenceFlags = Field(default_factory=EvidenceFlags)
    list_expected_count: int | None = Field(
        default=None,
        description="When the video claims N items (e.g. top 10), the expected count.",
    )
    items: list[ExtractedItem] = Field(default_factory=list)
    recategorize: bool = Field(
        default=True,
        description="False when current category is clearly correct despite sparse metadata.",
    )
    completeness_notes: str | None = None
    needs_visuals: bool = Field(
        default=False,
        description="True when key content (list items, places, text) likely appears only on screen "
        "and was not in the transcript or caption you were given.",
    )
    relevance: float | None = Field(default=None, description="Set by the classifier; leave null.")
    content_tags: list[str] = Field(default_factory=list, description="Set by the classifier; leave empty.")
