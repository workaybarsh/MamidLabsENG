"""Silero VAD: finds speech ranges (reduces hallucinated text on silence).

Uses the silero VAD bundled with faster-whisper (no separate model download). The ASR side
already uses `vad_filter=True`; the functions here serve the event pipeline by separating
non-speech windows and reporting.
"""
from __future__ import annotations

DEFAULT_MIN_SILENCE_MS = 500


def speech_ranges(wav_path: str, min_silence_ms: int = DEFAULT_MIN_SILENCE_MS) -> list[tuple[float, float]]:
    """Speech ranges as (start, end) seconds. Returns an empty list on error."""
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except Exception:
        return []
    from . import audio

    samples = audio.read_mono_float32(wav_path)
    options = VadOptions(min_silence_duration_ms=min_silence_ms)
    try:
        stamps = get_speech_timestamps(samples, options)
    except Exception:
        return []
    return [(t['start'] / float(audio.SAMPLE_RATE), t['end'] / float(audio.SAMPLE_RATE)) for t in stamps]


def is_speech(start: float, end: float, ranges: list[tuple[float, float]], min_overlap: float = 0.2) -> bool:
    """Does the interval overlap the speech ranges by at least `min_overlap`?"""
    span = max(end - start, 1e-6)
    for a, b in ranges:
        overlap = min(end, b) - max(start, a)
        if overlap > 0 and overlap / span >= min_overlap:
            return True
    return False
