"""Reading the policy number from an uploaded insurance document (PDF or photo).

Text PDFs are read with pypdf. Scanned PDFs and photos go through EasyOCR when it is installed.
Every step is optional: if nothing can be read, the claim still goes through and the admin
sees that the policy number needs a manual check."""
import re
import threading

_ocr = None
_ocr_lock = threading.Lock()

POLICY_PATTERNS = [
    r"Policy\s*(?:No|Number|#)?\.?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-]{5,30})",
    r"Certificate\s*(?:No|Number)\.?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-]{5,30})",
]


def _get_ocr():
    global _ocr
    if _ocr is None:
        with _ocr_lock:
            if _ocr is None:
                import easyocr  # heavy; imported only when needed
                _ocr = easyocr.Reader(["en"], gpu=False)
    return _ocr


def find_policy_number(text):
    if not text:
        return None
    flat = " ".join(text.split())
    for pat in POLICY_PATTERNS:
        m = re.search(pat, flat, re.IGNORECASE)
        if m:
            cand = m.group(1).strip("-/ ").upper()
            if any(ch.isdigit() for ch in cand):
                return cand
    m = re.search(r"\b(?=[A-Z0-9/\-]*\d)[A-Z0-9][A-Z0-9/\-]{9,24}\b", flat)
    return m.group(0) if m else None


def _pdf_text(data):
    try:
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:3])
    except Exception as e:  # noqa: BLE001
        print(f"[documents] pdf text failed: {e}")
        return ""


def _pdf_first_page_image(data):
    try:
        import fitz  # PyMuPDF, optional
        import numpy as np
        doc = fitz.open(stream=data, filetype="pdf")
        pix = doc[0].get_pixmap(dpi=200)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
        return img[:, :, :3][:, :, ::-1].copy()
    except Exception:
        return None


def _ocr_image(img):
    try:
        reader = _get_ocr()
        return " ".join(reader.readtext(img, detail=0))
    except Exception as e:  # noqa: BLE001
        print(f"[documents] OCR unavailable: {e}")
        return ""


def extract_policy_number(data: bytes, filename: str):
    """Returns (policy_number or None, source) where source is 'pdf-text', 'ocr' or None."""
    name = (filename or "").lower()
    if name.endswith(".pdf") or data[:4] == b"%PDF":
        num = find_policy_number(_pdf_text(data))
        if num:
            return num, "pdf-text"
        img = _pdf_first_page_image(data)
        if img is not None:
            num = find_policy_number(_ocr_image(img))
            if num:
                return num, "ocr"
        return None, None

    from detector import read_image
    img = read_image(data, max_side=2200)
    if img is None:
        return None, None
    num = find_policy_number(_ocr_image(img))
    return (num, "ocr") if num else (None, None)
