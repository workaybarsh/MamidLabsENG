"""The desktop shortcut did not work: launch-mamidlabs-desktop.vbs had been saved with a
UTF-8 BOM (a byte sequence starting with 0xEF 0xBB 0xBF). The classic VBScript
engine (cscript/wscript - the .vbs file that starts the WinForms app uses it)
does NOT understand a UTF-8 BOM; it rejects the file with an "Invalid character"
compile error. When run with wscript this error shows up as a silent "Windows
Script Host" error window - no console, it looks as if nothing opens, exactly
the symptom the user reported.

Verified on a real machine: the user's real .lnk shortcut (via Start-Process) and
cscript directly were run - BEFORE the fix both failed silently (wscript got
stuck on a "Windows Script Host" error window, cscript printed "compilation error:
Invalid character"). After the BOM was removed both actually opened the MamidLabs window.

This file must always carry ASCII content (no non-ASCII characters are used) -
so without a BOM there is no UTF-8/ANSI difference and it can safely be saved
without a BOM.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VBS = ROOT / "launch-mamidlabs-desktop.vbs"


def test_launcher_has_no_utf8_bom():
    data = VBS.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), (
        "launch-mamidlabs-desktop.vbs was saved with a UTF-8 BOM; the classic VBScript "
        "engine rejects it with an 'Invalid character' compile error and "
        "the desktop shortcut silently opens nothing."
    )


def test_launcher_is_pure_ascii():
    """Since no non-ASCII characters are used, saving without a BOM is safe;
    if a non-ASCII character is added in the future this test catches it -
    in that case a separate solution (e.g. ANSI) is needed instead of BOM-less UTF-8."""
    data = VBS.read_bytes()
    try:
        data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AssertionError(
            "launch-mamidlabs-desktop.vbs now contains a non-ASCII character; "
            "verify the right encoding for the classic VBScript engine (ANSI/UTF-16LE+BOM) "
            "separately, do not silently rely on UTF-8."
        ) from exc
