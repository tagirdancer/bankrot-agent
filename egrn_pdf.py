"""
Извлечение текста из PDF выписки ЕГРН: текстовый слой + OCR для сканов.
"""
from __future__ import annotations

import io
import logging
import re

log = logging.getLogger("egrn_pdf")

MIN_TEXT_LEN = 80
MAX_OCR_PAGES = 6


def extract_pdf_text(raw: bytes, max_pages: int = 8) -> tuple[str, str]:
    """
    Возвращает (text, method):
      - method='text' — текстовый слой PDF
      - method='ocr'  — распознано со скана
      - method='failed' — не удалось
    """
    try:
        if not raw or b"%PDF" not in raw[:10]:
            return "", "failed"

        text = _extract_with_pdfplumber(raw, max_pages)
        if len(text) >= MIN_TEXT_LEN:
            return text[:12000], "text"

        text = _extract_with_pymupdf(raw, max_pages)
        if len(text) >= MIN_TEXT_LEN:
            return text[:12000], "text"

        ocr_text = _ocr_pdf(raw, min(max_pages, MAX_OCR_PAGES))
        if len(ocr_text) >= MIN_TEXT_LEN:
            return ocr_text[:12000], "ocr"

        return (text or ocr_text or "").strip(), "failed"
    except Exception as e:
        log.warning("extract_pdf_text failed: %s", e)
        return "", "failed"


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
        import fitz  # pymupdf
        doc = fitz.open(stream=raw, filetype="pdf")
        parts = [doc[i].get_text() for i in range(min(len(doc), max_pages))]
        doc.close()
        return "\n".join(parts)
    except Exception as e:
        log.debug("pymupdf text: %s", e)
        return ""


def _ocr_pdf(raw: bytes, max_pages: int) -> str:
    try:
        import fitz
        import pytesseract
        from PIL import Image
    except ImportError as e:
        log.warning("OCR deps missing: %s", e)
        return ""

    parts = []
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            chunk = pytesseract.image_to_string(img, lang="rus+eng", config="--psm 6")
            if chunk.strip():
                parts.append(chunk)
        doc.close()
    except Exception as e:
        log.warning("OCR failed: %s", e)
        return ""
    return "\n".join(parts)


def discover_pdf_urls(html: str, lot_id: str) -> list[str]:
    """URL PDF с страницы лота + стандартные пути."""
    urls = [
        f"https://files.tbankrot.ru/egrn_files/{lot_id}.pdf",
        f"https://tbankrot.ru/files/egrn/{lot_id}.pdf",
        f"https://tbankrot.ru/item/egrn?id={lot_id}",
    ]
    if html:
        for m in re.finditer(r'https?://[^"\'>\s]+\.pdf[^"\'>\s]*', html, re.I):
            urls.append(m.group(0))
        for m in re.finditer(r'(/[^"\'>\s]*egrn[^"\'>\s]*\.pdf[^"\'>\s]*)', html, re.I):
            u = m.group(1)
            if not u.startswith("http"):
                u = "https://tbankrot.ru" + u
            urls.append(u)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
