import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator

from ask import resolve_cf
from dental_notes_schema import CF_PATTERN
from extract_note import OllamaUnreachable, extract_json
from storage import init_db, lookup_patient

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_ID = "llama3.2:3b"  # general model prompted to emit tool JSON, not dental-notes

DB_PATH = "db/clinic.sqlite"
UNDO_LOG = "db/undo_log.jsonl"

EDITABLE_FIELDS = {"phone"}

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
