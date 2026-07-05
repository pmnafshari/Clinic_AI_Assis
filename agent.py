import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Literal

import openpyxl
from pydantic import BaseModel, field_validator

from ask import resolve_cf
from dental_notes_schema import CF_PATTERN, DentalNote
from extract_note import OllamaUnreachable, extract_json
from storage import init_db, lookup_patient, upsert_note_chroma, upsert_note_sql

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_ID = "llama3.2:3b"  # general model prompted to emit tool JSON, not dental-notes

DB_PATH = "db/clinic.sqlite"
UNDO_LOG = "db/undo_log.jsonl"

EDITABLE_FIELDS = {"phone"}
INVOICE_HEADER = ["date", "amount", "description"]

INTERPRETER_PROMPT = (
    "You are a clinic assistant that turns a command into ONE tool call.\n"
    "Reply with exactly one JSON object and nothing else: "
    '{"tool": "<tool name>", "args": {...}}\n'
    "Tools:\n"
    '- update_field: args {"patient": str, "field": str, "value": str}\n'
    '- append_note: args {"patient": str, "text": str}\n'
    '- add_invoice: args {"patient": str, "amount": float, "description": str}\n'
    "Command: "
)


class UpdateFieldArgs(BaseModel):
    patient: str
    field: str
    value: str

    @field_validator("field")
    @classmethod
    def validate_field(cls, v):
        if v not in EDITABLE_FIELDS:
            raise ValueError(f"field must be one of {sorted(EDITABLE_FIELDS)}, got {v!r}")
        return v


class AppendNoteArgs(BaseModel):
    patient: str
    text: str


class AddInvoiceArgs(BaseModel):
    patient: str
    amount: float
    description: str


TOOL_ARGS = {
    "update_field": UpdateFieldArgs,
    "append_note": AppendNoteArgs,
    "add_invoice": AddInvoiceArgs,
}


class ToolCall(BaseModel):
    tool: Literal["update_field", "append_note", "add_invoice"]
    args: dict

    def parsed_args(self):
        return TOOL_ARGS[self.tool](**self.args)


def parse_tool_call(reply):
    # reply -> validated ToolCall with typed args. Raises ValueError so a
    # malformed, unknown, or non-whitelisted tool call never gets executed.
    obj = extract_json(reply)
    if obj is None:
        raise ValueError("model did not return valid JSON")
    try:
        call = ToolCall(**obj)
        call.parsed_args()
    except Exception as e:
        raise ValueError("model output failed schema validation: " + str(e))
    return call


def call_model(command, urlopen=urllib.request.urlopen):
    payload = {
        "model": MODEL_ID,
        "prompt": INTERPRETER_PROMPT + command,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=120) as resp:
            body = json.load(resp)
    except urllib.error.URLError:
        raise OllamaUnreachable("Ollama not reachable - run: ollama run llama3.2:3b")
    return body.get("response", "")


def write_undo_entry(entry, log_path=UNDO_LOG):
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def resolve_patient(name, conn):
    cf = resolve_cf(name, conn)
    if cf is None:
        print(f"no patient named {name} on record")
        return None
    if isinstance(cf, list):
        print(f"multiple patients named {name} found, candidates: {', '.join(cf)}")
        typed = input("type the codice fiscale to use: ").strip().upper()
        if not CF_PATTERN.match(typed):
            print("invalid codice fiscale")
            return None
        cf = typed
    return cf


def build_diff_line(field, current, new, name, cf):
    return f"{field}: {current} -> {new} ({name}, {cf})"


def confirm(input_fn=input):
    answer = input_fn("proceed? [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def update_field(cf, field, value, conn):
    # field is always "phone" - whitelisted at the schema layer, never
    # interpolated from a model-supplied column name.
    conn.execute("UPDATE patients SET phone = ? WHERE codice_fiscale = ?", (value, cf))
    conn.commit()


def add_invoice(cf, amount, description, visit_date, sorted_root=Path("sorted")):
    # cf must be validated before any path is built - a model-supplied value
    # containing "../" must never reach the filesystem (T-06-02).
    if not CF_PATTERN.match(cf):
        raise ValueError(f"codice_fiscale must match ^[A-Z]{{4}}[0-9]{{12}}$, got {cf!r}")

    xlsx_dir = sorted_root / cf / "records"
    xlsx_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = xlsx_dir / "invoices.xlsx"

    if xlsx_path.exists():
        wb = openpyxl.load_workbook(xlsx_path)
        ws = wb.active
        row_count = ws.max_row - 1  # rows before this append, excluding header
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(INVOICE_HEADER)
        row_count = 0

    ws.append([visit_date, amount, description])
    wb.save(xlsx_path)
    return row_count


def pick_target_visit(cf, conn):
    # most recent visit for the CF (D-11 discretion); also returns the total
    # visit count so the confirm diff can say "most recent of N"
    row = conn.execute(
        "SELECT source_path, clinical_notes, visit_date FROM visits"
        " WHERE codice_fiscale = ? ORDER BY id DESC LIMIT 1",
        (cf,),
    ).fetchone()
    if row is None:
        return None
    count = conn.execute(
        "SELECT COUNT(*) c FROM visits WHERE codice_fiscale = ?", (cf,)
    ).fetchone()["c"]
    return row["source_path"], row["clinical_notes"], row["visit_date"], count


def append_note(cf, text, source_path, conn, collection, sorted_root=Path("sorted")):
    # cf must be validated before any path is built (T-06-02)
    if not CF_PATTERN.match(cf):
        raise ValueError(f"codice_fiscale must match ^[A-Z]{{4}}[0-9]{{12}}$, got {cf!r}")

    json_path = sorted_root / cf / "notes" / (Path(source_path).stem + ".json")
    note = DentalNote.model_validate_json(json_path.read_text())
    note.clinical_notes = (note.clinical_notes + "\n" + text) if note.clinical_notes else text
    json_path.write_text(note.model_dump_json())

    upsert_note_sql(note, source_path, conn)
    upsert_note_chroma(note, source_path, collection)


def run_command(command, conn, dry_run, urlopen, input_fn=input, log_path=UNDO_LOG):
    reply = call_model(command, urlopen)
    call = parse_tool_call(reply)
    args = call.parsed_args()

    cf = resolve_patient(args.patient, conn)
    if cf is None:
        return

    if call.tool == "update_field":
        data = lookup_patient(cf, conn)
        current = data["phone"]
        name = data["patient_name"]
        print(build_diff_line("phone", current, args.value, name, cf))

        if dry_run:
            return

        if not confirm(input_fn):
            print("no changes made")
            return

        write_undo_entry({
            "ts": datetime.now().isoformat(),
            "tool": "update_field",
            "codice_fiscale": cf,
            "target": "sqlite:patients.phone",
            "before": current,
        }, log_path)
        update_field(cf, "phone", args.value, conn)


def undo_last(conn, log_path=UNDO_LOG, collection=None, sorted_root=Path("sorted")):
    lines = Path(log_path).read_text().strip().splitlines()
    entry = json.loads(lines[-1])
    target = entry["target"]

    if target == "sqlite:patients.phone":
        update_field(entry["codice_fiscale"], "phone", entry["before"], conn)
        print(f"restored phone to {entry['before']} for {entry['codice_fiscale']}")
    elif target.startswith("visit:"):
        source_path = target[len("visit:"):]
        cf = entry["codice_fiscale"]
        json_path = sorted_root / cf / "notes" / (Path(source_path).stem + ".json")
        note = DentalNote.model_validate_json(json_path.read_text())
        note.clinical_notes = entry["before"]
        json_path.write_text(note.model_dump_json())
        upsert_note_sql(note, source_path, conn)
        upsert_note_chroma(note, source_path, collection)
        print(f"restored clinical note text for {cf}")
    elif target.startswith("xlsx:"):
        path = target[len("xlsx:"):]
        print(f"cannot auto-undo an invoice row append at {path} - restore manually")


def selftest():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        log_path = str(Path(tmp) / "undo_log.jsonl")

        conn = init_db(db_path)
        cf = "RSSM800010150100"
        conn.execute(
            "INSERT INTO patients (codice_fiscale, patient_name, phone) VALUES (?, ?, ?)",
            (cf, "mario rossi", "333 9999999"),
        )
        conn.commit()

        def fake_urlopen(req, timeout=120):
            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    return False

                def read(self):
                    tool_call = {
                        "tool": "update_field",
                        "args": {"patient": "rossi", "field": "phone", "value": "333-1234"},
                    }
                    return json.dumps({"response": json.dumps(tool_call)}).encode()

            return FakeResponse()

        # a. --dry-run leaves the phone unchanged and writes no log line
        run_command("update rossi's phone to 333-1234", conn, True, fake_urlopen,
                    input_fn=lambda p: "y", log_path=log_path)
        assert lookup_patient(cf, conn)["phone"] == "333 9999999", "dry-run must not write"
        assert not Path(log_path).exists(), "dry-run must not touch the undo log"

        # b. a declined confirm writes nothing
        run_command("update rossi's phone to 333-1234", conn, False, fake_urlopen,
                     input_fn=lambda p: "n", log_path=log_path)
        assert lookup_patient(cf, conn)["phone"] == "333 9999999", "declined confirm must not write"
        assert not Path(log_path).exists(), "declined confirm must not write the undo log"

        # c. a confirmed run writes the new phone and exactly one undo-log line
        run_command("update rossi's phone to 333-1234", conn, False, fake_urlopen,
                    input_fn=lambda p: "y", log_path=log_path)
        assert lookup_patient(cf, conn)["phone"] == "333-1234", "confirmed run must write the new value"
        lines = Path(log_path).read_text().strip().splitlines()
        assert len(lines) == 1, f"expected 1 undo-log line, got {len(lines)}"

        # d. undo restores the before-image
        undo_last(conn, log_path)
        assert lookup_patient(cf, conn)["phone"] == "333 9999999", "undo must restore the original phone"

        # e. invalid tool JSON and unreachable Ollama are rejected cleanly
        try:
            parse_tool_call("not json")
            raise AssertionError("non-JSON reply should have been rejected")
        except ValueError:
            pass

        def boom(*a, **k):
            raise urllib.error.URLError("connection refused")

        try:
            call_model("anything", urlopen=boom)
            raise AssertionError("unreachable Ollama should raise OllamaUnreachable")
        except OllamaUnreachable:
            pass

    print("selftest passed")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "undo":
        conn = init_db(DB_PATH)
        undo_last(conn)
        return
    if len(sys.argv) < 2:
        print('usage: python agent.py "<command>" [--dry-run]  |  python agent.py undo  |  python agent.py --selftest')
        sys.exit(1)

    dry_run = "--dry-run" in sys.argv
    conn = init_db(DB_PATH)
    try:
        run_command(sys.argv[1], conn, dry_run, urllib.request.urlopen)
    except OllamaUnreachable as e:
        print(e)
        sys.exit(1)
    except ValueError as e:
        print("rejected:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
