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


# Labels written by an earlier Model C before categories were taxonomy keys.
CATEGORY_ALIASES: dict[str, str] = {
    "Music": "Saved Music",
}


def canonical_category(category: str) -> str:
    """Map a legacy label onto a taxonomy key. Unknown names are unchanged."""
    text = (category or "").strip()
    return CATEGORY_ALIASES.get(text, text)


def folder_for(category: str, folder_map: dict[str, str]) -> str:
    """Resolve a (possibly hallucinated) Gemini category to a vault folder.

    Unknown categories land in the Inbox rather than creating a new folder
    from model output, which also prevents path tricks in folder names.
    ``Music`` is the legacy name for ``Saved Music`` and stays in the Music folder.
    """
    key = canonical_category(category)
    return folder_map.get(key, DEFAULT_FOLDER)


def category_tag(category: str) -> str:
    """Obsidian-legal tag. ``&`` becomes ``and`` and ``/`` becomes ``-``.

    ``Recipes & Food`` -> ``category/recipes-and-food``.
    ``Art/Organization/Spaces`` -> ``category/art-organization-spaces``.
    ``second-brain migrate-cards`` rewrites slugs that used the old rules.
    """
    text = canonical_category(category).lower().replace("&", " and ").replace("/", " ")
    slug = "-".join(part for part in text.replace("-", " ").split() if part)
    return f"category/{slug or 'uncategorized'}"
