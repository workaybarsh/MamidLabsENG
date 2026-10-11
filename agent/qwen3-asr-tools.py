"""Qwen3-ASR worker: turns an attached audio file into text with Qwen3-ASR-1.7B.

Runs with the imagegen venv (torch cu130 + transformers 5.x + PyAV). Reads JSON on stdin,
writes JSON on stdout. Audio is converted to 16 kHz mono WAV with PyAV, then transcribed
with transformers (Qwen3ASR / AutoModelForMultimodalLM): multilingual dump (52 languages;
Turkish included, speech + singing/BGM). The model lives under the install root
(models\\qwen3-asr).

A single engine, a single model. It is a heavy LLM-based model, so a GPU
is recommended; without one it falls back to CPU (slow).
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

# Offline guarantee: the model is already local; transformers/HF must not hit the network.
os.environ.setdefault('HF_HUB_OFFLINE', '1')
os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')

# Derive the install root from this file's location: it always lives in <root>/agent/.
ROOT = pathlib.Path(__file__).resolve().parent.parent
MAX_AUDIO_BYTES = 1024 * 1024 * 1024
MODEL_DIRNAME = 'qwen3-asr'
# Requested language names (lowercase -> Qwen3-ASR language name). 'auto' -> auto-detect.
LANGUAGE_NAMES = {
    'tr': 'Turkish', 'en': 'English', 'de': 'German', 'fr': 'French', 'es': 'Spanish',
    'it': 'Italian', 'pt': 'Portuguese', 'ru': 'Russian', 'ar': 'Arabic', 'ja': 'Japanese',
    'ko': 'Korean', 'zh': 'Chinese', 'hi': 'Hindi', 'nl': 'Dutch', 'sv': 'Swedish',
    'da': 'Danish', 'fi': 'Finnish', 'pl': 'Polish', 'cs': 'Czech', 'el': 'Greek',
    'hu': 'Hungarian', 'ro': 'Romanian', 'th': 'Thai', 'vi': 'Vietnamese', 'id': 'Indonesian',
    'ms': 'Malay', 'fa': 'Persian', 'fil': 'Filipino', 'mk': 'Macedonian',
}

_MODEL_CACHE: dict = {}


def _model_path(explicit: str | None) -> str:
    if explicit and os.path.isdir(explicit):
        return explicit
    if explicit and os.path.isfile(explicit):
        return os.path.dirname(explicit)
    base = ROOT / 'models' / MODEL_DIRNAME
    if (base / 'config.json').is_file():
        return str(base)
    raise RuntimeError('Qwen3-ASR model not found (models\\%s). Download the model first.' % MODEL_DIRNAME)


def _decode_float32_16k(src: str):
    """Convert any audio/video file to a 16 kHz mono float32 numpy array.

    Uses PyAV (av); no soundfile/librosa needed. Resamples to 16 kHz mono s16 and scales
    to the [-1, 1] range.
    """
    import av
    import numpy as np
    from av.audio.resampler import AudioResampler

    chunks = []
    with av.open(src) as container:
        if not container.streams.audio:
            raise ValueError('No audio stream found.')
        stream = container.streams.audio[0]
        resampler = AudioResampler(format='s16', layout='mono', rate=16000)
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                chunks.append(resampled.to_ndarray())
        for resampled in resampler.resample(None):
            chunks.append(resampled.to_ndarray())
    if not chunks:
        raise ValueError('Audio could not be decoded (empty stream).')
    arr = np.concatenate([c.reshape(-1) for c in chunks]).astype('float32') / 32768.0
    return arr


def _load_model(model_dir: str, device: str):
    key = (model_dir, device)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    import torch
    from transformers import AutoProcessor

    try:
        from transformers import AutoModelForMultimodalLM as _ModelCls
    except Exception:  # older transformers
        from transformers import Qwen3ASRForConditionalGeneration as _ModelCls

    processor = AutoProcessor.from_pretrained(model_dir)
    if device == 'cpu':
        model = _ModelCls.from_pretrained(model_dir)
    else:
        # bf16 (unquantized). int8 was measured: same text but ~2x slower on long audio ->
        # not used. No device_map/accelerate required; move straight to the GPU.
        model = _ModelCls.from_pretrained(model_dir, dtype=torch.bfloat16)
        model = model.to(device)
    model.eval()
    _MODEL_CACHE[key] = (processor, model)
    return processor, model


def _extract_text(processor, raw: str) -> tuple[str, str]:
    """Qwen3-ASR output is 'language <LANG><asr_text>text'. Returns (text, language)."""
    text, lang = raw, ''
    try:
        parsed = processor.extract_transcription(raw)
        if isinstance(parsed, list):
            parsed = parsed[0] if parsed else raw
        if isinstance(parsed, str) and parsed.strip():
            text = parsed
    except Exception:
        try:
            parsed = processor.parse_asr_output(raw)
            if isinstance(parsed, list) and parsed:
                parsed = parsed[0]
            if isinstance(parsed, dict):
                text = parsed.get('transcription') or parsed.get('text') or text
                lang = parsed.get('language') or ''
        except Exception:
            pass
    if not lang and 'language ' in raw:
        try:
            head = raw.split('<asr_text>')[0]
            lang = head.replace('language', '').strip()
        except Exception:
            pass
    return text.strip(), lang.strip()


def transcribe(req: dict) -> dict:
    path = str(req.get('path') or '')
    if not path or not os.path.isfile(path):
        raise ValueError('Audio file not found.')
    if os.path.getsize(path) > MAX_AUDIO_BYTES:
        raise ValueError('Audio file exceeds the safety size limit.')
    model_dir = _model_path(req.get('model'))
    device = str(req.get('device') or '').lower()
    if device not in ('cpu', 'cuda', 'gpu'):
        try:
            import torch
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
        except Exception:
            device = 'cpu'
    if device == 'gpu':
        device = 'cuda'
    if device == 'cuda':
        device = 'cuda:0'

    language = str(req.get('language') or 'auto').strip().lower()
    lang_name = None if language in ('', 'auto') else LANGUAGE_NAMES.get(language, language.title())
    timeout_tokens = int(req.get('max_new_tokens') or 0)

    samples = _decode_float32_16k(path)
    processor, model = _load_model(model_dir, device)
    import torch
    req_kwargs = {'audio': samples}
    if lang_name:
        req_kwargs['language'] = lang_name
    inputs = processor.apply_transcription_request(**req_kwargs)
    inputs = inputs.to(model.device, model.dtype if model.dtype.is_floating_point else None)
    max_new = timeout_tokens or 1024
    with torch.no_grad():
        output_ids = model.generate(**inputs, max_new_tokens=max_new)
    generated = output_ids[:, inputs['input_ids'].shape[1]:]
    raw = processor.batch_decode(generated, skip_special_tokens=True)
    raw = raw[0] if isinstance(raw, list) and raw else str(raw)
    text, detected = _extract_text(processor, raw)
    return {
        'text': text,
        'model': 'Qwen3-ASR-1.7B',
        'language': detected or (lang_name or 'auto'),
        'device': device,
        'chars': len(text),
    }


ACTIONS = {'transcribe': transcribe}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stdin.reconfigure(encoding='utf-8')
    except Exception:
        pass
    req = json.loads(sys.stdin.read() or '{}')
    handler = ACTIONS.get(req.get('action'))
    if not handler:
        print(json.dumps({'error': 'unknown action'}, ensure_ascii=False))
        return
    try:
        print(json.dumps(handler(req), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': '%s: %s' % (type(exc).__name__, exc)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
