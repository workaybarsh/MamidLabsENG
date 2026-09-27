"""PaddleOCR worker used by the MamidLabs inference job.

The process accepts exactly one JSON request on stdin and writes exactly one JSON
response on stdout. Paddle's model/download logs are redirected so they cannot
corrupt the protocol consumed by PowerShell.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


# Derive the install root from this file's own location: this file always lives under <root>/agent/.
CACHE_ROOT = str(Path(__file__).resolve().parent.parent / "tools" / "paddle-cache")
MAX_IMAGE_FILE_BYTES = 100 * 1024 * 1024
MAX_PDF_FILE_BYTES = 256 * 1024 * 1024
MAX_RENDER_PIXELS = 40_000_000


def _validate_input(path: str, limit: int, label: str) -> None:
    size = Path(path).stat().st_size
    if size > limit:
        raise ValueError(f"{label} size exceeded the safety limit.")


def _configure_cache() -> None:
    # Paddle and PaddleX use different cache variables across releases.
    os.environ["PADDLE_HOME"] = CACHE_ROOT
    os.environ["PADDLEOCR_HOME"] = CACHE_ROOT
    os.environ["PADDLEX_HOME"] = CACHE_ROOT
    os.environ["PADDLE_PDX_HOME"] = CACHE_ROOT
    os.environ["PADDLE_PDX_CACHE_HOME"] = CACHE_ROOT
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    # PaddlePaddle 3.3.1's Windows CPU oneDNN path fails on OCR model
    # attributes; the regular CPU executor is stable and still CPU-only.
    os.environ["FLAGS_use_mkldnn"] = "0"


def classify_text(text: str) -> str:
    """Classify OCR output by how much readable text it contains.

    A short label or a few isolated words are useful mixed visual/text input;
    longer output is text-dominant. Empty OCR output is a visual image.
    """
    normalized = (text or "").strip()
    character_count = len(re.findall(r"\w", normalized, flags=re.UNICODE))
    if character_count == 0:
        return "visual"
    if character_count >= 20 or len(normalized.splitlines()) >= 3:
        return "text"
    return "mixed"


def _quiet_call(function, *args, **kwargs):
    """Run Paddle imports/model calls without leaking their console output."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return function(*args, **kwargs)


def _new_engine(device: str = "cpu"):
    _configure_cache()
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from paddleocr import PaddleOCR

    requested_device = "gpu:0" if device == "gpu" else "cpu"
    # PaddleOCR 3.x defaults to PP-OCRv6, whose medium recognition model is
    # not multilingual. Use the multilingual PP-OCRv5 pipeline explicitly.
    # lang="tr" selects PaddleOCR's shared Latin-script recognizer
    # (latin_PP-OCRv5_mobile_rec), which reads English as well as accented
    # Latin-script languages; "latin" itself is not an accepted language code.
    try:
        return _quiet_call(
            PaddleOCR,
            lang="tr",
            ocr_version="PP-OCRv5",
            device=requested_device,
            enable_mkldnn=False,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    except (TypeError, ValueError):
        # PaddleOCR 2.x uses use_gpu/use_angle_cls and rejects the 3.x options.
        return _quiet_call(
            PaddleOCR,
            lang="tr",
            use_gpu=device == "gpu",
            use_angle_cls=True,
            show_log=False,
        )


def _as_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if callable(value):
        try:
            value = value()
        except TypeError:
            return value
    return value


def _result_payload(result: Any) -> Any:
    result = _as_json(result)
    if hasattr(result, "json"):
        result = _as_json(result.json)
    if hasattr(result, "res"):
        result = _as_json(result.res)
    if isinstance(result, dict) and isinstance(result.get("res"), dict):
        result = result["res"]
    return result


def _pairs_from_result(result: Any) -> Iterable[tuple[str, float]]:
    payload = _result_payload(result)
    if isinstance(payload, dict):
        texts = payload.get("rec_texts") or payload.get("texts") or payload.get("text")
        scores = payload.get("rec_scores") or payload.get("scores") or payload.get("confidence")
        if isinstance(texts, str):
            texts = [texts]
        if isinstance(texts, list):
            scores = scores if isinstance(scores, list) else []
            for index, text in enumerate(texts):
                if text is None or not str(text).strip():
                    continue
                score = scores[index] if index < len(scores) else 0.0
                try:
                    score = float(score)
                except (TypeError, ValueError):
                    score = 0.0
                yield str(text).strip(), score
            return

    # PaddleOCR 2.x returns [[[box], [text, score]], ...].
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, list) and len(item) >= 2:
                candidate = item[1]
                if isinstance(candidate, (list, tuple)) and candidate:
                    text = str(candidate[0]).strip()
                    if text:
                        try:
                            score = float(candidate[1]) if len(candidate) > 1 else 0.0
                        except (TypeError, ValueError):
                            score = 0.0
                        yield text, score
                else:
                    yield from _pairs_from_result(item)


def _extract_pairs(results: Any) -> list[tuple[str, float]]:
    pairs: list[tuple[str, float]] = []
    if isinstance(results, list):
        for result in results:
            pairs.extend(_pairs_from_result(result))
    else:
        pairs.extend(_pairs_from_result(results))
    return pairs


def _run_ocr(engine: Any, path: str) -> Any:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        if hasattr(engine, "predict"):
            return list(engine.predict(path))
        return engine.ocr(path, cls=True)


def _ocr_response(pairs: list[tuple[str, float]], **extra: Any) -> dict[str, Any]:
    text = "\n".join(item[0] for item in pairs)
    confidence = sum(item[1] for item in pairs) / len(pairs) if pairs else 0.0
    response = {
        "text": text,
        "confidence": round(confidence, 4),
        "char_count": len(text),
        "line_count": len(pairs),
        "classification": classify_text(text),
        "engine": "paddleocr-cpu",
    }
    response.update(extra)
    return response


def ocr_image(request: dict[str, Any]) -> dict[str, Any]:
    path = str(request.get("path") or "")
    if not path:
        return {"error": "A file path is required for OCR."}
    if not Path(path).is_file():
        return {"error": f"Image not found: {path}"}
    _validate_input(path, MAX_IMAGE_FILE_BYTES, "Image")

    gpu_engine = _new_engine("gpu")
    gpu_pairs = _extract_pairs(_run_ocr(gpu_engine, path))
    return _ocr_response(gpu_pairs, engine="paddleocr-gpu")


def ocr_images(request: dict[str, Any]) -> dict[str, Any]:
    """OCR multiple images sequentially with one shared GPU engine."""
    requested_paths = request.get("paths") or []
    if isinstance(requested_paths, str):
        requested_paths = [requested_paths]
    paths = [str(path) for path in requested_paths if str(path)]
    if not paths:
        return {"error": "At least one image path is required for multi-image OCR."}

    engine = None
    results: list[dict[str, Any]] = []
    for index, path in enumerate(paths, start=1):
        item: dict[str, Any] = {"index": index, "path": path}
        try:
            if not Path(path).is_file():
                raise FileNotFoundError(f"Image not found: {path}")
            _validate_input(path, MAX_IMAGE_FILE_BYTES, "Image")
            if engine is None:
                engine = _new_engine("gpu")
            item.update(_ocr_response(_extract_pairs(_run_ocr(engine, path)), engine="paddleocr-gpu"))
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
            item.update(_ocr_response([], engine="paddleocr-gpu"))
        results.append(item)

    text_parts = []
    for item in results:
        if item.get("text"):
            text_parts.append(f"[Image {item['index']}]\n{item['text']}")
    combined_text = "\n\n".join(text_parts)
    successful = sum(1 for item in results if item.get("text"))
    confidence_values = [float(item["confidence"]) for item in results if item.get("text")]
    return {
        "text": combined_text,
        "confidence": round(sum(confidence_values) / len(confidence_values), 4) if confidence_values else 0.0,
        "char_count": len(combined_text),
        "line_count": sum(int(item.get("line_count", 0)) for item in results),
        "classification": classify_text(combined_text),
        "results": results,
        "image_count": len(results),
        "success_count": successful,
        "failed_count": len(results) - successful,
        "engine": "paddleocr-gpu-batch",
    }


def ocr_pdf(request: dict[str, Any]) -> dict[str, Any]:
    """OCR a scanned/near-empty PDF, preferring PaddleOCR's native PDF input."""
    path = str(request.get("path") or "")
    if not path:
        return {"error": "A file path is required for PDF OCR."}
    if not Path(path).is_file():
        return {"error": f"PDF not found: {path}"}
    _validate_input(path, MAX_PDF_FILE_BYTES, "PDF")

    # PDF OCR deliberately stays on the existing CPU path. It handles native
    # PDFs and rendered pages differently from the single-image fast path.
    engine = _new_engine("cpu")
    direct_error = None
    try:
        pairs = _extract_pairs(_run_ocr(engine, path))
        if pairs:
            return _ocr_response(pairs, page_count=request.get("max_pages"))
    except Exception as exc:
        direct_error = exc

    # PaddleOCR 2.x cannot consume PDFs directly. Render pages with PyMuPDF,
    # which is installed by install-ocr.ps1, and feed each page as an image.
    try:
        import fitz
    except ImportError as exc:
        detail = f"{type(direct_error).__name__}: {direct_error}" if direct_error else str(exc)
        return {"error": f"PyMuPDF is missing for PDF OCR: {detail}"}

    max_pages = max(1, min(int(request.get("max_pages", 50)), 100))
    pairs = []
    try:
        with fitz.open(path) as document, tempfile.TemporaryDirectory(prefix="mamidlabs-ocr-") as temp_dir:
            page_count = min(len(document), max_pages)
            for index in range(page_count):
                page = document.load_page(index)
                estimated_pixels = int(page.rect.width * 2) * int(page.rect.height * 2)
                if estimated_pixels > MAX_RENDER_PIXELS:
                    raise ValueError("The PDF page exceeded the pixel limit.")
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                page_path = os.path.join(temp_dir, f"page-{index + 1}.png")
                pixmap.save(page_path)
                pairs.extend(_extract_pairs(_run_ocr(engine, page_path)))
    except Exception as exc:
        return {"error": f"PDF OCR failed: {type(exc).__name__}: {exc}"}
    return _ocr_response(pairs, page_count=page_count)


def warmup(_request: dict[str, Any]) -> dict[str, Any]:
    device = str(_request.get("device") or "gpu")
    _new_engine(device)
    return {"ready": True, "engine": f"paddleocr-{device}", "cache": CACHE_ROOT}


ACTIONS = {"ocr_image": ocr_image, "ocr_images": ocr_images, "ocr_pdf": ocr_pdf, "warmup": warmup}


def _configure_stdout() -> None:
    """Keep the JSON protocol UTF-8 across Windows pipes and consoles."""
    if hasattr(sys.stdin, "reconfigure"):
        # PowerShell writes request JSON as UTF-8. Without this explicit
        # setting, a Windows pipe can be decoded with the active ANSI code
        # page, turning non-ASCII paths such as ``görüntüsü`` into ``gÃ¶rÃ¼ntÃ¼sÃ¼``.
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def main() -> None:
    _configure_stdout()
    try:
        request = json.loads(sys.stdin.read() or "{}")
        handler = ACTIONS.get(request.get("action"))
        if not handler:
            response = {"error": f"Unknown OCR action: {request.get('action')}"}
        else:
            response = handler(request)
    except Exception as exc:  # The worker must never crash the PowerShell job.
        response = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
