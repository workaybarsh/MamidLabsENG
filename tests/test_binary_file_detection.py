"""Binary file detection for read_bound_file in agent/qwen-inference.ps1.

If an .exe/.zip/image is read as text, garbage characters end up in the model context
(wasted tokens, undefined behavior). Binary files are detected and rejected; UTF-8 text
(including non-ASCII characters) is read normally. The function runs in isolation
against real files, without loading the whole inference script.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_read_bound_file_checks_for_binary_before_reading_as_text():
    inference = read_text("agent/qwen-inference.ps1")

    assert "function Test-BinaryFile" in inference
    assert "if (Test-BinaryFile $target)" in inference
    # The check must run before reading the file (before ReadAllText).
    check_index = inference.index("if (Test-BinaryFile $target)")
    read_index = inference.index("[IO.File]::ReadAllText($target)")
    assert check_index < read_index


def _run_function_isolated(function_text: str, invocation: str, tmp_path: Path) -> str:
    """Runs the function in isolation, without dot-sourcing the whole qwen-inference.ps1
    (including the top-level code that expects JobData/Shared). Uses -File instead of
    -Command: non-ASCII characters in a -Command argument can get garbled between the
    Python -> pwsh encoding layers; writing to a file with a UTF-8 BOM (the same technique
    as run_ps in test_desktop_behavior.py) prevents that."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    script_path = tmp_path / "test.ps1"
    script_path.write_text(function_text + "\n" + invocation, encoding="utf-8-sig")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script_path)],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _extract_function(name: str) -> str:
    inference = read_text("agent/qwen-inference.ps1")
    start = inference.index(f"function {name}(")
    # A simple brace counter: finds the close of the function body.
    depth = 0
    started = False
    for index in range(start, len(inference)):
        char = inference[index]
        if char == "{":
            depth += 1
            started = True
        elif char == "}":
            depth -= 1
            if started and depth == 0:
                return inference[start:index + 1]
    raise AssertionError(f"could not find the end of the {name} function")


@pytest.mark.parametrize("filename,expect_binary", [
    ("assets/icons/mamidlabs.ico", True),
    ("assets/icons/mamidlabs-64.png", True),
    ("README.md", False),
    ("scripts/manage-qwen.ps1", False),
])
def test_binary_detection_on_real_repository_files(filename, expect_binary, tmp_path):
    """Runs on real repository files; not mock data."""
    function_text = _extract_function("Test-BinaryFile")
    target = ROOT / filename
    assert target.exists(), f"test file missing: {target}"

    output = _run_function_isolated(function_text, f"Test-BinaryFile '{target}'", tmp_path)
    assert output.strip() == str(expect_binary)


def test_binary_detection_handles_empty_file(tmp_path):
    function_text = _extract_function("Test-BinaryFile")
    # Benzersiz yol (tmp_path) kullan: sabit paylasilan $env:TEMP adi, suite icinde
    # baska bir islem/dosyayla cakisip testi aralikli (flaky) bozabiliyordu.
    empty = (tmp_path / "empty.txt").as_posix()
    output = _run_function_isolated(
        function_text,
        f"New-Item -ItemType File -Path '{empty}' -Force | Out-Null; Test-BinaryFile '{empty}'",
        tmp_path,
    )
    assert output.strip() == "False"


def test_binary_detection_handles_non_ascii_utf8_text(tmp_path):
    """Non-ASCII characters (UTF-8 multi-byte) must not be mistaken for binary.
    Verified on a real machine: a file containing 'ığüşöç İĞÜŞÖÇ' returns False."""
    function_text = _extract_function("Test-BinaryFile")
    sample_path = tmp_path / "sample.txt"
    sample_path.write_text("A sample text with accented characters: ığüşöç İĞÜŞÖÇ é ñ ß", encoding="utf-8")
    output = _run_function_isolated(
        function_text,
        f"Test-BinaryFile '{sample_path}'",
        tmp_path,
    )
    assert output.strip() == "False"
