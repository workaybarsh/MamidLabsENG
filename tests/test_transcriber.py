"""Audio transcription pipeline (agent/transcriber) + UI/worker contracts.

Engine: faster-whisper large-v3 + AST audio events + pyannote speaker diarization.
Qwen3-ASR and whisper.cpp were fully removed. Heavy tests are skipped when the model/
environment is unavailable; the contracts are always checked.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
TRANSCRIBER = ROOT / "agent" / "transcriber"
WORKER = ROOT / "agent" / "whisper-tools.py"

if str(ROOT / "agent") not in sys.path:
    sys.path.insert(0, str(ROOT / "agent"))


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def load_worker():
    spec = importlib.util.spec_from_file_location("whisper_tools", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------- package / config
def test_package_modules_exist():
    for name in ("config", "audio", "vad", "asr", "events", "diarize", "merge", "main"):
        assert (TRANSCRIBER / f"{name}.py").is_file(), name + ".py missing"


def test_config_selects_profile_by_vram():
    """When no name is given (CLI) the total VRAM decides the tier: >=14000 MiB -> 'pro',
    below -> 'lite'."""
    from transcriber import config

    pro = config.select_profile(total_mib=16311)
    assert pro.name == "pro" and pro.compute_type == "float16" and pro.load_mode == "parallel"
    lite = config.select_profile(total_mib=8000)
    assert lite.name == "lite" and lite.compute_type == "int8_float16" and lite.load_mode == "sequential"
    assert lite.chunk_minutes > 0 and pro.chunk_minutes == 0
    # English edition: standard large-v3, automatic language detection.
    assert pro.language == "auto"
    assert "faster-whisper-large-v3" in pro.whisper_model


def test_select_profile_for_uses_selected_profile_name():
    """The SELECTED language-model profile NAME decides the tier; total VRAM is only the
    fallback when the name is absent."""
    from transcriber import config

    # 'lite' -> lite tier; stays lite even with plenty of VRAM (16311 MiB).
    lite_high = config.select_profile_for("lite", total_mib=16311)
    assert lite_high.name == "lite" and lite_high.load_mode == "sequential"
    # 'pro'/'max'/'flash' (and the old 'pc2') -> pro tier; stays pro even with little VRAM (8000 MiB).
    for name in ("pro", "max", "flash", "PRO", " max ", "pc2"):
        prof = config.select_profile_for(name, total_mib=8000)
        assert prof.name == "pro", name
        assert prof.compute_type == "float16" and prof.load_mode == "parallel"
    # The old hardware name 'pc1' -> lite; stays lite even with plenty of VRAM.
    assert config.select_profile_for("pc1", total_mib=16311).name == "lite"
    # No/unknown name -> VRAM fallback (8000 -> lite, 16311 -> pro).
    assert config.select_profile_for(None, total_mib=8000).name == "lite"
    assert config.select_profile_for(None, total_mib=16311).name == "pro"
    assert config.select_profile_for("unknown", total_mib=8000).name == "lite"
    assert config.select_profile_for("unknown", total_mib=16311).name == "pro"
    # overrides are applied to the profile selected by name too.
    tuned = config.select_profile_for("lite", total_mib=16311, language="auto", events=False)
    assert tuned.name == "lite" and tuned.language == "auto" and tuned.events is False


def test_config_overrides_are_applied():
    from transcriber import config

    profile = config.select_profile(total_mib=8000, language="en", events=False)
    assert profile.language == "en" and profile.events is False


# ---------------------------------------------------- automatic device choice
def test_choose_device_without_swap_always_cuda():
    """need_swap=False (API or LITE): no VRAM swap cost -> always cuda."""
    from transcriber import config

    profile = config.select_profile(total_mib=8000)
    assert config.choose_device(5, profile, need_swap=False) == "cuda"
    assert config.choose_device(10_000, profile, need_swap=False) == "cuda"
    # Even when the duration is unknown, no swap means cuda.
    assert config.choose_device(None, profile, need_swap=False) == "cuda"


def test_choose_device_short_uses_cpu_long_uses_cuda_when_swap_needed():
    from transcriber import config

    profile = config.select_profile(total_mib=8000)
    # Effective threshold: profile-specific (LITE 15 s) if set, else the default.
    threshold = profile.cpu_max_seconds or config.DEFAULT_CPU_MAX_SECONDS
    # Below the threshold -> CPU (the swap cost outweighs the gain).
    assert config.choose_device(threshold - 1, profile, need_swap=True) == "cpu"
    assert config.choose_device(5, profile, need_swap=True) == "cpu"
    # Boundary value -> CPU (<=).
    assert config.choose_device(threshold, profile, need_swap=True) == "cpu"
    # Above the threshold -> CUDA.
    assert config.choose_device(threshold + 1, profile, need_swap=True) == "cuda"
    # Unknown duration -> safe default cuda.
    assert config.choose_device(None, profile, need_swap=True) == "cuda"


def test_choose_device_cpu_max_seconds_override():
    from transcriber import config

    profile = config.select_profile(total_mib=8000)
    # Per-call override.
    assert config.choose_device(300, profile, need_swap=True, cpu_max_seconds=600) == "cpu"
    assert config.choose_device(700, profile, need_swap=True, cpu_max_seconds=600) == "cuda"
    # Per-profile override (select_profile overrides).
    tuned = config.select_profile(total_mib=8000, cpu_max_seconds=30)
    assert tuned.cpu_max_seconds == 30
    assert config.choose_device(10, tuned, need_swap=True) == "cpu"
    assert config.choose_device(60, tuned, need_swap=True) == "cuda"


def test_profile_auto_device_defaults_on():
    from transcriber import config

    assert config.select_profile(total_mib=8000).auto_device is True
    assert config.select_profile(total_mib=16311).auto_device is True


# ------------------------------------------------- memory plan (VRAM -> RAM -> swap)
def test_profile_carries_audio_memory_thresholds():
    from transcriber import config

    profile = config.select_profile(total_mib=8000)
    assert profile.audio_vram_mib > 0 and profile.audio_ram_mib > 0
    assert config.DEFAULT_AUDIO_VRAM_MIB == profile.audio_vram_mib


def test_choose_plan_priority_vram_then_ram_then_swap():
    """pro (the old PC2; CPU branch enabled) priority: (1) fits together -> GPU no swap,
    (2) fits in CPU RAM -> CPU no swap, (3) neither fits -> mandatory swap."""
    from transcriber import config

    profile = config.select_profile(total_mib=16311)
    vram = profile.audio_vram_mib
    ram = profile.audio_ram_mib
    # 1) Enough free VRAM -> GPU, NO swap.
    assert config.choose_plan(profile, vram_free_mib=vram, ram_free_mib=ram) == ("cuda", False)
    assert config.choose_plan(profile, vram_free_mib=vram + 1000, ram_free_mib=0) == ("cuda", False)
    # 2) Not enough VRAM but enough system RAM -> CPU, NO swap (slower, model stays resident).
    assert config.choose_plan(profile, vram_free_mib=vram - 1, ram_free_mib=ram) == ("cpu", False)
    assert config.choose_plan(profile, vram_free_mib=0, ram_free_mib=ram + 10_000) == ("cpu", False)
    # 3) Neither fits -> mandatory VRAM swap (GPU).
    assert config.choose_plan(profile, vram_free_mib=vram - 1, ram_free_mib=ram - 1) == ("cuda", True)
    assert config.choose_plan(profile, vram_free_mib=0, ram_free_mib=0) == ("cuda", True)


def test_choose_plan_lite_short_uses_cpu_long_uses_gpu_swap():
    """LITE policy (threshold: cpu_max_seconds = LITE_CPU_MAX_SECONDS = 15 s; the CPU branch
    needs LITE_AUDIO_RAM_MIB = 5120 MiB of system RAM, the SAME as PRO). Measurement-2 (which
    actually ran the LM and the ASR CPU pipeline TOGETHER) concludes: co-resident **DOES NOT
    FIT** (physical 15.25 GB < requirement ~16.1 GB; the LM pages are evicted from the
    reclaimable cache). So when VRAM does not fit, the CPU co-resident path is chosen ONLY when
    the recording is SHORTER than the threshold AND system RAM is PLENTIFUL (('cpu', False));
    with low RAM or a long recording -> GPU + mandatory swap (('cuda', True))."""
    from transcriber import config

    profile = config.select_profile(total_mib=8000)
    threshold = profile.cpu_max_seconds
    assert threshold == config.LITE_CPU_MAX_SECONDS
    # Measurement-2: LITE co-resident DOES NOT FIT -> the safe gate is the same as PRO (5120 MiB).
    assert profile.audio_ram_mib == config.LITE_AUDIO_RAM_MIB == config.DEFAULT_AUDIO_RAM_MIB
    assert config.select_profile(total_mib=16311).audio_ram_mib == config.DEFAULT_AUDIO_RAM_MIB
    ram_ok = profile.audio_ram_mib          # exactly the threshold counts as "enough"
    # Enough free VRAM -> GPU, no swap (previous rule preserved).
    assert config.choose_plan(profile, vram_free_mib=profile.audio_vram_mib,
                             ram_free_mib=0) == ("cuda", False)
    # SHORT recording + PLENTIFUL RAM -> CPU co-resident, no swap.
    assert config.choose_plan(profile, duration_seconds=5, vram_free_mib=0,
                             ram_free_mib=ram_ok) == ("cpu", False)
    assert config.choose_plan(profile, duration_seconds=threshold, vram_free_mib=0,
                             ram_free_mib=ram_ok) == ("cpu", False)
    # LONG recording (including just above the threshold) -> GPU + mandatory swap.
    assert config.choose_plan(profile, duration_seconds=threshold + 1, vram_free_mib=0,
                             ram_free_mib=ram_ok) == ("cuda", True)
    assert config.choose_plan(profile, duration_seconds=3600, vram_free_mib=0,
                             ram_free_mib=ram_ok) == ("cuda", True)
    # SHORT but RAM INSUFFICIENT -> mandatory swap. The typical value on this machine (with the
    # LM loaded free_ram is ~2957 MiB) lands exactly in this branch -> GPU + swap (correct).
    assert config.choose_plan(profile, duration_seconds=5, vram_free_mib=0,
                             ram_free_mib=ram_ok - 1) == ("cuda", True)
    assert config.choose_plan(profile, duration_seconds=5, vram_free_mib=0,
                             ram_free_mib=2957) == ("cuda", True)


def test_choose_plan_without_local_model_never_swaps():
    """On an API provider the local language model is not loaded; the GPU is free -> no swap."""
    from transcriber import config

    profile = config.select_profile(total_mib=8000)
    assert config.choose_plan(profile, vram_free_mib=0, ram_free_mib=0,
                             local_model_loaded=False) == ("cuda", False)


def test_choose_plan_uses_duration_short_cpu_long_gpu_when_vram_tight():
    """When VRAM is insufficient the DURATION decides: even with enough RAM a SHORT recording
    -> CPU (swap cost outweighs the gain), a LONG recording -> GPU + mandatory swap."""
    from transcriber import config

    profile = config.select_profile(total_mib=16311)
    vram = profile.audio_vram_mib - 1
    ram = profile.audio_ram_mib + 10_000
    threshold = config.DEFAULT_CPU_MAX_SECONDS
    assert config.choose_plan(profile, duration_seconds=threshold, vram_free_mib=vram,
                             ram_free_mib=ram) == ("cpu", False)
    assert config.choose_plan(profile, duration_seconds=threshold + 1, vram_free_mib=vram,
                             ram_free_mib=ram) == ("cuda", True)
    assert config.choose_plan(profile, duration_seconds=5, vram_free_mib=vram,
                             ram_free_mib=ram) == ("cpu", False)


def test_choose_plan_long_recording_swaps_even_cpu_max_override():
    """`cpu_max_seconds` can override the threshold; a long recording always uses GPU+swap."""
    from transcriber import config

    profile = config.select_profile(total_mib=16311)
    assert config.choose_plan(profile, duration_seconds=600, vram_free_mib=0, ram_free_mib=10**6,
                             cpu_max_seconds=1200) == ("cpu", False)
    assert config.choose_plan(profile, duration_seconds=2000, vram_free_mib=0, ram_free_mib=10**6,
                             cpu_max_seconds=1200) == ("cuda", True)


def test_choose_plan_no_duration_falls_back_to_memory_only():
    """With an unknown duration no swap is forced: enough RAM -> CPU, else mandatory swap."""
    from transcriber import config

    profile = config.select_profile(total_mib=16311)
    assert config.choose_plan(profile, duration_seconds=None, vram_free_mib=0,
                             ram_free_mib=10**6) == ("cpu", False)
    assert config.choose_plan(profile, duration_seconds=None, vram_free_mib=0,
                             ram_free_mib=0) == ("cuda", True)


def test_audio_budget_matches_measured_pipeline():
    """The VRAM margin must cover the documented components (Whisper int8 ~1.5-2.5 GB + AST
    ~0.4 GB + torch/CUDA ~0.3-0.5 GB); on 8 GB LITE the language model holds ~7.5 GB so this
    margin is not available and the plan falls back to CPU/swap."""
    from transcriber import config

    assert config.DEFAULT_AUDIO_VRAM_MIB >= 3500
    assert config.DEFAULT_AUDIO_RAM_MIB >= 4096


# -------------------------------------------------------------------- merge
def test_merge_is_chronological_without_timestamps_and_uses_speaker_labels():
    from transcriber import merge

    text = merge.merge(
        [{"start": 7, "end": 9, "text": "Did you come?"},
         {"start": 11, "end": 13, "text": "Yes, a bit late."}],
        [{"start": 3, "end": 5, "text": "footsteps"},
         {"start": 9, "end": 10.5, "text": "door closing"}],
    )
    assert text == (
        "(footsteps)\n"
        "Speaker: Did you come?\n"
        "(door closing)\n"
        "Speaker: Yes, a bit late."
    )
    assert "[" not in text


def test_merge_numbers_speakers_when_diarized():
    from transcriber import merge

    text = merge.merge(
        [{"start": 7, "end": 9, "text": "Did you come?"},
         {"start": 11, "end": 13, "text": "Yes."}],
        [],
        turns=[{"start": 7, "end": 9, "speaker": "SPEAKER_00"},
               {"start": 11, "end": 13, "speaker": "SPEAKER_01"}],
    )
    assert "Speaker 1: Did you come?" in text
    assert "Speaker 2: Yes." in text


def test_merge_can_include_timestamps_when_asked():
    from transcriber import merge

    text = merge.merge([{"start": 65, "end": 70, "text": "Hello"}], [], include_timestamps=True)
    assert text.startswith("[01:05] Speaker: Hello")


# ---------------------------------------------------------------------- asr
def test_asr_hallucination_filters():
    from transcriber import asr

    def seg(text, no_speech=0.0, logprob=0.0, compression=1.0):
        return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob,
                               compression_ratio=compression)

    assert asr._is_hallucination(seg("")) is True
    assert asr._is_hallucination(seg("   ...  ")) is True
    assert asr._is_hallucination(seg("Thanks for watching")) is True
    assert asr._is_hallucination(seg("Subscribe")) is True
    assert asr._is_hallucination(seg("okay " * 8)) is True
    assert asr._is_hallucination(seg("...", no_speech=0.95, logprob=-0.9)) is True
    assert asr._is_hallucination(seg("some text", compression=3.0)) is True
    assert asr._is_hallucination(seg("A real sentence.")) is False


def test_asr_whisper_accuracy_settings():
    src = read_text("agent/transcriber/asr.py")
    assert "condition_on_previous_text=False" in src
    assert "vad_filter=True" in src
    assert "no_speech_threshold" in src and "compression_ratio_threshold" in src


# ------------------------------------------------------------------- events
def test_events_filters_speech_labels_and_translates():
    from transcriber import events

    assert events._translate("Walk, footsteps") == "footsteps"
    assert events._translate("Door") == "door sound"
    assert "speech" in events.SPEECH_LABELS
    assert "male speech, man speaking" in events.SPEECH_LABELS
    src = read_text("agent/transcriber/events.py")
    assert "SPEECH_LABELS" in src and "_merge_repeats" in src


# ------------------------------------------------------------- worker / UI
def test_worker_exists_and_reads_json_on_stdin():
    src = read_text("agent/whisper-tools.py")
    assert "sys.stdin.read()" in src
    assert "'transcribe'" in src
    assert "from transcriber import" in src


@pytest.mark.skipif(not WORKER.is_file(), reason="worker missing")
def test_worker_rejects_missing_file():
    module = load_worker()
    with pytest.raises(ValueError):
        module.transcribe({"path": str(ROOT / "does-not-exist.wav")})


def _patch_transcriber_pipeline(monkeypatch, duration, capture):
    """Replaces the heavy model calls with fakes; captures the chosen device."""
    from transcriber import asr, audio, diarize, events, merge

    monkeypatch.setattr(audio, "to_wav16k_mono", lambda p: str(p))
    monkeypatch.setattr(audio, "duration_seconds", lambda p: duration)
    monkeypatch.setattr(audio, "split_wav", lambda p, m: [(str(p), 0.0)])

    def fake_transcribe(chunk, profile, device="cuda", **kw):
        capture["device"] = device
        return {"segments": [], "language": None}

    monkeypatch.setattr(asr, "transcribe", fake_transcribe)
    monkeypatch.setattr(asr, "unload_model", lambda: None)
    monkeypatch.setattr(events, "tag", lambda *a, **k: [])
    monkeypatch.setattr(events, "unload_events_model", lambda: None)
    monkeypatch.setattr(diarize, "diarize", lambda *a, **k: [])
    monkeypatch.setattr(diarize, "unload_pipeline", lambda: None)
    monkeypatch.setattr(merge, "merge", lambda *a, **k: "")


def test_run_auto_selects_cpu_for_short_recording(monkeypatch, tmp_path):
    """device='auto' + need_swap=True + short recording -> asr is called on the CPU."""
    from transcriber import config, main

    src = tmp_path / "short.wav"
    src.write_bytes(b"")
    capture = {}
    _patch_transcriber_pipeline(monkeypatch, 5.0, capture)
    result = main.run(str(src), profile=config.select_profile(total_mib=8000),
                      device="auto", need_swap=True)
    assert capture["device"] == "cpu"
    assert result["device"] == "cpu"


def test_run_auto_selects_cuda_for_long_recording(monkeypatch, tmp_path):
    from transcriber import config, main

    src = tmp_path / "long.wav"
    src.write_bytes(b"")
    capture = {}
    _patch_transcriber_pipeline(monkeypatch, config.DEFAULT_CPU_MAX_SECONDS + 120, capture)
    result = main.run(str(src), profile=config.select_profile(total_mib=8000),
                      device="auto", need_swap=True)
    assert capture["device"] == "cuda"
    assert result["device"] == "cuda"


def test_run_auto_uses_cuda_without_swap(monkeypatch, tmp_path):
    """need_swap=False (API/LITE) -> CUDA even for a short recording."""
    from transcriber import config, main

    src = tmp_path / "short2.wav"
    src.write_bytes(b"")
    capture = {}
    _patch_transcriber_pipeline(monkeypatch, 20.0, capture)
    result = main.run(str(src), profile=config.select_profile(total_mib=8000),
                      device="auto", need_swap=False)
    assert capture["device"] == "cuda"
    assert result["device"] == "cuda"


def test_run_explicit_device_is_not_overridden(monkeypatch, tmp_path):
    """An explicit device='cpu' disables the auto choice."""
    from transcriber import config, main

    src = tmp_path / "cpu.wav"
    src.write_bytes(b"")
    capture = {}
    _patch_transcriber_pipeline(monkeypatch, 5.0, capture)
    main.run(str(src), profile=config.select_profile(total_mib=8000),
             device="cpu", need_swap=True)
    assert capture["device"] == "cpu"


def test_cli_and_stdin_use_selected_profile_name(monkeypatch, tmp_path):
    """CLI/worker contract: `--profile` and the stdin `profile` field map the NAME to a tier;
    when the name is absent the profile stays None (transcriber falls back to VRAM)."""
    import io

    from transcriber import main as transcriber_main

    calls = []

    def fake_run(path, profile=None, **kw):
        calls.append(profile.name if profile else None)
        return {"text": "ok", "model": "m", "language": "auto",
                "profile": profile.name if profile else None}

    monkeypatch.setattr(transcriber_main, "run", fake_run)

    # stdin: 'max' -> pro.
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        '{"action":"transcribe","profile":"max","path":"x.wav"}'))
    assert transcriber_main.main(["--stdin"]) == 0
    assert calls[-1] == "pro"

    # stdin: no profile -> None (VRAM fallback).
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"action":"transcribe","path":"x.wav"}'))
    assert transcriber_main.main(["--stdin"]) == 0
    assert calls[-1] is None

    # CLI: 'lite' -> lite.
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    assert transcriber_main.main([str(wav), "--profile", "lite"]) == 0
    assert calls[-1] == "lite"

    # CLI: no profile -> None.
    assert transcriber_main.main([str(wav)]) == 0
    assert calls[-1] is None


def test_worker_accepts_auto_and_need_swap(monkeypatch, tmp_path):
    """Worker contract: 'auto' is passed through; need_swap is forwarded when present, else None."""
    module = load_worker()
    from transcriber import main as transcriber_main

    src = tmp_path / "a.wav"
    src.write_bytes(b"")
    captured = {}

    def fake_run(path, **kw):
        captured.clear()
        captured.update(kw)
        return {"text": "hello", "language": "auto", "events": [], "diarized": False,
                "model": "m"}

    monkeypatch.setattr(transcriber_main, "run", fake_run)
    module.transcribe({"path": str(src), "profile": "pc2", "device": "auto", "need_swap": True})
    assert captured["device"] == "auto"
    assert captured["need_swap"] is True
    # Missing device/need_swap -> defaults 'auto' and None.
    module.transcribe({"path": str(src), "profile": "pc2"})
    assert captured["device"] == "auto"
    assert captured["need_swap"] is None


def test_worker_profile_maps_name_to_tier(monkeypatch):
    """Worker contract: when the request carries a `profile` field the tier is chosen by NAME
    ('lite'/'pro'/'pc1'/'pc2'/'max'/'flash'); without a name the total VRAM decides."""
    module = load_worker()

    # The name wins: 'lite' -> lite tier even with plenty of VRAM (16311).
    monkeypatch.setattr(module.config, "total_vram_mib", lambda: 16311)
    assert module._profile({"profile": "lite"}).name == "lite"
    assert module._profile({"profile": "pc1"}).name == "lite"
    # 'max'/'pc2' -> pro tier even with little VRAM (8000).
    assert module._profile({"profile": "max"}).name == "pro"
    assert module._profile({"profile": "pc2"}).name == "pro"
    # With a name a valid language override is applied; the profile default ('auto') is kept.
    assert module._profile({"profile": "lite", "language": "en"}).language == "en"
    assert module._profile({"profile": "lite", "language": "auto"}).language == "auto"
    # No name: only language -> VRAM fallback (8000 -> lite; empty request -> lite).
    monkeypatch.setattr(module.config, "total_vram_mib", lambda: 8000)
    assert module._profile({"language": "en"}).name == "lite"
    assert module._profile({}).name == "lite"


def test_worker_plan_action_decides_device_and_swap(monkeypatch, tmp_path):
    """Worker contract: action='plan' returns (device, need_swap) from the measured free
    VRAM/RAM. Exercised with the pro profile (the old PC2; CPU branch enabled)."""
    module = load_worker()
    from transcriber import config

    profile = config.select_profile(total_mib=16311)
    vram, ram = profile.audio_vram_mib, profile.audio_ram_mib

    monkeypatch.setattr(config, "free_vram_mib", lambda: vram)
    monkeypatch.setattr(config, "available_ram_mib", lambda: 0)
    plan = module.plan({"profile": "pc2", "local_model_loaded": True})
    assert plan["device"] == "cuda" and plan["need_swap"] is False
    assert plan["profile"] == "pro" and plan["free_vram_mib"] == vram

    # Not enough VRAM but enough RAM -> CPU, no swap.
    monkeypatch.setattr(config, "free_vram_mib", lambda: 0)
    monkeypatch.setattr(config, "available_ram_mib", lambda: ram)
    plan = module.plan({"profile": "pc2", "local_model_loaded": True})
    assert plan["device"] == "cpu" and plan["need_swap"] is False

    # Neither fits -> mandatory swap.
    monkeypatch.setattr(config, "available_ram_mib", lambda: 0)
    plan = module.plan({"profile": "pc2", "local_model_loaded": True})
    assert plan["device"] == "cuda" and plan["need_swap"] is True

    # Local model not loaded (API) -> no swap.
    plan = module.plan({"profile": "pc2", "local_model_loaded": False})
    assert plan["device"] == "cuda" and plan["need_swap"] is False


def test_lite_profile_plan_short_cpu_long_gpu(monkeypatch, tmp_path):
    """LITE (8 GB) plan: with enough RAM, a SHORT recording uses the CPU co-resident path (no
    swap) and a LONG one uses GPU + mandatory swap; if RAM is insufficient (even a short
    recording) the swap is mandatory. With no local model (API) the GPU is free -> no swap."""
    module = load_worker()
    from transcriber import config, audio

    src = tmp_path / "recording.wav"
    src.write_bytes(b"RIFF")
    monkeypatch.setattr(config, "free_vram_mib", lambda: 0)
    monkeypatch.setattr(config, "available_ram_mib", lambda: 10 ** 6)
    monkeypatch.setattr(audio, "probe_duration_seconds", lambda path: 5.0)
    plan = module.plan({"profile": "pc1", "local_model_loaded": True, "path": str(src)})
    assert plan["device"] == "cpu" and plan["need_swap"] is False
    # LONG recording -> GPU + mandatory swap.
    monkeypatch.setattr(audio, "probe_duration_seconds", lambda path: 3600.0)
    plan = module.plan({"profile": "pc1", "local_model_loaded": True, "path": str(src)})
    assert plan["device"] == "cuda" and plan["need_swap"] is True
    # RAM insufficient -> even a SHORT recording requires the swap.
    monkeypatch.setattr(config, "available_ram_mib", lambda: 0)
    monkeypatch.setattr(audio, "probe_duration_seconds", lambda path: 5.0)
    plan = module.plan({"profile": "pc1", "local_model_loaded": True, "path": str(src)})
    assert plan["device"] == "cuda" and plan["need_swap"] is True
    # Local model not loaded (API) -> the GPU is free, no swap.
    plan = module.plan({"profile": "pc1", "local_model_loaded": False})
    assert plan["device"] == "cuda" and plan["need_swap"] is False


def test_worker_plan_action_uses_path_duration(monkeypatch, tmp_path):
    """The plan action estimates the duration from `path`: VRAM insufficient + SHORT -> CPU,
    LONG -> GPU + swap. This applies the "short -> CPU, long -> GPU" economy."""
    module = load_worker()
    from transcriber import config, audio

    profile = config.select_profile(total_mib=16311)
    src = tmp_path / "recording.wav"
    src.write_bytes(b"RIFF")
    monkeypatch.setattr(config, "free_vram_mib", lambda: 0)
    monkeypatch.setattr(config, "available_ram_mib", lambda: 10 ** 6)
    monkeypatch.setattr(audio, "probe_duration_seconds", lambda path: 5.0)
    plan = module.plan({"profile": "pc2", "local_model_loaded": True, "path": str(src)})
    assert plan["device"] == "cpu" and plan["need_swap"] is False
    monkeypatch.setattr(audio, "probe_duration_seconds", lambda path: 3600.0)
    plan = module.plan({"profile": "pc2", "local_model_loaded": True, "path": str(src)})
    assert plan["device"] == "cuda" and plan["need_swap"] is True


def test_transcription_uses_asr_environment_not_imagegen():
    """The audio worker runs in its OWN environment (venv\\asr); venv\\imagegen has no
    faster-whisper."""
    worker = read_text("agent/qwen-inference.ps1")
    assert "function Get-AsrPythonExe" in worker
    assert "venv\\asr\\Scripts\\python.exe" in worker
    plan = worker.split("function Get-AudioTranscriptionPlan")[1].split("function ")[0]
    assert "Get-AsrPythonExe" in plan
    assert "path = $path" in plan


def test_worker_maps_selected_profile_to_asr_tier():
    """The UI maps the SELECTED language-model profile to the audio-pipeline tier ('lite'->
    'lite', otherwise 'pro') and passes it as the `profile` field on both the plan and the
    transcribe requests."""
    worker = read_text("agent/qwen-inference.ps1")
    assert "function Get-AsrProfileName" in worker
    fn = worker.split("function Get-AsrProfileName")[1].split("function ")[0]
    assert "return 'lite'" in fn
    assert "return 'pro'" in fn
    assert "$planPayload.profile = $asrProfile" in worker
    assert "$payloadBase.profile = $asrProfile" in worker


def test_desktop_pipes_audio_to_worker_and_shows_transcript():
    desktop = read_text("qwen-desktop.ps1")
    worker = read_text("agent/qwen-inference.ps1")
    assert "currentTurnAudioPaths" in desktop
    assert "currentAudioPaths = @($script:currentTurnAudioPaths)" in desktop
    assert "function Invoke-TranscribeWithModelSwap" in worker
    assert "function Add-TranscriptToMessages" in worker
    assert "$JobData.currentAudioPaths" in worker
    assert "Emit 'transcript'" in worker
    assert "'transcript' {" in desktop


def test_microphone_red_button_cancels_recording():
    """While recording, the red microphone click CANCELS the recording (stop, delete, send
    nothing). Finishing and sending is done via the right-click menu or the Send button."""
    desktop = read_text("qwen-desktop.ps1")
    assert "function Start-MicrophoneCapture" in desktop
    assert "mciSendString" in desktop
    assert "open new type waveaudio alias" in desktop
    assert "function Stop-MicrophoneCapture" in desktop
    assert "function Complete-MicrophoneCapture" in desktop
    assert "function Cancel-MicrophoneCapture" in desktop
    assert "$micBtn.Add_Click({" in desktop
    click = desktop.split("$micBtn.Add_Click({")[1].split("})")[0]
    assert "Cancel-MicrophoneCapture" in click
    assert "Complete-MicrophoneCapture" not in click
    assert "$micUse.Add_Click({" in desktop and "Complete-MicrophoneCapture" in desktop
    assert "$micBtn.Add_MouseUp({" in desktop


def test_microphone_menu_has_non_speech_events_toggle():
    """The microphone menu has a "Non-speech sounds" checkbox: checked -> AST events are
    captured, unchecked -> only the speech transcript. Stored in api.json -> audioEvents and
    passed to the worker."""
    desktop = read_text("qwen-desktop.ps1")
    assert "Non-speech sounds" in desktop
    assert "$script:audioEvents" in desktop
    assert "audioEvents = [bool]$script:audioEvents" in desktop
    assert "$script:apiSettings.audioEvents = $script:audioEvents" in desktop
    worker = read_text("agent/qwen-inference.ps1")
    assert "events = $audioEvents" in worker


def test_transcription_uses_model_swap_for_local_provider():
    """Same pattern as OCR: on the local provider (language model loaded) the tier comes from
    the SELECTED profile (lite/pro); the device/swap decision is made from the REAL free
    VRAM/RAM measured when the audio job starts plus the recording duration; the model is
    unloaded only when required and restored afterwards."""
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    plan = worker.split("function Get-AudioTranscriptionPlan")[1].split("function ")[0]
    assert "(Get-ManageQwenPath) -Action stop" in block
    assert "Start-LocalModelRestore" in block
    # The plan helper calls whisper-tools.py (it decides the device/swap).
    assert "Get-WhisperPyPath" in plan and "action = 'plan'" in plan
    # The audio pipeline runs in its OWN environment: venv\asr python (not venv\imagegen).
    assert "function Get-AsrPythonExe" in worker
    assert "venv\\asr\\Scripts\\python.exe" in worker
    assert "Get-AsrPythonExe" in plan
    assert "Get-AsrPythonExe" in block
    assert "$whisperPy = Get-WhisperPyPath; $asrPy = Get-AsrPythonExe" in block
    assert "Invoke-PythonTool $payloadBase $whisperPy $asrPy" in block
    # The @('gpu','cpu') double loop is gone; the device/swap plan comes from the worker
    # (which also receives the path so it can estimate the duration).
    assert "@('gpu', 'cpu')" not in block
    assert "Get-AudioTranscriptionPlan $path" in block
    assert "path = $path" in plan
    assert "$needSwap = [bool]$plan.need_swap" in block
    assert "device = $device" in block
    assert "need_swap = $needSwap" in block
    # The profile-name decision (LITE exception) was REMOVED; LITE also swaps per VRAM/RAM.
    assert "([string]$JobData.profile) -ne 'lite'" not in worker


def test_swap_status_message_reflects_process_stop():
    """Cosmetic but important: during the swap the language model is NOT moved to RAM;
    `manage-qwen -Action stop` STOPS the process (RAM-friendly). The status text must reflect
    that (not the misleading "Moving the language model to RAM"). Code behavior is unchanged."""
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "stopping the language model" in block
    assert "Moving the language model to RAM" not in block


def test_transcription_honors_cancel_and_stop_does_not_wait_for_restore():
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "$Shared.Cancel" in block
    wait = worker.split("function Wait-LocalModelReady")[1].split("function ")[0]
    assert "if ($Shared.Cancel) { return }" in wait
    caller = worker.split("$currentAudioPaths = @($JobData.currentAudioPaths")[1].split("Automatically")[0]
    assert "Emit 'cancelled'" in caller


def test_worker_steers_model_away_from_reading_failed_audio():
    """Regression (end-to-end audio test): when no transcript is obtained (error or empty result)
    the worker does NOT leave the '[The user attached an audio file: ...]' note in the last user
    message alone. Otherwise the model takes the file for an 'attached document', tries to open it
    with read_document/read_bound_file, wastes ALL agent turns (>8) and gives the user not a single
    word; no transcript bubble is produced either. Also, if the plan says 'no swap' but no
    transcript is obtained after all, the language model is unloaded and retried ONCE with a CLEAN
    GPU/RAM."""
    worker = read_text("agent/qwen-inference.ps1")
    assert "function Set-AudioUnavailableNote" in worker
    # In BOTH failure paths of a failed transcript (empty/error result + exception) the steering
    # note is added.
    caller = worker.split("$currentAudioPaths = @($JobData.currentAudioPaths")[1].split("Automatic context")[0]
    assert caller.count("Set-AudioUnavailableNote") >= 2
    assert "read_document/read_bound_file" in worker
    # If the no-swap path fails, retry with a swap (clean GPU + more free RAM).
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "!$swapModel -and $provider -eq 'local'" in block
    # Hashtable '+' throws on a duplicate key; use clone + field overwrite.
    assert "$retryPayload = $payloadBase.Clone(); $retryPayload['device'] = 'cuda'; $retryPayload['need_swap'] = $true" in block
    assert "Wait-LocalModelStopped" in block
    # Regression: the old "continuing without a transcript" path cannot continue without the note.
    assert 'Emit \'status\' "Could not transcribe the audio; continuing without a transcript. $reason"' in caller


def test_set_audio_unavailable_note_isolated(tmp_path):
    """Set-AudioUnavailableNote is exercised in isolation: the steering note is added to the last
    user message and explicitly steers the model away from reading the file (real pwsh)."""
    worker = read_text("agent/qwen-inference.ps1")
    fn = _extract_ps_function(worker, "Set-AudioUnavailableNote")
    script = fn + """
$items = [Collections.ArrayList]::new()
[void]$items.Add(@{ role = 'user'; content = @(@{ type = 'text'; text = '[The user attached an audio file: audio.wav]' }) })
$ok = Set-AudioUnavailableNote $items 'audio.wav' 'CUDA out of memory'
'OK=' + $ok
'COUNT=' + @($items[0].content).Count
'NOTE=' + [string]$items[0].content[-1].text
"""
    out = _run_ps1(script, tmp_path)
    assert "OK=True" in out
    assert "COUNT=2" in out
    assert "NOT a document" in out
    assert "read_document" in out
    assert "audio.wav" in out


def test_audio_turn_preempts_language_model_loading():
    desktop = read_text("qwen-desktop.ps1")
    assert "function Stop-ModelSwitchForAudio" in desktop
    begin = desktop.split("function Begin-Generation {")[1].split("function Set-LastUserImage")[0]
    assert "$turnAudio" in begin and "Stop-ModelSwitchForAudio" in begin
    send = desktop.split("$send.Add_Click({")[1].split("$promptBox.Add_KeyDown")[0]
    assert "currentTurnAudioPaths.Count -gt 0" in send


def test_python_tool_subprocess_is_killed_instantly_on_stop():
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("function Invoke-PythonTool")[1].split("function ")[0]
    assert "ReadToEndAsync" in block
    assert "$Shared.Cancel" in block
    assert "$p.Kill($true)" in block
    asr = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    # The device loop was removed; the CPU fallback runs only if NOT cancelled.
    assert "if ($err -and !$Shared.Cancel) {" in asr


# ---------------------------------------------------- D2: wall-clock limit
def test_python_tool_has_wall_clock_timeout():
    """D2: the Invoke-PythonTool wait loop used to poll only for cancellation; if the subprocess
    deadlocked the whole job waited forever. There is now an upper bound (default 180 s;
    overridable by a parameter or $payload.timeoutSeconds) and when it is exceeded the subprocess
    tree is killed and an error returned; cancel behavior is preserved."""
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("function Invoke-PythonTool")[1].split("function ")[0]
    assert "function Invoke-PythonTool([hashtable]$payload, [string]$scriptPath = $null, [string]$pythonPath = $null, [int]$timeoutSeconds = 0)" in worker
    assert "$payload.timeoutSeconds" in block
    assert "if ($timeoutSeconds -le 0) { $timeoutSeconds = 180 }" in block
    assert "$deadline = [DateTime]::UtcNow.AddSeconds($timeoutSeconds)" in block
    assert "[DateTime]::UtcNow -ge $deadline" in block
    assert "return \"Tool timed out after $timeoutSeconds s.\"" in block
    # Cancel behavior is preserved (cancel is checked first, then the timeout).
    assert block.index("$Shared.Cancel") < block.index("-ge $deadline")


def test_python_tool_wall_clock_timeout_kills_subprocess(tmp_path):
    """D2 behavior: a subprocess sleeping forever is killed at the timeout and the job returns an
    error WITHOUT hanging (real pwsh + a real Python process)."""
    worker = read_text("agent/qwen-inference.ps1")
    fn = _extract_ps_function(worker, "Invoke-PythonTool")
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    script = fn + f"""
$shared = @{{ Cancel = $false }}
$sw = [Diagnostics.Stopwatch]::StartNew()
$out = Invoke-PythonTool @{{ x = 1 }} '{sleeper}' '{sys.executable}' 2
'OUT=' + $out
'ELAPSED=' + [int]$sw.Elapsed.TotalSeconds
"""
    out = _run_ps1(script, tmp_path)
    assert "Tool timed out" in out, out
    elapsed = int(out.split("ELAPSED=")[1].split()[0])
    assert elapsed < 15, out


# ------------------------------------- audio: finite timeout from recording duration
def test_transcribe_sets_finite_timeout_from_recording_duration():
    """The fixed 180 s tool wall-clock must not cut off long audio recordings (the CPU
    co-resident path runs at ~real time; GPU + swap adds latency): the transcribe request
    carries a FINITE `timeoutSeconds` derived from the recording duration, and the plan
    action returns the recording duration (`duration_seconds`) so the timeout can be
    computed from it."""
    worker = read_text("agent/qwen-inference.ps1")
    assert "function Get-AudioTimeoutSeconds" in worker
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "Get-AudioTimeoutSeconds" in block
    assert "timeoutSeconds = $audioTimeout" in block
    plan = worker.split("function Get-AudioTranscriptionPlan")[1].split("function ")[0]
    assert "duration_seconds" in plan


def test_audio_timeout_seconds_is_clamped(tmp_path):
    """Get-AudioTimeoutSeconds rule: max(600, d*4+300), capped at 3600 s; if the duration is
    unknown/0 -> 1800 s. Verified with a real pwsh."""
    worker = read_text("agent/qwen-inference.ps1")
    fn = _extract_ps_function(worker, "Get-AudioTimeoutSeconds")
    script = fn + """
'T0=' + (Get-AudioTimeoutSeconds 0)
'T5=' + (Get-AudioTimeoutSeconds 5)
'T120=' + (Get-AudioTimeoutSeconds 120)
'T600=' + (Get-AudioTimeoutSeconds 600)
'T3600=' + (Get-AudioTimeoutSeconds 3600)
"""
    out = _run_ps1(script, tmp_path)
    assert "T0=1800" in out, out
    assert "T5=600" in out, out
    assert "T120=780" in out, out
    assert "T600=2700" in out, out
    assert "T3600=3600" in out, out


# --------------------------------------------- F1/F2: context overflow guard
def test_inference_loop_guards_against_context_overflow():
    """F1/F2: tool results accumulating during a turn could push the prompt past ctx-size and,
    with --no-context-shift, the server returned an ERROR and the turn failed. At the START of
    every turn the estimated prompt size is computed; if the budget is exceeded the OLDEST tool
    outputs are shortened, and if it still does not fit the model is asked for a visible answer
    without continuing with tools. max_tokens is also capped to the remaining room."""
    worker = read_text("agent/qwen-inference.ps1")
    assert "function Estimate-MessagesTokens" in worker
    assert "function Compress-TurnMessages" in worker
    assert "$promptTokensEst = Estimate-MessagesTokens $messages" in worker
    assert "Compress-TurnMessages $messages $trimBudget" in worker
    assert "Approaching the context limit; old tool outputs were shortened." in worker
    assert "The context limit was exceeded; asking for an answer from the findings at hand without continuing with tools." in worker
    # max_tokens is capped to the remaining room; the sent body uses that value.
    assert "$maxTokens = [math]::Min($maxTokens, [math]::Max(256, $contextLimit - $promptTokensEst - $contextReserve))" in worker
    assert "max_tokens = $maxTokens" in worker
    assert "max_tokens = [int]$JobData.maxTokens" not in worker
    # Safe ending: no tools are offered.
    assert "$offeredTools = if ($forceFinalAnswer)" in worker
    assert "$forceFinalAnswer" in worker.split("function Estimate-MessagesTokens")[1]


def test_estimate_and_compress_turn_messages_isolated(tmp_path):
    """F1 behavior: the estimate grows with tool output; Compress-TurnMessages shortens the
    OLDEST over-budget tool output, keeps the system message and the LAST user message, and does
    not delete a message (the assistant tool_calls / tool pairing is not broken)."""
    worker = read_text("agent/qwen-inference.ps1")
    fn = _extract_ps_function(worker, "Estimate-MessagesTokens") + "\n\n" + \
        _extract_ps_function(worker, "Compress-TurnMessages")
    script = fn + """
$items = [Collections.ArrayList]::new()
[void]$items.Add(@{ role = 'system'; content = 'SYSTEM INSTRUCTIONS' })
[void]$items.Add(@{ role = 'user'; content = 'first question' })
[void]$items.Add(@{ role = 'assistant'; content = ''; tool_calls = @(@{ id = 'c1'; type = 'function'; function = @{ name = 'read_bound_file'; arguments = '{}' } }) })
[void]$items.Add(@{ role = 'tool'; tool_call_id = 'c1'; content = ('X' * 30000) })
[void]$items.Add(@{ role = 'user'; content = 'LAST QUESTION' })
'BEFORE=' + (Estimate-MessagesTokens $items)
$n = Compress-TurnMessages $items 500
'AFTER=' + (Estimate-MessagesTokens $items)
'TRIMMED=' + $n
'COUNT=' + $items.Count
'SYS=' + [string]$items[0].content
'LAST=' + [string]$items[4].content
'TOOL=' + [string]$items[3].content
'ARGS=' + [string]$items[2].tool_calls[0].function.arguments
"""
    out = _run_ps1(script, tmp_path)
    before = int(out.split("BEFORE=")[1].split()[0])
    after = int(out.split("AFTER=")[1].split()[0])
    assert before >= 10000, out
    assert after < before, out
    trimmed = int(out.split("TRIMMED=")[1].split()[0])
    assert trimmed >= 1, out
    assert "COUNT=5" in out
    assert "SYS=SYSTEM INSTRUCTIONS" in out
    assert "LAST=LAST QUESTION" in out
    assert "shortened" in out.split("TOOL=")[1]
    assert "ARGS={}" in out


# --------------------------------------------------- F3: audit log rotation
def test_audit_log_has_size_rotation():
    """F3: logs/audit.jsonl grew without bound; there was no size cap/rotation. When the cap
    (5 MB) is exceeded the current file is rotated to audit.1.jsonl (overwritten if present) and
    the new record goes to a clean file. The rotation is inside the same try; audit writing NEVER
    fails the turn (the existing catch swallows)."""
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("function Write-AuditLog")[1].split("$endpoint =")[0]
    assert "5MB" in block
    assert "audit.1.jsonl" in block
    assert "Move-Item" in block and "-Force" in block
    # The rotation must be INSIDE the mutex-held block and BEFORE the write (races prevented).
    after_lock = block.split("$owns = $lock.WaitOne(2000)")[1]
    assert after_lock.index("Move-Item") < after_lock.index("Add-Content")
    # try/catch is preserved: a write/rotation error is swallowed, the turn does not fail.
    assert "catch {" in block


def test_write_audit_log_rotates_when_oversized(tmp_path):
    """F3 behavior (real pwsh): an audit.jsonl over 5 MB is rotated to audit.1.jsonl before the
    next write (overwriting an existing rotation file), and the new line goes to the clean file.
    Because the file is small after the rotation, the second write does not rotate again."""
    worker = read_text("agent/qwen-inference.ps1")
    fn = _extract_ps_function(worker, "Get-AuditLock") + "\n\n" + \
        _extract_ps_function(worker, "Write-AuditLog")
    log_dir = tmp_path / "logs"
    log_path = log_dir / "audit.jsonl"
    rotated = log_dir / "audit.1.jsonl"
    script = fn + f"""
function Get-AuditLogPath {{ '{log_path}' }}
[IO.Directory]::CreateDirectory('{log_dir}') | Out-Null
# An existing rotation file (should be overwritten) + 6 MB of filled records over the cap.
[IO.File]::WriteAllText('{rotated}', 'OLD-ROTATED-CONTENT')
[IO.File]::WriteAllText('{log_path}', ('x' * 999 + "`n") * 6000)
Write-AuditLog 'read_bound_file' 'not_required' 'ok' 'a.txt' ''
Write-AuditLog 'run_command' 'asked_yes' 'ok' 'echo hi' 'C:\\tmp\\out.txt'
'WROTE=ok'
"""
    out = _run_ps1(script, tmp_path)
    assert "WROTE=ok" in out, out
    new_lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(new_lines) == 2, new_lines
    assert '"tool":"read_bound_file"' in new_lines[0]
    assert '"tool":"run_command"' in new_lines[1]
    rotated_text = rotated.read_text(encoding="utf-8")
    assert rotated_text.startswith("x" * 100), rotated_text[:120]
    assert "OLD-ROTATED-CONTENT" not in rotated_text
    assert len(rotated_text.splitlines()) == 6000


# --------------------------------------------- F4: reasoning repeat guard
def test_reasoning_repeat_guard_uses_same_threshold():
    """F4: thinking is unlimited on LITE; because the runaway detector looked only at the visible
    content, if the model started repeating in the reasoning stream it spun to the 8192-token cap
    without a visible answer. The SAME threshold/pattern is applied to the reasoning accumulation
    and the stream is stopped safely; if there is no visible answer the existing 'empty answer'
    path (which does not look at finishReason) still applies unchanged."""
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("if ($delta.reasoning_content) {")[1].split("if ($delta.content) {")[0]
    assert "(Test-RepeatedOutput $result.reasoning)" in block
    assert "Remove-RepeatedTail $result.reasoning" in block
    assert "$stopForRepeat = $true" in block
    assert "$finished = $true" in block
    # The guard is limited to the local stream; remote providers are unaffected (same as content).
    assert "$provider -eq 'local'" in block
    # The empty-answer path still looks at the visible content; a reasoning repeat does not break it.
    assert 'IsNullOrWhiteSpace($turn.content)' in worker


class ReasoningRepeatHandler(BaseHTTPRequestHandler):
    """SSE: sends only reasoning_content and repeats the same word 6+ times; [DONE] is not sent
    and the connection stays open. Without the F4 guard the client would spin to 8192 tokens or
    wait for the 120 s silence timeout."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for _ in range(400):
                chunk = {"choices": [{"index": 0, "delta": {"reasoning_content": "thinking "}}]}
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.flush()
            time.sleep(60)
        except OSError:
            pass

    def log_message(self, *args):
        pass


def test_reasoning_repeat_stops_stream_early(tmp_path):
    """F4 behavior (real pwsh + a fake SSE server): if a repeat in the reasoning stream is not
    stopped early, LITE spins to 8192 tokens with no visible answer. The same threshold/pattern
    is applied to the reasoning; the stream closes safely with 'repeat', there is no visible
    answer (so the existing empty-answer/retry path can run) and no exception leaks."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), ReasoningRepeatHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        script = f"""
$job = @{{ messages = @(@{{ role = 'user'; content = 'hi' }}); compactOnly = $true; model = 'test'; contextLimit = 32768; maxTokens = 8192; agent = $false; internet = $false; office = $false; documentCreate = $false }}
$shared = @{{ Cancel = $false }}
. '{ROOT}\\agent\\qwen-inference.ps1' $job $shared | Out-Null
$endpoint = 'http://127.0.0.1:{server.server_port}/v1/chat/completions'
$script:http = [Net.Http.HttpClient]::new(); $script:http.Timeout = [Threading.Timeout]::InfiniteTimeSpan
$turn = @{{ reasoning = ''; content = ''; usage = $null; toolParts = @{{}}; finishReason = $null }}
$sw = [Diagnostics.Stopwatch]::StartNew()
$threw = ''
try {{ Invoke-ChatCompletion @{{ messages = @() }} $shared $false $turn | Out-Null }} catch {{ $threw = [string]$_.Exception.GetBaseException().Message }}
@{{ content = $turn.content; reasoningLen = $turn.reasoning.Length; finish = $turn.finishReason; threw = $threw; seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1) }} | ConvertTo-Json -Compress
"""
        out = _run_ps1(script, tmp_path)
    finally:
        server.shutdown()
    got = json.loads(out.strip().splitlines()[-1])
    assert got["finish"] == "repeat", got
    assert got["content"] == "", got
    assert got["threw"] == "", got
    assert got["reasoningLen"] < 2000, got
    assert got["seconds"] < 15, got


def test_stop_cancels_work_but_never_kills_an_in_flight_model_load():
    desktop = read_text("qwen-desktop.ps1")
    cancel = desktop.split("function Cancel-Generation {")[1].split("function Stop-LocalQwen")[0]
    assert "switchProcess" not in cancel
    assert "pendingModelChange" not in cancel


# ------------------------------------------------------------------ installer
def test_installer_builds_asr_venv_and_downloads_models():
    """Engine is faster-whisper (Whisper large-v3) + AST. The installer creates venv\\asr and
    prefetches models into the HF cache (whisper.cpp and Qwen3-ASR removed)."""
    installer = read_text("scripts/install-mamidlabs.ps1")
    assert "venv\\asr" in installer
    assert "Systran/faster-whisper-large-v3" in installer
    assert "asr       = { Invoke-Asr }" in installer
    assert "whisper-cli.exe" not in installer
    assert "ggml-large-v3" not in installer
    assert "whisperCuda" not in installer
    assert "Qwen/Qwen3-ASR" not in installer
    assert "models\\qwen3-asr" not in installer
    asr_req = read_text("requirements/asr.txt")
    assert "faster-whisper" in asr_req and "pyannote.audio" in asr_req


def test_old_audio_workers_are_removed():
    assert not (ROOT / "agent" / "qwen3-asr-tools.py").exists()
    assert (ROOT / "agent" / "whisper-tools.py").is_file()


def test_installer_known_stages_include_asr_after_python():
    installer = read_text("scripts/install-mamidlabs.ps1")
    stages = installer.split("$script:KnownStages = @(")[1].split(")")[0]
    assert "'asr'" in stages and "'whisper'" not in stages
    order = installer.split("$stages = [ordered]@{")[1]
    assert order.index("python") < order.index("asr       = { Invoke-Asr }")


def _extract_ps_function(source: str, name: str) -> str:
    """Extracts the `function <name> { ... }` body with a simple brace counter."""
    start = source.index(f"function {name}")
    depth = 0
    started = False
    for index in range(start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
            started = True
        elif char == "}":
            depth -= 1
            if started and depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"end of {name} not found")


def _run_ps1(script: str, tmp_path: Path) -> str:
    """Runs the script with a real pwsh via -File (text is safe there)."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    script_path = tmp_path / "test.ps1"
    script_path.write_text(script, encoding="utf-8-sig")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script_path)],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_asr_profile_name_ignores_non_local_provider(tmp_path):
    """A2 regression: in API mode (no local model) `$JobData.profile` can come from the
    default 'max' rather than the selected local profile; even if the user's saved
    localProfile is 'lite', ASR must not be forced to 'pro'. Get-AsrProfileName must return
    $null for a non-local provider so the worker picks the tier from real VRAM
    (config.select_profile); local + lite -> 'lite', local + max/flash -> 'pro'.
    The function is exercised in isolation in a real pwsh."""
    worker = read_text("agent/qwen-inference.ps1")
    fn = _extract_ps_function(worker, "Get-AsrProfileName")
    script = fn + """

function Ask([string]$prov, $prof) {
    $script:provider = $prov
    $script:JobData = [pscustomobject]@{ profile = $prof }
    '[' + (Get-AsrProfileName) + ']'
}
Ask 'openai' 'max'
Ask 'claude' 'flash'
Ask 'custom' 'lite'
Ask 'local' 'lite'
Ask 'local' 'max'
Ask 'local' 'flash'
Ask 'local' 'pc1'
Ask 'local' ''
"""
    out = _run_ps1(script, tmp_path)
    assert out.split() == ["[]", "[]", "[]", "[lite]", "[pro]", "[pro]", "[lite]", "[]"]


def test_transcribe_cpu_fallback_overrides_device_without_duplicate_key():
    """PowerShell hashtable '+' throws on a DUPLICATE key ('device'/'need_swap' already
    present), so the CPU fallback payload cannot be merged with '+'. It must clone and
    overwrite the field; otherwise a failed CUDA transcription never falls back to CPU
    and the transcript silently disappears ("Item has already been added. Key: device")."""
    worker = read_text("agent/qwen-inference.ps1")
    assert "$payloadBase + @{" not in worker
    assert "$cpuPayload['device'] = 'cpu'" in worker
