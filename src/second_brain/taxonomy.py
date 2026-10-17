"""Gemini category -> vault folder mapping (P.A.R.A. "3 - Resources").

Order matters: it is the order categories are listed in the Gemini prompt, and
changing the prompt can change classifications.
"""

from __future__ import annotations

DEFAULT_CATEGORY = "Miscellaneous"
DEFAULT_FOLDER = "Inbox"

DEFAULT_FOLDER_MAP: dict[str, str] = {
    "Saved Music": "Music",
    "Movies & Shows": "Movies & Shows",
    "Study Tips": "Study Tips",
    "Tips": "Tips",
    "Project Ideas": "Project Ideas",
    "Fitness & Health": "Fitness & Health",
    "Recipes & Food": "Recipes & Food",
    "Tech & Coding": "Tech & Coding",
    "Art/Organization/Spaces": "Art & Spaces",
    "Relationships/Dating": "Relationships",
    "Comedy/Funny": "Comedy",
    "Sports": "Sports",
    "Travel": "Travel",
    "Miscellaneous": "Inbox",
}

# Disambiguation for categories that are easily confused (Step 2, crusher, Jev).
CATEGORY_HINTS: dict[str, str] = {
    "Study Tips": "Study methods, exams, note-taking, and learning techniques (not general life hacks).",
    "Tips": "Crafts, DIY, life hacks, and practical how-tos (not study, health, food, or software).",
    "Fitness & Health": "Workouts plus health, sleep, wellness, and nutrition advice (not full recipes).",
    "Art/Organization/Spaces": "Room layout, decor, organization, and visual art spaces.",
}


def format_category_hints(category_names: list[str]) -> str:
    """Compact guidance block for categorization prompts (token-conscious)."""
    lines = [f"- {name}: {CATEGORY_HINTS[name]}" for name in category_names if name in CATEGORY_HINTS]
    if not lines:
        return ""
    return "Category guidance:\n" + "\n".join(lines) + "\n\n"


def jev_category_criteria(taxonomy: list[str]) -> dict[str, str]:
    """Jev choice keys are exact category names; values are hint text when defined."""
    return {name: CATEGORY_HINTS.get(name, name) for name in taxonomy}


def folder_for(category: str, folder_map: dict[str, str]) -> str:
    """Resolve a (possibly hallucinated) Gemini category to a vault folder.

    Unknown categories land in the Inbox rather than creating a new folder
    from model output, which also prevents path tricks in folder names.
    """
    return folder_map.get(category, DEFAULT_FOLDER)


def category_tag(category: str) -> str:
    """Tag slug exactly as the original scripts produced it.

    TODO: This yields tags like ``category/movies-&-shows`` and nested
    ``category/art/organization/spaces``. Obsidian tags do not allow ``&``, so
    some of these do not render as tags. Kept for compatibility with existing
    cards and Dataview queries; changing it needs a one-time vault migration.
    """
    return f"category/{category.lower().replace(' ', '-')}"
