"""Keyframe extraction: no video stream, and a fixed-interval fallback when scene select yields nothing."""

from __future__ import annotations

import subprocess
from pathlib import Path

from second_brain.config import CrusherSettings
from second_brain.crusher import acquire
from second_brain.crusher.acquire import extract_keyframes
from second_brain.crusher.probe import ProbeResult


def _completed(cmd, code=0, err=""):
    return subprocess.CompletedProcess(cmd, code, "", err)


def test_no_video_stream_skips_ffmpeg(tmp_path, monkeypatch):
    video = tmp_path / "audio.m4a"
    video.write_bytes(b"\x00")
    monkeypatch.setattr(acquire, "probe_file", lambda path: ProbeResult(has_audio=True, has_video=False))
    called = []
    monkeypatch.setattr(acquire, "_ffmpeg", lambda cmd: called.append(cmd) or _completed(cmd))
    assert extract_keyframes(video, tmp_path / "frames", CrusherSettings()) == []
    assert called == []


def test_scene_select_miss_falls_back_to_interval(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")
    monkeypatch.setattr(acquire, "probe_file", lambda path: ProbeResult(has_video=True, duration_seconds=15))
    calls: list[list[str]] = []

    def fake_ffmpeg(cmd):
        calls.append(cmd)
        pattern = cmd[-1]
        if "iv_%03d" in pattern:
            dest = Path(pattern.replace("%03d", "001"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\xff\xd8")
        return _completed(cmd, code=1 if "select=" in " ".join(cmd) else 0, err="Invalid argument")

    monkeypatch.setattr(acquire, "_ffmpeg", fake_ffmpeg)
    frames = extract_keyframes(video, tmp_path / "frames", CrusherSettings(max_keyframes=4, keyframe_interval_seconds=8))
    assert len(frames) == 1
    assert frames[0].name.startswith("iv_")
    assert any(part == "0:v:0" for part in calls[0])
    assert calls[0][-1].replace("\\", "/").endswith("kf_%03d.jpg")
    assert "\\" not in calls[0][-1]
    assert "fps=1/8" in calls[1][calls[1].index("-vf") + 1]
