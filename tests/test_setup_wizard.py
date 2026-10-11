"""The one-click installer package: the wizard script, the profile report, the model settings.

The Inno Setup build is done on Windows with GitHub Actions (installer.yml);
here the PowerShell scripts the wizard calls are really run and the contract of the .iss
file with the scripts (parameter names, file paths) is checked.
"""
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "packaging" / "windows" / "mamidlabs.iss"


def read_text(path) -> str:
    return Path(path).read_text(encoding="utf-8-sig")


def pwsh_or_skip():
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    return pwsh


def script_params(path: str) -> set:
    """The parameter names in the script's param() block (via the AST)."""
    pwsh = pwsh_or_skip()
    command = (
        f"$ast = [Management.Automation.Language.Parser]::ParseFile('{ROOT / path}', [ref]$null, [ref]$null);"
        "$ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath }"
    )
    out = subprocess.run([pwsh, "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=60)
    return set(out.stdout.split())


# ------------------------------------------------------------------ profile report

def run_profile_report(tmp_path, gpu_line):
    pwsh = pwsh_or_skip()
    if os.name == "nt":
        pytest.skip("the fake nvidia-smi is only prepared on POSIX")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if gpu_line:
        smi = bindir / "nvidia-smi"
        smi.write_text(f"#!/bin/sh\necho '{gpu_line}'\n")
        smi.chmod(smi.stat().st_mode | stat.S_IEXEC)
    runner = tmp_path / "run.ps1"
    runner.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "function Get-CimInstance { param([string]$ClassName, $ErrorAction)\n"
        "  switch ($ClassName) {\n"
        "    'Win32_OperatingSystem' { [pscustomobject]@{ Caption = 'Windows 11'; Version = '10.0.26100' } }\n"
        "    'Win32_Processor' { [pscustomobject]@{ Name = 'Test CPU' } }\n"
        "    'Win32_ComputerSystem' { [pscustomobject]@{ TotalPhysicalMemory = 32GB } }\n"
        "    'Win32_VideoController' { [pscustomobject]@{ Name = 'Intel UHD' } }\n"
        "  } }\n"
        f"& '{ROOT / 'scripts' / 'profile-report.ps1'}' -Root 'C:\\MamidLabs' "
        f"-OutFile '{tmp_path / 'r.txt'}' -JsonFile '{tmp_path / 'r.json'}' -RecommendFile '{tmp_path / 'o.txt'}'\n",
        encoding="utf-8-sig",
    )
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + "/usr/bin:/bin")
    result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(runner)],
                            capture_output=True, text=True, timeout=60, env=env, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
    return (tmp_path / "o.txt").read_text(), json.loads((tmp_path / "r.json").read_text()), read_text(tmp_path / "r.txt")


def test_profile_report_recommends_lite_for_an_8_gb_card(tmp_path):
    recommended, data, text = run_profile_report(tmp_path, "NVIDIA GeForce RTX 4060 Laptop GPU, 8188, 580.12")
    assert recommended == "lite"
    assert data["vramMiB"] == 8188
    assert data["profiles"]["flash"]["status"] == "not suitable"
    assert "Recommendation: LITE" in text


def test_profile_report_recommends_flash_for_a_16_gb_card(tmp_path):
    recommended, data, text = run_profile_report(tmp_path, "NVIDIA GeForce RTX 5060 Ti, 16311, 580.12")
    assert recommended == "flash"
    # Every file in the catalog has a download source; the report must not ask for a manual install.
    assert not data["profiles"]["max"]["manualFiles"]
    assert "to install manually" not in text


def test_profile_report_suggests_api_without_an_nvidia_card(tmp_path):
    recommended, data, text = run_profile_report(tmp_path, None)
    assert recommended == "api"
    assert data["nvidia"] is False
    assert "API" in text


def test_profile_requirements_match_the_installer():
    report = read_text(ROOT / "scripts/profile-report.ps1")
    installer = read_text(ROOT / "scripts/install-mamidlabs.ps1")
    for profile, vram, ram, disk in (("lite", 7000, 14, 58), ("flash", 15000, 30, 75), ("max", 15000, 30, 75)):
        pattern = rf"{profile}\s*=\s*@\{{\s*vramMiB = {vram};\s*ramGB = {ram};\s*diskGB = {disk}"
        assert re.search(pattern, report), profile
        assert re.search(pattern, installer), profile


# ------------------------------------------------------------------ model settings

def run_configure(tmp_path, args):
    pwsh = pwsh_or_skip()
    (tmp_path / "agent").mkdir(exist_ok=True)
    shutil.copy(ROOT / "agent" / "media-paths.ps1", tmp_path / "agent" / "media-paths.ps1")
    # Windows path separator: the script uses Join-Path $Root 'agent\media-paths.ps1'.
    if os.name != "nt":
        shutil.copy(ROOT / "agent" / "media-paths.ps1", tmp_path / "agent\\media-paths.ps1")
    result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / "scripts" / "configure-models.ps1"),
                             "-Root", str(tmp_path), *args], capture_output=True, text=True, timeout=60,
                            encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((tmp_path / "settings" / "api.json").read_text(encoding="utf-8"))


def test_configure_registers_own_llm_and_selects_it(tmp_path):
    model = tmp_path / "My Model.Q4_K_M.gguf"
    model.write_bytes(b"x")
    settings = run_configure(tmp_path, ["-LlmPath", str(model), "-OpenApiSettings"])
    entry = settings["localModels"][0]
    assert entry["path"] == str(model)
    assert settings["selectedModel"] == entry["id"] == settings["selectedLocalModel"]
    assert settings["firstRunApiSettings"] is True


def test_configure_without_local_model_turns_autoload_off(tmp_path):
    settings = run_configure(tmp_path, ["-NoLocalModel"])
    assert settings["selectedModel"] == "off"


def test_configure_sets_builtin_profile(tmp_path):
    settings = run_configure(tmp_path, ["-DefaultProfile", "lite"])
    assert settings["selectedModel"] == "builtin-lite"
    assert settings["localProfile"] == "lite"


def test_configure_rejects_projector_as_llm(tmp_path):
    pwsh = pwsh_or_skip()
    projector = tmp_path / "model.mmproj-Q8_0.gguf"
    projector.write_bytes(b"x")
    result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / "scripts" / "configure-models.ps1"),
                             "-Root", str(tmp_path), "-LlmPath", str(projector)], capture_output=True, text=True, timeout=60)
    assert result.returncode != 0


# ------------------------------------------------------------------ wizard contract

def test_wizard_passes_only_parameters_that_setup_run_accepts():
    iss = read_text(ISS)
    block = iss[iss.index("function BuildSetupParams"):iss.index("procedure CurStepChanged")]
    params = script_params("scripts/setup-run.ps1")
    used = set(re.findall(r"' -([A-Za-z]+)", block))
    assert used, "the wizard passes no parameters to setup-run.ps1"
    assert used <= params, used - params


def test_wizard_passes_only_parameters_that_profile_report_accepts():
    iss = read_text(ISS)
    block = iss[iss.index("procedure RunProfileReport"):iss.index("procedure CurPageChanged")]
    used = set(re.findall(r"' -([A-Za-z]+)", block))
    assert used <= script_params("scripts/profile-report.ps1")


def test_setup_run_calls_helpers_with_existing_parameters():
    run = read_text(ROOT / "scripts/setup-run.ps1")
    for script, flags in (
        ("scripts/configure-models.ps1", ("DefaultProfile", "NoLocalModel", "LlmPath", "ImageModelPath", "VideoModelPath", "OpenApiSettings")),
        ("scripts/download-profile.ps1", ("Profile", "Root")),
        ("scripts/smoke-test.ps1", ("Profile", "Root", "SkipVideo", "SkipOcr", "ReportPath")),
    ):
        params = script_params(script)
        for flag in flags:
            assert f"-{flag}" in run, (script, flag)
            assert flag in params, (script, flag)


def test_wizard_source_files_exist():
    iss = read_text(ISS)
    base = ISS.parent
    for source in re.findall(r'Source: "([^"]+)"', iss):
        path = (base / source.replace("\\", "/")).resolve()
        if "*" in path.name:
            assert path.parent.is_dir(), source
        else:
            assert path.is_file(), source
    for image in re.findall(r"Wizard(?:Small)?ImageFile=(.+)", iss):
        for item in image.split(","):
            assert (base / item.strip().replace("\\", "/")).resolve().is_file(), item


def test_wizard_offers_every_profile_later_and_own_model_choices():
    iss = read_text(ISS)
    for text in ("LITE", "FLASH", "MAX", "I will install the profile later", "I will use my own model or my API account"):
        assert text in iss
    assert 'MessagesFile: "compiler:Default.isl"' in iss
    assert "LicenseFile=..\\..\\EULA.txt" in iss
    assert "PrivilegesRequired=lowest" in iss
    # Own models are not copied; the file path is passed to setup-run.
    assert "-LlmPath" in iss and "-ImageModelPath" in iss and "-VideoModelPath" in iss


def test_uninstall_asks_before_deleting_models_and_chats():
    iss = read_text(ISS)
    block = iss[iss.index("procedure CurUninstallStepChanged"):]
    assert "MsgBox(" in block and "MB_DEFBUTTON2" in block
    assert "Type: dirifempty; Name: \"{app}\"" in iss
    assert "Type: filesandordirs; Name: \"{app}\"" not in iss


def test_installer_workflow_builds_the_setup():
    workflow = read_text(ROOT / ".github/workflows/installer.yml")
    assert "innosetup" in workflow
    assert "packaging\\windows\\mamidlabs.iss" in workflow
    assert "MamidLabs_Setup.exe" in workflow


def test_eula_exists_and_covers_ownership():
    eula = read_text(ROOT / "EULA.txt")
    assert "Altunta" in eula
    assert "2026" in eula
    assert len(eula) > 1500


def test_wizard_optional_components_are_default_on_and_passed_as_skips():
    """Image/video, audio and OCR are optional; all come CHECKED by default (no 'unchecked').
    Unchecking passes -SkipMedia / -SkipAudio to the install."""
    iss = read_text(ROOT / "packaging" / "windows" / "mamidlabs.iss")
    for task in ("media", "audio", "ocr", "smoketest"):
        assert 'Name: "%s"' % task in iss, task + " task missing"
    assert "unchecked" not in iss, "all tasks must be checked by default"
    assert "if not WizardIsTaskSelected('media') then P := P + ' -SkipMedia'" in iss
    assert "if not WizardIsTaskSelected('audio') then P := P + ' -SkipAudio'" in iss
    installer = read_text("scripts/install-mamidlabs.ps1")
    assert "[switch]$SkipAudio" in installer
    setup = read_text("scripts/setup-run.ps1")
    assert "[switch]$SkipMedia" in setup and "[switch]$SkipAudio" in setup


def test_repository_is_proprietary_licensed():
    license_text = read_text(ROOT / "LICENSE")
    assert "All Rights Reserved" in license_text
    assert "Aybars Haydar Altuntaş" in license_text
    assert "Permission is hereby granted, free of charge" not in license_text


def test_new_scripts_carry_a_bom_for_windows_powershell():
    """Windows PowerShell 5.1 reads BOM-less UTF-8 as ANSI; non-ASCII text gets garbled."""
    for path in ("scripts/setup-run.ps1", "scripts/profile-report.ps1", "scripts/download-profile.ps1",
                 "scripts/configure-models.ps1", "agent/media-paths.ps1", "packaging/windows/mamidlabs.iss"):
        assert (ROOT / path).read_bytes().startswith(b"\xef\xbb\xbf"), path
