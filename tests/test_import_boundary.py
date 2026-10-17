"""Hot-path modules must not import setup_ai."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOT_PATHS = [
    REPO / "src/second_brain/crusher/run.py",
    REPO / "src/second_brain/crusher/analyze.py",
    REPO / "src/second_brain/steps/categorize.py",
    REPO / "src/second_brain/model_a/ingest.py",
    REPO / "src/second_brain/model_a/enrich.py",
]


def test_setup_ai_not_imported_on_hot_path():
    for path in HOT_PATHS:
        text = path.read_text(encoding="utf-8")
        assert "setup_ai" not in text, f"{path.name} must not import setup_ai"
