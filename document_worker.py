"""Extract text from one document. Run only by documents.py, in a sandbox.

    document_worker.py <kind> <path>      -> one JSON object on stdout

This process is started with the network denied by the operating system, a CPU
limit, a file-size limit and a stripped environment. It reads the file, never
executes anything in it, and prints either {"pages": [...]} or {"error": code}.
"""
import json
import os
import struct
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


EXIF_TAGS = {0x9003: "original", 0x9011: "offset", 0x0132: "modified"}
EXIF_READ = 256 * 1024


def _exif(path):
    """The camera's own times from a JPEG's EXIF, as written. Dates only; no pixel is read."""
    with open(path, "rb") as f:
        data = f.read(EXIF_READ)
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
            return _tiff_times(data[i + 10:i + 2 + length])
        if marker == 0xDA:
            break
        i += 2 + length
    return {}


def _tiff_times(tiff):
    order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if order is None or len(tiff) < 8:
        return {}
    found, seen = {}, set()
    todo = [struct.unpack(order + "I", tiff[4:8])[0]]
    while todo and len(seen) < 4:
        at = todo.pop()
        if at in seen or at + 2 > len(tiff):
            continue
        seen.add(at)
        count = struct.unpack(order + "H", tiff[at:at + 2])[0]
        for n in range(min(count, 64)):
            e = at + 2 + 12 * n
            if e + 12 > len(tiff):
                break
            tag, kind, size, value = struct.unpack(order + "HHII", tiff[e:e + 12])
            if tag == 0x8769:
                todo.append(value)
            elif tag in EXIF_TAGS and kind == 2 and size <= 64:
                raw = tiff[e + 8:e + 8 + size] if size <= 4 else tiff[value:value + size]
                found[EXIF_TAGS[tag]] = raw.split(b"\x00")[0].decode("ascii", "replace")
    return found


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
            if kind == "jpeg" and "pages" in result:
                result["exif"] = _exif(path)
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
