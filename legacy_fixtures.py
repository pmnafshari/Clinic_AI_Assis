"""The fixed synthetic legacy folder for P25 (cases E01-E20 in the plan).

    python legacy_fixtures.py <empty folder>          the evaluation folder (temp databases, selftests)
    python legacy_fixtures.py --uat <empty folder>    the UAT folder from the dev database's fictional cohort

Every person here is fictional; the codes are real-shaped with a correct check
character so the checksum rules are exercised. The folder carries the marker
file legacy_import requires before it reads anything.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from codice_fiscale import check_char


MARKER = ".synthetic-legacy-fixture"


def cf(first15):
    return first15 + check_char(first15)


# (key, codice fiscale, name, birth date)
PEOPLE = [
    ("rossi", cf("RSSMRA80A01H501"), "Mario Rossi", "1980-01-01"),
    ("bianchi_a", cf("BNCPLA85C50H501"), "Paola Bianchi", "1985-03-10"),
    ("bianchi_b", cf("BNCPLA72S42F205"), "Paola Bianchi", "1972-11-02"),
    ("verdi", cf("VRDLCU90E15L219"), "Luca Verdi", "1990-05-15"),
    ("neri", cf("NRENNA75D45G273"), "Anna Neri", "1975-04-05"),
]
NOBODY = cf("GRGGRG60M01H501")
CF = {k: c for k, c, _n, _b in PEOPLE}


def wrong_check(code):
    """The same code with its check character wrong, as an OCR slip would leave it."""
    bad = "A" if code[15] != "A" else "B"
    return code[:15] + bad


def seed(conn):
    """The fixture patients in a database. -> {key: patient_id}"""
    import patient_id
    return {k: patient_id.seed_patient(conn, c, n) for k, c, n, _b in PEOPLE}


def pdf(pages):
    from documents_selftest import pdf as make
    return make(pages)


def jpeg_from_text(text, work):
    """A real JPEG with the text drawn on it (rendered by macOS sips)."""
    work = Path(work)
    src, out = work / "render.pdf", work / "render.jpg"
    src.write_bytes(pdf([text]))
    subprocess.run(["sips", "-s", "format", "jpeg", str(src), "--out", str(out)],
                   check=True, capture_output=True)
    data = out.read_bytes()
    src.unlink()
    out.unlink()
    return strip_app1(data)


def strip_app1(data):
    """Drop any APP1 (EXIF) segment, so a fixture's times are only the ones it is given."""
    out, i = bytearray(data[:2]), 2
    while i + 4 <= len(data) and data[i] == 0xFF and data[i + 1] not in (0xDA, 0xD9):
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if data[i + 1] != 0xE1:
            out += data[i:i + 2 + length]
        i += 2 + length
    return bytes(out + data[i:])


def with_exif(data, original, offset=None):
    """A JPEG with EXIF DateTimeOriginal (and OffsetTimeOriginal if given)."""
    tags = [(0x9003, original.encode() + b"\x00")]
    if offset:
        tags.append((0x9011, offset.encode() + b"\x00"))
    # IFD0 holds one pointer to the EXIF IFD; the EXIF IFD holds the times
    exif_ifd_at = 8 + 2 + 12 + 4
    values_at = exif_ifd_at + 2 + 12 * len(tags) + 4
    ifd0 = struct.pack("<H", 1) + struct.pack("<HHII", 0x8769, 4, 1, exif_ifd_at) + b"\0\0\0\0"
    entries, values = b"", b""
    for tag, value in tags:
        entries += struct.pack("<HHII", tag, 2, len(value), values_at + len(values))
        values += value
    tiff = b"II*\x00" + struct.pack("<I", 8) + ifd0 + struct.pack("<H", len(tags)) + entries \
        + b"\0\0\0\0" + values
    body = b"Exif\x00\x00" + tiff
    segment = b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body
    data = strip_app1(data)
    return data[:2] + segment + data[2:]


def dicom(patient_id_value, name):
    """DICOM-shaped bytes: preamble, DICM, and a patient id element. Never parsed by P25."""
    def element(group, elem, vr, value):
        value = value.encode() + (b" " if len(value) % 2 else b"")
        return struct.pack("<HH", group, elem) + vr + struct.pack("<H", len(value)) + value
    return b"\0" * 128 + b"DICM" + element(0x0010, 0x0010, b"PN", name) \
        + element(0x0010, 0x0020, b"LO", patient_id_value)


def build(root):
    """Write the evaluation folder. -> {case: (relative path, expected)}"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("P25 synthetic fixture - fictional people only\n")
    work = root.parent / (root.name + ".work")
    work.mkdir(exist_ok=True)
    have_sips = shutil.which("sips") is not None
    cases = {}

    def put(case, rel, data, expected):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        cases[case] = (rel, expected)

    rossi_report = (f"Referto\nPaziente: Mario Rossi\nCodice fiscale: {CF['rossi']}\n"
                    "Data esame: 04/03/2021 10:20\nControllo periodico.\n").encode()
    put("E01", "referti/2021/referto_0412.txt", rossi_report, ("proposed", "strong", "rossi"))
    put("E02", "referti/2021/verdi.pdf",
        pdf([f"Codice fiscale: {CF['verdi']} Nato il: 15/05/1990 Data esame: 12/06/2021"]),
        ("proposed", "strong", "verdi"))
    put("E03", "referti/2022/scan_881.pdf", pdf([f"Codice fiscale: {CF['rossi']}"]),
        ("proposed", "check", "rossi"))
    put("E05", "lettere/lettera_bianchi.txt", b"Gentile paziente\nPaziente: Paola Bianchi\n",
        ("conflict", None, None))
    put("E06", "lettere/lettera_bianchi_1985.txt",
        b"Paziente: Paola Bianchi\nData di nascita: 10/03/1985\n", ("proposed", "check", "bianchi_a"))
    put("E07", "referti/2022/referto_misto.txt",
        f"Paziente: Luca Verdi\nCodice fiscale: {CF['rossi']}\n".encode(), ("conflict", None, None))
    put("E09", "referti/2022/due_pazienti.pdf",
        pdf([f"Paziente: Mario Rossi Codice fiscale: {CF['rossi']}",
             f"Paziente: Anna Neri Codice fiscale: {CF['neri']}"]), ("conflict", None, None))
    put("E10", "referti/2023/ocr_errato.txt",
        f"Codice fiscale: {wrong_check(CF['rossi'])}\n".encode(), ("unmatched", None, None))
    put("E11", "referti/2023/sconosciuto.txt",
        f"Paziente: Mario Rossi\nCodice fiscale: {NOBODY}\n".encode(), ("conflict", None, None))
    put("E13", "Rossi Mario/nota.txt", b"Controllo, nessuna nota.\n", ("unmatched", None, None))
    put("E14", "copie/referto_0412_copia.txt", rossi_report, ("proposed", "strong", "rossi"))
    put("E16", f"rx/{CF['rossi']}_opg.dcm", dicom(CF["verdi"], "VERDI^LUCA"), ("refused", None, None))
    put("E17", "amministrazione/fatture.xlsx", b"PK\x03\x04" + b"\0" * 60, ("refused", None, None))
    put("E20", "referti/2024/neri.txt", b"Paziente: Anna Neri\nID paziente: {clinic_id:neri}\n",
        ("proposed", "strong", "neri"))
    link = root / "referti/2021/collegamento.txt"
    os.symlink(root / "referti/2021/referto_0412.txt", link)
    cases["E18"] = ("referti/2021/collegamento.txt", ("refused", None, None))
    if have_sips:
        plain = jpeg_from_text("OPG", work)
        put("E04", f"rx/{CF['neri']}_opg.jpg", plain, ("proposed", "check", "neri"))
        pan = jpeg_from_text("PAN", work)
        put("E15", f"rx/{CF['verdi']}_pan.jpg", pan, ("conflict", None, None))
        put("E15b", f"rx/{CF['rossi']}_pan.jpg", pan, ("conflict", None, None))
        put("E08", f"rx/{CF['rossi']}_bitewing.jpg",
            jpeg_from_text(f"Codice fiscale: {CF['verdi']}", work), ("conflict", None, None))
        put("E12", "foto/IMG_0001.jpg", with_exif(jpeg_from_text("foto", work), "2021:03:04 10:15:00"),
            ("unmatched", None, None))
        put("E19", "foto/IMG_0002.jpg",
            with_exif(jpeg_from_text(f"Codice fiscale: {CF['verdi']} Paziente: Luca Verdi Data esame:"
                                     " 12/06/2021", work), "2019:01:01 00:03:00"),
            ("proposed", "check", "verdi"))
    shutil.rmtree(work, ignore_errors=True)
    return cases


def fill_clinic_ids(root, pids):
    """E20 names a clinic id, known only once the fixture patients exist."""
    path = Path(root) / "referti/2024/neri.txt"
    if path.exists():
        path.write_text(path.read_text().replace("{clinic_id:neri}", pids["neri"]))


# the dev database's own fictional cohort (UAT-MASTER-GUIDE §3), for the human walk (FIL-*)
UAT_PEOPLE = {"francesca": ("GLLFNC51R55F839P", "Francesca Gallo"), "marco": ("GLLMRC67A11F839U", "Marco Gallo"),
              "lorenzo": ("BRNLNZ61B19H501V", "Lorenzo Bruno"), "giulia": ("SNTGLI51R49D612Y", "Giulia Santoro")}


def build_uat(root, conn):
    """The UAT folder, U01-U12, for the dev database. -> {case: (relative path, expected)}"""
    for key, (code, name) in UAT_PEOPLE.items():
        if not conn.execute("SELECT 1 FROM patients WHERE codice_fiscale = ? AND patient_name = ?",
                            (code, name)).fetchone():
            raise SystemExit(f"{name} ({code}) is not in this database: run on the UAT dev database")
    if conn.execute("SELECT COUNT(*) FROM patients WHERE patient_name = 'Paola Rossi'").fetchone()[0] != 2:
        raise SystemExit("the two Paola Rossi records are not both in this database")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("P25 synthetic UAT fixture - fictional people only\n")
    work = root.parent / (root.name + ".work")
    work.mkdir(exist_ok=True)
    F, M, L, G = (UAT_PEOPLE[k][0] for k in ("francesca", "marco", "lorenzo", "giulia"))
    cases = {}

    def put(case, rel, data, expected):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        cases[case] = (rel, expected)
    report = (f"Referto DEMO\nPaziente: Francesca Gallo\nCodice fiscale: {F}\n"
              "Data esame: 05/10/2026 09:40\nControllo periodico (dati inventati).\n").encode()
    put("U01", "referti/2026/referto_gallo.txt", report, "proposed, strong - Francesca Gallo")
    put("U02", "referti/2026/scan_bruno.pdf", pdf([f"Codice fiscale: {L}"]), "proposed, needs checking - Lorenzo Bruno")
    put("U03", "lettere/lettera_paola_rossi.txt", b"Paziente: Paola Rossi\n", "conflict - two patients have this name")
    put("U04", "referti/2026/referto_misto.txt", f"Paziente: Giulia Santoro\nCodice fiscale: {M}\n".encode(),
        "conflict - the name is not the name of the patient the code names")
    put("U05", "referti/2026/due_pazienti.pdf",
        pdf([f"Paziente: Francesca Gallo Codice fiscale: {F}", f"Paziente: Lorenzo Bruno Codice fiscale: {L}"]),
        "conflict - two people in one file")
    put("U06", "Gallo Francesca/nota.txt", b"Nota senza dati (inventata).\n", "unmatched - folder name is only a lead")
    put("U07", f"rx/{F}_opg.dcm", dicom(G, "SANTORO^GIULIA"), "refused - DICOM not read")
    put("U08", "amministrazione/fatture.xlsx", b"PK\x03\x04" + b"\0" * 60, "refused - type not allowed")
    put("U09", "copie/referto_gallo_copia.txt", report, "proposed, strong - Francesca Gallo (same bytes as U01)")
    if shutil.which("sips"):
        put("U10", f"rx/{M}_opg.jpg", jpeg_from_text("OPG DEMO", work), "proposed, needs checking - Marco Gallo")
        put("U11", "foto/IMG_2001.jpg", with_exif(jpeg_from_text("DEMO", work), "2026:10:06 09:35:00"),
            "unmatched - a camera time is not identity")
        put("U12", "foto/IMG_2002.jpg", with_exif(jpeg_from_text("DEMO 2", work), "1980:01:01 00:00:00"),
            "unmatched - camera clock flagged as implausible")
    shutil.rmtree(work, ignore_errors=True)
    return cases


def main(argv):
    if argv[:1] == ["--uat"] and len(argv) == 2:
        from storage import connect
        conn = connect("db/clinic.sqlite")
        cases = build_uat(argv[1], conn)
        conn.close()
        for case, (rel, expected) in sorted(cases.items()):
            print(f"{case}  {rel:45}  {expected}")
        return 0
    if len(argv) != 1:
        print(__doc__)
        return 2
    cases = build(argv[0])
    print(json.dumps(cases, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
