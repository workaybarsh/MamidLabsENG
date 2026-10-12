"""Audio preparation: converts any audio/video file to a 16 kHz mono PCM WAV.

Uses PyAV (no ffmpeg binary needed). Long recordings (PC1) are split into chunks with the
`wave` module so 16 GB of system RAM does not swell.
"""
from __future__ import annotations

import os
import wave

SAMPLE_RATE = 16000


def to_wav16k_mono(src: str, dst: str | None = None) -> str:
    """src -> 16 kHz mono s16le WAV. Without dst, writes next to src."""
    if dst is None:
        base, _ = os.path.splitext(src)
        dst = base + '.16k.wav'
    import av
    from av.audio.resampler import AudioResampler

    with av.open(src) as container:
        if not container.streams.audio:
            raise ValueError('No audio stream found: %s' % src)
        stream = container.streams.audio[0]
        resampler = AudioResampler(format='s16', layout='mono', rate=SAMPLE_RATE)
        with av.open(dst, 'w', format='wav') as out:
            out_stream = out.add_stream('pcm_s16le', rate=SAMPLE_RATE)
            out_stream.layout = 'mono'
            for frame in container.decode(stream):
                for resampled in resampler.resample(frame):
                    for packet in out_stream.encode(resampled):
                        out.mux(packet)
            for resampled in resampler.resample(None):
                for packet in out_stream.encode(resampled):
                    out.mux(packet)
    return dst


def duration_seconds(wav_path: str) -> float:
    with wave.open(wav_path, 'rb') as w:
        return w.getnframes() / float(w.getframerate() or SAMPLE_RATE)


def probe_duration_seconds(path: str) -> float | None:
    """Estimates the duration (seconds) of any audio/video file WITHOUT full decoding.

    Used for the device/swap plan (short vs long decision); there is no need to convert to
    a full WAV. Returns None when unreadable.
    """
    try:
        import av

        with av.open(path) as container:
            if container.duration is not None:
                return float(container.duration) / float(av.time_base)
            for stream in container.streams:
                if stream.duration is not None and stream.time_base is not None:
                    return float(stream.duration * stream.time_base)
    except Exception:
        return None
    return None


def read_mono_float32(wav_path: str):
    """Reads a 16 kHz mono WAV into a [-1, 1] float32 numpy array (for the VAD)."""
    import numpy as np

    with wave.open(wav_path, 'rb') as w:
        assert w.getframerate() == SAMPLE_RATE, 'the VAD expects 16 kHz'
        frames = w.readframes(w.getnframes())
    data = np.frombuffer(frames, dtype='<i2').astype('float32') / 32768.0
    return data


def split_wav(wav_path: str, minutes: int) -> list[tuple[str, float]]:
    """Splits the WAV into `minutes` slices. Returns [(chunk_path, start_second), ...].

    With minutes <= 0, or when the recording is short, returns a single [(wav_path, 0.0)].
    """
    if minutes <= 0:
        return [(wav_path, 0.0)]
    with wave.open(wav_path, 'rb') as w:
        params = w.getparams()
        total = w.getnframes()
        rate = w.getframerate() or SAMPLE_RATE
    per_chunk = int(minutes * 60 * rate)
    if total <= per_chunk:
        return [(wav_path, 0.0)]
    chunks: list[tuple[str, float]] = []
    base, _ = os.path.splitext(wav_path)
    with wave.open(wav_path, 'rb') as w:
        index = 0
        offset = 0
        while True:
            frames = w.readframes(per_chunk)
            if not frames:
                break
            chunk_path = '%s.part%02d.wav' % (base, index)
            with wave.open(chunk_path, 'wb') as out:
                out.setparams(params)
                out.writeframes(frames)
            chunks.append((chunk_path, offset / float(rate)))
            index += 1
            offset += len(frames) // (params.sampwidth * params.nchannels)
    return chunks
