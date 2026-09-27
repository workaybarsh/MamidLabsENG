"""Document edge cases: corrupt, encrypted and permission-restricted PDFs.

- PDF encrypted with a user password: a clear error message instead of a technical
  exception.
- PDF with only restricted permissions (empty user password): opened with decrypt('')
  and read; the distinction uses decrypt('')'s return code, not is_encrypted.
- Corrupt PDF: main() wraps every handler in a generic try/except; the error comes back
  in the JSON error field and the worker does not crash.
"""
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "agent" / "office-tools.py"
SPEC = importlib.util.spec_from_file_location("office_tools", WORKER)
OFFICE_TOOLS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(OFFICE_TOOLS)


def make_plain_pdf_to_buffer(dest, text):
    """dest can be a file path (str) or a BytesIO."""
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(dest)
    c.drawString(50, 700, text)
    c.save()
    if hasattr(dest, "seek"):
        dest.seek(0)


def run_worker(payload: dict) -> dict:
    """Runs agent/office-tools.py in a separate process the way it is really called
    (stdin JSON -> stdout JSON); also covers the general try/except in main()."""
    proc = subprocess.run(
        [sys.executable, str(WORKER)],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    assert proc.returncode == 0, f"the worker must not have crashed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_read_pdf_works_on_a_plain_unencrypted_file(tmp_path):
    pdf = tmp_path / "plain.pdf"
    make_plain_pdf_to_buffer(str(pdf), "Plain unencrypted PDF text.")
    result = OFFICE_TOOLS.read_pdf({"path": str(pdf)})
    assert "error" not in result
    assert "Plain unencrypted PDF text" in result["text"]


def test_read_pdf_opens_permission_only_encrypted_files(tmp_path):
    """PDFs with an empty user password that only restrict permissions are very common
    (e.g. documents exported with 'prevent printing') and must really be
    readable."""
    from pypdf import PdfReader, PdfWriter

    buf = io.BytesIO()
    make_plain_pdf_to_buffer(buf, "Permission restricted but readable text.")
    plain = PdfReader(buf)
    writer = PdfWriter()
    writer.add_page(plain.pages[0])
    writer.encrypt(user_password="", owner_password="ownerpassword", permissions_flag=-4)
    pdf = tmp_path / "perm-only.pdf"
    with open(pdf, "wb") as f:
        writer.write(f)

    result = OFFICE_TOOLS.read_pdf({"path": str(pdf)})
    assert "error" not in result, f"permission-restricted PDF could not be read: {result}"
    assert "Permission restricted but readable text" in result["text"]


def test_read_pdf_gives_a_clear_error_for_password_protected_files(tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("arealpassword")
    pdf = tmp_path / "encrypted.pdf"
    with open(pdf, "wb") as f:
        writer.write(f)

    result = OFFICE_TOOLS.read_pdf({"path": str(pdf)})
    assert result.get("error") == "This PDF is password protected; it cannot be read without the password."


def test_worker_reports_a_readable_error_for_a_corrupt_pdf_without_crashing(tmp_path):
    pdf = tmp_path / "corrupt.pdf"
    pdf.write_bytes(b"%PDF-1.4 corrupt data" + bytes([0, 1, 2, 3]) * 50)

    result = run_worker({"action": "read_pdf", "path": str(pdf)})
    assert "error" in result
    assert result["error"]  # a non-empty, readable message
