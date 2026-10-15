import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    end = source.find("\nfunction ", start + 1)
    return source[start:end if end != -1 else len(source)]


def test_delete_all_keeps_the_open_chat(tmp_path):
    """'Delete all' must never delete the chat currently on screen; the open
    chat has to stay in history. The decision is made by the pure
    Get-DeletableChatPaths helper; verified in a real PowerShell here (the open
    chat's file is removed from the delete set, the others are kept; when there
    is no open chat every chat can be deleted)."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    desktop = (ROOT / "mamidlabs-desktop.ps1").read_text(encoding="utf-8-sig")
    function = extract_function(desktop, "Get-DeletableChatPaths")
    chat_dir = tmp_path / "chats"
    chat_dir.mkdir()
    open_id = "a" * 32
    other1 = "b" * 32
    other2 = "c" * 32
    for cid in (open_id, other1, other2):
        (chat_dir / f"{cid}.json").write_text("x", encoding="utf-8")
    body = (
        "$ErrorActionPreference = 'Stop'\n"
        + function
        + "\n$dir = '" + str(chat_dir) + "'\n"
        + "$ids = @('" + open_id + "', '" + other1 + "', '" + other2 + "')\n"
        + "$paths = @($ids | ForEach-Object { Join-Path $dir \"$_.json\" })\n"
        + "$withKeep = @(Get-DeletableChatPaths $dir $paths '" + open_id + "')\n"
        + "$noKeep = @(Get-DeletableChatPaths $dir $paths '')\n"
        + "$nullKeep = @(Get-DeletableChatPaths $dir $paths $null)\n"
        + "@{ withKeep = $withKeep.Count; noKeep = $noKeep.Count; nullKeep = $nullKeep.Count; "
        + "keepsOpen = -not (($withKeep -join ';').Contains('" + open_id + ".json')) } | ConvertTo-Json -Compress\n"
    )
    script = tmp_path / "delete.ps1"
    script.write_text(body, encoding="utf-8-sig")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got == {"withKeep": 2, "noKeep": 3, "nullKeep": 3, "keepsOpen": True}


def test_delete_all_handler_protects_open_chat():
    """The delete loop now runs over the filtered $targets instead of the whole
    list ($entries); the open chat is announced to the user."""
    desktop = (ROOT / "mamidlabs-desktop.ps1").read_text(encoding="utf-8-sig")
    assert "Get-DeletableChatPaths $chatDir $entries $script:chatId" in desktop
    assert "foreach ($path in @($targets)) { Remove-Item" in desktop
    assert "The open chat is kept." in desktop
