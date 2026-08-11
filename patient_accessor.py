import json

from auth import log_audit

# the only module the patient surface may use to read db/clinic.sqlite.
# every function filters on the session's own codice fiscale, sourced from
# the caller - never from request input. the dentist's free-text clinical
# notes field is excluded from every select list, even for the patient's
# own row. there is no write function here by construction, not by policy.


def get_demographics(cf, conn):
    row = conn.execute(
        "SELECT codice_fiscale, patient_name, phone FROM patients"
        " WHERE codice_fiscale = ?", (cf,)
    ).fetchone()
    if row is None:
        return None
    rows = _scope_rows([row], cf, conn, "get_demographics")
    if not rows:
        return None
    row = rows[0]
    return {"patient_name": row["patient_name"], "phone": row["phone"]}


def get_visits(cf, conn):
    rows = conn.execute(
        "SELECT codice_fiscale, visit_date, procedures, next_appointment FROM visits"
        " WHERE codice_fiscale = ? ORDER BY id", (cf,)
    ).fetchall()
    rows = _scope_rows(rows, cf, conn, "get_visits")
    return [
        {
            "visit_date": row["visit_date"],
            "procedures": json.loads(row["procedures"]) if row["procedures"] else [],
            "next_appointment": row["next_appointment"],
        }
        for row in rows
    ]


def get_next_appointment(cf, conn):
    row = conn.execute(
        "SELECT codice_fiscale, next_appointment FROM visits"
        " WHERE codice_fiscale = ? ORDER BY id DESC LIMIT 1", (cf,)
    ).fetchone()
    if row is None:
        return None
    rows = _scope_rows([row], cf, conn, "get_next_appointment")
    if not rows:
        return None
    return rows[0]["next_appointment"]


def get_invoices(cf, conn):
    rows = conn.execute(
        "SELECT codice_fiscale, amount, description FROM invoices"
        " WHERE codice_fiscale = ? ORDER BY id", (cf,)
    ).fetchall()
    rows = _scope_rows(rows, cf, conn, "get_invoices")
    return [{"amount": row["amount"], "description": row["description"]} for row in rows]


# on the sqlite path a parameterised WHERE codice_fiscale = ? cannot return
# another patient's row - this check is defence-in-depth against a future
# JOIN widening the result set. it does not catch a note ingested under the
# wrong CF at write time, because a correctly scoped query and a wrongly
# attributed row read the same column.
def _scope_rows(rows, cf, conn, fn_name):
    kept = []
    for row in rows:
        if row["codice_fiscale"] == cf:
            kept.append(row)
        else:
            log_audit(conn, cf, "patient", "patient_scope_violation", fn_name, allowed=0)
    return kept
