"""Single choke point for every yt-dlp subprocess the crusher runs.

Why one runner: 2,000 URLs against TikTok/Instagram get throttled (HTTP 429)
or soft-banned when calls are unpaced. Routing every call through here gives
one process-wide rate limiter, one retry policy, and one place that decides
whether a failure is permanent (never retry) or transient (retry next run).
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from ..config import CrusherSettings

logger = logging.getLogger(__name__)

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# Substrings (lowercase) in yt-dlp stderr. Order matters: permanent is checked
# first so "private video ... HTTP Error 403" is not retried forever.
_PERMANENT_MARKERS = (
    "video unavailable",
    "private video",
    "this video is private",
    "has been removed",
    "was deleted",
    "account is private",
    "not available in your country",
    "geo restricted",
    "geo-restricted",
    "unsupported url",
    "404: not found",
    "http error 404",
    "requested content is not available",
    "login required",
    "requires login",
    "log in to",
    "sign in to confirm your age",
)
_TRANSIENT_MARKERS = (
    "http error 429",
    "too many requests",
    "rate-limit",
    "rate limit",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "temporary failure in name resolution",
    "remote end closed connection",
    "unable to download webpage",
)
_COOKIE_LOCK_MARKERS = ("cookie database", "could not copy chrome cookie")


class YtDlpError(RuntimeError):
    """Base class for classified yt-dlp failures."""


class PermanentMediaError(YtDlpError):
    """Deleted, private, geo-blocked, login-required, or unsupported. Do not retry."""


class TransientFetchError(YtDlpError):
    """429 / 5xx / timeout / network. Safe to retry later."""


def classify_stderr(stderr: str) -> type[YtDlpError] | None:
    """Map yt-dlp stderr to an error class, or None when nothing recognizable failed."""
    text = (stderr or "").lower()
    if any(marker in text for marker in _PERMANENT_MARKERS):
        return PermanentMediaError
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return TransientFetchError
    return None


def validate_url(url: str) -> str:
    """Reject anything that is not an http(s) URL before it reaches a subprocess.

    Arguments are passed as a list (no shell) and after ``--``, so injection is
    already blocked; this additionally stops ``file://`` and similar schemes
    that yt-dlp would happily open.
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PermanentMediaError(f"Refusing non-http(s) URL: {url!r}")
    return url


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


class YtDlpRunner:
    """Rate-limited, retrying yt-dlp wrapper.

    ``clock``/``sleep``/``runner`` are injectable so tests never spawn processes
    or actually wait.
    """

    def __init__(
        self,
        settings: CrusherSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        rng: Callable[[float, float], float] = random.uniform,
    ):
        self.settings = settings
        self._clock = clock
        self._sleep = sleep
        self._runner = runner
        self._rng = rng
        self._lock = threading.Lock()
        self._last_call: float | None = None
        self._cookies_disabled = False
        self.calls = 0

    # -- pacing -----------------------------------------------------------

    def _wait_for_slot(self) -> None:
        """Enforce ``ytdlp_min_interval_seconds`` (+ jitter) between process starts."""
        with self._lock:
            interval = max(0.0, float(self.settings.ytdlp_min_interval_seconds))
            jitter_max = max(0.0, float(self.settings.ytdlp_jitter_seconds))
            target = interval + (self._rng(0.0, jitter_max) if jitter_max else 0.0)
            if self._last_call is not None:
                elapsed = self._clock() - self._last_call
                if elapsed < target:
                    self._sleep(target - elapsed)
            self._last_call = self._clock()

    # -- command building -------------------------------------------------

    def _base_args(self) -> list[str]:
        args = [
            "yt-dlp",
            "--no-warnings",
            "--retries",
            str(self.settings.ytdlp_retries),
            "--extractor-retries",
            str(self.settings.ytdlp_retries),
            "--socket-timeout",
            str(int(self.settings.ytdlp_socket_timeout_seconds)),
        ]
        if self.settings.ytdlp_sleep_requests_seconds:
            args += ["--sleep-requests", str(self.settings.ytdlp_sleep_requests_seconds)]
        if self.settings.cookies_from_browser and not self._cookies_disabled:
            args += ["--cookies-from-browser", self.settings.cookies_from_browser]
        return args

    def _exec(self, argv: list[str], timeout: float) -> RunResult:
        self._wait_for_slot()
        self.calls += 1
        logger.debug("yt-dlp: %s", " ".join(argv[1:]))
        try:
            proc = self._runner(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=_CREATION_FLAGS,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(returncode=-1, stdout="", stderr=f"timed out after {exc.timeout}s")
        except FileNotFoundError as exc:
            # Not transient: yt-dlp is not installed. Surface loudly.
            raise RuntimeError("yt-dlp is not on PATH. Run: pip install -e .") from exc
        except OSError as exc:
            return RunResult(returncode=-1, stdout="", stderr=f"connection aborted: {exc}")
        return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    # -- public -----------------------------------------------------------

    def run(self, extra_args: list[str], url: str, *, timeout: float | None = None) -> RunResult:
        """Run yt-dlp with retries. Raises Permanent/TransientFetchError on classified failure.

        A non-zero exit with unrecognized stderr is returned as-is so callers
        can decide (e.g. an optional subtitle fetch failing is not fatal).
        """
        validate_url(url)
        timeout = timeout or self.settings.ytdlp_timeout_seconds
        attempts = max(1, int(self.settings.ytdlp_max_attempts))
        last: RunResult | None = None
        for attempt in range(1, attempts + 1):
            argv = self._base_args() + list(extra_args) + ["--", url]
            last = self._exec(argv, timeout)
            stderr_lower = last.stderr.lower()
            if (
                last.returncode != 0
                and not self._cookies_disabled
                and any(m in stderr_lower for m in _COOKIE_LOCK_MARKERS)
            ):
                # yt-dlp issue #7271: Edge/Chrome hold the cookie DB lock while open.
                self._cookies_disabled = True
                logger.warning(
                    "Browser cookies could not be read (cookie database locked). Continuing without "
                    "cookies. Close the browser completely and re-run for Instagram carousels."
                )
                last = self._exec(self._base_args() + list(extra_args) + ["--", url], timeout)
                stderr_lower = last.stderr.lower()
            if last.returncode == 0:
                return last
            kind = classify_stderr(last.stderr)
            if kind is PermanentMediaError:
                raise PermanentMediaError(_tail(last.stderr))
            if kind is TransientFetchError and attempt < attempts:
                delay = self.settings.ytdlp_backoff_base_seconds * (2 ** (attempt - 1))
                delay += self._rng(0.0, max(0.0, float(self.settings.ytdlp_jitter_seconds)))
                logger.warning(
                    "yt-dlp transient failure (attempt %d/%d), retrying in %.0fs: %s",
                    attempt,
                    attempts,
                    delay,
                    _tail(last.stderr, 160),
                )
                self._sleep(delay)
                continue
            if kind is TransientFetchError:
                raise TransientFetchError(_tail(last.stderr))
            return last
        assert last is not None  # pragma: no cover - loop always runs once
        return last

    def info_json(self, url: str) -> dict | None:
        """One ``--dump-single-json`` call. Returns None when yt-dlp printed no JSON."""
        result = self.run(["--dump-single-json", "--skip-download", "--ignore-errors"], url)
        return parse_last_json_line(result.stdout)

    def download(self, url: str, *, fmt: str, output_template: str, max_filesize_mb: float) -> RunResult:
        """Download exactly one format selection. No format fallback, by design."""
        return self.run(
            [
                "-f",
                fmt,
                "--no-playlist",
                "--max-filesize",
                f"{int(max_filesize_mb)}M",
                "-o",
                output_template,
            ],
            url,
        )


def parse_last_json_line(stdout: str) -> dict | None:
    """Instagram carousels log per-slide errors, then emit playlist JSON last."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text
