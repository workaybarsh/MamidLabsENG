"""Whisper.cpp ses dökum iscisi (agent\\whisper-tools.py) + arayuz/isci sozlesmeleri.

Model/ikili kurulu degilse agir testler atlanir; sozlesmeler her zaman denetlenir.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "agent" / "whisper-tools.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("whisper_tools", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_worker_exists_and_reads_json_on_stdin():
    src = read_text("agent/whisper-tools.py")
    assert "sys.stdin.read()" in src
    assert "'transcribe'" in src


def test_turbo_model_preferred_for_speed_and_accuracy():
    """Olculdu: turbo-q5_0 CPU'da ~2x, CUDA'da ~4-13x hizli ve temiz konusmada large-v3
    ile ayni dogrulukta; halusinasyon VAD ile bastirilir. Bu yuzden tercih sirasinda ilk
    turbo gelir."""
    module = load_worker()
    assert module.MODEL_PREFERENCE[0] == "ggml-large-v3-turbo-q5_0.bin"
    assert "ggml-large-v3.bin" in module.MODEL_PREFERENCE


def test_vad_is_wired_to_suppress_hallucination():
    """Sessizlikte hem turbo hem large-v3 halusinasyon uretiyordu; VAD ile bastirildi.
    Isci VAD modelini arar ve whisper-cli'ye --vad -vm ... -mc 0 gecirir."""
    src = read_text("agent/whisper-tools.py")
    assert "ggml-silero-" in src
    assert "'--vad'" in src and "'-vm'" in src
    assert "'-mc'" in src and "'0'" in src


def test_transcribe_rejects_missing_file():
    module = load_worker()
    with pytest.raises(ValueError):
        module.transcribe({"path": str(ROOT / "does-not-exist.wav")})


@pytest.mark.skipif(not (ROOT / "agent" / "whisper-tools.py").is_file(), reason="worker missing")
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
    VARSAYILAN kayit cihazini (WAVE_MAPPER) kullanir — cihaz secimi/hardcode yok. Kayit
    toggle ile baslar/durur ve ses eki olarak eklenip otomatik gonderilir."""
    desktop = read_text("qwen-desktop.ps1")
    assert "function Start-MicrophoneCapture" in desktop
    assert "mciSendString" in desktop
    assert "open new type waveaudio alias" in desktop  # WAVE_MAPPER = default device
    assert "Add-SelectedAudio" in desktop
    assert "script:micRecording" in desktop


def test_audio_quality_toggle_switches_between_turbo_and_large_v3():
    """Mikrofon menusunde 'Yuksek kalite' anahtari: normal = turbo-q5_0 (hizli), yuksek =
    large-v3 (en yuksek dogruluk, daha yavas). Secim ayarlarda saklanir ve isci, kaliteye
    gore acik model yolunu whisper'a gecirir."""
    desktop = read_text("qwen-desktop.ps1")
    worker = read_text("agent/qwen-inference.ps1")
    assert "$script:audioQuality" in desktop
    assert "audioQuality = $script:audioQuality" in desktop  # isciye tasinir
    assert "$script:apiSettings.audioQuality = $script:audioQuality" in desktop  # saklanir
    assert "CheckOnClick" in desktop
    # Isci, 'high' ise large-v3 tam yolunu gecirir; aksi halde varsayilan (turbo) kalir.
    assert "audioQuality" in worker
    assert "ggml-large-v3.bin" in worker
    assert "payloadBase.model" in worker


def test_installer_downloads_both_default_and_high_quality_models():
    """'Yuksek kalite' secenegi sifirdan kurulumda da calissin diye kurulum hem turbo-q5_0
    (varsayilan) hem large-v3 (yuksek kalite) hem de Silero VAD modelini indirir. Aksi
    halde yuksek kalite sessizce turbo'ya duser."""
    installer = read_text("scripts/install-mamidlabs.ps1")
    assert "ggml-large-v3-turbo-q5_0.bin" in installer
    assert "ggml-large-v3.bin" in installer
    assert "ggml-silero-v5.1.2.bin" in installer


def test_transcription_uses_model_swap_for_local_provider():
    """OCR ile ayni desen: yerel saglayicida dil modeli bosaltilir (GPU whisper'a kalir),
    dökum alinir, model geri yuklenir."""
    worker = read_text("agent/qwen-inference.ps1")
    block = worker.split("function Invoke-TranscribeWithModelSwap")[1].split("function ")[0]
    assert "(Get-ManageQwenPath) -Action stop" in block
    assert "Start-LocalModelRestore" in block
    assert "Get-WhisperPyPath" in block
