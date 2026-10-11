"""Qwen3-ASR ses dökum iscisi (agent\\qwen3-asr-tools.py) + arayuz/isci sozlesmeleri.

Model/ortam kurulu degilse agir testler atlanir; sozlesmeler her zaman denetlenir.
Whisper tamamen kaldirildi; motor Qwen3-ASR-1.7B'dir.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "agent" / "qwen3-asr-tools.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("qwen3_asr_tools", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_worker_exists_and_reads_json_on_stdin():
    src = read_text("agent/qwen3-asr-tools.py")
    assert "sys.stdin.read()" in src
    assert "'transcribe'" in src


def test_worker_is_offline_first():
    """Model yerelde; calisma aninda transformers/HF aga cikmamali (cevrimdisi garanti)."""
    src = read_text("agent/qwen3-asr-tools.py")
    assert "HF_HUB_OFFLINE" in src
    assert "TRANSFORMERS_OFFLINE" in src


def test_worker_uses_pyav_not_librosa_or_soundfile():
    """Ses, PyAV ile numpy dizisine cozulur; imagegen venv'inde librosa/soundfile yok."""
    src = read_text("agent/qwen3-asr-tools.py")
    assert "_decode_float32_16k" in src
    assert "import av" in src
    assert "import librosa" not in src
    assert "import soundfile" not in src


def test_worker_targets_qwen3_asr_model_dir():
    module = load_worker()
    assert module.MODEL_DIRNAME == "qwen3-asr"
    assert "tr" in module.LANGUAGE_NAMES and module.LANGUAGE_NAMES["tr"] == "Turkish"


def test_worker_parses_qwen3_asr_output():
    src = read_text("agent/qwen3-asr-tools.py")
    assert "apply_transcription_request" in src
    assert "extract_transcription" in src
    assert "<asr_text>" in src


def test_whisper_fully_removed():
    """Whisper kaldirildi: isci dosyasi, kurulum asamasi ve arayuz/isci referanslari kalmamali."""
    assert not (ROOT / "agent" / "whisper-tools.py").exists()
    installer = read_text("scripts/install-mamidlabs.ps1")
    assert "whisper-cli.exe" not in installer
    assert "ggml-large-v3" not in installer
    assert "whisperCuda" not in installer
    desktop = read_text("qwen-desktop.ps1")
    worker = read_text("agent/qwen-inference.ps1")
    for text in (desktop, worker):
        assert "whisper" not in text.lower()
        assert "audioQuality" not in text


def test_desktop_pipes_audio_to_worker_and_shows_transcript():
    """Ses eklenince: masaustu currentAudioPaths gecirir; isci otomatik dökumler ve
    'transcript' olayi yayar; arayuz bunu baloncukta gosterir (OCR gibi)."""
    desktop = read_text("qwen-desktop.ps1")
    worker = read_text("agent/qwen-inference.ps1")
    assert "currentTurnAudioPaths" in desktop
    assert "currentAudioPaths = @($script:currentTurnAudioPaths)" in desktop
    assert "function Invoke-TranscribeWithModelSwap" in worker
    assert "function Add-TranscriptToMessages" in worker
    assert "$JobData.currentAudioPaths" in worker
    assert "Emit 'transcript'" in worker
    assert "'transcript' {" in desktop


def test_microphone_capture_uses_default_device_via_mci():
    """Dependency-free microphone recording: winmm mciSendString. `open new type waveaudio` uses
    the Windows DEFAULT recording device (WAVE_MAPPER) - no device selection/hardcode. Recording
    starts; when finished the audio is attached and sent automatically; it can be cancelled."""
    desktop = read_text("qwen-desktop.ps1")
    assert "function Start-MicrophoneCapture" in desktop
    assert "mciSendString" in desktop
    assert "open new type waveaudio alias" in desktop  # WAVE_MAPPER = default device
    assert "Add-SelectedAudio" in desktop
    assert "script:micRecording" in desktop
    # Stop / cancel / send separation.
    assert "function Stop-MicrophoneCapture" in desktop
    assert "function Complete-MicrophoneCapture" in desktop
    assert "function Cancel-MicrophoneCapture" in desktop
    # Red microphone (while recording) click finishes+sends; the menu "Cancel recording" discards.
    assert "$micBtn.Add_Click({" in desktop and "Complete-MicrophoneCapture" in desktop
    assert "$micCancel.Add_Click({" in desktop and "Cancel-MicrophoneCapture" in desktop
    assert "$micBtn.Add_MouseUp({" in desktop  # right click: cancel menu reachable while recording


def test_extract_text_strips_wrapper_and_handles_silence():
    """The '<asr_text>' tag and the 'language ...' wrapper are always stripped; on a silent or
    short recording the text comes back EMPTY. A silent microphone once leaked the raw
    'language None<asr_text>' to the model (caught in the UI test)."""
    module = load_worker()
    assert module._extract_text(None, 'language Turkish<asr_text>Merhaba dünya') == ('Merhaba dünya', 'Turkish')
    assert module._extract_text(None, 'language English<asr_text>Hello.') == ('Hello.', 'English')
    # Silence: empty tail -> empty text, the 'None' language is cleaned up.
    assert module._extract_text(None, 'language None<asr_text>') == ('', '')
    assert module._extract_text(None, 'language None<asr_text>   ') == ('', '')
    # The wrapper never leaks into the text.
    text, _ = module._extract_text(None, 'language Turkish<asr_text>Merhaba')
    assert '<asr_text>' not in text and 'language' not in text


def test_transcription_uses_model_swap_for_local_provider():
    """Like OCR: on the local provider the language model is unloaded (the GPU is left to the
    audio model), the transcript is taken, then the model is restored."""
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "(Get-ManageQwenPath) -Action stop" in block
    assert "Start-LocalModelRestore" in block
    assert "Get-AsrPyPath" in block
    # CUDA first, then the CPU fallback.
    assert "@('cuda', 'cpu')" in block


def test_transcription_honors_cancel_and_stop_does_not_wait_for_restore():
    """After audio is sent "Stop" must work: transcription and the model-restore wait honor the
    cancel flag; the UI does not stay in "Stopping" for long."""
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "$Shared.Cancel" in block
    wait = worker.split("function Wait-LocalModelReady")[1].split("function ")[0]
    assert "if ($Shared.Cancel) { return }" in wait
    # During a cancel a "ready" message would mislead.
    assert "if (!$Shared.Cancel) { Emit 'status' 'Language model reloaded; preparing the answer" in block
    # The caller does not show a cancel as an error.
    caller = worker.split("$currentAudioPaths = @($JobData.currentAudioPaths")[1].split("Automatic context")[0]
    assert "Emit 'cancelled'" in caller


def test_audio_turn_preempts_language_model_loading():
    """When audio is sent while the language model is loading, the load is abandoned and ASR runs
    first; the language model returns after the transcript. The UI starts immediately for audio."""
    desktop = read_text("qwen-desktop.ps1")
    assert "function Stop-ModelSwitchForAudio" in desktop
    begin = desktop.split("function Begin-Generation {")[1].split("function Set-LastUserImage")[0]
    assert "$turnAudio" in begin and "Stop-ModelSwitchForAudio" in begin
    send = desktop.split("$send.Add_Click({")[1].split("$promptBox.Add_KeyDown")[0]
    assert "currentTurnAudioPaths.Count -gt 0" in send


def test_python_tool_subprocess_is_killed_instantly_on_stop():
    """"Stop" must cancel EVERYTHING instantly: long Python subprocesses (ASR/OCR/search) must not
    block. Output is collected asynchronously; the wait loop polls for cancellation and kills the
    process tree."""
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("function Invoke-PythonTool")[1].split("function ")[0]
    assert "ReadToEndAsync" in block
    assert "$Shared.Cancel" in block
    assert "$p.Kill($true)" in block
    # The ASR device loop breaks on cancel (no CPU fallback); the tool loop does not treat a
    # cancel as a tool error.
    asr = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "if ($Shared.Cancel) { break }" in asr
    assert "if ($Shared.Cancel) { Emit 'cancelled'" in worker


def test_stop_cancels_work_but_never_kills_an_in_flight_model_load():
    """"Stop" cancels running work BUT does not touch a language-model LOAD: switchProcess is left
    alone and the load completes in the background. (Regression guard: Cancel-Generation does not
    reference switchProcess.)"""
    desktop = read_text("qwen-desktop.ps1")
    cancel = desktop.split("function Cancel-Generation {")[1].split("function Stop-LocalQwen")[0]
    assert "switchProcess" not in cancel
    assert "pendingModelChange" not in cancel


@pytest.mark.skipif(not WORKER.is_file(), reason="worker missing")
def test_worker_rejects_missing_file():
    module = load_worker()
    with pytest.raises(ValueError):
        module.transcribe({"path": str(ROOT / "does-not-exist.wav")})
