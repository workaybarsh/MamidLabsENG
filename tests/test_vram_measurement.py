"""VRAM measurement (agent/gpu-info.ps1) + memory-gate integration.

Regression: the old code used `-replace '\\D',''`, which concatenated EVERY digit; driver
version numbers in an error message could become a huge "free VRAM" value, `[N/A]`/empty
output became 0 and `-5` lost its sign. A 0 treated as a real measurement made ComfyUI get
killed for nothing (~30 s), while a bogus LARGE value bypassed the gate and let the model
spill into shared memory (chat ~40 -> ~27 tok/s). Parsing is strict now: only an exact integer
(optionally "MiB") is accepted; everything else is "unmeasurable" = $null.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pwsh() -> str:
    exe = shutil.which("pwsh")
    if not exe:
        pytest.skip("PowerShell 7 is unavailable")
    return exe


def _run(body: str, workdir: Path, timeout: int = 90) -> str:
    script = workdir / "run.ps1"
    script.write_text(body, encoding="utf-8-sig")
    proc = subprocess.run(
        [_pwsh(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=workdir, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def _fake_smi(workdir: Path, lines) -> Path:
    """nvidia-smi'yi taklit eden bir .cmd yazar (argumanlari yok sayar)."""
    case = workdir / "fakesmi"
    case.mkdir(parents=True, exist_ok=True)
    body = "\r\n".join("@echo " + str(x) for x in lines) if lines else "@echo."
    (case / "nvidia-smi.cmd").write_text(body + "\r\n", encoding="ascii")
    return case


def test_free_vram_parser_rejects_garbage_and_keeps_real_values(tmp_path):
    """Real pwsh: only a clean integer is accepted; [N/A], error text, negative and empty
    -> $null (NOT 0). So "unmeasurable" never looks like "0 MiB free"."""
    gpu_info = ROOT / "agent" / "gpu-info.ps1"
    assert gpu_info.is_file()
    case_dir = _fake_smi(tmp_path, ["15628"])
    body = f"""
$env:PATH = '{case_dir};' + $env:PATH
. '{gpu_info}'
'REAL=' + (Get-NvidiaFreeVramMiB)
foreach ($raw in @('[N/A]', '', '-5', 'NVIDIA-SMI has failed 580.88 driver', '15628 MiB', '0')) {{
    $v = ConvertFrom-MiBText $raw
    'PARSE[' + $raw + ']=' + $(if ($null -eq $v) {{ 'NULL' }} else {{ [string]$v }})
}}
"""
    out = _run(body, tmp_path)
    assert "REAL=15628" in out
    assert "PARSE[[N/A]]=NULL" in out
    assert "PARSE[]=NULL" in out
    assert "PARSE[-5]=NULL" in out
    assert "PARSE[NVIDIA-SMI has failed 580.88 driver]=NULL" in out
    assert "PARSE[15628 MiB]=15628" in out
    assert "PARSE[0]=0" in out


def test_measurement_is_not_used_as_zero_when_unavailable(tmp_path):
    """The media/smoke wrappers must return `$null`; returning 0 makes Release-Comfy believe
    there is not enough VRAM and kill ComfyUI for nothing."""
    gpu_info = ROOT / "agent" / "gpu-info.ps1"
    media = ROOT / "agent" / "media-model-swap.ps1"
    smoke = ROOT / "scripts" / "smoke-test.ps1"
    case_dir = _fake_smi(tmp_path, ["[N/A]"])
    body = f"""
$env:PATH = '{case_dir};' + $env:PATH
function Emit {{ param($k, $t, $d = $null) }}
. '{gpu_info}'
. '{media}'
$m = Get-MediaFreeVramMiB
'MEDIA=' + $(if ($null -eq $m) {{ 'NULL' }} else {{ [string]$m }})
"""
    out = _run(body, tmp_path)
    assert "MEDIA=NULL" in out
    # Smoke'ta tanim, paylasilan olcume delege etmeli (kopya ayristirma kalmamali).
    text = smoke.read_text(encoding="utf-8-sig")
    assert "return (Get-NvidiaFreeVramMiB)" in text


def test_all_vram_readers_delegate_to_one_strict_source():
    """There used to be four separate `-replace '\\D'` copies; one source is used now, so a
    fix can no longer be applied to one and forgotten in another."""
    files = [
        ROOT / "scripts" / "manage-mamidlabs.ps1",
        ROOT / "agent" / "media-model-swap.ps1",
        ROOT / "scripts" / "smoke-test.ps1",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8-sig")
        assert "Get-NvidiaFreeVramMiB" in text, f"{path.name} paylasilan olcumu kullanmiyor"
        assert "-replace '\\D'" not in text, f"{path.name} icinde eski kayipsiz ayristirma kaldi"
        assert "gpu-info.ps1" in text, f"{path.name} gpu-info.ps1'i yuklemiyor"


def test_max_context_parameter_binding_positional_named_and_spaced_paths(tmp_path):
    """Parameter BINDING behavior (never tested before; only text matching existed).
    `$modelPath` must really be used: different model sizes must produce DIFFERENT context,
    positional and named calls must agree, paths with spaces must not break, a missing path
    must fall back to the 27B default, and LITE must not compute anything.
    (To avoid creating huge files the same distinction is made with a fake free-VRAM value.)"""
    gpu_info = ROOT / "agent" / "gpu-info.ps1"
    small = tmp_path / "kucuk.gguf"
    small.write_bytes(b"x" * (4 * 1024 * 1024))  # 4 MiB -> base ~282 MiB -> hits the ceiling
    spaced_dir = tmp_path / "bosluklu klasor"
    spaced_dir.mkdir()
    spaced = spaced_dir / "bosluklu model.gguf"
    spaced.write_bytes(b"x" * (4 * 1024 * 1024))
    prelude = f"""
$root = '{ROOT}'
function Get-Functions([string]$path) {{
    $t=$null; $e=$null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$t, [ref]$e)
    if ($e.Count) {{ throw ($e | Out-String) }}
    return (($ast.EndBlock.Statements | Where-Object {{ $_ -is [Management.Automation.Language.FunctionDefinitionAst] }} |
        ForEach-Object {{ $_.Extent.Text }}) -join "`n")
}}
. (Join-Path $root 'agent/gpu-info.ps1')
. ([scriptblock]::Create((Get-Functions (Join-Path $root 'scripts/manage-mamidlabs.ps1'))))
"""
    # PLENTY free VRAM: the small model hits the ceiling, the 27B default gives 20480.
    big_free = _fake_smi(tmp_path / "big", ["15538"])
    body = f"""
$env:PATH = '{big_free};' + $env:PATH
{prelude}
'KUCUK_POS='   + (Get-MaxContextForProfile 'flash' '{small}')
'BOSLUK_POS='  + (Get-MaxContextForProfile 'flash' '{spaced}')
'KUCUK_NAMED=' + (Get-MaxContextForProfile -profileName 'flash' -modelPath '{small}')
'YOL_YOK='     + (Get-MaxContextForProfile 'flash')
'BOS_DIZE='    + (Get-MaxContextForProfile 'flash' '')
'VARSAYIM='    + (Get-MaxContextForProfile 'flash' 'C:\\yok\\boyle.gguf')
'LITE=['       + (Get-MaxContextForProfile 'lite' '{small}') + ']'
"""
    out = _run(body, tmp_path, timeout=180)
    # 4 MiB model -> small base -> ceiling; the spaced path must give the SAME result (path not mangled).
    assert "KUCUK_POS=131072" in out, out
    assert "BOSLUK_POS=131072" in out, out
    # Positional and named calls must agree.
    assert "KUCUK_NAMED=131072" in out, out
    # Missing / empty / non-existent path -> 27B default (14878 MiB base) -> 20480.
    assert "YOL_YOK=20480" in out, out
    assert "BOS_DIZE=20480" in out, out
    assert "VARSAYIM=20480" in out, out
    # LITE computes nothing (keeps its fixed context).
    assert "LITE=[]" in out, out

    # TIGHT free VRAM: even the same 4 MiB model cannot reach the ceiling -> must be SMALLER than the default.
    tight_free = _fake_smi(tmp_path / "tight", ["15300"])
    out2 = _run(f"""
$env:PATH = '{tight_free};' + $env:PATH
{prelude}
'TIGHT_KUCUK=' + (Get-MaxContextForProfile 'flash' '{small}')
'TIGHT_DEFAULT=' + (Get-MaxContextForProfile 'flash')
""", tmp_path, timeout=180)
    values = dict(line.split("=", 1) for line in out2.strip().splitlines() if "=" in line)
    assert int(values["TIGHT_KUCUK"]) == 131072, out2   # the small model still hits the ceiling
    assert int(values["TIGHT_DEFAULT"]) == 10240, out2  # the 27B default shrinks with the tight VRAM
    assert int(values["TIGHT_DEFAULT"]) != int(values["TIGHT_KUCUK"]), out2


def test_max_context_uses_the_real_model_size(tmp_path):
    """The context computation must use the REAL model file, not the 27B constant 14878 MiB;
    otherwise a user's own (custom) model gets the wrong context."""
    manage = ROOT / "scripts" / "manage-mamidlabs.ps1"
    text = manage.read_text(encoding="utf-8-sig")
    fn = text.split("function Get-MaxContextForProfile")[1].split("function ")[0]
    assert "[string]$modelPath" in fn
    assert "Length / 1MB" in fn
    assert "$baseMiB = [int][math]::Ceiling" in fn
    assert "$dynamicCtx = Get-MaxContextForProfile $Profile $model" in text

    gpu_info = ROOT / "agent" / "gpu-info.ps1"
    case_dir = _fake_smi(tmp_path, ["15628"])
    small = tmp_path / "kucuk-model.gguf"
    small.write_bytes(b"x" * (4 * 1024 * 1024))  # 4 MiB
    body = f"""
$env:PATH = '{case_dir};' + $env:PATH
$root = '{ROOT}'
function Get-Functions([string]$path) {{
    $t=$null; $e=$null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$t, [ref]$e)
    if ($e.Count) {{ throw ($e | Out-String) }}
    return (($ast.EndBlock.Statements | Where-Object {{ $_ -is [Management.Automation.Language.FunctionDefinitionAst] }} |
        ForEach-Object {{ $_.Extent.Text }}) -join "`n")
}}
. (Join-Path $root 'agent/gpu-info.ps1')
. ([scriptblock]::Create((Get-Functions (Join-Path $root 'scripts/manage-mamidlabs.ps1'))))
'SMALL=' + (Get-MaxContextForProfile 'flash' '{small}')
'DEFAULT=' + (Get-MaxContextForProfile 'flash' '')
'LITE=' + (Get-MaxContextForProfile 'lite' '{small}')
"""
    out = _run(body, tmp_path)
    # Kucuk model -> taban ~282 MiB -> tavana (131072) dayanir.
    assert "SMALL=131072" in out
    # Model yolu yoksa 27B varsayimi (14878) kullanilir.
    assert "DEFAULT=22528" in out
    # LITE sabit baglamini korur (hesap yapilmaz).
    assert "LITE=" in out
