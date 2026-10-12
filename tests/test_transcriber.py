"""Audio transcription pipeline (agent/transcriber) + UI/worker contracts.

Engine: faster-whisper large-v3 + AST audio events + pyannote speaker diarization.
Qwen3-ASR and whisper.cpp were fully removed. Heavy tests are skipped when the model/
environment is unavailable; the contracts are always checked.
"""
import importlib.util
import sys
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
    from transcriber import config

    pc2 = config.select_profile(total_mib=16311)
    assert pc2.name == "pc2" and pc2.compute_type == "float16" and pc2.load_mode == "parallel"
    pc1 = config.select_profile(total_mib=8000)
    assert pc1.name == "pc1" and pc1.compute_type == "int8_float16" and pc1.load_mode == "sequential"
    assert pc1.chunk_minutes > 0 and pc2.chunk_minutes == 0
    # English edition: standard large-v3, automatic language detection.
    assert pc2.language == "auto"
    assert "faster-whisper-large-v3" in pc2.whisper_model


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
    """PC2 (CPU branch enabled) priority: (1) fits together -> GPU no swap, (2) fits in CPU
    RAM -> CPU no swap, (3) neither fits -> mandatory swap."""
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


def test_choose_plan_lite_uses_cpu_branch_for_short_long_uses_gpu():
    """LITE also uses the same unified logic (RAM measured as sufficient): VRAM insufficient +
    SHORT -> CPU/no swap, LONG -> GPU + mandatory swap."""
    from transcriber import config

    profile = config.select_profile(total_mib=8000)
    assert config.choose_plan(profile, vram_free_mib=profile.audio_vram_mib,
                             ram_free_mib=0) == ("cuda", False)
    threshold = config.DEFAULT_CPU_MAX_SECONDS
    assert config.choose_plan(profile, duration_seconds=5, vram_free_mib=0,
                             ram_free_mib=10 ** 6) == ("cpu", False)
    assert config.choose_plan(profile, duration_seconds=threshold + 1, vram_free_mib=0,
                             ram_free_mib=10 ** 6) == ("cuda", True)


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


def test_worker_plan_action_decides_device_and_swap(monkeypatch, tmp_path):
    """Worker contract: action='plan' returns (device, need_swap) from the measured free
    VRAM/RAM. Exercised with the PC2 profile (CPU branch enabled)."""
    module = load_worker()
    from transcriber import config

    profile = config.select_profile(total_mib=16311)
    vram, ram = profile.audio_vram_mib, profile.audio_ram_mib

    monkeypatch.setattr(config, "free_vram_mib", lambda: vram)
    monkeypatch.setattr(config, "available_ram_mib", lambda: 0)
    plan = module.plan({"profile": "pc2", "local_model_loaded": True})
    assert plan["device"] == "cuda" and plan["need_swap"] is False
    assert plan["profile"] == "pc2" and plan["free_vram_mib"] == vram

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


def test_lite_profile_uses_shared_plan(monkeypatch, tmp_path):
    """LITE (8 GB) uses the same plan: since RAM is sufficient, a short recording uses the CPU
    and a long one uses GPU + swap. With no local model (API) the GPU is free -> no swap."""
    module = load_worker()
    from transcriber import config, audio

    src = tmp_path / "recording.wav"
    src.write_bytes(b"RIFF")
    monkeypatch.setattr(config, "free_vram_mib", lambda: 0)
    monkeypatch.setattr(config, "available_ram_mib", lambda: 10 ** 6)
    monkeypatch.setattr(audio, "probe_duration_seconds", lambda path: 5.0)
    plan = module.plan({"profile": "pc1", "local_model_loaded": True, "path": str(src)})
    assert plan["device"] == "cpu" and plan["need_swap"] is False
    monkeypatch.setattr(audio, "probe_duration_seconds", lambda path: 3600.0)
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
    """Same pattern as OCR: on the local provider (language model loaded) the device/swap
    decision is made from the REAL free VRAM/RAM measured when the audio job starts; the
    model is unloaded only when required and restored afterwards. The decision is NOT based
    on the profile name (LITE runs on both 8 GB and 16 GB)."""
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


def test_transcription_honors_cancel_and_stop_does_not_wait_for_restore():
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "$Shared.Cancel" in block
    wait = worker.split("function Wait-LocalModelReady")[1].split("function ")[0]
    assert "if ($Shared.Cancel) { return }" in wait
    caller = worker.split("$currentAudioPaths = @($JobData.currentAudioPaths")[1].split("Automatically")[0]
    assert "Emit 'cancelled'" in caller


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
