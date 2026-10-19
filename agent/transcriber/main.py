"""Hatti calistirir: audio -> vad/asr -> diarize -> merge -> txt + json.

Iki kullanim:
    python -m transcriber.main <ses_dosyasi> [--json cikti.json] [--timestamps]
    echo '{"action":"transcribe","path":"...","device":"auto"}' | python -m transcriber.main --stdin

With `device='auto'` the device (cuda/cpu) is chosen from the recording duration (see
config.choose_device).

In the LITE (old name PC1) profile models are loaded SEQUENTIALLY and memory is freed between
them; in the PRO (MAX/FLASH; old name PC2) profile they can stay resident together. The only
difference is in the config profile; the selected profile name is passed from the UI in the
`profile` field (see config.select_profile_for).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import asr, audio, config, diarize, merge


def run(path: str, profile=None, device: str = 'cuda', initial_prompt: str | None = None,
        diarize_enabled: bool | None = None,
        token: str | None = None, include_timestamps: bool = False,
        out_base: str | None = None, keep_wav: bool = False,
        need_swap: bool | None = None) -> dict:
    if not path or not os.path.isfile(path):
        raise ValueError('Ses dosyasi bulunamadi: %s' % path)
    profile = profile or config.select_profile()
    sequential = (profile.load_mode == 'sequential')
    want_diarize = profile.diarize if diarize_enabled is None else bool(diarize_enabled)

    wav = audio.to_wav16k_mono(path)
    language = profile.language
    duration = None
    try:
        duration = audio.duration_seconds(wav)
    except Exception:
        pass

    # --- Device selection (device='auto') ----------------------------------
    # The decision uses the WHOLE recording duration; with chunked transcription the same
    # device is used for every chunk so it never switches mid-recording.
    device = str(device or 'auto').lower()
    if device == 'auto':
        if profile.auto_device:
            # If need_swap is not given explicitly, assume a swap is needed (safe side);
            # do NOT infer it from load_mode.
            use_swap = True if need_swap is None else bool(need_swap)
            device = config.choose_device(duration, profile, use_swap)
        else:
            device = 'cuda'

    chunks = audio.split_wav(wav, profile.chunk_minutes)

    # --- Konusma (Whisper) -------------------------------------------------
    segments: list[dict] = []
    for chunk_path, offset in chunks:
        result = asr.transcribe(chunk_path, profile, device=device,
                                initial_prompt=initial_prompt, offset=offset)
        segments.extend(result['segments'])
        if result.get('language'):
            language = result['language']
    if sequential:
        asr.unload_model()

    # --- Konusmaci ayrimi (pyannote) --------------------------------------
    turns: list[dict] = []
    if want_diarize:
        turns = diarize.diarize(wav, token=token, device=device)
        if sequential:
            diarize.unload_pipeline()

    text = merge.merge(segments, turns, include_timestamps=include_timestamps)

    payload = {
        'text': text,
        'language': language,
        'duration': duration,
        'device': device,
        'profile': profile.name,
        'compute_type': profile.compute_type,
        'model': profile.whisper_model,
        'diarized': bool(turns),
        'segments': segments,
        'speakers': turns,
    }

    # --- Cikti dosyalari ---------------------------------------------------
    base = out_base or os.path.splitext(path)[0]
    with open(base + '.txt', 'w', encoding='utf-8') as fh:
        fh.write(text)
    with open(base + '.json', 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    payload['txt_path'] = base + '.txt'
    payload['json_path'] = base + '.json'

    # --- Gecici dosya temizligi -------------------------------------------
    if not keep_wav:
        for chunk_path, _ in chunks:
            if chunk_path != path and chunk_path != wav:
                try:
                    os.remove(chunk_path)
                except Exception:
                    pass
        if wav != path:
            try:
                os.remove(wav)
            except Exception:
                pass
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Yerel konusma + ses olayi dökumu')
    parser.add_argument('path', nargs='?', help='ses/video dosyasi')
    parser.add_argument('--stdin', action='store_true', help='JSON istegi stdin\'den oku')
    parser.add_argument('--device', default='auto', choices=['auto', 'cuda', 'cpu'])
    parser.add_argument('--profile', default=None, choices=['lite', 'pro', 'pc1', 'pc2', 'max', 'flash'])
    parser.add_argument('--prompt', default=None, help='initial_prompt (ozel isim/terim)')
    parser.add_argument('--no-diarize', action='store_true')
    parser.add_argument('--timestamps', action='store_true')
    parser.add_argument('--json', dest='json_out', default=None)
    parser.add_argument('--keep-wav', action='store_true')
    args = parser.parse_args(argv)

    if args.stdin:
        request = json.loads(sys.stdin.read() or '{}')
        if request.get('action') not in (None, 'transcribe'):
            print(json.dumps({'error': 'bilinmeyen islem'}))
            return 0
        profile = None
        if request.get('profile'):
            profile = config.select_profile_for(str(request['profile']))
        try:
            result = run(
                str(request.get('path') or ''),
                profile=profile,
                device=str(request.get('device') or 'auto'),
                need_swap=request.get('need_swap'),
                initial_prompt=request.get('initial_prompt'),
                diarize_enabled=request.get('diarize'),
                token=request.get('token'),
                include_timestamps=bool(request.get('include_timestamps')),
                out_base=request.get('out_base'),
                keep_wav=bool(request.get('keep_wav')),
            )
        except Exception as exc:
            print(json.dumps({'error': '%s: %s' % (type(exc).__name__, exc)}, ensure_ascii=False))
            return 0
        print(json.dumps({'text': result['text'], 'model': result['model'],
                          'language': result['language'], 'profile': result['profile'],
                          'chars': len(result['text'])}, ensure_ascii=False))
        return 0

    if not args.path:
        parser.error('ses dosyasi gerekli')
    profile = None
    if args.profile:
        profile = config.select_profile_for(args.profile)
    result = run(args.path, profile=profile, device=args.device, initial_prompt=args.prompt,
                 diarize_enabled=not args.no_diarize if args.no_diarize else None,
                 include_timestamps=args.timestamps, keep_wav=args.keep_wav)
    print(result['text'])
    if args.json_out:
        with open(args.json_out, 'w', encoding='utf-8') as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
