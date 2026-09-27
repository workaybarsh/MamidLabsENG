"""Install root portability.

The app runs from whichever folder it was installed to; no script hardcodes an install
path. qwen-desktop.ps1 starts its workers (qwen-inference.ps1, imagegen-run.ps1,
videogen-run.ps1, api-media-run.ps1) with Start-ThreadJob -FilePath; in that context
$PSScriptRoot is empty and Get-Location may be an unrelated folder. The root is
therefore passed explicitly as JobData.root; workers fall back to $PSScriptRoot only
when run directly.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_no_file_hardcodes_the_install_path():
    for path in ROOT.rglob("*"):
        if path.suffix not in (".ps1", ".vbs", ".py") or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        # The default root is %LOCALAPPDATA%\\Programs\\MamidLabs; a fixed drive root
        # (e.g. C:\\AI-Local) must not be written anywhere.
        for line in text.splitlines():
            if "AI-Local" in line:
                raise AssertionError(f"{path.relative_to(ROOT)}: hardcoded path left -> {line.strip()!r}")


def test_worker_scripts_derive_root_from_their_own_location():
    for path in ("agent/imagegen-run.ps1", "agent/videogen-run.ps1", "agent/api-media-run.ps1"):
        text = read_text(path)
        assert "Split-Path -Parent $PSScriptRoot" in text
        # CRITICAL: $PSScriptRoot is empty in the Start-ThreadJob -FilePath context;
        # JobData.root must be checked FIRST.
        assert "if ($JobData.root)" in text
        assert text.index("if ($JobData.root)") < text.index("Split-Path -Parent $PSScriptRoot")


def test_qwen_inference_uses_get_install_root_with_jobdata_priority():
    inference = read_text("agent/qwen-inference.ps1")

    assert "function Get-InstallRoot" in inference
    assert "if ($JobData -and $JobData.root)" in inference
    assert "elseif ($PSScriptRoot)" in inference


def test_desktop_passes_root_to_every_threadjob_worker():
    """All 4 Start-ThreadJob calls of qwen-desktop.ps1 must pass JobData.root;
    if one is forgotten that worker silently falls back to the wrong root."""
    desktop = read_text("qwen-desktop.ps1")

    threadjob_count = desktop.count("Start-ThreadJob -FilePath (Join-Path $root 'agent")
    assert threadjob_count == 4, f"expected 4 Start-ThreadJob calls, found {threadjob_count}"

    # Every $jobData = @{ block must be followed right away by 'root = $root' (compact,
    # chat, image and video jobs, the fourth included).
    assert desktop.count("root = $root") >= 4


def test_hf_and_profile_catalog_pass_root_for_api_media_run():
    """api-media-run.ps1 also starts with Start-ThreadJob -FilePath; every place that
    calls it (the HF window, the profile downloader, the media API) must put the root
    into JobData."""
    for path in ("agent/hf-settings.ps1", "agent/profile-catalog.ps1", "agent/media-settings.ps1"):
        text = read_text(path)
        assert "root=$root" in text or "root = $root" in text, f"{path}: JobData.root missing"


def test_launcher_derives_root_from_its_own_location():
    vbs = read_text("launch-qwen-desktop.vbs")
    assert "GetParentFolderName(WScript.ScriptFullName)" in vbs


def test_python_workers_derive_root_from_file_location():
    for path, marker in (
        ("agent/api-media.py", "pathlib.Path(__file__).resolve().parent.parent"),
        ("agent/media-tools.py", "pathlib.Path(__file__).resolve().parent.parent"),
        ("agent/ocr-tools.py", "Path(__file__).resolve().parent.parent"),
        ("agent/office-tools.py", "os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"),
        ("agent/qwen-agent-tools.py", "os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"),
    ):
        assert marker in read_text(path), f"{path}: __file__-based root derivation missing"
