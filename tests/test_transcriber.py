"""Audio transcription pipeline (agent/transcriber) + UI/worker contracts.

Engine: faster-whisper large-v3 + AST audio events + pyannote speaker diarization.
Qwen3-ASR and whisper.cpp were fully removed. Heavy tests are skipped when the model/
environment is unavailable; the contracts are always checked.
"""
import importlib.util
import json
import os
import shutil
import socket
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
    """On an API provider the local model is neither loaded nor required; the GPU is free -> no swap."""
    from transcriber import config

    profile = config.select_profile(total_mib=8000)
    assert config.choose_plan(profile, vram_free_mib=0, ram_free_mib=0,
                             local_model_required=False, local_model_loaded=False) == ("cuda", False)


def test_choose_plan_required_but_not_resident_forces_swap():
    """Regression (real failure): when an audio turn cancels a model LOAD
    (Stop-ModelSwitchForAudio) the language model is NOT in VRAM right now but IS required at the
    end of the turn. Free VRAM looks large; the plan must NOT say "no swap", otherwise the ASR
    models (~3.9 GB) take the 27B's place, the model is not restored and the next load is rejected
    with "not enough free VRAM". Required + not resident -> ('cuda', True): drop + stop + restore."""
    from transcriber import config

    profile = config.select_profile(total_mib=16311)
    # Even with plenty of free VRAM (model not loaded) and RAM the swap is MANDATORY.
    assert config.choose_plan(profile, duration_seconds=5, vram_free_mib=10 ** 6,
                              ram_free_mib=10 ** 6, local_model_required=True,
                              local_model_loaded=False) == ("cuda", True)
    # The same rule holds for LITE (8 GB).
    lite = config.select_profile(total_mib=8000)
    assert config.choose_plan(lite, duration_seconds=5, vram_free_mib=10 ** 6,
                              ram_free_mib=10 ** 6, local_model_required=True,
                              local_model_loaded=False) == ("cuda", True)
    # When the model really IS loaded the rule is UNCHANGED (VRAM/RAM decides as before).
    assert config.choose_plan(profile, duration_seconds=5, vram_free_mib=10 ** 6,
                              ram_free_mib=10 ** 6, local_model_required=True,
                              local_model_loaded=True) == ("cuda", False)


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


def test_events_load_failure_does_not_break_transcription():
    """If the AST model cannot be loaded (e.g. the HF cache is missing) the WHOLE transcription
    must NOT fail; the events are skipped. Regression: `_load_events_model` was called OUTSIDE a
    try; when the app did not set HF_HOME at runtime (the default cache has no AST) voice input
    failed completely."""
    import re

    src = read_text("agent/transcriber/events.py")
    assert re.search(
        r"try:\s*\n\s*extractor, model = _load_events_model\(profile\)\s*\n"
        r"\s*except Exception:\s*\n\s*return \[\]",
        src,
    ), "events.tag must guard the AST model load with try/except"


def test_events_tag_returns_empty_when_model_missing(monkeypatch):
    """Behavior: when the events model cannot be loaded `tag` does not leak an exception and
    returns an empty list."""
    from transcriber import events

    def boom(profile):
        raise OSError("no model")

    monkeypatch.setattr(events, "_load_events_model", boom)
    profile = SimpleNamespace(events=True, events_model="MIT/ast-x", name="pro")
    assert events.tag("missing.wav", profile) == []


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


def test_worker_exposes_serve_and_unload_contract():
    """Warm (persistent) worker contract: `--serve` + `unload` + ping. Loads NO model."""
    src = read_text("agent/whisper-tools.py")
    assert "--serve" in src
    assert "def serve(" in src
    assert "'unload'" in src
    assert "def _dispatch(" in src
    assert "def _parent_gone(" in src


@pytest.mark.skipif(not WORKER.is_file(), reason="worker missing")
def test_worker_dispatch_ping_unknown_and_unload(monkeypatch):
    """`_dispatch`: ping -> ok; unknown action -> error; unload -> drops the loaded models
    (the process STAYS ALIVE, only the models are dropped). Loads NO model."""
    module = load_worker()
    assert module._dispatch({"action": "ping"})["ok"] is True
    assert "error" in module._dispatch({"action": "no-such-action"})

    from transcriber import asr, diarize, events
    calls = []
    monkeypatch.setattr(asr, "unload_model", lambda: calls.append("asr"))
    monkeypatch.setattr(events, "unload_events_model", lambda: calls.append("events"))
    monkeypatch.setattr(diarize, "unload_pipeline", lambda: calls.append("diarize"))
    out = module._dispatch({"action": "unload"})
    assert out["ok"] is True
    assert calls == ["asr", "events", "diarize"]


@pytest.mark.skipif(not WORKER.is_file(), reason="worker missing")
def test_parent_gone_detects_live_and_dead_pid():
    """When the parent (app) closes the worker must exit itself: live pid -> False, dead pid ->
    True, 0 -> monitoring off (False)."""
    module = load_worker()
    assert module._parent_gone(os.getpid()) is False
    assert module._parent_gone(999999999) is True
    assert module._parent_gone(0) is False


@pytest.mark.skipif(not WORKER.is_file(), reason="worker missing")
def test_serve_mode_ping_and_idle_exit(tmp_path):
    """`--serve` in a real process: state file + ping + unknown action + self-exit when idle +
    state-file cleanup. Loads NO model (imports/protocol only)."""
    state = tmp_path / "asr-server.json"
    proc = subprocess.Popen(
        [sys.executable, str(WORKER), "--serve", "--state-file", str(state),
         "--parent-pid", "0", "--idle-timeout", "2"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ROOT / "agent"))
    try:
        deadline = time.time() + 40
        info = None
        while time.time() < deadline:
            if state.is_file():
                try:
                    info = json.loads(state.read_text(encoding="utf-8"))
                    break
                except Exception:
                    pass
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        assert info and info.get("port"), "worker did not publish the state file"

        def request(payload):
            with socket.create_connection(("127.0.0.1", info["port"]), timeout=10) as conn:
                conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                return json.loads(buf.decode("utf-8"))

        assert request({"action": "ping"})["ok"] is True
        assert "error" in request({"action": "no-such-action"})

        # After the idle timeout (2 s) the process exits itself and removes the state file.
        deadline = time.time() + 20
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.3)
        assert proc.poll() is not None, "worker did not exit itself when idle"
        assert not state.exists(), "state file was not cleaned up"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


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

    # API provider: the local model is neither loaded nor required -> no swap.
    plan = module.plan({"profile": "pc2", "local_model_required": False, "local_model_loaded": False})
    assert plan["device"] == "cuda" and plan["need_swap"] is False
    # Local provider + model NOT currently loaded (e.g. an audio turn cancelled the load) -> swap is
    # MANDATORY; after the transcription the audio models are dropped and the model is restored.
    plan = module.plan({"profile": "pc2", "local_model_required": True, "local_model_loaded": False})
    assert plan["device"] == "cuda" and plan["need_swap"] is True


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
    # API provider: the local model is neither loaded nor required -> no swap.
    plan = module.plan({"profile": "pc1", "local_model_required": False, "local_model_loaded": False})
    assert plan["device"] == "cuda" and plan["need_swap"] is False
    # Local provider + model NOT currently loaded (e.g. an audio turn cancelled the load) -> swap is
    # MANDATORY: after the transcription the audio models are dropped and the model is restored.
    plan = module.plan({"profile": "pc1", "local_model_required": True, "local_model_loaded": False})
    assert plan["device"] == "cuda" and plan["need_swap"] is True


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
    worker = read_text("agent/mamidlabs-inference.ps1")
    assert "function Get-AsrPythonExe" in worker
    assert "venv\\asr\\Scripts\\python.exe" in worker
    plan = worker.split("function Get-AudioTranscriptionPlan")[1].split("function ")[0]
    assert "Get-AsrPythonExe" in plan
    assert "path = $path" in plan


def test_asr_worker_uses_install_hf_cache():
    """The audio worker must use the INSTALL HF cache (root\\tools\\hf-home). Regression: the app
    never set HF_HOME at runtime; the worker fell back to the DEFAULT HF cache, the AST audio-event
    model was not there, so the events model could not be loaded and ALL voice input failed. The
    installer uses the same path (install-mamidlabs.ps1)."""
    worker = read_text("agent/mamidlabs-inference.ps1")
    assert "function Get-HfHome" in worker
    assert "tools\\hf-home" in worker
    # The one-shot python tool and the persistent worker both get HF_HOME.
    py = worker.split("function Invoke-PythonTool")[1].split("function ")[0]
    assert "HF_HOME" in py and "Get-HfHome" in py
    srv = worker.split("function Start-AsrServerProcess")[1].split("function ")[0]
    assert "HF_HOME" in srv and "Get-HfHome" in srv
    # It is also set at app startup (so every child reached through the UI inherits it).
    desktop = read_text("mamidlabs-desktop.ps1")
    assert "$env:HF_HOME = Join-Path $root 'tools\\hf-home'" in desktop


def test_python_tool_sets_hf_home_for_child(tmp_path):
    """Behavior: Invoke-PythonTool gives the child process HF_HOME=<root>\\tools\\hf-home, so the
    audio worker (and the other python tools) use the install cache offline (real pwsh + real
    Python)."""
    worker = read_text("agent/mamidlabs-inference.ps1")
    probe = tmp_path / "probe.py"
    # The child writes HF_HOME both to stdout and to a marker file next to the probe, so the
    # assertion does not depend on pwsh's own stdout (which occasionally came back empty on a
    # loaded runner and made this test flaky).
    probe.write_text(
        "import os, pathlib\n"
        "value = os.environ.get('HF_HOME', '')\n"
        "print(value)\n"
        "pathlib.Path(__file__).with_name('hf_home.txt').write_text(value, encoding='utf-8')\n",
        encoding="utf-8")
    script = _invoke_python_tool_harness(worker) + f"""
$shared = @{{ Cancel = $false }}
$out = Invoke-PythonTool @{{}} '{probe}' '{sys.executable}' 15
'HF=' + $out.Trim()
"""
    out = _run_ps1(script, tmp_path)
    marker = tmp_path / "hf_home.txt"
    assert marker.is_file(), out
    value = marker.read_text(encoding="utf-8").strip().replace("/", "\\").lower()
    assert value.endswith(r"tools\hf-home"), value
    # pwsh's own output must agree when it is available.
    lines = [line for line in out.splitlines() if line.startswith("HF=")]
    if lines:
        assert lines[0][3:].replace("/", "\\").lower().endswith(r"tools\hf-home")


def test_worker_maps_selected_profile_to_asr_tier():
    """The UI maps the SELECTED language-model profile to the audio-pipeline tier ('lite'->
    'lite', otherwise 'pro') and passes it as the `profile` field on both the plan and the
    transcribe requests."""
    worker = read_text("agent/mamidlabs-inference.ps1")
    assert "function Get-AsrProfileName" in worker
    fn = worker.split("function Get-AsrProfileName")[1].split("function ")[0]
    assert "return 'lite'" in fn
    assert "return 'pro'" in fn
    assert "$planPayload.profile = $asrProfile" in worker
    assert "$payloadBase.profile = $asrProfile" in worker


def test_desktop_pipes_audio_to_worker_and_shows_transcript():
    desktop = read_text("mamidlabs-desktop.ps1")
    worker = read_text("agent/mamidlabs-inference.ps1")
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
    desktop = read_text("mamidlabs-desktop.ps1")
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
    desktop = read_text("mamidlabs-desktop.ps1")
    assert "Non-speech sounds" in desktop
    assert "$script:audioEvents" in desktop
    assert "audioEvents = [bool]$script:audioEvents" in desktop
    assert "$script:apiSettings.audioEvents = $script:audioEvents" in desktop
    worker = read_text("agent/mamidlabs-inference.ps1")
    assert "events = $audioEvents" in worker


def test_transcription_uses_model_swap_for_local_provider():
    """Same pattern as OCR: on the local provider (language model loaded) the tier comes from
    the SELECTED profile (lite/pro); the device/swap decision is made from the REAL free
    VRAM/RAM measured when the audio job starts plus the recording duration; the model is
    unloaded only when required and restored afterwards."""
    worker = read_text("agent/mamidlabs-inference.ps1")
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    plan = worker.split("function Get-AudioTranscriptionPlan")[1].split("function ")[0]
    assert "(Get-ManageMamidLabsPath) -Action stop" in block
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
    `manage-mamidlabs -Action stop` STOPS the process (RAM-friendly). The status text must reflect
    that (not the misleading "Moving the language model to RAM"). Code behavior is unchanged."""
    worker = read_text("agent/mamidlabs-inference.ps1")
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "stopping the language model" in block
    assert "Moving the language model to RAM" not in block


def test_transcription_honors_cancel_and_stop_does_not_wait_for_restore():
    worker = read_text("agent/mamidlabs-inference.ps1")
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "$Shared.Cancel" in block
    wait = worker.split("function Wait-LocalModelReady")[1].split("function ")[0]
    assert "if ($Shared.Cancel) { return }" in wait
    caller = worker.split("$currentAudioPaths = @($JobData.currentAudioPaths")[1].split("Automatically")[0]
    assert "Emit 'cancelled'" in caller


def test_inference_uses_warm_asr_server_with_fallback():
    """Transcription uses the WARM (persistent) worker: the fixed cost repeated on every audio
    turn (Python start-up + torch/transformers/faster-whisper imports; measured ~10 s) is paid
    ONCE per session. If the worker is UNREACHABLE/on a transport error we FALL BACK to the
    one-shot `Invoke-PythonTool` path, so the audio pipeline always works (behavior preserved).
    Also, before the language model is reloaded into VRAM the audio models are dropped and the
    worker is STOPPED (the remaining CUDA context ~140 MiB could eat the 27B's thin margin);
    it is restarted IN THE BACKGROUND after the turn so the next turn is warm."""
    worker = read_text("agent/mamidlabs-inference.ps1")
    for fn in ("Get-AsrServerStatePath", "Send-AsrServerLine", "Get-AsrServerEndpoint",
               "Start-AsrServerProcess", "Start-AsrServer", "Start-AsrServerAsync",
               "Ensure-AsrServer", "Stop-AsrServer", "Unload-AsrServerModels",
               "Invoke-AsrServerRequest"):
        assert f"function {fn}" in worker
    # Plan: warm worker first, otherwise the one-shot path.
    plan = worker.split("function Get-AudioTranscriptionPlan")[1].split("function ")[0]
    assert "Invoke-AsrServerRequest $planPayload 120" in plan
    assert "if (!$raw) { $raw = Invoke-PythonTool $planPayload $whisperPy $asrPy }" in plan
    # Transcription: the primary (GPU) + CPU fallback + "unload the model and retry" paths ALL
    # try the warm worker first and fall back to the one-shot path.
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "Invoke-AsrServerRequest $payloadBase $audioTimeout" in block
    assert "if (!$candidate) { $candidate = Invoke-PythonTool $payloadBase $whisperPy $asrPy }" in block
    assert "Invoke-AsrServerRequest $cpuPayload $audioTimeout" in block
    assert "Invoke-AsrServerRequest $retryPayload $audioTimeout" in block
    # BEFORE the reload: drop + stop; AFTER: restart in the background.
    assert block.index("Unload-AsrServerModels") < block.index("Start-LocalModelRestore")
    assert block.index("Stop-AsrServer") < block.index("Start-LocalModelRestore")
    assert "Start-AsrServerAsync" in block
    # The state file lives under the install root (nothing is written outside it; no uninstaller impact).
    assert "logs\\asr-server.json" in worker


def test_audio_turn_releases_asr_and_restores_model_when_local_model_is_down(tmp_path):
    """Regression (real failure): when an audio turn cancels a language-model LOAD
    (`Stop-ModelSwitchForAudio`) the plan may say "no swap" because free VRAM looks large. So at
    the end of the turn it is checked AGAIN: on the local provider, if the language model is NOT in
    VRAM the audio models are dropped, the warm worker is stopped and the model is restored.
    Otherwise the ASR holds ~3.9 GB, the model is not restored and the next load is rejected with
    "not enough free VRAM". `Test-LocalModelResident` measures whether the model is loaded right now
    via /health."""
    worker = read_text("agent/mamidlabs-inference.ps1")
    assert "function Test-LocalModelResident" in worker
    resident = worker.split("function Test-LocalModelResident")[1].split("function ")[0]
    assert "8080/health" in resident
    # The plan carries TWO SEPARATE fields: 'is it required' (local provider) + 'is it loaded now'.
    plan = worker.split("function Get-AudioTranscriptionPlan")[1].split("function ")[0]
    assert "$localProvider = ($provider -eq 'local')" in plan
    assert "local_model_required = $localProvider" in plan
    assert "local_model_loaded = ($localProvider -and (Test-LocalModelResident))" in plan
    # End-of-turn safety net: even with NO swap plan, drop + restore when the model is not loaded.
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "$releaseAsr = $swapModel -or ($provider -eq 'local' -and !(Test-LocalModelResident))" in block
    assert "if ($releaseAsr) {" in block
    assert "if ($releaseAsr -and !$Shared.Cancel) { Start-AsrServerAsync }" in block
    # Real pwsh: the probe returns True on a healthy reply and False on a closed/failing endpoint
    # (no exception escapes).
    fn = _extract_ps_function(worker, "Test-LocalModelResident")
    script = fn + r"""
$script:mode = 'ok'
function Invoke-RestMethod { param($Uri, $TimeoutSec, $ErrorAction) if ($script:mode -eq 'ok') { [pscustomobject]@{ status = 'ok' } } else { throw 'closed' } }
'OK=' + (Test-LocalModelResident)
$script:mode = 'down'
'DOWN=' + (Test-LocalModelResident)
"""
    out = _run_ps1(script, tmp_path)
    assert "OK=True" in out, out
    assert "DOWN=False" in out, out


def test_worker_steers_model_away_from_reading_failed_audio():
    """Regression (end-to-end audio test): when no transcript is obtained (error or empty result)
    the worker does NOT leave the '[The user attached an audio file: ...]' note in the last user
    message alone. Otherwise the model takes the file for an 'attached document', tries to open it
    with read_document/read_bound_file, wastes ALL agent turns (>8) and gives the user not a single
    word; no transcript bubble is produced either. Also, if the plan says 'no swap' but no
    transcript is obtained after all, the language model is unloaded and retried ONCE with a CLEAN
    GPU/RAM."""
    worker = read_text("agent/mamidlabs-inference.ps1")
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
    worker = read_text("agent/mamidlabs-inference.ps1")
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


def test_audio_turn_hides_bound_file_tools_unless_file_intent():
    """On a turn with an audio attachment the bound-folder FILE tools are NOT offered: the audio
    is already transcribed and turned into text, so the small model must not try to
    find/read/write the audio FILE with list_bound_files/read_bound_file/write_bound_file (it
    wasted turns on that in the UI test). If the user explicitly asks for a file (Test-FileIntent)
    the tools are still offered."""
    worker = read_text("agent/mamidlabs-inference.ps1")
    assert "$audioAttachedThisTurn = @($job.currentAudioPaths" in worker
    assert "if (!($audioAttachedThisTurn -and !(Test-FileIntent $userText)))" in worker
    guard = worker.split("$audioAttachedThisTurn = @($job.currentAudioPaths")[1]
    guard = guard.split("if (Test-CommandIntent")[0]
    assert "list_bound_files" in guard and "read_bound_file" in guard and "write_bound_file" in guard


def test_transcript_note_tells_model_not_to_open_the_audio_file(tmp_path):
    """A successful transcript also tells the model NOT to open the audio FILE with tools
    (symmetric to Set-AudioUnavailableNote); otherwise it takes it for an attached document and
    wastes turns. Runs with a real pwsh."""
    worker = read_text("agent/mamidlabs-inference.ps1")
    fn = _extract_ps_function(worker, "Add-TranscriptToMessages")
    script = fn + """
$items = [Collections.ArrayList]::new()
[void]$items.Add(@{ role = 'user'; content = 'Transcribe the audio.' })
$ok = Add-TranscriptToMessages $items 'Speaker: hello' 'audio.wav'
'OK=' + $ok
'TEXT=' + [string]$items[0].content
"""
    out = _run_ps1(script, tmp_path)
    assert "OK=True" in out
    assert "read_bound_file" in out
    assert "audio.wav" in out
    assert "Speaker: hello" in out


def test_audio_turn_preempts_language_model_loading():
    desktop = read_text("mamidlabs-desktop.ps1")
    assert "function Stop-ModelSwitchForAudio" in desktop
    begin = desktop.split("function Begin-Generation {")[1].split("function Set-LastUserImage")[0]
    assert "$turnAudio" in begin and "Stop-ModelSwitchForAudio" in begin
    send = desktop.split("$send.Add_Click({")[1].split("$promptBox.Add_KeyDown")[0]
    assert "currentTurnAudioPaths.Count -gt 0" in send


def test_python_tool_subprocess_is_killed_instantly_on_stop():
    worker = read_text("agent/mamidlabs-inference.ps1")
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
    worker = read_text("agent/mamidlabs-inference.ps1")
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
    worker = read_text("agent/mamidlabs-inference.ps1")
    fn = _invoke_python_tool_harness(worker)
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
    worker = read_text("agent/mamidlabs-inference.ps1")
    assert "function Get-AudioTimeoutSeconds" in worker
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "Get-AudioTimeoutSeconds" in block
    assert "timeoutSeconds = $audioTimeout" in block
    plan = worker.split("function Get-AudioTranscriptionPlan")[1].split("function ")[0]
    assert "duration_seconds" in plan


def test_audio_timeout_seconds_is_clamped(tmp_path):
    """Get-AudioTimeoutSeconds rule: max(600, d*4+300), capped at 3600 s; if the duration is
    unknown/0 -> 1800 s. Verified with a real pwsh."""
    worker = read_text("agent/mamidlabs-inference.ps1")
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
    worker = read_text("agent/mamidlabs-inference.ps1")
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
    worker = read_text("agent/mamidlabs-inference.ps1")
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
    worker = read_text("agent/mamidlabs-inference.ps1")
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
    worker = read_text("agent/mamidlabs-inference.ps1")
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
    worker = read_text("agent/mamidlabs-inference.ps1")
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
. '{ROOT}\\agent\\mamidlabs-inference.ps1' $job $shared | Out-Null
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
    desktop = read_text("mamidlabs-desktop.ps1")
    cancel = desktop.split("function Cancel-Generation {")[1].split("function Stop-LocalMamidLabs")[0]
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


def _invoke_python_tool_harness(worker: str) -> str:
    """The functions needed to run Invoke-PythonTool in isolation + a Get-InstallRoot stub.

    Invoke-PythonTool now hands the child process HF_HOME (the install cache); so the isolated
    run must also define Get-HfHome (and the Get-InstallRoot it depends on)."""
    return ("function Get-InstallRoot { 'C:\\fake\\root' }\n"
            + _extract_ps_function(worker, "Get-HfHome") + "\n"
            + _extract_ps_function(worker, "Invoke-PythonTool"))


# ------------------------------- Empty/truncated visible-answer recovery (no thinking)
def test_empty_answer_recovery_disables_thinking(tmp_path):
    """On a hard math question the model spent the entire output budget (8192) on thinking
    and produced no visible answer, or left only a broken fragment ("İntegral"); the bubble
    had no answer. The recovery path retries with `enable_thinking = $false` (the only
    guaranteed way, since LITE ignores reasoning_effort) and also asks /no_think so the
    model writes the answer directly. Trigger: the visible answer is EMPTY or was cut off
    at the output cap leaving a broken fragment."""
    worker = read_text("agent/mamidlabs-inference.ps1")
    assert "function Set-NoThinkingRequest" in worker
    assert "$body.chat_template_kwargs = @{ enable_thinking = $false }" in worker
    # Trigger: visible answer EMPTY or cut off ('length') leaving a broken fragment.
    assert "$emptyVisible = [string]::IsNullOrWhiteSpace($turn.content)" in worker
    assert "$truncatedFragment = $turn.finishReason -eq 'length' -and $turn.content.Length -lt 400" in worker
    assert "($emptyVisible -or $truncatedFragment)" in worker
    # The recovery body TURNS THINKING OFF (not low effort) and asks /no_think.
    block = worker.split("($emptyVisible -or $truncatedFragment)")[1].split("$turn = $retryTurn")[0]
    assert "Set-NoThinkingRequest $retryBody" in block
    assert "Set-LowEffortRequest $retryBody" not in block
    assert "/no_think" in block
    # Behavior: when the function is called the request body turns thinking off (real pwsh).
    fn = _extract_ps_function(worker, "Set-NoThinkingRequest")
    script = fn + """
$b = @{ messages = @(); max_tokens = 8192 }
Set-NoThinkingRequest $b
'ENABLED=' + [string]$b.chat_template_kwargs.enable_thinking
'BODY=' + ($b | ConvertTo-Json -Depth 6 -Compress)
"""
    out = _run_ps1(script, tmp_path)
    assert "ENABLED=False" in out, out
    assert '"enable_thinking":false' in out, out


def test_asr_profile_name_ignores_non_local_provider(tmp_path):
    """A2 regression: in API mode (no local model) `$JobData.profile` can come from the
    default 'max' rather than the selected local profile; even if the user's saved
    localProfile is 'lite', ASR must not be forced to 'pro'. Get-AsrProfileName must return
    $null for a non-local provider so the worker picks the tier from real VRAM
    (config.select_profile); local + lite -> 'lite', local + max/flash -> 'pro'.
    The function is exercised in isolation in a real pwsh."""
    worker = read_text("agent/mamidlabs-inference.ps1")
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
    worker = read_text("agent/mamidlabs-inference.ps1")
    assert "$payloadBase + @{" not in worker
    assert "$cpuPayload['device'] = 'cpu'" in worker
