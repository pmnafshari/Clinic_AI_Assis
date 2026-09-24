"""the non-clinical X-ray demo (P19, D04 = DEMO_GO, clinical approval pending).

    CLINIC_XRAY_DEMO=1 .venv/bin/python xray_demo.py demo --out DIR
    .venv/bin/python xray_demo_selftest.py --selftest

WHAT IT IS. the whole pipeline a clinical version would need - input check,
analysis, overlay, abstention, human review, a locked benchmark with metrics -
run on synthetic images this module draws itself. the "detector" is a plain
threshold rule that finds the bright and dark squares the generator painted:
DEMO MARKS, not findings. it is not a model, it says nothing about teeth, and it
never outputs a confidence.

WHAT IT REFUSES. any image the generator did not make (a real radiograph is
refused, not analysed); a changed image; anything when the demo switch is off.
it has no web route, touches no patient record and sends nothing. the tag that
marks a generated image is a digest with a public salt: it stops accidents, not
someone forging a tag on purpose.
"""

import hashlib
import json
import struct
import sys
import threading
import zlib
from pathlib import Path

import xray_gate

GENERATOR = "clinic-xray-demo-synth-1"
DETECTOR = "demo-marks-threshold-1"
TAG_KEY = "clinic-xray-demo"
DEMO_LABEL = "DEMO OUTPUT - synthetic image, demo marks only. Not a finding, not a diagnosis, not for clinical use."
SALT = b"clinic-xray-demo-public-salt-v1"   # public on purpose: an accident guard, not a secret
MAX_BYTES = 256 * 1024
MIN_SIDE, MAX_SIDE = 64, 512
KINDS = {"demo_mark_a": 235, "demo_mark_b": 15}   # painted value of each kind
FLAT_STD = 3.0
TIMEOUT_S = 5.0
DEFAULT_CRITERIA = {"version": 1, "level": "image", "min_sensitivity": 0.8, "min_specificity": 0.8,
                    "note": "demo criteria for synthetic marks; not clinical criteria"}
PNG_SIG = b"\x89PNG\r\n\x1a\n"

SCHEMA = """
CREATE TABLE IF NOT EXISTS xray_demo_reviews (
    id INTEGER PRIMARY KEY,
    input_sha256 TEXT NOT NULL,
    generator TEXT NOT NULL,
    detector TEXT NOT NULL,
    output TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject', 'correct')),
    corrected TEXT,
    reviewer TEXT NOT NULL,
    role TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS xray_demo_reviews_no_update BEFORE UPDATE ON xray_demo_reviews
    BEGIN SELECT RAISE(ABORT, 'xray demo reviews are append-only'); END;
CREATE TRIGGER IF NOT EXISTS xray_demo_reviews_no_delete BEFORE DELETE ON xray_demo_reviews
    BEGIN SELECT RAISE(ABORT, 'xray demo reviews are append-only'); END;
"""


class Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# --- png, 8-bit grayscale only -----------------------------------------------


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def write(pixels, tags):
    height, width = len(pixels), len(pixels[0])
    raw = b"".join(b"\x00" + bytes(row) for row in pixels)
    text = b"".join(_chunk(b"tEXt", k.encode() + b"\x00" + v.encode()) for k, v in tags.items())
    return (PNG_SIG + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)) + text
            + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b""))


def read(data):
    """-> {width, height, pixels, tags}. refuses anything that is not a plain 8-bit grayscale PNG."""
    if not data.startswith(PNG_SIG):
        raise Refused("not a PNG")
    pos, idat, tags, head = len(PNG_SIG), b"", {}, None
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        crc = data[pos + 8 + length:pos + 12 + length]
        if len(body) != length or len(crc) != 4 or struct.unpack(">I", crc)[0] != zlib.crc32(kind + body):
            raise Refused("damaged PNG")
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", body)
        elif kind == b"tEXt" and b"\x00" in body:
            k, v = body.split(b"\x00", 1)
            tags[k.decode("latin-1")] = v.decode("latin-1")
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length
    if head is None or head[2:] != (8, 0, 0, 0, 0):
        raise Refused("not an 8-bit grayscale PNG")
    width, height = head[0], head[1]
    try:
        raw = zlib.decompress(idat)
    except zlib.error:
        raise Refused("damaged PNG")
    if len(raw) != height * (width + 1) or any(raw[r * (width + 1)] != 0 for r in range(height)):
        raise Refused("unsupported PNG layout")
    pixels = [list(raw[r * (width + 1) + 1:(r + 1) * (width + 1)]) for r in range(height)]
    return {"width": width, "height": height, "pixels": pixels, "tags": tags}


def _digest(pixels):
    return hashlib.sha256(SALT + b"".join(bytes(row) for row in pixels)).hexdigest()


# --- generator ----------------------------------------------------------------


def synth(seed, marks, size=(128, 128), noise=10, background=110):
    """a synthetic image: noise around a gray background, with squares painted in.
    marks: [(kind, x, y, w, h)]. tagged so validate() knows it came from here."""
    width, height = size
    state = seed * 2654435761 % 2 ** 32 or 1
    pixels = []
    for _y in range(height):
        row = []
        for _x in range(width):
            state = (1103515245 * state + 12345) % 2 ** 31
            row.append(max(0, min(255, background + (state % (2 * noise + 1)) - noise if noise else background)))
        pixels.append(row)
    for kind, x, y, w, h in marks:
        for yy in range(y, min(y + h, height)):
            for xx in range(x, min(x + w, width)):
                pixels[yy][xx] = KINDS[kind]
    return write(pixels, {TAG_KEY: f"synthetic;generator={GENERATOR};digest={_digest(pixels)}"})


# --- the pipeline ---------------------------------------------------------------


def validate(data):
    """-> {sha256, width, height, generator}. never changes the bytes."""
    if len(data) > MAX_BYTES:
        raise Refused(f"too large (over {MAX_BYTES} bytes)")
    img = read(data)
    tag = img["tags"].get(TAG_KEY, "")
    fields = dict(p.split("=", 1) for p in tag.split(";")[1:] if "=" in p)
    if not tag.startswith("synthetic;") or fields.get("generator") != GENERATOR:
        raise Refused("not a synthetic demo image: only images made by this demo are accepted")
    if fields.get("digest") != _digest(img["pixels"]):
        raise Refused("the image was changed after it was generated")
    if not (MIN_SIDE <= img["width"] <= MAX_SIDE and MIN_SIDE <= img["height"] <= MAX_SIDE):
        raise Refused(f"out of spec: {img['width']}x{img['height']} (allowed {MIN_SIDE}-{MAX_SIDE} per side)")
    return {"sha256": hashlib.sha256(data).hexdigest(), "width": img["width"], "height": img["height"],
            "generator": fields["generator"], "pixels": img["pixels"]}


def _std(pixels):
    flat = [v for row in pixels for v in row]
    mean = sum(flat) / len(flat)
    return (sum((v - mean) ** 2 for v in flat) / len(flat)) ** 0.5


def detect(pixels):
    """the demo rule: connected squares of the two painted values. no confidence."""
    height, width = len(pixels), len(pixels[0])
    seen, marks = set(), []
    for kind, value in KINDS.items():
        for y in range(height):
            for x in range(width):
                if (x, y) in seen or pixels[y][x] != value:
                    continue
                stack, box = [(x, y)], [x, y, x, y]
                seen.add((x, y))
                while stack:
                    cx, cy = stack.pop()
                    box = [min(box[0], cx), min(box[1], cy), max(box[2], cx), max(box[3], cy)]
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in seen and pixels[ny][nx] == value:
                            seen.add((nx, ny))
                            stack.append((nx, ny))
                w, h = box[2] - box[0] + 1, box[3] - box[1] + 1
                if w * h >= 9:
                    marks.append({"kind": kind, "x": box[0], "y": box[1], "w": w, "h": h})
    return marks


def analyse(data, env=None, detector=detect, timeout=TIMEOUT_S):
    if not xray_gate.demo_enabled(env=env):
        raise Refused("the X-ray demo is switched off (CLINIC_XRAY_DEMO=1 and DEMO_GO in docs/xray/gate.json)")
    meta = validate(data)
    out = {"input_sha256": meta["sha256"], "generator": meta["generator"], "detector": DETECTOR,
           "label": DEMO_LABEL, "status": "result", "reason": None, "marks": []}
    if _std(meta["pixels"]) < FLAT_STD:
        return {**out, "status": "abstained", "reason": "image too flat to read"}
    box = {}

    def run():
        try:
            box["marks"] = detector(meta["pixels"])
        except Exception as e:                    # the detector's failure is an abstention, never a result
            box["error"] = type(e).__name__
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return {**out, "status": "abstained", "reason": f"detector timed out after {timeout} s"}
    if "error" in box:
        return {**out, "status": "abstained", "reason": f"detector failed ({box['error']})"}
    return {**out, "marks": box["marks"]}


def overlay(data, marks):
    """the same image at the same size, with each mark outlined. tagged as an overlay, so it is
    never taken for an input."""
    meta = validate(data)
    pixels = [row[:] for row in meta["pixels"]]
    for m in marks:
        x0, y0, x1, y1 = m["x"] - 1, m["y"] - 1, m["x"] + m["w"], m["y"] + m["h"]
        for x in range(max(x0, 0), min(x1 + 1, meta["width"])):
            for y in (y0, y1):
                if 0 <= y < meta["height"]:
                    pixels[y][x] = 255
        for y in range(max(y0, 0), min(y1 + 1, meta["height"])):
            for x in (x0, x1):
                if 0 <= x < meta["width"]:
                    pixels[y][x] = 255
    return write(pixels, {TAG_KEY: f"overlay-of={meta['sha256']};detector={DETECTOR}",
                          "label": DEMO_LABEL})


# --- human review ------------------------------------------------------------


def record_review(conn, analysis, decision, actor, role, corrected=None):
    import clinic_time
    from auth import authorize, log_audit
    target = f"demo:{analysis['input_sha256'][:12]}"
    if not authorize(role, "review_xray_demo"):
        log_audit(conn, actor, role, "xray_demo_review", target, allowed=0)
        raise Refused(f"{role} is not allowed to review demo output")
    if decision not in ("accept", "reject", "correct"):
        raise Refused("decision must be accept, reject or correct")
    if decision == "correct" and not corrected:
        raise Refused("a correction needs the corrected marks")
    rid = conn.execute(
        "INSERT INTO xray_demo_reviews (input_sha256, generator, detector, output, decision, corrected,"
        " reviewer, role, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (analysis["input_sha256"], analysis["generator"], analysis["detector"], json.dumps(analysis["marks"]),
         decision, json.dumps(corrected) if corrected else None, actor, role, clinic_time.stamp())).lastrowid
    conn.commit()
    log_audit(conn, actor, role, "xray_demo_review", target, allowed=1, reason=decision)
    return rid


def reviews(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM xray_demo_reviews ORDER BY id")]


# --- benchmark and evaluation ----------------------------------------------------


def benchmark(n, seed):
    """n synthetic cases for n//2 synthetic patients, split by patient (never by image)."""
    cases, manifest = [], []
    for i in range(n):
        s = seed * 1000 + i
        patient = f"SYN{(i // 2):04d}"
        split = "test" if (i // 2) % 2 else "train"
        truth = [k for j, k in enumerate(KINDS) if (s >> j) % 3 == 0]
        marks = [(k, 10 + 40 * j, 20 + (s % 50), 8 + (s % 5), 8 + (s % 4)) for j, k in enumerate(truth)]
        noise = 5 if s % 2 else 25
        img = synth(s, marks, noise=noise)
        sha = hashlib.sha256(img).hexdigest()
        cases.append({"image": img, "truth": truth, "noise": noise, "sha": sha})
        manifest.append({"image_sha256": sha, "patient_id": patient, "split": split})
    return {"cases": cases, "manifest": manifest}


def _sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def lock(criteria_path, manifest):
    found = xray_gate.split_check(manifest)
    if any(found.values()):
        raise Refused(f"the benchmark manifest has leakage or gaps: {found}")
    return {"criteria_sha256": _sha_file(criteria_path),
            "manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}


def rate(k, n):
    """k of n with a Wilson 95% interval."""
    if n == 0:
        return {"value": None, "ci95": None, "n": 0}
    z, p = 1.96, k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return {"value": round(p, 4), "ci95": [round(centre - half, 4), round(centre + half, 4)], "n": n}


def evaluate(bench, criteria_path, locked, env=None):
    """image-level metrics per mark kind on the test split, against criteria locked beforehand."""
    if _sha_file(criteria_path) != locked["criteria_sha256"]:
        raise Refused("the criteria changed after the benchmark was locked; thresholds cannot move afterwards")
    if hashlib.sha256(json.dumps(bench["manifest"], sort_keys=True).encode()).hexdigest() != locked["manifest_sha256"]:
        raise Refused("the benchmark changed after it was locked")
    criteria = json.loads(Path(criteria_path).read_text())
    split = {m["image_sha256"]: m["split"] for m in bench["manifest"]}
    per_kind = {k: {"tp": 0, "fp": 0, "tn": 0, "fn": 0} for k in KINDS}
    abstained, by_noise = 0, {}
    for case in bench["cases"]:
        if split[case["sha"]] != "test":
            continue
        result = analyse(case["image"], env=env)
        if result["status"] != "result":
            abstained += 1
            continue
        found = {m["kind"] for m in result["marks"]}
        for kind, c in per_kind.items():
            truth, pred = kind in case["truth"], kind in found
            c[("t" if truth == pred else "f") + ("p" if pred else "n")] += 1
            bucket = by_noise.setdefault(f"noise_{case['noise']}", {"right": 0, "n": 0})
            bucket["n"] += 1
            bucket["right"] += truth == pred
    report = {"criteria_sha256": locked["criteria_sha256"], "detector": DETECTOR, "abstained": abstained,
              "per_kind": {}, "subgroups": {g: rate(b["right"], b["n"]) for g, b in by_noise.items()},
              "label": DEMO_LABEL}
    for kind, c in per_kind.items():
        sens = rate(c["tp"], c["tp"] + c["fn"])
        spec = rate(c["tn"], c["tn"] + c["fp"])
        report["per_kind"][kind] = {
            "level": "image", "counts": c, "sensitivity": sens, "specificity": spec,
            "precision": rate(c["tp"], c["tp"] + c["fp"]), "recall": sens,
            "calibration": "not applicable: no confidence output",
            "meets_criteria": (sens["value"] is not None and spec["value"] is not None
                               and sens["value"] >= criteria["min_sensitivity"]
                               and spec["value"] >= criteria["min_specificity"])}
    return report


def main(argv):
    if argv[:1] != ["demo"] or "--out" not in argv:
        print("usage: CLINIC_XRAY_DEMO=1 python xray_demo.py demo --out DIR")
        return 2
    if not xray_gate.demo_enabled():
        print("refused: the X-ray demo is switched off (CLINIC_XRAY_DEMO=1 and DEMO_GO in docs/xray/gate.json)")
        return 1
    out = Path(argv[argv.index("--out") + 1])
    out.mkdir(parents=True, exist_ok=True)
    try:
        bench = benchmark(n=40, seed=1)
        criteria = out / "criteria.json"
        criteria.write_text(json.dumps(DEFAULT_CRITERIA, indent=1))
        locked = lock(criteria, bench["manifest"])
        report = evaluate(bench, criteria, locked)
        sample = bench["cases"][0]["image"]
        (out / "sample.png").write_bytes(sample)
        (out / "sample-overlay.png").write_bytes(overlay(sample, analyse(sample)["marks"]))
    except Refused as e:
        print(f"refused: {e.reason}")
        return 1
    (out / "report.json").write_text(json.dumps({**report, "lock": locked}, indent=1))
    print(json.dumps({"out": str(out), "per_kind": {k: v["sensitivity"] for k, v in report["per_kind"].items()},
                      "abstained": report["abstained"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
