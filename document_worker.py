"""Extract text from one document. Run only by documents.py, in a sandbox.

    document_worker.py <kind> <path>      -> one JSON object on stdout

This process is started with the network denied by the operating system, a CPU
limit, a file-size limit and a stripped environment. It reads the file, never
executes anything in it, and prints either {"pages": [...]} or {"error": code}.
"""
import json
import os
import subprocess
import sys

MAX_PAGES = 50
MB = 1024 * 1024
# pypdf's own limits, set here rather than trusted as defaults. Any one of them
# reached means the file is built to be expensive: it is quarantined, not read.
PDF_LIMITS = {
    "maximum_declared_stream_length": 20 * MB,
    "array_based_stream_maximum_output_length": 20 * MB,
    "lzw_maximum_output_length": 20 * MB,
    "run_length_maximum_output_length": 20 * MB,
    "zlib_maximum_output_length": 20 * MB,
    "zlib_maximum_recovery_input_length": 1 * MB,
    "flate_maximum_columns": 20_000,
    "flate_maximum_row_length": 1 * MB,
    "image_maximum_buffer_size": 20 * MB,
    "xmp_maximum_input_length": 1 * MB,
    "xmp_maximum_element_count": 10_000,
    "outline_maximum_entries": 1_000,
    "outline_maximum_depth": 20,
    "page_tree_maximum_entries": 1_000,
    "page_tree_maximum_depth": 20,
    "xform_maximum_invocations_per_extraction": 500,
    "jbig2dec_binary": None,
}
ACTIVE_KEYS = ("/JavaScript", "/JS", "/Launch", "/EmbeddedFiles", "/RichMedia", "/XFA", "/AA")


def _pdf(path):
    from pypdf import PdfReader, apply_configuration
    from pypdf.errors import LimitReachedError
    try:
        with apply_configuration(**PDF_LIMITS):
            return _read_pdf(PdfReader, path)
    except LimitReachedError:
        return {"error": "too_complex"}
    except RecursionError:
        return {"error": "too_complex"}


def _read_pdf(PdfReader, path):
    reader = PdfReader(path)
    if reader.is_encrypted:
        return {"error": "protected"}
    if len(reader.pages) > MAX_PAGES:
        return {"error": "too_many_pages"}
    # second look after documents.py's raw-byte scan: the parsed catalog, which
    # also sees keys that were inside compressed object streams
    catalog = str(reader.trailer["/Root"])
    if any(key in catalog for key in ACTIVE_KEYS):
        return {"error": "active_content"}
    pages = []
    for n, page in enumerate(reader.pages, 1):
        pages.append({"page": n, "text": (page.extract_text() or "").strip(), "confidence": 100})
    return {"pages": pages}


def _image(path):
    # tesseract's TSV gives a confidence per word; -1 marks non-words
    # the setting, not the "tsv" config file: that file lives in the system
    # tessdata folder, and only the pinned language data is on TESSDATA_PREFIX
    out = subprocess.run(["tesseract", path, "stdout", "-l", "ita+eng", "-c", "tessedit_create_tsv=1"],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return {"error": "ocr_failed"}
    words, confs = [], []
    for line in out.stdout.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) == 12 and cols[11].strip() and float(cols[10]) >= 0:
            words.append(cols[11].strip())
            confs.append(float(cols[10]))
    confidence = round(sum(confs) / len(confs), 1) if confs else 0
    return {"pages": [{"page": 1, "text": " ".join(words), "confidence": confidence}]}


def _text(path):
    with open(path, "rb") as f:
        raw = f.read()
    return {"pages": [{"page": 1, "text": raw.decode("utf-8").strip(), "confidence": 100}]}


def main():
    kind, path = sys.argv[1], sys.argv[2]
    try:
        if kind == "pdf":
            result = _pdf(path)
        elif kind in ("png", "jpeg"):
            result = _image(path)
        elif kind == "text":
            result = _text(path)
        else:
            result = {"error": "unsupported"}
    except Exception as e:
        # the type of failure only; never the document's content
        result = {"error": "unreadable", "detail": type(e).__name__}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    os.umask(0o077)
    main()
