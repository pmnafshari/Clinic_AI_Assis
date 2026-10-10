"""The isolated demo's starting data (P28), built at package time with the clinic's own seed code - synthetic only:
the documented dev accounts and opening hours (seed_users.py), the P22 cohort (demo_seed.py), the demo stock and
billing fixtures, and the P24 synthetic guide library (guide_fixtures.py). Built in a fresh temporary folder - never
from the clinic's own database - then listed with SHA-256 so the app can check it before the first copy.

    .venv/bin/python desktop/macos/backend/build_demo_seed.py OUT_DIR
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STEPS = [
    ["seed_users.py"],
    ["demo_seed.py"],
    ["demo_inventory.py", "--apply"],
    ["demo_billing.py", "--apply"],
]


def run(work, args):
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, str(ROOT / args[0]), *args[1:]], cwd=work, env=env, capture_output=True,
                       text=True, timeout=600)
    if r.returncode != 0:
        raise SystemExit(f"{' '.join(args)} failed: {r.stderr[-800:]}")
    print(f"  {' '.join(args)}: ok")


def main(out):
    out = Path(out)
    if out.exists():
        raise SystemExit(f"{out} already exists")
    work = Path(tempfile.mkdtemp(prefix="clinic-demo-seed-"))
    try:
        (work / "db").mkdir()
        for step in STEPS:
            run(work, step)
        library = work / "library-src"
        run(work, ["guide_fixtures.py", "--load-dev", str(library)])
        shutil.rmtree(library)
        for derived in ("db/chroma", "db/doc_chroma", ".env", "db/guide-worker-slots", "db/document-worker-slots"):
            p = work / derived
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        for wal in work.rglob("*-wal"):
            raise SystemExit(f"{wal} left open - a store was not closed")
        shutil.copytree(work, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    lines = []
    for f in sorted(p for p in out.rglob("*") if p.is_file()):
        lines.append(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.relative_to(out)}")
    (out / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    print(f"demo snapshot: {len(lines)} files in {out}")


if __name__ == "__main__":
    main(*sys.argv[1:])
