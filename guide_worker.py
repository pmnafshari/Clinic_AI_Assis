"""Read one clinic guide PDF page by page. Run only by clinic_guides.py, in the same sandbox as P15's worker.

    guide_worker.py <pdf> <out folder>      -> one JSON object on stdout; page-N.png written to the folder

Per page: the text layer, every embedded JPEG picture read by OCR, and the page rendered to PNG by macOS sips
and read by OCR too. The higher-confidence OCR reading is kept, with its confidence. Nothing in the file is
executed or followed; its text is data.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from document_worker import ACTIVE_KEYS, MAX_PAGES, PDF_LIMITS

OCR_LANGS = "ita+eng"


def _ocr(path):
    out = subprocess.run(["tesseract", str(path), "stdout", "-l", OCR_LANGS, "-c", "tessedit_create_tsv=1"],
                         capture_output=True, text=True, timeout=60)
    words, confs = [], []
    for line in out.stdout.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) == 12 and cols[11].strip() and float(cols[10]) >= 0:
            words.append(cols[11].strip())
            confs.append(float(cols[10]))
    return " ".join(words), round(sum(confs) / len(confs), 1) if confs else 0.0


def _pictures(page):
    """The JPEG bytes of the page's pictures. Other picture types are read from the rendered page only."""
    out = []
    xobjects = (page.get("/Resources") or {}).get("/XObject") or {}
    for name in xobjects:
        obj = xobjects[name].get_object()
        if obj.get("/Subtype") == "/Image" and obj.get("/Filter") == "/DCTDecode":
            out.append(obj._data)
        elif obj.get("/Subtype") == "/Image":
            out.append(None)
    return out


def read(pdf, out):
    from pypdf import PdfReader, PdfWriter, apply_configuration
    from pypdf.errors import LimitReachedError
    out = Path(out)
    try:
        with apply_configuration(**PDF_LIMITS):
            reader = PdfReader(pdf)
            if reader.is_encrypted:
                return {"error": "protected"}
            if len(reader.pages) > MAX_PAGES:
                return {"error": "too_many_pages"}
            if any(key in str(reader.trailer["/Root"]) for key in ACTIVE_KEYS):
                return {"error": "active_content"}
            pages = []
            for n, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").strip()
                pictures = _pictures(page)
                single = out / f"page-{n}.pdf"
                writer = PdfWriter()
                writer.add_page(page)
                with open(single, "wb") as f:
                    writer.write(f)
                image = out / f"page-{n}.png"
                subprocess.run(["sips", "-s", "format", "png", str(single), "--out", str(image)],
                               capture_output=True, timeout=60)
                single.unlink()
                ocr_text, ocr_conf = "", 0.0
                if pictures or len(text) < 20:
                    readings = [_ocr(image)] if image.exists() else []
                    for i, jpeg in enumerate(p for p in pictures if p):
                        pic = out / f"pic-{n}-{i}.jpg"
                        pic.write_bytes(jpeg)
                        readings.append(_ocr(pic))
                        pic.unlink()
                    if readings:
                        ocr_text, ocr_conf = max(readings, key=lambda r: r[1])
                pages.append({"page": n, "text": text, "has_figure": bool(pictures), "ocr_text": ocr_text,
                              "ocr_conf": ocr_conf, "image": image.name if image.exists() else None})
            return {"pages": pages}
    except (LimitReachedError, RecursionError):
        return {"error": "too_complex"}


def main():
    try:
        result = read(sys.argv[1], sys.argv[2])
    except Exception as e:
        result = {"error": "unreadable", "detail": type(e).__name__}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    os.umask(0o077)
    main()
