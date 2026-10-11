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
    """Mikrofon kaydi bagimliliksiz: winmm mciSendString. `open new type waveaudio` Windows
    VARSAYILAN kayit cihazini (WAVE_MAPPER) kullanir - cihaz secimi/hardcode yok. Kayit
    toggle ile baslar/durur ve ses eki olarak eklenip otomatik gonderilir."""
    desktop = read_text("qwen-desktop.ps1")
    assert "function Start-MicrophoneCapture" in desktop
    assert "mciSendString" in desktop
    assert "open new type waveaudio alias" in desktop  # WAVE_MAPPER = default device
    assert "Add-SelectedAudio" in desktop
    assert "script:micRecording" in desktop


def test_transcription_uses_model_swap_for_local_provider():
    """OCR ile ayni desen: yerel saglayicida dil modeli bosaltilir (GPU ses modeline kalir),
    dökum alinir, model geri yuklenir."""
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "(Get-ManageQwenPath) -Action stop" in block
    assert "Start-LocalModelRestore" in block
    assert "Get-AsrPyPath" in block
    # CUDA once, sonra CPU yedegi.
    assert "@('cuda', 'cpu')" in block


@pytest.mark.skipif(not WORKER.is_file(), reason="worker missing")
def test_worker_rejects_missing_file():
    module = load_worker()
    with pytest.raises(ValueError):
        module.transcribe({"path": str(ROOT / "does-not-exist.wav")})
