"""Whisper.cpp worker: transcribes an attached audio file into text.

Runs with the imagegen venv (PyAV installed). Reads JSON on stdin, writes JSON on stdout.
The audio is converted to 16 kHz mono WAV with PyAV, then transcribed with whisper-cli
(whisper.cpp). The model and binary are looked up under the install root
(models\\whisper, whisper.cpp).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

# Derive the install root from this file's own location: it always lives under <root>/agent/.
ROOT = pathlib.Path(__file__).resolve().parent.parent
MAX_AUDIO_BYTES = 512 * 1024 * 1024
# Preferred model order; otherwise the first ggml-*.bin in the folder is used. turbo comes
# first: measured (RTX 5060 Ti + Ryzen 5 3600) ~2x faster on CPU, ~4-13x on CUDA, and the
# same accuracy as large-v3 on clean speech. Hallucination (silence/music) is suppressed by
# VAD (see below); turbo is NOT more prone to hallucination than large-v3.
MODEL_PREFERENCE = (
    'ggml-large-v3-turbo-q5_0.bin',
    'ggml-large-v3-turbo-q8_0.bin',
    'ggml-large-v3-turbo.bin',
    'ggml-large-v3.bin',
    'ggml-large-v2.bin',
    'ggml-medium.bin',
    'ggml-small.bin',
)


def _find_cli() -> str:
    base = ROOT / 'whisper.cpp'
    candidates = list(base.rglob('whisper-cli.exe')) if base.exists() else []
    if not candidates:
        raise RuntimeError('whisper-cli.exe not found; whisper.cpp may not be downloaded.')
    # Prefer a CUDA build if present.
    cuda = [c for c in candidates if 'cuda' in str(c).lower()]
    return str((cuda or candidates)[0])


def _find_model(explicit: str | None) -> str:
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise ValueError('The given Whisper model was not found: %s' % explicit)
    model_dir = ROOT / 'models' / 'whisper'
    for name in MODEL_PREFERENCE:
        candidate = model_dir / name
        if candidate.is_file():
            return str(candidate)
    found = sorted(model_dir.glob('ggml-*.bin')) if model_dir.exists() else []
    if found:
        return str(found[0])
    raise RuntimeError('No Whisper model found (models\\whisper). Download a model first.')


def _to_wav16k_mono(src: str, dst: str) -> None:
    import av
    from av.audio.resampler import AudioResampler

    with av.open(src) as container:
        if not container.streams.audio:
            raise ValueError('No audio stream found.')
        stream = container.streams.audio[0]
        resampler = AudioResampler(format='s16', layout='mono', rate=16000)
        with av.open(dst, 'w', format='wav') as out:
            out_stream = out.add_stream('pcm_s16le', rate=16000)
            out_stream.layout = 'mono'
            for frame in container.decode(stream):
                for resampled in resampler.resample(frame):
                    for packet in out_stream.encode(resampled):
                        out.mux(packet)
            for resampled in resampler.resample(None):
                for packet in out_stream.encode(resampled):
                    out.mux(packet)


def _find_vad() -> str | None:
    """Silero VAD model (prevents hallucination). Returns it if present, else None."""
    model_dir = ROOT / 'models' / 'whisper'
    for candidate in sorted(model_dir.glob('ggml-silero-*.bin')):
        if candidate.is_file():
            return str(candidate)
    return None


def transcribe(req: dict) -> dict:
    path = str(req.get('path') or '')
    if not path or not os.path.isfile(path):
        raise ValueError('Audio file not found.')
    if os.path.getsize(path) > MAX_AUDIO_BYTES:
        raise ValueError('Audio file exceeds the size safety limit.')
    cli = _find_cli()
    model = _find_model(req.get('model'))
    language = str(req.get('language') or 'auto').strip() or 'auto'
    timeout = int(req.get('timeout') or 1800)
    device = str(req.get('device') or '').lower()
    vad = None if req.get('vad') is False else _find_vad()

    with tempfile.TemporaryDirectory() as temp_dir:
        wav = os.path.join(temp_dir, 'audio.wav')
        _to_wav16k_mono(path, wav)
        out_base = os.path.join(temp_dir, 'transcript')
        args = [cli, '-m', model, '-f', wav, '-of', out_base, '-otxt', '-nt']
        if language != 'auto':
            args += ['-l', language]
        if device == 'cpu':
            args += ['-ng']  # do not use the GPU
        # VAD (Silero): suppresses hallucination on silence/music and helps split speech.
        # Disabling text conditioning (-mc 0) alone was not enough; VAD is required.
        if vad:
            args += ['--vad', '-vm', vad, '-mc', '0']
        proc = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout)
        text_file = out_base + '.txt'
        if os.path.exists(text_file):
            text = pathlib.Path(text_file).read_text(encoding='utf-8', errors='replace').strip()
        elif proc.stdout:
            text = proc.stdout.strip()
        else:
            text = ''
        if proc.returncode != 0 and not text:
            detail = (proc.stderr or proc.stdout or '').strip()
            raise RuntimeError('whisper-cli failed: %s' % detail[:400])
    return {
        'text': text,
        'model': os.path.basename(model),
        'vad': os.path.basename(vad) if vad else '',
        'language': language,
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
