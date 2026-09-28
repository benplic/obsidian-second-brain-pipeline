"""Tiered acquisition + visual gate + transcript parsing (all mocked, no network)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from second_brain.config import CrusherSettings
from second_brain.crusher import acquire as acq
from second_brain.crusher.acquire import (
    AUDIO_FORMAT,
    TIER_AUDIO,
    TIER_CAPTIONS,
    AcquiredMedia,
    acquire_text,
    evenly_sample,
    json3_to_text,
    vtt_to_text,
)
from second_brain.crusher.gate import decide_visuals, has_list_claim
from second_brain.crusher.probe import ProbeResult
from second_brain.crusher.schema import CrusherAnalysis, ExtractedItem
from second_brain.crusher.transcribe import TranscriptResult, WhisperTranscriber, build_result
from second_brain.crusher.ytdlp import PermanentMediaError

URL = "https://www.tiktok.com/@u/video/42"
LONG_CAPTIONS = " ".join(["word"] * 60)


class FakeRunner:
    def __init__(self, info: dict | None, *, write_audio: bool = True):
        self.info = info
        self.write_audio = write_audio
        self.info_calls = 0
        self.downloads: list[str] = []

    def info_json(self, url):
        self.info_calls += 1
        return self.info

    def download(self, url, *, fmt, output_template, max_filesize_mb):
        self.downloads.append(fmt)
        if self.write_audio:
            Path(output_template.replace("%(ext)s", "m4a")).write_bytes(b"\x00audio")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def run(self, *args, **kwargs):  # pragma: no cover - not used in these tests
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class FakeTranscriber:
    def __init__(self, text="hello there this is speech", ratio=0.8, installed=True):
        self.text, self.ratio, self.installed = text, ratio, installed
        self.calls = 0

    def available(self):
        return self.installed

    def transcribe(self, path):
        self.calls += 1
        return TranscriptResult(text=self.text, language="en", speech_ratio=self.ratio, duration_seconds=30)


class MemCache:
    def __init__(self):
        self.data: dict[tuple[str, str], dict] = {}

    def load_stage(self, url, stage):
        return self.data.get((url, stage))

    def save_stage(self, url, stage, payload):
        self.data[(url, stage)] = payload


@pytest.fixture
def cs():
    return CrusherSettings(min_transcript_words=25, min_speech_ratio=0.2)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(acq, "resolve_short_url", lambda url: url)
    monkeypatch.setattr(acq, "probe_file", lambda path: ProbeResult(duration_seconds=30, has_audio=True, has_video=True))


def _info(**extra):
    base = {"title": "T", "description": "D", "uploader": "u", "duration": 30}
    base.update(extra)
    return base


def test_captions_long_enough_skip_audio_and_video(cs, monkeypatch):
    monkeypatch.setattr(acq, "fetch_captions", lambda info: LONG_CAPTIONS)
    runner, tr = FakeRunner(_info()), FakeTranscriber()
    media = acquire_text(URL, cs, runner, transcriber=tr)
    assert media.transcript_source == "captions"
    assert media.tier_reached == TIER_CAPTIONS
    assert runner.downloads == []  # no audio, no video
    assert tr.calls == 0
    media.cleanup()


def test_empty_captions_run_audio_and_whisper(cs, monkeypatch):
    monkeypatch.setattr(acq, "fetch_captions", lambda info: "")
    runner, tr = FakeRunner(_info()), FakeTranscriber()
    media = acquire_text(URL, cs, runner, transcriber=tr)
    assert runner.downloads == [AUDIO_FORMAT]
    assert tr.calls == 1
    assert media.transcript_source == "whisper"
    assert media.tier_reached == TIER_AUDIO
    assert media.av_path is not None
    media.cleanup()


def test_whisper_missing_degrades_gracefully(cs, monkeypatch):
    monkeypatch.setattr(acq, "fetch_captions", lambda info: "")
    runner = FakeRunner(_info())
    media = acquire_text(URL, cs, runner, transcriber=FakeTranscriber(installed=False))
    assert runner.downloads == []  # no point downloading audio we cannot transcribe
    assert media.transcript_source == "none"
    assert decide_visuals(media, cs).needs_visuals  # falls through to the visual gate
    media.cleanup()


def test_silent_audio_gates_to_visuals(cs, monkeypatch):
    monkeypatch.setattr(acq, "fetch_captions", lambda info: "")
    media = acquire_text(URL, cs, FakeRunner(_info()), transcriber=FakeTranscriber(text="", ratio=0.0))
    decision = decide_visuals(media, cs)
    assert decision.needs_visuals
    assert any("speech ratio" in r for r in decision.reasons)
    media.cleanup()


def test_carousel_goes_straight_to_images(cs, monkeypatch):
    monkeypatch.setattr(acq, "fetch_captions", lambda info: "")
    runner, tr = FakeRunner(_info(_type="playlist", entries=[{"url": "https://x/1.jpg"}])), FakeTranscriber()
    media = acquire_text("https://www.instagram.com/p/abc/", cs, runner, transcriber=tr)
    assert media.is_carousel
    assert runner.downloads == [] and tr.calls == 0
    assert "carousel/photo post" in decide_visuals(media, cs).reasons
    media.cleanup()


def test_too_long_is_permanent(cs, monkeypatch):
    monkeypatch.setattr(acq, "fetch_captions", lambda info: LONG_CAPTIONS)
    with pytest.raises(PermanentMediaError):
        acquire_text(URL, cs, FakeRunner(_info(duration=9999)))


def test_stage_cache_skips_ytdlp_and_transcription(cs, monkeypatch):
    monkeypatch.setattr(acq, "fetch_captions", lambda info: "")
    cache, tr = MemCache(), FakeTranscriber(text=" ".join(["spoken"] * 40))
    first_runner = FakeRunner(_info())
    acquire_text(URL, cs, first_runner, cache=cache, transcriber=tr).cleanup()
    second_runner = FakeRunner(_info())
    media = acquire_text(URL, cs, second_runner, cache=cache, transcriber=tr)
    assert second_runner.info_calls == 0 and second_runner.downloads == []
    assert tr.calls == 1
    assert media.transcript_source == "whisper"
    media.cleanup()


def test_no_json_on_photo_post_uses_minimal_info(cs, monkeypatch):
    monkeypatch.setattr(acq, "fetch_captions", lambda info: "")
    media = acquire_text("https://www.tiktok.com/@u/photo/7", cs, FakeRunner(None), transcriber=FakeTranscriber())
    assert media.is_photo_post and media.is_carousel
    media.cleanup()


# -- gate -------------------------------------------------------------------


def _media(words: int = 200, source: str = "captions", **kw) -> AcquiredMedia:
    return AcquiredMedia(url=URL, transcript_text=" ".join(["w"] * words), transcript_source=source, **kw)


def test_gate_text_rich_video_stays_text_only(cs):
    assert not decide_visuals(_media(), cs).needs_visuals


def test_gate_list_claim_with_little_speech(cs):
    media = _media(words=30, source="whisper", title="My top 10 songs of 2025")
    assert decide_visuals(media, cs).needs_visuals


def test_gate_spoken_album_list_stays_text_only(cs):
    media = _media(words=300, source="whisper", title="my favorite albums")
    assert not decide_visuals(media, cs).needs_visuals


def test_gate_short_list_after_summary_escalates(cs):
    analysis = CrusherAnalysis(
        category="Saved Music",
        confidence=0.8,
        title="t",
        summary="s",
        list_expected_count=10,
        items=[ExtractedItem(name=f"song {i}") for i in range(6)],
    )
    decision = decide_visuals(_media(), cs, analysis=analysis)
    assert decision.needs_visuals
    assert any("claims 10" in r for r in decision.reasons)


def test_gate_jev_probability_threshold(cs):
    assert decide_visuals(_media(), cs, jev_needs_visuals=0.9).needs_visuals
    assert not decide_visuals(_media(), cs, jev_needs_visuals=0.1).needs_visuals


@pytest.mark.parametrize(
    "text,expected",
    [("Top 10 beaches in Portugal", True), ("5 best apps for students", True), ("my day in Rome", False), ("", False)],
)
def test_list_claim_regex(text, expected):
    assert has_list_claim(text) is expected


# -- parsing / sampling -----------------------------------------------------


def test_vtt_dedupes_rolling_lines_and_adds_markers():
    vtt = (
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n<c>hello</c> world\n\n"
        "00:00:02.000 --> 00:00:04.000\nhello world\n\n"
        "00:00:15.000 --> 00:00:17.000\nsecond part\n"
    )
    assert vtt_to_text(vtt) == "[0:00] hello world [0:15] second part"


def test_json3_parse():
    raw = '{"events": [{"tStartMs": 0, "segs": [{"utf8": "hi"}]}, {"tStartMs": 12000, "segs": [{"utf8": "there"}]}]}'
    assert json3_to_text(raw) == "[0:00] hi [0:12] there"
    assert json3_to_text("not json") == ""


def test_caption_candidates_prefer_manual_english():
    info = {
        "subtitles": {"fr": [{"ext": "vtt", "url": "u-fr"}], "eng-US": [{"ext": "vtt", "url": "u-en"}]},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "a-en"}]},
    }
    urls = [entry["url"] for _b, _l, entry in acq._caption_candidates(info)]
    assert urls[:3] == ["u-en", "a-en", "u-fr"]


def test_evenly_sample_keeps_first_and_last():
    items = list(range(40))
    picked = evenly_sample(items, 5)
    assert picked[0] == 0 and picked[-1] == 39 and len(picked) == 5
    assert evenly_sample(items[:3], 5) == [0, 1, 2]
    assert evenly_sample(items, 0) == []


# -- transcribe --------------------------------------------------------------


def test_build_result_drops_music_hallucinations():
    segs = [
        SimpleNamespace(start=0.0, end=4.0, text=" Real speech here", no_speech_prob=0.1, avg_logprob=-0.3),
        SimpleNamespace(start=4.0, end=6.0, text=" Thanks for watching!", no_speech_prob=0.9, avg_logprob=-1.5),
    ]
    result = build_result(segs, SimpleNamespace(duration=20.0, language="en"))
    assert "Thanks for watching" not in result.text
    assert result.speech_ratio == pytest.approx(0.2)


def test_build_result_empty_audio():
    result = build_result([], SimpleNamespace(duration=0.0, language=None))
    assert result.text == "" and result.speech_ratio == 0.0


def test_whisper_falls_back_to_cpu_on_missing_cuda(monkeypatch):
    import second_brain.crusher.transcribe as transcribe_mod

    monkeypatch.setattr(transcribe_mod, "_cuda_broken", False)
    tr = WhisperTranscriber(CrusherSettings(whisper_device="auto"))
    devices: list[str] = []

    def fake_run(path):
        devices.append(tr._device)
        if tr._device != "cpu":
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        return TranscriptResult(text="ok", language="en", speech_ratio=0.5, duration_seconds=10)

    monkeypatch.setattr(tr, "_run", fake_run)
    assert tr.transcribe(Path("a.m4a")).text == "ok"
    assert devices == ["auto", "cpu"]
    # A second transcriber in the same process must not retry the broken GPU.
    assert WhisperTranscriber(CrusherSettings(whisper_device="auto"))._device == "cpu"


def test_whisper_non_cuda_error_propagates(monkeypatch):
    tr = WhisperTranscriber(CrusherSettings())
    monkeypatch.setattr(tr, "_run", lambda path: (_ for _ in ()).throw(RuntimeError("corrupt file")))
    with pytest.raises(RuntimeError, match="corrupt"):
        tr.transcribe(Path("a.m4a"))


def test_whisper_transcriber_reports_missing_package(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    tr = WhisperTranscriber(CrusherSettings())
    assert tr.available() is False
    with pytest.raises(RuntimeError):
        tr.transcribe(Path("x.m4a"))
