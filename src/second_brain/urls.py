"""URL normalization and validation.

Dedupe correctness depends on every step normalizing URLs the same way, so
this is the single place that defines it. Existing vault cards are normalized
with the same function when scanned, so changing the rules here applies to old
cards and new links alike.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Share/tracking parameters that never identify the content. Removing them
# makes "the same video shared twice" dedupe to one URL. Identity parameters
# (YouTube ``v``, Instagram carousel ``img_index``) are deliberately absent.
TRACKING_PARAMS = frozenset({
    # Instagram / Meta
    "igsh", "igshid", "fbclid", "mibextid",
    # TikTok
    "is_from_webapp", "sender_device", "sender_web_id", "share_app_id", "share_link_id",
    "share_item_id", "share_author_id", "social_share_type", "tt_from", "u_code",
    "preview_pb", "timestamp", "user_id", "_r", "_t", "_d", "checksum", "sec_uid",
    # Generic ad/click trackers
    "gclid", "dclid", "msclkid", "si", "ref_src", "ref_url",
})


def _is_tracking_param(name: str) -> bool:
    name = name.lower()
    return name in TRACKING_PARAMS or name.startswith("utm_")


def normalize_url(url: str) -> str:
    """Canonical form used for dedupe.

    * trims whitespace;
    * drops tracking query parameters (``utm_*``, ``igsh``, ``is_from_webapp`` ...);
    * lowercases the scheme and host;
    * removes the trailing slash from the path (as the original scripts did).

    Non-http input is only trimmed, so it stays recognizable in logs.

    TODO: ``vm.tiktok.com`` / ``vt.tiktok.com`` short links redirect to the
    canonical ``/@user/video/<id>`` URL; resolving them would need a network
    call, so they are compared as-is for now.
    """
    raw = (url or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw.rstrip("/")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return raw.rstrip("/")

    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not _is_tracking_param(k)]
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/"),
        parsed.params,
        urlencode(kept, doseq=True),
        parsed.fragment,
    ))


def is_valid_http_url(url: str) -> bool:
    """True for absolute http(s) URLs with a host.

    Lines in pending_links.txt come from a phone Share Sheet synced through a
    cloud drive, so treat them as untrusted input. Rejecting anything that is
    not http(s) also guarantees we never hand yt-dlp an option-looking string.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
