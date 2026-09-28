"""Local speech-to-text for videos without captions (tier T2).

faster-whisper runs on this machine, so transcription costs $0 in API spend.
It is an optional extra (``pip install -e ".[transcribe]"``); without it the
crusher skips T2 and lets the visual gate decide.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import CrusherSettings

logger = logging.getLogger(__name__)

# Whisper invents "Thanks for watching!" over music. Drop segments the model
# itself thinks are not speech and that it decoded with low confidence.
_NO_SPEECH_PROB_MAX = 0.6
_AVG_LOGPROB_MIN = -1.0
_MARKER_EVERY_SECONDS = 10.0

# Process-wide: after one CUDA library failure, a second GPU attempt in the
# same process can crash natively inside CTranslate2 (observed on Windows).
_cuda_broken = False


@dataclass
class TranscriptResult:
    text: str
    language: str | None
    speech_ratio: float  # 0..1 share of the clip that VAD kept as speech
    duration_seconds: float


class WhisperTranscriber:
    """Lazily loads one faster-whisper model and reuses it for the whole run."""

    def __init__(self, settings: CrusherSettings):
        self.settings = settings
        self._model = None
        self._available: bool | None = None
        self._device = "cpu" if _cuda_broken else settings.whisper_device

    def available(self) -> bool:
        if self._available is None:
            self._available = importlib.util.find_spec("faster_whisper") is not None
        return self._available

    def _load(self, device: str | None = None):
        if self._model is not None:
            return self._model
        if not self.available():
            raise RuntimeError('faster-whisper is not installed. Run: pip install -e ".[transcribe]"')
        from faster_whisper import WhisperModel

        device = device or self._device
        logger.info(
            "Loading faster-whisper model '%s' (device=%s, compute=%s). First run downloads weights.",
            self.settings.whisper_model,
            device,
            self.settings.whisper_compute_type,
        )
        self._model = WhisperModel(
            self.settings.whisper_model,
            device=device,
            compute_type=self.settings.whisper_compute_type,
        )
        return self._model

    def _run(self, path: Path) -> TranscriptResult:
        model = self._load()
        segments_iter, info = model.transcribe(
            str(path),
            language=self.settings.whisper_language,
            vad_filter=True,
            beam_size=1,  # greedy: ~2x faster, accuracy loss is negligible for short clips
            condition_on_previous_text=False,  # stops repetition loops on music beds
        )
        # Segments are lazy: CUDA library errors surface here, not at load time.
        return build_result(list(segments_iter), info)

    def transcribe(self, path: Path) -> TranscriptResult:
        try:
            return self._run(path)
        except RuntimeError as exc:
            if self._device == "cpu" or not _is_cuda_runtime_error(exc):
                raise
            # device=auto picks a visible GPU even when the CUDA 12 cuBLAS/cuDNN
            # DLLs are not installed (common on Windows). Fall back for this process:
            # a second GPU attempt can crash natively inside CTranslate2.
            global _cuda_broken
            logger.warning("GPU transcription unavailable (%s). Falling back to CPU for this process.", exc)
            _cuda_broken = True
            self._device = "cpu"
            self._model = None
            return self._run(path)


def _is_cuda_runtime_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in ("cublas", "cudnn", "cuda", "libcudart"))


def build_result(segments: list, info) -> TranscriptResult:
    """Turn faster-whisper segments + info into text and a speech ratio. Pure; unit-tested."""
    kept: list[str] = []
    speech_seconds = 0.0
    last_marker = -_MARKER_EVERY_SECONDS
    for seg in segments:
        no_speech = float(getattr(seg, "no_speech_prob", 0.0) or 0.0)
        logprob = float(getattr(seg, "avg_logprob", 0.0) or 0.0)
        if no_speech > _NO_SPEECH_PROB_MAX and logprob < _AVG_LOGPROB_MIN:
            continue
        text = str(getattr(seg, "text", "") or "").strip()
        if not text:
            continue
        start = float(getattr(seg, "start", 0.0) or 0.0)
        end = float(getattr(seg, "end", start) or start)
        speech_seconds += max(0.0, end - start)
        if start - last_marker >= _MARKER_EVERY_SECONDS:
            kept.append(f"[{int(start) // 60}:{int(start) % 60:02d}]")
            last_marker = start
        kept.append(text)

    duration = float(getattr(info, "duration", 0.0) or 0.0)
    ratio = min(1.0, speech_seconds / duration) if duration > 0 else 0.0
    return TranscriptResult(
        text=" ".join(kept),
        language=getattr(info, "language", None),
        speech_ratio=round(ratio, 3),
        duration_seconds=duration,
    )
