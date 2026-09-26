"""Second Brain: saved short-form videos (TikTok, Instagram) -> Obsidian cards.

Pipeline (see README for the diagram):
    Step 1  extract / parse-instagram  -> clean_metadata.json
    Step 2  categorize (Gemini)        -> organized_tiktoks.csv
    Step 3  write-cards                -> <vault>/3 - Resources/<folder>/*.md
"""

__version__ = "0.1.0"
