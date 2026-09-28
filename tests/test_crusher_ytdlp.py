"""yt-dlp runner: pacing, retries, error classification (no processes spawned)."""

from __future__ import annotations

import subprocess

import pytest

from second_brain.config import CrusherSettings
from second_brain.crusher.ytdlp import (
    PermanentMediaError,
    TransientFetchError,
    YtDlpRunner,
    classify_stderr,
    parse_last_json_line,
    validate_url,
)

URL = "https://www.tiktok.com/@u/video/1"


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def scripted(results):
    """Fake subprocess.run returning (returncode, stdout, stderr) tuples in order."""
    calls: list[list[str]] = []
    queue = list(results)

    def run(argv, **_kwargs):
        calls.append(argv)
        code, out, err = queue.pop(0)
        return subprocess.CompletedProcess(argv, code, out, err)

    return run, calls


def make_runner(results, **settings_kwargs):
    fc = FakeClock()
    run, calls = scripted(results)
    settings = CrusherSettings(
        ytdlp_min_interval_seconds=settings_kwargs.pop("interval", 0.0),
        ytdlp_jitter_seconds=0.0,
        ytdlp_backoff_base_seconds=1.0,
        **settings_kwargs,
    )
    runner = YtDlpRunner(settings, clock=fc.clock, sleep=fc.sleep, runner=run, rng=lambda a, b: 0.0)
    return runner, calls, fc


def test_transient_429_retries_then_succeeds():
    runner, calls, fc = make_runner(
        [(1, "", "ERROR: HTTP Error 429: Too Many Requests"), (0, '{"title": "ok"}', "")]
    )
    assert runner.info_json(URL) == {"title": "ok"}
    assert len(calls) == 2
    assert fc.sleeps == [1.0]  # backoff base * 2^0


def test_transient_exhausted_raises_transient():
    runner, _calls, fc = make_runner([(1, "", "HTTP Error 503")] * 3, ytdlp_max_attempts=3)
    with pytest.raises(TransientFetchError):
        runner.info_json(URL)
    assert fc.sleeps == [1.0, 2.0]  # exponential


def test_permanent_error_not_retried():
    runner, calls, _ = make_runner([(1, "", "ERROR: [TikTok] 1: Video unavailable")])
    with pytest.raises(PermanentMediaError):
        runner.info_json(URL)
    assert len(calls) == 1


def test_private_beats_transient_marker():
    assert classify_stderr("This video is private. HTTP Error 429") is PermanentMediaError


def test_unrecognized_failure_is_returned_not_raised():
    runner, _calls, _ = make_runner([(1, "", "some odd warning")])
    result = runner.run(["--skip-download"], URL)
    assert result.returncode == 1


def test_rate_limiter_enforces_min_interval():
    runner, _calls, fc = make_runner([(0, "{}", ""), (0, "{}", "")], interval=5.0)
    runner.run([], URL)
    fc.now += 1.0  # 1s later: must wait 4 more
    runner.run([], URL)
    assert fc.sleeps == [pytest.approx(4.0)]


def test_cookie_lock_falls_back_without_cookies():
    runner, calls, _ = make_runner(
        [(1, "", "ERROR: Could not copy Chrome cookie database"), (0, '{"title": "x"}', "")],
        cookies_from_browser="edge",
    )
    assert runner.info_json(URL) == {"title": "x"}
    assert "--cookies-from-browser" in calls[0]
    assert "--cookies-from-browser" not in calls[1]


def test_url_always_after_double_dash():
    runner, calls, _ = make_runner([(0, "{}", "")])
    runner.run(["--skip-download"], URL)
    assert calls[0][-2:] == ["--", URL]


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "", "javascript:alert(1)", "https://"])
def test_validate_url_rejects_non_http(bad):
    with pytest.raises(PermanentMediaError):
        validate_url(bad)


def test_parse_last_json_line_skips_noise():
    out = "[instagram] slide 1 failed\n{\"_type\": \"playlist\"}\n"
    assert parse_last_json_line(out) == {"_type": "playlist"}
    assert parse_last_json_line("no json here") is None
