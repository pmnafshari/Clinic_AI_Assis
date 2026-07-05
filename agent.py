import json
import urllib.error
import urllib.request
from typing import Literal

from pydantic import BaseModel, field_validator

from dental_notes_schema import CF_PATTERN
from extract_note import OllamaUnreachable, extract_json

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_ID = "llama3.2:3b"  # general model prompted to emit tool JSON, not dental-notes

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
