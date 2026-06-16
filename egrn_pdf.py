"""
Извлечение текста из PDF: текстовый слой + OCR для сканов (ЕГРН, отчёты об оценке).
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("egrn_pdf")

MIN_TEXT_LEN = 80
MAX_OCR_PAGES = 6
MAX_APPRAISAL_OCR_PAGES = 22
MAX_APPRAISAL_TEXT = 35000

_TESSDATA_PATHS = (
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tesseract-ocr/tessdata",
)


def _configure_tesseract() -> str:
    """Находит бинарник tesseract и настраивает pytesseract."""
    candidates = [
        shutil.which("tesseract"),
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
    ]
    path = next((p for p in candidates if p and os.path.isfile(p)), "")
    if not path:
        return ""
    for td in _TESSDATA_PATHS:
        if os.path.isdir(td):
            os.environ.setdefault("TESSDATA_PREFIX", td)
            break
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = path
    except ImportError:
        pass
    return path


def ocr_diagnostics() -> dict:
    """Полная диагностика OCR для логов при старте и при ошибках."""
    diag: dict = {
        "available": False,
        "reason": "",
        "tesseract": "NOT FOUND",
        "tesseract_version": "",
        "pymupdf": "NOT FOUND",
        "pymupdf_version": "",
        "pytesseract": "NOT FOUND",
        "pillow": "NOT FOUND",
    }
    missing: list[str] = []

    tess = _configure_tesseract()
    if tess:
        diag["tesseract"] = tess
        try:
            r = subprocess.run(
                [tess, "--version"],
                capture_output=True, text=True, timeout=8,
            )
            ver = (r.stdout or r.stderr or "").strip().split("\n")[0]
            diag["tesseract_version"] = ver[:120] or "unknown"
        except Exception as e:
            diag["tesseract_version"] = f"version check failed: {e}"
    else:
        missing.append("tesseract binary")

    try:
        import fitz
        diag["pymupdf"] = "ok"
        diag["pymupdf_version"] = getattr(fitz, "__doc__", "")[:20] or str(getattr(fitz, "VersionBind", ""))[:30]
        try:
            diag["pymupdf_version"] = fitz.__version__  # type: ignore[attr-defined]
        except Exception:
            pass
    except ImportError:
        missing.append("pymupdf/fitz")
        diag["pymupdf"] = "NOT FOUND"

    try:
        import pytesseract  # noqa: F401
        diag["pytesseract"] = "ok"
    except ImportError:
        missing.append("pytesseract")
        diag["pytesseract"] = "NOT FOUND"

    try:
        from PIL import Image  # noqa: F401
        diag["pillow"] = "ok"
    except ImportError:
        missing.append("Pillow")
        diag["pillow"] = "NOT FOUND"

    if missing:
        diag["reason"] = ", ".join(missing)
    else:
        diag["available"] = True
        diag["reason"] = "ok"
    return diag


def ocr_available() -> bool:
    return ocr_status()[0]


def ocr_status() -> tuple[bool, str]:
    """(available, reason) — для логов при старте."""
    d = ocr_diagnostics()
    if d["available"]:
        tv = d.get("tesseract_version") or "?"
        return True, f"ok; tesseract={tv[:60]}"
    return False, d.get("reason") or "unknown"


def extract_pdf_text(raw: bytes, max_pages: int = 8) -> tuple[str, str]:
    """ЕГРН и прочие PDF: text → pymupdf → OCR (до 6 стр.)."""
    try:
        if not raw or b"%PDF" not in raw[:10]:
            return "", "failed"

        text = _extract_with_pdfplumber(raw, max_pages)
        if len(text) >= MIN_TEXT_LEN:
            return text[:12000], "text"

        text = _extract_with_pymupdf(raw, max_pages)
        if len(text) >= MIN_TEXT_LEN:
            return text[:12000], "text"

        ocr_text = _ocr_pdf(raw, min(max_pages, MAX_OCR_PAGES), dpi=2.0, log_prefix="egrn")
        if len(ocr_text) >= MIN_TEXT_LEN:
            return ocr_text[:12000], "ocr"

        return (text or ocr_text or "").strip(), "failed"
    except Exception as e:
        log.warning("extract_pdf_text failed: %s", e)
        return "", "failed"


def extract_appraisal_pdf_text(raw: bytes) -> tuple[str, str]:
    """
    Отчёт об оценке (часто скан): агрессивный OCR до 22 стр., DPI 2.5.
    Подробные логи на каждом шаге — для диагностики на Railway.
    """
    prefix = "appraisal"
    try:
        if not raw or b"%PDF" not in raw[:10]:
            log.warning("%s step: not a PDF (bytes=%d)", prefix, len(raw or b""))
            return "", "failed"

        log.info("%s step: downloaded bytes=%d", prefix, len(raw))
        diag = ocr_diagnostics()
        if not diag["available"]:
            log.warning(
                "%s step: OCR unavailable — %s; tesseract=%s pymupdf=%s",
                prefix, diag["reason"], diag["tesseract"], diag["pymupdf"],
            )

        page_count = _pdf_page_count(raw)
        log.info("%s step: pdf_pages=%s", prefix, page_count if page_count else "?")

        for max_p, label in ((12, "text12"), (MAX_APPRAISAL_OCR_PAGES, "text22")):
            text = _extract_with_pdfplumber(raw, max_p)
            log.info("%s step: pdfplumber chars=%d (max_pages=%d)", prefix, len(text), max_p)
            if len(text) >= MIN_TEXT_LEN:
                log.info("%s step: using text layer method=%s", prefix, label)
                return text[:MAX_APPRAISAL_TEXT], "text"

        text = _extract_with_pymupdf(raw, MAX_APPRAISAL_OCR_PAGES)
        log.info("%s step: pymupdf chars=%d", prefix, len(text))
        if len(text) >= MIN_TEXT_LEN:
            log.info("%s step: using pymupdf text layer", prefix)
            return text[:MAX_APPRAISAL_TEXT], "text"

        if not diag["available"]:
            partial = (text or "").strip()
            log.warning(
                "%s step: OCR skipped (deps missing); partial_chars=%d",
                prefix, len(partial),
            )
            return partial, "failed" if len(partial) < 60 else "ocr_partial"

        log.info(
            "%s step: OCR starting pages=%d dpi=2.5 tesseract=%s",
            prefix, min(page_count or MAX_APPRAISAL_OCR_PAGES, MAX_APPRAISAL_OCR_PAGES),
            diag["tesseract"],
        )
        ocr_text = _ocr_pdf(
            raw, MAX_APPRAISAL_OCR_PAGES, dpi=2.5, log_prefix=prefix,
        )
        log.info("%s step: OCR finished chars=%d", prefix, len(ocr_text))
        if len(ocr_text) >= 60:
            return ocr_text[:MAX_APPRAISAL_TEXT], "ocr"

        partial = (text or ocr_text or "").strip()
        log.warning(
            "%s step: all methods weak; partial_chars=%d method=%s",
            prefix, len(partial), "failed" if len(partial) < 60 else "ocr_partial",
        )
        return partial, "failed" if len(partial) < 60 else "ocr_partial"
    except Exception as e:
        log.exception("%s step: extract_appraisal_pdf_text failed: %s", prefix, e)
        return "", "failed"


def _pdf_page_count(raw: bytes) -> int:
    try:
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        n = len(doc)
        doc.close()
        return n
    except Exception as e:
        log.debug("page count failed: %s", e)
        return 0


def _extract_with_pdfplumber(raw: bytes, max_pages: int) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            parts = [(p.extract_text() or "") for p in pdf.pages[:max_pages]]
        return "\n".join(parts)
    except Exception as e:
        log.debug("pdfplumber: %s", e)
        return ""


def _extract_with_pymupdf(raw: bytes, max_pages: int) -> str:
    try:
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        parts = [doc[i].get_text() for i in range(min(len(doc), max_pages))]
        doc.close()
        return "\n".join(parts)
    except Exception as e:
        log.debug("pymupdf text: %s", e)
        return ""


def _ocr_pdf(raw: bytes, max_pages: int, dpi: float = 2.0, log_prefix: str = "ocr") -> str:
    try:
        import fitz
        import pytesseract
        from PIL import Image
    except ImportError as e:
        log.warning("%s: OCR deps missing: %s", log_prefix, e)
        return ""

    tess = _configure_tesseract()
    if not tess:
        log.warning("%s: tesseract binary NOT FOUND in PATH", log_prefix)
        return ""

    scale = dpi
    parts: list[str] = []
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        n = min(len(doc), max_pages)
        log.info("%s step: convert_to_image pages=%d dpi=%.1f", log_prefix, n, dpi)
        for i in range(n):
            page = doc[i]
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            try:
                chunk = pytesseract.image_to_string(img, lang="rus+eng", config="--psm 6")
            except Exception as pe:
                log.warning("%s step: OCR page %d/%d error: %s", log_prefix, i + 1, n, pe)
                chunk = ""
            clen = len((chunk or "").strip())
            log.info("%s step: OCR page %d/%d chars=%d", log_prefix, i + 1, n, clen)
            if chunk.strip():
                parts.append(chunk)
        doc.close()
    except Exception as e:
        log.warning("%s: OCR failed: %s", log_prefix, e)
        return ""
    total = "\n".join(parts)
    log.info("%s step: OCR total_chars=%d pages_with_text=%d", log_prefix, len(total), len(parts))
    return total


def discover_pdf_urls(html: str, lot_id: str) -> list[str]:
    """Все PDF со страницы лота; угадываемые URL ЕГРН — в конце как fallback."""
    urls: list[str] = []
    if html:
        for m in re.finditer(r'https?://[^"\'>\s]+\.pdf(?:[^"\'>\s]*)?', html, re.I):
            urls.append(m.group(0).split("&quot;")[0].split('"')[0])
        for m in re.finditer(r'["\']([^"\']+\.pdf(?:[^"\']*)?)["\']', html, re.I):
            u = m.group(1).replace("\\/", "/")
            if u.startswith("//"):
                u = "https:" + u
            elif not u.startswith("http"):
                u = "https://tbankrot.ru" + (u if u.startswith("/") else "/" + u)
            urls.append(u)
        for m in re.finditer(
            r'(?:href|data-url|data-href)\s*=\s*["\']([^"\']*(?:pdf|files\.tbankrot)[^"\']*)["\']',
            html, re.I,
        ):
            u = m.group(1)
            if ".pdf" in u.lower() or "files.tbankrot" in u.lower():
                if not u.startswith("http"):
                    u = "https://tbankrot.ru" + (u if u.startswith("/") else "/" + u)
                urls.append(u)

    for u in (
        f"https://files.tbankrot.ru/egrn_files/{lot_id}.pdf",
        f"https://tbankrot.ru/files/egrn/{lot_id}.pdf",
    ):
        urls.append(u)

    seen, out = set(), []
    for u in urls:
        u = u.split("#")[0].strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out
