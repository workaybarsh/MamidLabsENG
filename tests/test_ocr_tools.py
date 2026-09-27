import base64
import json
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "agent" / "ocr-tools.py"
SPEC = importlib.util.spec_from_file_location("ocr_tools", WORKER)
OCR_TOOLS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(OCR_TOOLS)


class OcrToolsTests(unittest.TestCase):
    def test_classifies_text_weighted_output_as_text(self):
        self.assertEqual(
            OCR_TOOLS.classify_text("MamidLabs local AI assistant\nSecond line"),
            "text",
        )


    def test_classifies_short_output_as_mixed(self):
        self.assertEqual(OCR_TOOLS.classify_text("SALE"), "mixed")


    def test_classifies_empty_output_as_visual(self):
        self.assertEqual(OCR_TOOLS.classify_text(""), "visual")

    def test_extracts_paddleocr_3_result_pairs(self):
        pairs = list(
            OCR_TOOLS._pairs_from_result(
                {
                    "rec_texts": ["First line", "Second line"],
                    "rec_scores": [0.91, 0.83],
                }
            )
        )
        self.assertEqual(pairs, [("First line", 0.91), ("Second line", 0.83)])


    def test_missing_image_returns_json_fallback(self):
        process = subprocess.run(
            [sys.executable, str(WORKER)],
            input=json.dumps({"action": "ocr_image", "path": str(ROOT / "does-not-exist.png")}),
            text=True,
            capture_output=True,
            check=True,
        )
        response = json.loads(process.stdout)
        self.assertTrue(response["error"])
        self.assertIn("not found", response["error"])

    def test_missing_pdf_returns_json_fallback(self):
        process = subprocess.run(
            [sys.executable, str(WORKER)],
            input=json.dumps({"action": "ocr_pdf", "path": str(ROOT / "does-not-exist.pdf")}),
            text=True,
            capture_output=True,
            check=True,
        )
        response = json.loads(process.stdout)
        self.assertTrue(response["error"])
        self.assertIn("PDF not found", response["error"])

    def test_plain_image_ocr_uses_gpu_engine(self):
        with self.subTest("device selection"):
            image = ROOT / "tests" / "ocr-device-test.png"
            image.touch()
            devices = []
            try:
                with patch.object(OCR_TOOLS, "_new_engine", side_effect=lambda device: devices.append(device) or object()), patch.object(
                    OCR_TOOLS,
                    "_run_ocr",
                    return_value=[{"rec_texts": ["GPU text"], "rec_scores": [0.9]}],
                ):
                    response = OCR_TOOLS.ocr_image({"path": str(image)})
            finally:
                image.unlink()

        self.assertEqual(devices, ["gpu"])
        self.assertEqual(response["engine"], "paddleocr-gpu")

    def test_plain_image_ocr_does_not_fall_back_to_cpu_when_gpu_returns_no_text(self):
        image = ROOT / "tests" / "ocr-device-empty-test.png"
        image.touch()
        devices = []
        try:
            with patch.object(OCR_TOOLS, "_new_engine", side_effect=lambda device: devices.append(device) or object()), patch.object(
                OCR_TOOLS,
                "_run_ocr",
                return_value=[],
            ):
                response = OCR_TOOLS.ocr_image({"path": str(image)})
        finally:
            image.unlink()

        self.assertEqual(devices, ["gpu"])
        self.assertEqual(response["text"], "")
        self.assertEqual(response["engine"], "paddleocr-gpu")

    def test_multiple_image_ocr_uses_one_gpu_engine_sequentially(self):
        images = [ROOT / "tests" / f"ocr-batch-{index}.png" for index in (1, 2)]
        for image in images:
            image.touch()
        devices = []
        try:
            with patch.object(OCR_TOOLS, "_new_engine", side_effect=lambda device: devices.append(device) or object()), patch.object(
                OCR_TOOLS,
                "_run_ocr",
                side_effect=[
                    [{"rec_texts": ["First image"], "rec_scores": [0.91]}],
                    [{"rec_texts": ["Second image"], "rec_scores": [0.87]}],
                ],
            ):
                response = OCR_TOOLS.ocr_images({"paths": [str(image) for image in images]})
        finally:
            for image in images:
                image.unlink()

        self.assertEqual(devices, ["gpu"])
        self.assertEqual(response["image_count"], 2)
        self.assertEqual(response["success_count"], 2)
        self.assertIn("[Image 1]", response["text"])
        self.assertIn("First image", response["text"])
        self.assertIn("[Image 2]", response["text"])
        self.assertIn("Second image", response["text"])

    def test_pdf_ocr_keeps_cpu_engine(self):
        pdf = ROOT / "tests" / "ocr-device-test.pdf"
        pdf.touch()
        devices = []
        try:
            with patch.object(OCR_TOOLS, "_new_engine", side_effect=lambda device: devices.append(device) or object()), patch.object(
                OCR_TOOLS,
                "_run_ocr",
                return_value=[{"rec_texts": ["PDF text"], "rec_scores": [0.9]}],
            ):
                response = OCR_TOOLS.ocr_pdf({"path": str(pdf)})
        finally:
            pdf.unlink()

        self.assertEqual(devices, ["cpu"])
        self.assertEqual(response["engine"], "paddleocr-cpu")

    def test_windows_powershell_pipe_preserves_non_ascii_paths(self):
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("PowerShell 7 is unavailable")
        # The OCR environment only exists in an installed tree (ROOT\venv\ocr).
        if not (ROOT / "venv" / "ocr" / "Scripts" / "python.exe").exists():
            self.skipTest("venv\\ocr is unavailable (run in an installed tree)")

        # ocr-tools.py derives CACHE_ROOT from its own location; the test also uses the
        # ROOT where the real files live.
        script = r"""
$ErrorActionPreference = 'Stop'
$python = '__ROOT__\venv\ocr\Scripts\python.exe'
$worker = '__ROOT__\agent\ocr-tools.py'
$path = 'C:\Users\test\Pictures\Ekran g' + [char]0x00F6 + 'r' + [char]0x00FC + 'nt' + [char]0x00FC + 's' + [char]0x00FC + '.png'
$payload = @{ action = 'ocr_image'; path = $path } | ConvertTo-Json -Compress
$psi = [Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $python
$psi.ArgumentList.Add($worker)
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
$psi.StandardOutputEncoding = [Text.Encoding]::UTF8
$process = [Diagnostics.Process]::Start($psi)
$process.StandardInput.Write($payload)
$process.StandardInput.Close()
$output = $process.StandardOutput.ReadToEnd()
$process.StandardError.ReadToEnd() | Out-Null
$process.WaitForExit()
Write-Output ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($output)))
""".replace("__ROOT__", str(ROOT))

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ps1", encoding="utf-8-sig", delete=False
        ) as handle:
            handle.write(script)
            script_path = Path(handle.name)
        try:
            process = subprocess.run(
                [str(pwsh), "-NoProfile", "-NonInteractive", "-File", str(script_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
        finally:
            script_path.unlink(missing_ok=True)

        response = json.loads(base64.b64decode(process.stdout.strip()).decode("utf-8"))
        self.assertIn("Ekran görüntüsü.png", response["error"])
        self.assertNotIn("gÃ", response["error"])
