import re
import sys
from pathlib import Path

from cf_generator import make_cf, seed_cf
from dental_notes_schema import CF_PATTERN

PATIENTS = [
    ("Mario", "Rossi"),
    ("Giulia", "Ferrari"),
    ("Luca", "Esposito"),
    ("Anna", "Bianchi"),
    ("Marco", "Ricci"),
]


def build(drop_dir):
    seed_cf(42)
    drop_dir = Path(drop_dir)
    drop_dir.mkdir(parents=True, exist_ok=True)

    cfs = [make_cf(fn, ln) for fn, ln in PATIENTS]

    # txt notes with CF in text → route_note files under sorted/<CF>/notes
    (drop_dir / "nota_mario_2026.txt").write_text(f"paziente: mario rossi CF {cfs[0]} visita controllo")
    (drop_dir / "nota_giulia_gen.txt").write_text(f"giulia ferrari {cfs[1]} pulizia denti")
    (drop_dir / "nota_luca_consult.txt").write_text(f"CF: {cfs[2]} - luca esposito, tartaro rimosso")

    # txt notes with no CF — route_note sends to needs_review (designed D-10 case)
    (drop_dir / "nota_senza_cf.txt").write_text("paziente: giovanna bianchi, visita 27 gen, tartaro")
    (drop_dir / "appunto_senza_id.txt").write_text("visita generica non identificata, preventivo da fare")

    # xlsx with CF in filename → sorted/<CF>/records
    (drop_dir / f"fattura_{cfs[0]}_2026.xlsx").write_bytes(b"PK")
    (drop_dir / f"preventivo_{cfs[3]}_q1.xlsx").write_bytes(b"PK")

    # xlsx without CF → sorted/records
    (drop_dir / "fattura_generica.xlsx").write_bytes(b"PK")

    # jpg/png with CF in filename → sorted/<CF>/images
    (drop_dir / f"rx_{cfs[1]}.jpg").write_bytes(b"\xff\xd8\xff")
    (drop_dir / f"panoramica_{cfs[4]}.png").write_bytes(b"\x89PNG")

    # images without CF → sorted/images
    (drop_dir / "rx_generica.jpg").write_bytes(b"\xff\xd8\xff")
    (drop_dir / "scan_anonima.png").write_bytes(b"\x89PNG")

    # nested subfolder — exercises recursive watcher
    sub = drop_dir / "inbox" / "subfolder"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "nota_nested.txt").write_text(f"anna bianchi CF {cfs[3]} controllo radici")

    # unknown extension → needs_review
    (drop_dir / "scheda_sconosciuta.dat").write_bytes(b"raw data")

    print(f"fixtures written to {drop_dir}")


def _has_cf_in_name(name):
    # tokenize so CF_PATTERN.match works on each isolated token
    for token in re.split(r'[\W_]+', name.upper()):
        if CF_PATTERN.match(token):
            return True
    return False


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            build(tmp)
            all_files = [f for f in Path(tmp).rglob("*") if f.is_file()]
            assert any(f.suffix == ".txt" for f in all_files), "no .txt files created"
            assert any(f.suffix == ".xlsx" for f in all_files), "no .xlsx files created"
            assert any(f.suffix in (".jpg", ".png") for f in all_files), "no image files created"
            sub = Path(tmp) / "inbox" / "subfolder"
            assert sub.is_dir(), "nested subfolder not created"
            assert any(_has_cf_in_name(f.name) for f in all_files), "no filename contains a CF"
            name_only = [
                f for f in all_files
                if f.suffix == ".txt" and not _has_cf_in_name(f.name)
            ]
            assert name_only, "no name-only .txt found"
        print("selftest ok")
        return
    drop = sys.argv[1] if len(sys.argv) > 1 else "drop"
    build(drop)


if __name__ == "__main__":
    main()
