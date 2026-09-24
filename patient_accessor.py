import json
import re
import sys
import inspect
import ast
import sqlite3
import tempfile
from pathlib import Path

from auth import log_audit

# the only module the patient surface may use to read db/clinic.sqlite.
# every function filters on the session's own patient_id, sourced from the
# caller - never from request input. (Phase 51: the scope key used to be the
# codice fiscale. It is now the surrogate, which is what the session carries;
# the property is unchanged and so is _scope_rows below.) the dentist's free-text clinical
# notes field is excluded from every select list, even for the patient's
# own row. there is no write function here by construction, not by policy.


def get_demographics(pid, conn, ip=None):
    row = conn.execute(
        "SELECT patient_id, patient_name, phone FROM patients"
        " WHERE patient_id = ?", (pid,)
    ).fetchone()
    if row is None:
        return None
    rows = _scope_rows([row], pid, conn, "get_demographics", ip=ip)
    if not rows:
        return None
    row = rows[0]
    return {"patient_name": row["patient_name"], "phone": row["phone"]}


def get_visits(pid, conn, ip=None):
    rows = conn.execute(
        "SELECT patient_id, visit_date, procedures, next_appointment FROM visits"
        " WHERE patient_id = ? ORDER BY id", (pid,)
    ).fetchall()
    rows = _scope_rows(rows, pid, conn, "get_visits", ip=ip)
    return [
        {
            "visit_date": row["visit_date"],
            "procedures": json.loads(row["procedures"]) if row["procedures"] else [],
            "next_appointment": row["next_appointment"],
        }
        for row in rows
    ]


def get_next_appointment(pid, conn, ip=None):
    # P22: a booking, by the one rule every surface uses - not the recall text in
    # a visit note, which was never scheduled. clinic time, "YYYY-MM-DD HH:MM".
    import appointments
    booked = appointments.next_booked(conn, pid)
    if booked is None:
        return None
    # defence in depth: re-read that row scoped to this patient before it is used
    row = conn.execute("SELECT patient_id, starts_at FROM appointments WHERE patient_id = ? AND id = ?",
                       (pid, booked["id"])).fetchone()
    rows = _scope_rows([row or {"patient_id": booked["patient_id"]}], pid, conn, "get_next_appointment", ip=ip)
    if not rows:
        return None
    return appointments.next_booked_local(conn, pid)


def get_invoices(pid, conn, ip=None):
    rows = conn.execute(
        "SELECT patient_id, amount, description FROM invoices"
        " WHERE patient_id = ? ORDER BY id", (pid,)
    ).fetchall()
    rows = _scope_rows(rows, pid, conn, "get_invoices", ip=ip)
    return [{"amount": row["amount"], "description": row["description"]} for row in rows]


def get_billing(pid, conn, ip=None):
    # the ledger's own summary - the one computation staff, the portal and the
    # chat all read (P07.02). scope-checked like every other patient read.
    # the check is this module's own scoped read: every invoice in the
    # summary must be a visit this patient's own lines belong to
    import ledger
    own = {row["visit_id"] for row in conn.execute(
        "SELECT visit_id FROM invoices WHERE patient_id = ?", (pid,)).fetchall()}
    summary = ledger.patient_summary(conn, pid)
    rows = _scope_rows([{"patient_id": i["patient_id"] if i["visit_id"] in own else None}
                        for i in summary["invoices"]], pid, conn, "get_billing", ip=ip)
    if len(rows) != len(summary["invoices"]):
        return None
    return summary


# ip rides along so the mismatch row records where the request came from.
# this is the most security-relevant of the three patient rows, and a sweep
# with no source recorded is invisible after the fact. it defaults to None,
# so a caller that passes nothing still gets its row with ip NULL.
#
# on the sqlite path a parameterised WHERE patient_id = ? cannot return
# another patient's row - this check is defence-in-depth against a future
# JOIN widening the result set. it does not catch a note ingested under the
# wrong CF at write time, because a correctly scoped query and a wrongly
# attributed row read the same column.
def _scope_rows(rows, pid, conn, fn_name, ip=None):
    kept = []
    for row in rows:
        if row["patient_id"] == pid:
            kept.append(row)
        else:
            log_audit(conn, pid, "patient", "patient_scope_violation", fn_name, allowed=0, ip=ip)
    return kept


import patient_id as _pidmod


def selftest():
    # 0. fixture: a temp db with the tables this module reads, built by hand
    # rather than through storage.init_db - this module does not import
    # storage (D-08), and the selftest needs a schema to seed against.
    # Phase 51: the scope key is patient_id, so the fixture carries it too.
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(str(Path(tmp) / "clinic.sqlite"))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE patients (
                patient_id TEXT PRIMARY KEY NOT NULL,
                codice_fiscale TEXT UNIQUE NOT NULL,
                patient_name TEXT NOT NULL,
                phone TEXT
            );
            CREATE TABLE visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL,
                visit_date TEXT,
                procedures TEXT,
                clinical_notes TEXT,
                next_appointment TEXT,
                source_path TEXT UNIQUE NOT NULL
            );
            CREATE TABLE invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL,
                visit_id INTEGER NOT NULL,
                line_index INTEGER NOT NULL,
                amount REAL NOT NULL,
                description TEXT
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                allowed INTEGER NOT NULL,
                ip TEXT,
                reason TEXT
            );
        """)
        conn.commit()

        # 1. seed two patients, each with a visit and an invoice. patient A's
        # visit carries a distinctive free-text sentinel in the column this
        # module must never select - section 4 checks that sentinel never
        # comes back through get_visits.
        cf_a = "AAAA800010150100"
        cf_b = "BBBB850315150200"
        sentinel = "SENTINEL_DO_NOT_LEAK"

        pid_a = _pidmod.seed_patient(conn, cf_a, "anna alfa", "111000111")
        pid_b = _pidmod.seed_patient(conn, cf_b, "bruno beta", "222000222")
        conn.execute(
            "INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes,"
            " next_appointment, source_path) VALUES (?, ?, ?, ?, ?, ?)",
            (pid_a, "2026-06-01", json.dumps(["filling 14"]), sentinel, "2026-09-01", "a/n1.json"),
        )
        conn.execute(
            "INSERT INTO visits (patient_id, visit_date, procedures, clinical_notes,"
            " next_appointment, source_path) VALUES (?, ?, ?, ?, ?, ?)",
            (pid_b, "2026-06-02", json.dumps(["cleaning"]), "cleaning done", "2026-09-02", "b/n1.json"),
        )
        visit_id_a = conn.execute(
            "SELECT id FROM visits WHERE source_path = ?", ("a/n1.json",)
        ).fetchone()["id"]
        visit_id_b = conn.execute(
            "SELECT id FROM visits WHERE source_path = ?", ("b/n1.json",)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO invoices (patient_id, visit_id, line_index, amount, description)"
            " VALUES (?, ?, ?, ?, ?)",
            (pid_a, visit_id_a, 0, 80.0, "filling 14"),
        )
        conn.execute(
            "INSERT INTO invoices (patient_id, visit_id, line_index, amount, description)"
            " VALUES (?, ?, ?, ?, ?)",
            (pid_b, visit_id_b, 0, 40.0, "cleaning"),
        )
        conn.commit()

        # 2. each accessor, called with A's cf, returns only A's data - B's
        # name, phone, visit date, procedure and invoice description appear
        # nowhere in the four results. this is CHAT-04's unit-level proof;
        # SC2's live proof is plan 18-06.
        # P22: the next appointment is a booking (clinic time), not visits.next_appointment
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS appointments (id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL, dentist TEXT NOT NULL, starts_at TEXT NOT NULL,
                minutes INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'booked', note TEXT,
                period TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS patient_merges (source_cf TEXT, target_patient_id TEXT);
        """)
        conn.execute("INSERT INTO appointments (patient_id, dentist, starts_at, minutes, status, created_at,"
                     " updated_at) VALUES (?, 'dentist', '2026-10-01T08:00:00+00:00', 30, 'booked', 'x', 'x')",
                     (pid_a,))
        import clinic_time as _ct
        from datetime import datetime as _dt
        _real_now = _ct.now
        _ct.now = lambda env=None: _dt(2026, 9, 17, 9, 0)
        demo_a = get_demographics(pid_a, conn)
        visits_a = get_visits(pid_a, conn)
        next_a = get_next_appointment(pid_a, conn)
        _ct.now = _real_now
        invoices_a = get_invoices(pid_a, conn)

        assert demo_a == {"patient_name": "anna alfa", "phone": "111000111"}, \
            f"2: get_demographics returned {demo_a}"
        assert len(visits_a) == 1 and visits_a[0]["visit_date"] == "2026-06-01", \
            f"2: get_visits returned {visits_a}"
        assert next_a == "2026-10-01 10:00", f"2: get_next_appointment returned {next_a}"
        assert len(invoices_a) == 1 and invoices_a[0]["amount"] == 80.0, \
            f"2: get_invoices returned {invoices_a}"

        blob_a = json.dumps([demo_a, visits_a, next_a, invoices_a])
        for leak in ("bruno beta", "222000222", "2026-06-02", "cleaning", "40.0", "40"):
            assert leak not in blob_a, f"2: patient B's {leak!r} leaked into A's results"

        # 3. each accessor, called with a cf that exists in neither row,
        # returns None/None/empty lists - never a fallback to "all rows".
        missing_cf = "ZZZZ000000000000"
        assert get_demographics(missing_cf, conn) is None, "3: unknown cf should give None"
        assert get_visits(missing_cf, conn) == [], "3: unknown cf should give []"
        assert get_next_appointment(missing_cf, conn) is None, "3: unknown cf should give None"
        assert get_invoices(missing_cf, conn) == [], "3: unknown cf should give []"

        # 4. get_visits for the patient with a clinical-notes value never
        # returns that sentinel, and the returned keys are exactly the three
        # named in this plan's interfaces block.
        assert sentinel not in json.dumps(visits_a), "4: clinical notes sentinel leaked"
        assert set(visits_a[0].keys()) == {"visit_date", "procedures", "next_appointment"}, \
            f"4: unexpected visit keys {visits_a[0].keys()}"

        # 5. _scope_rows drops a mismatched row and audits exactly one
        # denial - the only way to exercise D-09, since a parameterised
        # WHERE patient_id = ? cannot itself produce a mismatch.
        before = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
        fake_rows = [
            {"patient_id": pid_a, "value": "keep"},
            {"patient_id": pid_b, "value": "drop"},
        ]
        kept = _scope_rows(fake_rows, pid_a, conn, "get_demographics")
        assert len(kept) == 1 and kept[0]["value"] == "keep", f"5: _scope_rows kept {kept}"
        after = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
        assert after == before + 1, "5: exactly one denial row should be written"
        denial = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        assert denial["role"] == "patient", f"5: denial role was {denial['role']}"
        assert denial["action"] == "patient_scope_violation", \
            f"5: denial action was {denial['action']}"
        assert denial["allowed"] == 0, "5: denial should be allowed=0"
        assert denial["target"] == "get_demographics", f"5: denial target was {denial['target']}"
        # the four-positional call above passes no ip. that default is what
        # keeps patient_app_selftest's negative control and eval_chat.py
        # working unedited, so it is pinned rather than assumed.
        assert denial["ip"] is None, f"5: a call with no ip must leave ip NULL, got {denial['ip']}"

        # D-07: the same drop, with a source address, records it. TEST-NET-3
        # so the fixture can never be confused for a real client.
        before_ip = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
        kept_ip = _scope_rows(fake_rows, pid_a, conn, "get_visits", ip="203.0.113.9")
        assert len(kept_ip) == 1, f"5: _scope_rows kept {kept_ip}"
        after_ip = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
        assert after_ip == before_ip + 1, "5: exactly one denial row should be written"
        denial_ip = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        assert denial_ip["action"] == "patient_scope_violation", \
            f"5: denial action was {denial_ip['action']}"
        assert denial_ip["allowed"] == 0, "5: denial should be allowed=0"
        assert denial_ip["target"] == "get_visits", f"5: denial target was {denial_ip['target']}"
        assert denial_ip["ip"] == "203.0.113.9", \
            f"5: the scope-violation row must carry the source address, got {denial_ip['ip']}"

        # --- static sections: the §3.1 mandated invariant, and the
        # mechanical answer to roadmap SC3 ("no write function exists").
        # build the scan target: this module's own source, with comment
        # lines, the module docstring and the selftest/main bodies removed.
        source = inspect.getsource(sys.modules[__name__])
        tree = ast.parse(source)
        lines = source.splitlines()

        # comments are stripped because a module must stay free to explain
        # in a comment why a write statement or the excluded column is
        # absent - a naive substring check over raw source would forbid
        # exactly that explanation.
        docstring = ast.get_docstring(tree)

        # the selftest has to write its own fixture rows before it can read
        # them back, so a whole-file token scan would be self-defeating.
        # excluding exactly these two named functions keeps the invariant
        # meaning what §3.1 intends - no write and no wildcard on any path a
        # request can reach - rather than being quietly relaxed to a weaker
        # check. a separate selftest module was rejected: it would move the
        # static invariant away from the module it guards.
        excluded_names = {"selftest", "main"}
        excluded_ranges = [
            (node.lineno, node.end_lineno) for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name in excluded_names
        ]

        def in_excluded_range(lineno):
            return any(start <= lineno <= end for start, end in excluded_ranges)

        scannable_lines = []
        for idx, line in enumerate(lines, start=1):
            if line.lstrip()[:1] == "#":
                continue
            if docstring and docstring in line:
                continue
            if in_excluded_range(idx):
                continue
            scannable_lines.append(line)
        scannable = "\n".join(scannable_lines)

        # 6. every get_* function's body carries the literal
        # "patient_id = ?" - discovered by name prefix, not hardcoded, so a
        # fifth accessor added later cannot escape the check. Phase 51 moved
        # the scope key off the codice fiscale; the PROPERTY this pins is
        # unchanged - every read here is filtered to one patient by a bound
        # parameter, never by string building.
        get_functions = [
            (name, fn) for name, fn in inspect.getmembers(sys.modules[__name__], inspect.isfunction)
            if name.startswith("get_")
        ]
        assert get_functions, "6: no get_* functions found - selftest fixture is broken"
        for name, fn in get_functions:
            assert "patient_id = ?" in inspect.getsource(fn), \
                f"6: {name} is missing the literal patient_id = ? filter"

        # 7. the excluded free-text clinical column never appears anywhere
        # in the scannable source (D-10).
        assert "clinical_notes" not in scannable, \
            "7: clinical_notes must never appear in patient_accessor.py (D-10)"

        # 8. no wildcard column list anywhere - checked in the strict form,
        # since this module has no legitimate use for the character.
        assert re.search(r"(?i)select\s+\*", scannable) is None, \
            "8: no SELECT * allowed in patient_accessor.py"
        assert "*" not in scannable, "8: no bare * allowed in patient_accessor.py"

        # 9. CHAT-05 / roadmap SC3: no write-verb token and no write-verb
        # function name anywhere on a path a request can reach.
        assert re.search(r"(?i)\b(insert|update|delete|drop|alter|replace)\b", scannable) is None, \
            "9: CHAT-05/SC3 - no write-statement token allowed in patient_accessor.py"
        write_prefixes = (
            "insert_", "update_", "add_", "set_", "delete_", "remove_",
            "append_", "write_", "save_", "create_",
        )
        for name, _ in inspect.getmembers(sys.modules[__name__], inspect.isfunction):
            assert not name.startswith(write_prefixes), \
                f"9: CHAT-05/SC3 - {name} looks like a write function, none may exist here"

        # 10. the import graph never reaches storage/ask/app/web_auth/
        # web_session/patient_app - and resolve_cf, ask.py's inverse
        # operation, never appears in the source at all (§3.2).
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden_imports = {"storage", "ask", "app", "web_auth", "web_session", "patient_app"}
        assert not (forbidden_imports & imported), \
            f"10: patient_accessor.py must not import {forbidden_imports & imported}"
        assert "resolve_cf" not in scannable, \
            "10: resolve_cf is ask.py's inverse operation, patient code must never call it"

        # 11. the selftest/main exclusion is bounded: exactly those two
        # names are excluded, main's own source carries none of section 9's
        # write tokens, and nothing outside those two functions calls
        # selftest - that is what keeps the fixture's write statements
        # unreachable from any path a request can take.
        excluded_funcs = sorted(
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name in excluded_names
        )
        assert excluded_funcs == ["main", "selftest"], \
            f"11: expected exactly selftest and main excluded, got {excluded_funcs}"

        main_node = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        main_source = "\n".join(lines[main_node.lineno - 1:main_node.end_lineno])
        assert re.search(r"(?i)\b(insert|update|delete|drop|alter|replace)\b", main_source) is None, \
            "11: main() must carry none of the write tokens excluded from the scan"

        selftest_calls_outside_main = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "selftest"
            and not (main_node.lineno <= node.lineno <= main_node.end_lineno)
        ]
        assert not selftest_calls_outside_main, \
            f"11: selftest() must only be called from main(), also called at lines {selftest_calls_outside_main}"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python patient_accessor.py --selftest")


if __name__ == "__main__":
    main()
