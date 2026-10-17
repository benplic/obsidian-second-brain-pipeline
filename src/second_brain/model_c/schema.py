"""Structured Model C extraction. Classification is not this model's job."""

from __future__ import annotations

from pydantic import BaseModel, Field

# Taxonomy keys the dispatcher sends to this transform.
MUSIC = "Saved Music"
FITNESS = "Fitness & Health"
RECIPES = "Recipes & Food"
MODEL_C_CATEGORIES = (MUSIC, FITNESS, RECIPES)


class ModelCExtraction(BaseModel):
    """Usable material for a category that was already chosen."""

    summary: str = Field(description="One or two sentences describing what to do with this save.")
    actionable_items: list[str] = Field(
        default_factory=list,
        description="Exercises with reps, ingredients and steps, or artist and track lines.",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the extraction, from 0.0 to 1.0.")
    needs_manual_review: bool = Field(description="True when the clip is too thin to use.")
    artist: str | None = Field(default=None, description="Performer, when this is music.")
    sound: str | None = Field(default=None, description="Track or sound name, when this is music.")
