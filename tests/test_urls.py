"""URL normalization: tracking parameters must not defeat dedupe."""

from __future__ import annotations

import pytest

from second_brain.urls import is_valid_http_url, normalize_url

TIKTOK = "https://www.tiktok.com/@sample.creator/video/7000000000000000001"
INSTAGRAM = "https://www.instagram.com/reel/SAMPLE00001"


@pytest.mark.parametrize("shared", [
    TIKTOK,
    TIKTOK + "/",
    TIKTOK + "?is_from_webapp=1&sender_device=pc",
    TIKTOK + "?is_from_webapp=1&sender_device=mobile&sender_web_id=123&_r=1&_t=8abc",
    TIKTOK + "?_t=ZT-8abc&_r=1&share_app_id=1233&u_code=xyz&tt_from=copy",
    "  https://WWW.TikTok.com/@sample.creator/video/7000000000000000001?utm_source=copy&utm_medium=ios  ",
])
def test_tiktok_share_variants_collapse(shared):
    assert normalize_url(shared) == TIKTOK


@pytest.mark.parametrize("shared", [
    INSTAGRAM,
    INSTAGRAM + "/",
    INSTAGRAM + "/?igsh=MWQ1ZGUxMzBkMA==",
    INSTAGRAM + "/?igshid=NTc4MTIwNjQ2YQ%3D%3D",
    INSTAGRAM + "/?utm_source=ig_web_copy_link&igsh=abc",
    INSTAGRAM + "?fbclid=IwAR0abc&mibextid=Zxz2cZ",
])
def test_instagram_share_variants_collapse(shared):
    assert normalize_url(shared) == INSTAGRAM


def test_identity_params_are_kept_and_order_preserved():
    assert normalize_url("https://www.youtube.com/watch?v=abc123&si=track") == "https://www.youtube.com/watch?v=abc123"
    assert normalize_url("https://www.instagram.com/p/X/?img_index=2&igsh=t") == "https://www.instagram.com/p/X?img_index=2"
    assert normalize_url("https://e.com/a?b=2&a=1&utm_x=0") == "https://e.com/a?b=2&a=1"


def test_different_videos_stay_different():
    assert normalize_url(TIKTOK + "?_r=1") != normalize_url(TIKTOK[:-1] + "2?_r=1")


def test_non_http_input_is_only_trimmed():
    assert normalize_url("  not a url/ ") == "not a url"
    assert normalize_url("") == ""


def test_vault_card_with_tracking_params_dedupes_against_clean_link(settings):
    from conftest import make_card
    from second_brain.vault import get_vault_urls

    make_card(settings.resources_dir, "Comedy", "a", INSTAGRAM + "/?igsh=abc")
    assert normalize_url(INSTAGRAM + "?utm_source=ig_web_copy_link") in get_vault_urls(settings.resources_dir)


def test_valid_http_url():
    assert is_valid_http_url(TIKTOK)
    assert not is_valid_http_url("ftp://x.y/z")
    assert not is_valid_http_url("-o evil")
