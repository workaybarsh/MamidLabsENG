"""Whisper backend (faster-whisper). Model + compute_type come from the config profile.

HALLUCINATION CONTROL (accuracy first):
- Silero VAD keeps non-speech spans away from Whisper (`vad_filter`).
- `condition_on_previous_text=False`: avoids feeding repetition loops.
- `no_speech_threshold` / `log_prob_threshold` / `compression_ratio_threshold`: drop
  low-confidence and looping segments.
- Low-temperature fallback + `suppress_blank`.
- Post-filter: empty/punctuation-only, known hallucination phrases, excessive repetition
  and low-probability segments are removed; consecutive duplicates are collapsed.
"""
from __future__ import annotations

import os
import re

_MODEL_CACHE: dict = {}


def _add_cuda_dll_dirs() -> None:
    """Adds CUDA runtime DLLs (pip nvidia-*-cu12 packages) to the DLL search path.

    CTranslate2 (faster-whisper) looks for cublas64_12.dll / cudnn DLLs on Windows; when a
    full CUDA toolkit is not installed these come from the pip packages. If missing, the GPU
    attempt falls back to CPU.
    """
    if os.name != 'nt':
        return
    try:
        import glob
        import site
    except Exception:
        return
    bases = set(site.getsitepackages())
    try:
        bases.add(site.getusersitepackages())
    except Exception:
        pass
    extra = []
    for base in bases:
        extra.extend(glob.glob(os.path.join(base, 'nvidia', '*', 'bin')))
    for path in extra:
        try:
            os.add_dll_directory(path)
        except Exception:
            pass
    # CTranslate2 uses LoadLibrary; the directories must also be on PATH.
    if extra:
        os.environ['PATH'] = os.pathsep.join(extra + [os.environ.get('PATH', '')])


def _normalize(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r'[\s\.,!?…\-_*\[\]()"\']+', ' ', t).strip()
    return t


# Phrases Whisper tends to emit on silence / wrong language.
_RAW_HALLUCINATIONS = {
    'Thanks for watching', 'Thank you for watching', 'Subscribe', 'Please subscribe',
    'Subtitles by', 'www.', '.com',
}
HALLUCINATION_PHRASES = {_normalize(p) for p in _RAW_HALLUCINATIONS}


def _is_hallucination(segment) -> bool:
    text = (segment.text or '').strip()
    if not text:
        return True
    if not re.search(r'\w', text, flags=re.UNICODE):
        return True
    norm = _normalize(text)
    if norm in HALLUCINATION_PHRASES:
        return True
    words = norm.split()
    if len(words) >= 6 and len(set(words)) <= 2:
        return True
    try:
        if segment.no_speech_prob is not None and segment.no_speech_prob > 0.85 \
                and segment.avg_logprob is not None and segment.avg_logprob < -0.4:
            return True
        if segment.compression_ratio is not None and segment.compression_ratio > 2.4:
            return True
    except Exception:
        pass
    return False


def load_model(profile, device: str = 'cuda'):
    """Loads the faster-whisper model (cached). Falls back to CPU + int8 without CUDA."""
    from faster_whisper import WhisperModel

    _add_cuda_dll_dirs()
    device = (device or 'cuda').lower()
    if device not in ('cuda', 'cpu'):
        device = 'cuda'
    compute_type = profile.compute_type if device == 'cuda' else 'int8'
    key = (profile.whisper_model, device, compute_type)
    if key not in _MODEL_CACHE:
        try:
            _MODEL_CACHE[key] = WhisperModel(
                profile.whisper_model, device=device, compute_type=compute_type, device_index=0,
            )
        except Exception:
            if device == 'cuda':
                key = (profile.whisper_model, 'cpu', 'int8')
                _MODEL_CACHE[key] = WhisperModel(profile.whisper_model, device='cpu', compute_type='int8')
            else:
                raise
    return _MODEL_CACHE[key]


def unload_model():
    _MODEL_CACHE.clear()
    try:
        import gc
        gc.collect()
    except Exception:
        pass
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def transcribe(wav_path: str, profile, device: str = 'cuda', initial_prompt: str | None = None,
               offset: float = 0.0) -> dict:
    """wav_path -> {language, language_probability, duration, segments:[...]}.

    offset: start second of this chunk within the original recording.
    """
    model = load_model(profile, device)
    # Convert to a 16 kHz mono float32 numpy array: faster-whisper then avoids its own PyAV
    # version dependency (faster, no double decode).
    from . import audio as _audio
    if isinstance(wav_path, str):
        samples = _audio.read_mono_float32(wav_path)
    else:
        samples = wav_path
    segments, info = model.transcribe(
        samples,
        language=profile.language,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        condition_on_previous_text=False,
        word_timestamps=True,
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4,
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        suppress_blank=True,
        initial_prompt=initial_prompt,
    )
    out: list[dict] = []
    previous_norm = None
    for segment in segments:
        if _is_hallucination(segment):
            continue
        norm = _normalize(segment.text)
        if norm == previous_norm:
            continue
        previous_norm = norm
        words = []
        for word in (segment.words or []):
            words.append({'start': round(word.start + offset, 2),
                          'end': round(word.end + offset, 2),
                          'word': word.word})
        out.append({
            'start': round(segment.start + offset, 2),
            'end': round(segment.end + offset, 2),
            'text': segment.text.strip(),
            'avg_logprob': round(float(segment.avg_logprob or 0.0), 3),
            'no_speech_prob': round(float(segment.no_speech_prob or 0.0), 3),
            'words': words,
        })
    return {
        'language': info.language,
        'language_probability': getattr(info, 'language_probability', None),
        'duration': getattr(info, 'duration', None),
        'segments': out,
    }
