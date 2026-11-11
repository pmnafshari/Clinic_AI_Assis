"""P29 R-029: Run a command, in either language. The owner flagged the page; checked on 2026-11-11: an Italian command
("cambia il telefono di Giulia Santoro in 333 7654321") failed - the model names the field as it was said
("telefono") - and the page showed the validation internals. A field said in plain Italian or English is the field
it names (the column still comes only from agent.EDITABLE_FIELDS); a command that cannot be understood is said so in
plain words with an example, never with the model's or the validator's wording."""
import json
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

import agent
import patient_id
import storage
import web_session

CF = "SNTGLI51R49D612Y"


def parser():
    # 1. the words a person uses for a field are that field; anything else is still refused
    for said, field in (("phone", "phone"), ("telefono", "phone"), ("Telefono", "phone"), ("numero di telefono", "phone"),
                        ("cellulare", "phone"), ("phone number", "phone"), ("mobile", "phone"), ("nome", "patient_name"),
                        ("name", "patient_name"), ("patient_name", "patient_name"), ("nome del paziente", "patient_name")):
        call = agent.parse_tool_call(json.dumps({"tool": "update_field",
                                                 "args": {"patient": "Giulia Santoro", "field": said, "value": "1"}}))
        assert call.parsed_args().field == field, f"1: {said!r} -> {call.parsed_args().field!r}"
    for said in ("email", "codice_fiscale", "role", "password", ""):
        try:
            agent.parse_tool_call(json.dumps({"tool": "update_field", "args": {"patient": "x", "field": said, "value": "1"}}))
            raise AssertionError(f"1: HARD FAIL - {said!r} accepted as a field")
        except ValueError:
            pass


def page():
    with tempfile.TemporaryDirectory() as tmp:
        import app.agent_routes as agent_routes
        import app.db as app_db
        from app import create_app
        app_db.DB_PATH = str(Path(tmp) / "c.sqlite")
        app_db.CHROMA_PATH = str(Path(tmp) / "chroma")
        conn = storage.init_db(app_db.DB_PATH)
        conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES ('dr_cmd', ?, 'dentist', 1)",
                     (generate_password_hash("x"),))
        patient_id.seed_patient(conn, CF, "Giulia Santoro", "+393401111111")
        conn.commit()
        replies = []

        def model(req, timeout=120):
            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({"response": replies.pop(0)}).encode()
            return R()
        old = agent_routes._urlopen
        agent_routes._urlopen = model
        try:
            app = create_app()
            app.config["TESTING"] = True
            app.config["WTF_CSRF_ENABLED"] = False
            c = app.test_client()
            c.set_cookie(web_session.COOKIE_NAME, web_session.create_session(conn, "dr_cmd", "dentist"))

            # 2. the Italian command reaches the confirmation, as the English one does
            replies.append(json.dumps({"tool": "update_field", "args": {"patient": "Giulia Santoro", "field": "telefono",
                                                                         "value": "333 7654321"}}))
            html = c.post("/agent/command", data={"command": "cambia il telefono di Giulia Santoro in 333 7654321"},
                          follow_redirects=True).get_data(as_text=True)
            assert "Confirm change" in html and "+393337654321" in html, "2: the Italian command is not understood"
            assert conn.execute("SELECT phone FROM patients").fetchone()[0] == "+393401111111", "2: written before confirm"

            # 3. not understood: plain words and an example - no validator or model wording
            for reply in (json.dumps({"tool": "update_field", "args": {"patient": "x", "field": "email", "value": "1"}}),
                          "I am not sure what you mean", json.dumps({"tool": "drop_table", "args": {}})):
                replies.append(reply)
                html = c.post("/agent/command", data={"command": "something odd"}).get_data(as_text=True)
                main = html.split("<main", 1)[1]
                assert "could not understand that command" in main and "change Mario Rossi" in main, f"3: {reply[:40]}"
                for inner in ("schema", "validation", "must be one of", "JSON", "drop_table", "model"):
                    assert inner not in main.split("Run command", 1)[0], f"3: HARD FAIL - {inner!r} shown to the user"
            c.set_cookie("staff_lang", "it")
            replies.append("??")
            html = c.post("/agent/command", data={"command": "boh"}).get_data(as_text=True)
            assert "Non ho capito quel comando" in html, "3: not in Italian"
        finally:
            agent_routes._urlopen = old
            conn.close()


def selftest():
    parser()
    page()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python agent_command_selftest.py --selftest")
