"""Vault-as-registry: URL detection from card frontmatter."""

from __future__ import annotations

from second_brain.vault import get_vault_urls, parse_frontmatter, sanitize_filename, unique_card_path, yaml_str
from second_brain.steps.write_cards import render_card

from conftest import make_card


def test_detects_quoted_and_unquoted_urls_and_normalizes_trailing_slash(settings):
    res = settings.resources_dir
    make_card(res, "Comedy", "a", "https://t.co/v/1/")
    make_card(res, "Inbox/Manual Review", "b", "https://t.co/v/2", quoted=False)
    assert get_vault_urls(res) == {"https://t.co/v/1", "https://t.co/v/2"}


def test_ignores_url_lines_outside_frontmatter_and_non_markdown(settings):
    res = settings.resources_dir
    (res / "Comedy").mkdir()
    (res / "Comedy" / "note.md").write_text('# Note\n\nurl: "https://t.co/body"\n', encoding="utf-8")
    (res / "Comedy" / "data.txt").write_text('---\nurl: "https://t.co/txt"\n---\n', encoding="utf-8")
    assert get_vault_urls(res) == set()


def test_broken_yaml_falls_back_to_regex():
    content = '---\ncategory: "unterminated\nurl: "https://t.co/v/9"\nstatus: tossed\n---\nbody'
    fm = parse_frontmatter(content)
    assert fm["url"] == "https://t.co/v/9"
    assert fm["status"] == "tossed"


def test_missing_resources_dir_is_empty(tmp_path):
    assert get_vault_urls(tmp_path / "nope") == set()


def test_unreadable_note_is_skipped_not_fatal(settings):
    res = settings.resources_dir
    make_card(res, "Comedy", "ok", "https://t.co/ok")
    (res / "Comedy" / "binary.md").write_bytes(b"\xff\xfe\x00bad")
    assert get_vault_urls(res) == {"https://t.co/ok"}


def test_rendered_card_escapes_quotes_and_round_trips():
    row = {"Category": "Comedy/Funny", "Summary": "s", "Creator": 'The "Real" One\\', "URL": "https://t.co/q", "Title": "T", "Tags": ""}
    _, _, content = render_card(row)
    fm = parse_frontmatter(content)
    assert fm["creator"] == 'The "Real" One\\'
    assert fm["url"] == "https://t.co/q"
    assert fm["tags"] == ["saved-media", "category/comedy/funny"]


def test_sanitize_filename_edge_cases():
    assert sanitize_filename('a/b\\c:d*e?f"g<h>i|j') == "abcdefghij"
    assert sanitize_filename("   ") == "Saved Video"
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("trailing dots...") == "trailing dots"
    assert len(sanitize_filename("x" * 200)) == 60


def test_unique_card_path_never_overwrites(tmp_path):
    (tmp_path / "Same.md").write_text("x")
    (tmp_path / "Same (2).md").write_text("x")
    assert unique_card_path(tmp_path, "Same").name == "Same (3).md"


def test_yaml_str_plain_value_matches_original_format():
    assert yaml_str("Tech & Coding") == '"Tech & Coding"'
