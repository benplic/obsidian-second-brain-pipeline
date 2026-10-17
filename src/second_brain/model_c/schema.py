"""Structured Model C response. The model classifies and extracts; routing is separate."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CategoryType = Literal["Music", "Fitness & Health", "Recipes & Food", "Other / Ambiguous"]

MODEL_C_CATEGORIES = ["Music", "Fitness & Health", "Recipes & Food", "Other / Ambiguous"]
MUSIC = "Music"
FITNESS = "Fitness & Health"
RECIPES = "Recipes & Food"
AMBIGUOUS = "Other / Ambiguous"


class ModelCAnalysis(BaseModel):
    """One saved video, shaped so a hub can act on it.

    ``category`` is the model's folder choice. Callers still override that
    when confidence is low or the link is dead; do not treat the field as a path.
    """

    category: CategoryType = Field(description="Music, Fitness & Health, Recipes & Food, or Other / Ambiguous.")
    summary: str = Field(description="One or two sentences describing what to do with this save.")
    actionable_items: list[str] = Field(
        default_factory=list,
        description="Exercises with reps, ingredients and steps, or artist and track lines.",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence from 0.0 to 1.0.")
    needs_manual_review: bool = Field(description="True when the clip is too ambiguous to file.")
    artist: str | None = Field(default=None, description="Performer, when this is music.")
    sound: str | None = Field(default=None, description="Track or sound name, when this is music.")
