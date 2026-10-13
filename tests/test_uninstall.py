"""uninstall-mamidlabs.ps1: verified by really deleting inside a fake install root.

The uninstaller runs with Windows PowerShell 5.1 (since the bundled PowerShell 7 is deleted
too); the tests use it as well. A temporary desktop folder and a test key under HKCU are
used instead of the real desktop and registry.
"""
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "uninstall-mamidlabs.ps1"

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows uninstaller")


def powershell():
    exe = shutil.which("powershell")
    if not exe:
        pytest.skip("no Windows PowerShell")
    return exe


def ps(command, timeout=60):
    return subprocess.run([powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")


def make_install(base: Path) -> Path:
    root = base / "MamidLabsInstall"
    for folder in ("agent", "scripts", "assets", "models", "logs", "llama.cpp", "ComfyUI/models/loras", "venv/imagegen/Lib",
                   "runtime/pwsh", "tools/pip-cache", "tools/hf-home", "chats", "outputs/images", "settings",
                   "requirements"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "agent/profiles.json").write_text("{}", encoding="utf-8")
    (root / "qwen-desktop.ps1").write_text("# app", encoding="utf-8")
    (root / "launch-qwen-desktop.vbs").write_text("' launcher", encoding="utf-8")
    # The script install puts EULA.txt and requirements\ in the root too (see Invoke-Layout).
    (root / "EULA.txt").write_text("license", encoding="utf-8")
    (root / "requirements/app.txt").write_text("pytest", encoding="utf-8")
    (root / "models/model.gguf").write_bytes(b"x" * 1024)
    (root / "chats/chat.json").write_text("{}", encoding="utf-8")
    (root / "outputs/images/a.png").write_bytes(b"png")
    (root / "settings/api.json").write_text("{}", encoding="utf-8")
    # A deep path over 260 characters: really happens in venv/ComfyUI.
    deep = root / "venv/imagegen/Lib" / ("d" * 60) / ("e" * 60) / ("f" * 60) / ("g" * 60)
    os.makedirs("\\\\?\\" + str(deep), exist_ok=True)
    with open("\\\\?\\" + str(deep / "x.py"), "w") as handle:
        handle.write("x")
    return root


def make_shortcut(desktop: Path, target_root: Path, name: str = "MamidLabs.lnk"):
    desktop.mkdir(parents=True, exist_ok=True)
    link = desktop / name
    result = ps(
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}'); "
        f"$s.TargetPath = 'C:\\Windows\\System32\\wscript.exe'; $s.Arguments = '\"{target_root}\\launch-qwen-desktop.vbs\"'; "
        f"$s.WorkingDirectory = '{target_root}'; $s.Save()")
    assert result.returncode == 0, result.stderr
    return link


def run_uninstall(root: Path, desktop: Path, *args, registry="HKCU:\\Software\\MamidLabsTest-none"):
    command = [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
               "-Root", str(root), "-DesktopPath", str(desktop), "-UninstallRegistryPaths", registry, *args]
    return subprocess.run(command, capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")


def test_full_uninstall_removes_everything_and_only_this_installs_shortcut(tmp_path):
    root = make_install(tmp_path)
    desktop = tmp_path / "Desktop"
    link = make_shortcut(desktop, root)
    other_desktop = tmp_path / "Desktop2"
    other_link = make_shortcut(other_desktop, tmp_path / "OtherInstall")

    result = run_uninstall(root, desktop, "-Yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not root.exists(), result.stdout
    assert not link.exists()

    # A shortcut pointing to another copy is not touched.
    result = run_uninstall(make_install(tmp_path / "second"), other_desktop, "-Yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert other_link.exists()


def test_second_install_shortcut_is_removed_and_the_other_install_keeps_its_own(tmp_path):
    """A second installation does not overwrite MamidLabs.lnk, it creates "MamidLabs (<folder>).lnk"; the uninstaller deletes that."""
    root = make_install(tmp_path)
    desktop = tmp_path / "Desktop"
    main_link = make_shortcut(desktop, tmp_path / "MainInstall")
    second_link = make_shortcut(desktop, root, f"MamidLabs ({root.name}).lnk")
    result = run_uninstall(root, desktop, "-Yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not second_link.exists()
    assert main_link.exists()


def test_installer_opens_a_separate_shortcut_when_another_install_owns_the_default():
    install = (ROOT / "scripts/install-mamidlabs.ps1").read_text(encoding="utf-8-sig")
    assert "'MamidLabs (' + (Split-Path -Leaf $Root) + ').lnk'" in install
    assert "MamidLabs is open; the program files are locked" in install


def test_keep_user_data_leaves_chats_outputs_and_settings(tmp_path):
    root = make_install(tmp_path)
    result = run_uninstall(root, tmp_path / "Desktop", "-Yes", "-KeepUserData")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in root.iterdir()) == ["chats", "outputs", "settings"]
    assert (root / "chats/chat.json").exists()


def test_foreign_files_are_kept_without_confirmation(tmp_path):
    root = make_install(tmp_path)
    (root / "my-notes.txt").write_text("personal", encoding="utf-8")
    result = run_uninstall(root, tmp_path / "Desktop", "-Yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [p.name for p in root.iterdir()] == ["my-notes.txt"]


def test_dry_run_deletes_nothing(tmp_path):
    root = make_install(tmp_path)
    before = sorted(p.name for p in root.iterdir())
    result = run_uninstall(root, tmp_path / "Desktop", "-DryRun")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing was deleted" in result.stdout
    assert sorted(p.name for p in root.iterdir()) == before


def test_refuses_folders_that_are_not_a_mamidlabs_install(tmp_path):
    folder = tmp_path / "MyDocuments"
    folder.mkdir()
    (folder / "thesis.docx").write_text("x", encoding="utf-8")
    result = run_uninstall(folder, tmp_path / "Desktop", "-Yes")
    assert result.returncode == 1
    assert (folder / "thesis.docx").exists()


def test_stops_processes_running_from_the_install(tmp_path):
    root = make_install(tmp_path)
    # A process that carries the root path on its command line, like the app/ComfyUI.
    marker = root / "scripts" / "wait.ps1"
    marker.write_text("Start-Sleep -Seconds 120", encoding="utf-8")
    proc = subprocess.Popen([powershell(), "-NoProfile", "-File", str(marker)], cwd=str(tmp_path))
    try:
        time.sleep(2)
        result = run_uninstall(root, tmp_path / "Desktop", "-Yes")
        assert result.returncode == 0, result.stdout + result.stderr
        assert proc.wait(timeout=10) is not None
        assert not root.exists()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_removes_only_the_uninstall_entry_of_this_root(tmp_path):
    root = make_install(tmp_path)
    base = f"HKCU:\\Software\\MamidLabsTest-{uuid.uuid4().hex[:8]}"
    setup = ps(
        f"New-Item -Path '{base}\\MamidLabs-this' -Force | Out-Null; "
        f"Set-ItemProperty -Path '{base}\\MamidLabs-this' -Name DisplayName -Value 'MamidLabs ({root})'; "
        f"Set-ItemProperty -Path '{base}\\MamidLabs-this' -Name InstallLocation -Value '{root}\\'; "
        f"New-Item -Path '{base}\\MamidLabs-other' -Force | Out-Null; "
        f"Set-ItemProperty -Path '{base}\\MamidLabs-other' -Name DisplayName -Value 'MamidLabs (D:\\Other)'; "
        f"Set-ItemProperty -Path '{base}\\MamidLabs-other' -Name InstallLocation -Value 'D:\\Other'")
    assert setup.returncode == 0, setup.stderr
    try:
        result = run_uninstall(root, tmp_path / "Desktop", "-Yes", registry=base)
        assert result.returncode == 0, result.stdout + result.stderr
        left = ps(f"(Get-ChildItem '{base}').PSChildName")
        assert left.stdout.split() == ["MamidLabs-other"]
    finally:
        ps(f"Remove-Item -LiteralPath '{base}' -Recurse -Force")


def test_uninstall_removes_eula_and_requirements_from_the_root(tmp_path):
    """The script install copies EULA.txt and requirements\\ into the root; the uninstaller
    must know them, otherwise they stay behind and the root folder is not deleted."""
    root = make_install(tmp_path)
    assert (root / "EULA.txt").exists()
    assert (root / "requirements" / "app.txt").exists()
    result = run_uninstall(root, tmp_path / "Desktop", "-Yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not root.exists(), result.stdout


def test_eula_and_requirements_are_program_entries_and_deleted_by_the_wizard():
    uninstall = (ROOT / "scripts/uninstall-mamidlabs.ps1").read_text(encoding="utf-8-sig")
    assert "'requirements'" in uninstall
    assert "'EULA.txt'" in uninstall
    iss = (ROOT / "packaging/windows/mamidlabs.iss").read_text(encoding="utf-8-sig")
    assert 'Type: files; Name: "{app}\\EULA.txt"' in iss
    assert 'Type: filesandordirs; Name: "{app}\\requirements"' in iss


def test_installers_keep_caches_inside_the_install_root():
    install = (ROOT / "scripts/install-mamidlabs.ps1").read_text(encoding="utf-8-sig")
    ocr = (ROOT / "scripts/install-ocr.ps1").read_text(encoding="utf-8-sig")
    hub = (ROOT / "agent/hf-hub.py").read_text(encoding="utf-8")
    assert "$env:PIP_CACHE_DIR = Join-Path $Root 'tools\\pip-cache'" in install
    assert "$env:PIP_CACHE_DIR = Join-Path $Root 'tools\\pip-cache'" in ocr
    assert hub.index("HF_HOME") < hub.index("from huggingface_hub import")


def test_wizard_uninstaller_runs_the_script_and_no_longer_deletes_foreign_shortcuts():
    iss = (ROOT / "packaging/windows/mamidlabs.iss").read_text(encoding="utf-8-sig")
    assert "uninstall-mamidlabs.ps1" in iss and "-Yes -FromWizard" in iss
    assert "{userdesktop}\\MamidLabs.lnk" not in iss
    assert "-InstallPrereqs -FromWizard" in iss


def test_uninstaller_is_ascii_for_windows_powershell_51():
    # 5.1 reads a BOM-less file as ANSI; non-ASCII characters would silently get garbled.
    SCRIPT.read_bytes().decode("ascii")
