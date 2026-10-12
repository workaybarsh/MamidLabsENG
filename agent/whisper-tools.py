"""Audio transcription worker: calls the `transcriber` package.

Engine: faster-whisper large-v3. Audio events (AST) and speaker diarization (pyannote)
are enabled by default. Output is chronological and timestamp-free:

    (footsteps)
    Speaker: Did you come?
    (door closing)
    Speaker: Yes, a bit late.

UI/worker contract: reads JSON on stdin, writes JSON on stdout.
    request: {"action":"transcribe","path":...,"device":"auto|cuda|cpu","need_swap":bool,"profile":"lite|pro","language":"auto", ...}
    request: {"action":"plan","local_model_loaded":bool,"profile":"lite|pro"}  -> {"device":..,"need_swap":..}
    reply:   {"text":..., "model":..., "chars":..., "events":N, "diarized":bool} or {"error":...}
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

# The transcriber package lives next to this file (<root>/agent/transcriber).
AGENT_DIR = pathlib.Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

# Keep the Hugging Face cache under the install root (matches the installer's HF_HOME).
_ROOT = AGENT_DIR.parent
os.environ.setdefault('HF_HOME', str(_ROOT / 'tools' / 'hf-home'))
os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')

from transcriber import config, main as transcriber_main  # noqa: E402


def _profile(req: dict):
    """The audio-pipeline tier comes from the SELECTED profile name: 'lite', 'pro' ('max'/'flash'),
    or the old names 'pc1'/'pc2'. When the name is absent/unknown the machine's total VRAM decides
    the tier (see config.select_profile_for)."""
    language = str(req.get('language') or '').strip()
    overrides = {}
    if language:
        overrides['language'] = language
    name = req.get('profile')
    if name:
        return config.select_profile_for(name, **overrides)
    return config.select_profile(**overrides) if overrides else config.select_profile()


def transcribe(req: dict) -> dict:
    # 'auto' is passed through as-is; the transcriber package picks the device from the
    # recording duration.
    device = str(req.get('device') or 'auto').lower()
    if device == 'gpu':
        device = 'cuda'
    # need_swap: tells the worker that the local 27B provider has a VRAM swap cost. When the
    # field is absent, leave it None (the package applies its safe default).
    need_swap = req.get('need_swap')
    if need_swap is not None:
        need_swap = bool(need_swap)
    profile = _profile(req)
    payload = transcriber_main.run(
        str(req.get('path') or ''),
        profile=profile,
        device=device,
        need_swap=need_swap,
        initial_prompt=req.get('initial_prompt'),
        events=req.get('events'),
        diarize_enabled=req.get('diarize'),
        token=req.get('token'),
        include_timestamps=False,
        out_base=req.get('out_base'),
        keep_wav=False,
    )
    return {
        'text': payload['text'],
        'model': os.path.basename(profile.whisper_model),
        'vad': 'silero',
        'chars': len(payload['text']),
        'language': payload['language'],
        'profile': profile.name,
        'events': len(payload['events']),
        'diarized': payload['diarized'],
    }


def plan(req: dict) -> dict:
    """Returns the (device, swap) plan before the audio job starts (see config.choose_plan).

    Priority: (1) fits in free VRAM alongside the language model -> GPU + no swap; (2) does
    not fit in VRAM and the recording is SHORT (swap cost outweighs the gain) but fits in
    system RAM on the CPU -> CPU + no swap; (3) otherwise (long recording, or RAM
    insufficient) -> GPU + mandatory swap. The duration is estimated from `path` without
    decoding. The worker reports this plan to the UI/worker layer; the language model is
    unloaded ONLY when a swap is required.
    """
    profile = _profile(req)
    local = req.get('local_model_loaded')
    local = True if local is None else bool(local)
    vram = config.free_vram_mib()
    ram = config.available_ram_mib()
    # Recording duration (short -> CPU, long -> GPU economy). Estimated without decoding.
    duration = None
    path = str(req.get('path') or '')
    if path:
        try:
            from transcriber import audio
            duration = audio.probe_duration_seconds(path)
        except Exception:
            duration = None
    device, need_swap = config.choose_plan(
        profile, duration_seconds=duration, vram_free_mib=vram, ram_free_mib=ram,
        local_model_loaded=local)
    return {
        'device': device,
        'need_swap': need_swap,
        'profile': profile.name,
        'duration_seconds': duration,
        'free_vram_mib': vram,
        'free_ram_mib': ram,
    }


ACTIONS = {'transcribe': transcribe, 'plan': plan}


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
