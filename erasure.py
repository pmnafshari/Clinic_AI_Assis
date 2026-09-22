"""Erase one patient from every store, and do it again after a restore (P06.06).

Stores: the SQLite tables keyed on the patient, their sorted/ directory (and
any legacy or merged-away directory), unprocessed drop/ files naming them, the
agent's undo log, the search index, and any export built for them. The audit
trail is kept - it names patients by surrogate only - but rows whose target
carries a codice fiscale or a file path of theirs are redacted.

What retention.json holds on erasure stays: under the demo policy, invoices,
with the name and codice fiscale they were issued to, and the visit dates they
bill. The phone number and every clinical detail go. The reason is returned
so the request records it.

Every erasure appends a tombstone to TOMBSTONES, which is deliberately outside
the backup set. backup.restore calls reapply() on what it extracts, so a
restored archive older than the erasure does not bring the patient back. A
tombstone holds the surrogate id and an HMAC of the codice fiscale, never the
code itself.
"""
import hashlib
import hmac
import json
import os
import shutil
import sys
from pathlib import Path

import clinic_time
from auth import authorize, log_audit

ROOT = Path(__file__).resolve().parent
TOMBSTONES = ROOT / "db" / "erasures.jsonl"
KEY_ENV = "CLINIC_ERASURE_KEY_FILE"
DEFAULT_KEY = Path.home() / ".clinic-erasure.key"
RETENTION = ROOT / "retention.json"


def _key(path=None):
    path = Path(path or os.environ.get(KEY_ENV) or DEFAULT_KEY)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(os.urandom(32).hex() + "\n")
    return bytes.fromhex(path.read_text().strip())


def cf_mac(cf, key_path=None):
    return hmac.new(_key(key_path), cf.upper().encode(), hashlib.sha256).hexdigest()


def holds(policy_path=None):
    types = json.loads(Path(policy_path or RETENTION).read_text())["types"]
    return {name: t for name, t in types.items() if t.get("hold_on_erasure")}


def _has(conn, table):
    # a restored archive can predate a table; there is nothing in it to erase
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,)).fetchone() is not None


def _sqlite(conn, pid, cf, sources, hold_invoices):
    invoiced = []
    if hold_invoices:
        invoiced = [r[0] for r in conn.execute(
            "SELECT DISTINCT visit_id FROM invoices WHERE patient_id = ?", (pid,))]
    marks = ",".join("?" * len(invoiced))
    keys = [cf] + sources

    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO audit_unlock (reason) VALUES ('erasure')")
        if _has(conn, "consent_records"):
            conn.execute("DELETE FROM consent_records WHERE patient_id = ?", (pid,))
        if _has(conn, "billing_events"):
            # queued reminders are not fiscal records; they go whatever is held
            conn.execute("DELETE FROM billing_events WHERE patient_id = ?", (pid,))
        if _has(conn, "reminder_jobs"):
            # P09: the queue itself, sent rows included. a sent reminder is not
            # evidence of anything fiscal - what it was about is in the ledger
            conn.execute("DELETE FROM reminder_jobs WHERE patient_id = ?", (pid,))
        if _has(conn, "delivery_receipts"):
            # P11: receipts for this patient's reminders. the reminder rows
            # themselves go above, so a receipt pointing at nothing is worse
            # than no receipt
            conn.execute("DELETE FROM delivery_receipts WHERE job_id IN"
                         " (SELECT id FROM reminder_jobs WHERE patient_id = ?)", (pid,))
        for table in ("patient_agent_actions", "handoff_requests"):
            # P10: a half-finished booking and a queued call-back are neither
            # clinical nor fiscal; they go with the patient whatever is held
            if _has(conn, table):
                conn.execute(f"DELETE FROM {table} WHERE patient_id = ?", (pid,))
        if not hold_invoices and _has(conn, "billing_invoices"):
            # the ledger is fiscal: it goes only when invoices are not held
            conn.execute("DELETE FROM payment_allocations WHERE payment_id IN"
                         " (SELECT id FROM payments WHERE patient_id = ?)", (pid,))
            conn.execute("DELETE FROM payments WHERE patient_id = ? AND reverses_payment_id"
                         " IS NOT NULL", (pid,))
            conn.execute("DELETE FROM payments WHERE patient_id = ?", (pid,))
            conn.execute("DELETE FROM installments WHERE plan_id IN (SELECT p.id FROM"
                         " installment_plans p JOIN billing_invoices b ON b.id = p.invoice_id"
                         " WHERE b.patient_id = ?)", (pid,))
            conn.execute("DELETE FROM installment_plans WHERE invoice_id IN"
                         " (SELECT id FROM billing_invoices WHERE patient_id = ?)", (pid,))
            conn.execute("DELETE FROM billing_invoices WHERE patient_id = ?", (pid,))
        if not hold_invoices:
            conn.execute("DELETE FROM invoices WHERE patient_id = ?", (pid,))
        if not invoiced and _has(conn, "billing_invoices"):
            # hold requested but nothing invoiced: no ledger row can remain
            conn.execute("DELETE FROM billing_invoices WHERE patient_id = ?", (pid,))
        if invoiced:
            conn.execute(f"DELETE FROM visits WHERE patient_id = ? AND id NOT IN ({marks})",
                         [pid] + invoiced)
            # a visit an invoice still points at keeps its date and nothing else
            conn.execute(f"UPDATE visits SET procedures = '[]', clinical_notes = '',"
                         f" next_appointment = NULL, source_path = 'erased:' || id"
                         f" WHERE patient_id = ? AND id IN ({marks})", [pid] + invoiced)
        else:
            conn.execute("DELETE FROM visits WHERE patient_id = ?", (pid,))
        for table in ("appointments", "patient_credentials", "patient_sessions"):
            if _has(conn, table):
                conn.execute(f"DELETE FROM {table} WHERE patient_id = ?", (pid,))
        if _has(conn, "patient_merges"):
            conn.execute("DELETE FROM patient_merges WHERE target_patient_id = ?", (pid,))
        if _has(conn, "patient_duplicate_dismissals"):
            conn.execute("DELETE FROM patient_duplicate_dismissals"
                         " WHERE patient_id_a = ? OR patient_id_b = ?", (pid, pid))
        for key in keys + [pid]:
            conn.execute("DELETE FROM pending_actions WHERE payload LIKE ?", (f"%{key}%",))
        if _has(conn, "data_requests"):
            conn.execute("UPDATE data_requests SET detail = NULL, export_file = NULL"
                         " WHERE patient_id = ?", (pid,))
        for key in keys:
            conn.execute("UPDATE audit_log SET target = 'erased:' || ? WHERE"
                         " lower(target) LIKE ?", (pid, f"%{key.lower()}%"))
        conn.execute("UPDATE audit_log SET target = 'erased:' || ? WHERE target LIKE ?",
                     (pid, f"sorted/{pid}/%"))
        if invoiced:
            conn.execute("UPDATE patients SET phone = NULL WHERE patient_id = ?", (pid,))
        else:
            conn.execute("DELETE FROM patients WHERE patient_id = ?", (pid,))
        conn.execute("DELETE FROM audit_unlock")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return bool(invoiced)


def _files(pid, keys, sorted_root, drop_dir, keep_records):
    removed = 0
    for name in [pid] + keys:
        folder = Path(sorted_root) / name
        if not folder.is_dir():
            continue
        for f in sorted(folder.rglob("*")):
            if f.is_file() and not (keep_records and f.relative_to(folder).parts[0] == "records"):
                f.unlink()
                removed += 1
        if not keep_records:
            shutil.rmtree(folder, ignore_errors=True)
            continue
        for sub in sorted(folder.rglob("*"), reverse=True):
            if sub.is_dir() and sub.name != "records" and not any(sub.iterdir()):
                sub.rmdir()
    if drop_dir and Path(drop_dir).is_dir():
        for f in Path(drop_dir).rglob("*"):
            if f.is_file() and any(k.lower() in f.name.lower() for k in keys):
                f.unlink()
                removed += 1
    return removed


def _undo_log(undo_log, keys):
    path = Path(undo_log)
    if not path.exists():
        return 0
    import agent
    with agent._undo_lock:
        kept, dropped = [], 0
        for line in path.read_text().splitlines():
            try:
                cf = json.loads(line).get("codice_fiscale", "")
            except json.JSONDecodeError:
                cf = ""
            if cf and cf.upper() in keys:
                dropped += 1
            else:
                kept.append(line)
        path.write_text("\n".join(kept) + ("\n" if kept else ""))
    return dropped


def _index(collection, pid, keys):
    if collection is None:
        return None
    for where in [{"patient_id": pid}] + [{"codice_fiscale": k} for k in keys]:
        collection.delete(where=where)
    return 0


def remaining(conn, pid, keys, sorted_root, undo_log, collection, keep_records):
    left = {}
    for table in ("consent_records", "appointments", "patient_credentials", "patient_sessions"):
        if not _has(conn, table):
            continue
        left[table] = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE patient_id = ?",
                                   (pid,)).fetchone()[0]
    left["visit_content"] = conn.execute(
        "SELECT COUNT(*) FROM visits WHERE patient_id = ? AND (clinical_notes != ''"
        " OR procedures != '[]' OR source_path NOT LIKE 'erased:%')", (pid,)).fetchone()[0]
    left["phone"] = conn.execute("SELECT COUNT(*) FROM patients WHERE patient_id = ?"
                                 " AND phone IS NOT NULL", (pid,)).fetchone()[0]
    left["audit_cf"] = sum(conn.execute("SELECT COUNT(*) FROM audit_log WHERE lower(target)"
                                        " LIKE ? OR username = ?",
                                        (f"%{k.lower()}%", k)).fetchone()[0] for k in keys)
    left["files"] = sum(1 for name in [pid] + keys
                        for f in (Path(sorted_root) / name).rglob("*")
                        if f.is_file() and not (keep_records and "records" in f.parts))
    path = Path(undo_log)
    left["undo_log"] = sum(1 for k in keys for line in
                           (path.read_text().splitlines() if path.exists() else [])
                           if k in line)
    if collection is not None:
        left["index"] = len(collection.get(where={"patient_id": pid})["ids"]) + sum(
            len(collection.get(where={"codice_fiscale": k})["ids"]) for k in keys)
    return left


def erase(conn, pid, actor, role, req_id, sorted_root=Path("sorted"), drop_dir=Path("drop"),
          undo_log=None, collection=None, exports_dir=None, tombstones=None,
          policy_path=None, key_path=None, write_tombstone=True):
    if not (authorize(role, "manage_data_requests") or authorize(role, "reapply_erasure")):
        log_audit(conn, actor, role, "erase_patient", pid, allowed=0)
        raise PermissionError(f"{role} may not erase a patient")
    import agent
    import data_rights
    undo_log = undo_log or agent.UNDO_LOG
    row = conn.execute("SELECT codice_fiscale FROM patients WHERE patient_id = ?",
                       (pid,)).fetchone()
    if row is None:
        return {"hold_reason": None, "held": [], "left": {}, "already": True}
    cf = row[0].upper()
    sources = []
    if _has(conn, "patient_merges"):
        sources = [r[0].upper() for r in conn.execute(
            "SELECT source_cf FROM patient_merges WHERE target_patient_id = ?", (pid,))]
    keys = [cf] + sources

    if _has(conn, "data_requests"):
        for r in conn.execute("SELECT export_file FROM data_requests WHERE patient_id = ?"
                              " AND export_file IS NOT NULL", (pid,)).fetchall():
            (Path(exports_dir or data_rights.EXPORTS_DIR) / r[0]).unlink(missing_ok=True)

    held_types = holds(policy_path)
    held = _sqlite(conn, pid, cf, sources, "invoices" in held_types)
    _files(pid, keys, sorted_root, drop_dir, keep_records=held)
    _undo_log(undo_log, keys)
    _index(collection, pid, keys)

    hold_reason = None
    if held:
        hold_reason = ("invoices kept under the retention policy (demo template: "
                       f"{held_types['invoices']['basis']})")
    if write_tombstone:
        tombstones = Path(tombstones or TOMBSTONES)
        tombstones.parent.mkdir(parents=True, exist_ok=True)
        with open(tombstones, "a") as f:
            f.write(json.dumps({"patient_id": pid, "cf_hmac": [cf_mac(k, key_path) for k in keys],
                                "request_id": req_id, "erased_at": clinic_time.stamp(),
                                "held": ["invoices"] if held else []}) + "\n")
    left = remaining(conn, pid, keys, sorted_root, undo_log, collection, held)
    log_audit(conn, actor, role, "erase_patient", pid, allowed=1,
              reason="held: invoices" if held else "complete")
    return {"hold_reason": hold_reason, "held": ["invoices"] if held else [], "left": left}


def reapply(conn, sorted_root, drop_dir, undo_log, tombstones=None, key_path=None,
            policy_path=None):
    """Erase again, in a restored copy, everyone a tombstone names."""
    path = Path(tombstones or TOMBSTONES)
    if not path.exists():
        return []
    macs = {}
    for r in conn.execute("SELECT patient_id, codice_fiscale FROM patients"):
        macs[cf_mac(r[1], key_path)] = r[0]
    done = []
    for line in path.read_text().splitlines():
        stone = json.loads(line)
        targets = {stone["patient_id"]} | {macs[m] for m in stone["cf_hmac"] if m in macs}
        for pid in sorted(targets):
            row = conn.execute("SELECT codice_fiscale FROM patients WHERE patient_id = ?",
                               (pid,)).fetchone()
            if row is None:
                continue
            if stone["held"]:
                # a held patient stays as a shell; only erase again if the
                # restored copy has more than the shell
                left = remaining(conn, pid, [row[0].upper()], sorted_root, undo_log, None, True)
                if not any(left.values()):
                    continue
            result = erase(conn, pid, "restore", "system", stone["request_id"],
                           sorted_root=sorted_root, drop_dir=drop_dir, undo_log=undo_log,
                           write_tombstone=False, policy_path=policy_path, key_path=key_path)
            if not result.get("already"):
                done.append(pid)
    return done


def selftest():
    import tempfile

    import patient_id
    import storage

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ[KEY_ENV] = str(root / "erasure.key")
        conn = storage.init_db(str(root / "clinic.sqlite"))
        sorted_root, drop, undo = root / "sorted", root / "drop", root / "undo_log.jsonl"
        stones = root / "erasures.jsonl"
        collection = storage.get_collection(str(root / "chroma"))

        def seed(cf, name, invoice):
            pid = patient_id.seed_patient(conn, cf, name, "3330000000")
            cur = conn.execute("INSERT INTO visits (patient_id, visit_date, procedures,"
                               " clinical_notes, source_path) VALUES (?, '2026-05-01',"
                               " '[\"rct 26\"]', ?, ?)", (pid, f"{name} secret", f"{cf}.json"))
            if invoice:
                conn.execute("INSERT INTO invoices (patient_id, visit_id, line_index, amount,"
                             " description) VALUES (?, ?, 0, 90.0, 'visita')", (pid, cur.lastrowid))
            conn.execute("INSERT INTO appointments (patient_id, dentist, starts_at, minutes,"
                         " status, created_at, updated_at) VALUES (?, 'drossi',"
                         " ?, 30, 'booked', '2026-09-01T08:00:00+00:00',"
                         " '2026-09-01T08:00:00+00:00')", (pid, f"2030-01-0{cf[-1]}T09:00:00+00:00"))
            conn.commit()
            for sub in ("notes", "records"):
                (sorted_root / pid / sub).mkdir(parents=True)
                (sorted_root / pid / sub / f"{cf}.txt").write_text(f"{name} secret")
            drop.mkdir(exist_ok=True)
            (drop / f"nota_{cf.lower()}.txt").write_text("waiting")
            with open(undo, "a") as f:
                f.write(json.dumps({"codice_fiscale": cf, "before": f"{name} secret"}) + "\n")
            collection.upsert(ids=[cf], documents=[f"{name} secret"],
                              metadatas=[{"codice_fiscale": cf, "patient_id": pid}])
            import consent
            consent.record(conn, pid, "messaging", True, "drossi", "dentist")
            log_audit(conn, "drossi", "dentist", "upload_file", f"sorted/{pid}/notes/{cf}.txt", 1)
            return pid

        gone = seed("ZZER800101010101", "Ezio Gone", invoice=False)
        held = seed("ZZER800101010102", "Hilda Held", invoice=True)
        import ledger
        hv = conn.execute("SELECT visit_id FROM invoices WHERE patient_id = ?", (held,)).fetchone()[0]
        conn.execute("UPDATE invoices SET amount_cents = 9000 WHERE patient_id = ?", (held,))
        hinv = ledger.ensure_invoice(conn, held, hv)
        conn.commit()
        ledger.issue(conn, hinv, "drossi", "dentist")
        ledger.record_payment(conn, hinv, "40,00", "card", "er-1", "drossi", "dentist")
        stay = seed("ZZER800101010103", "Stan Stays", invoice=False)

        # 1. only a role that may approve erasures can run one
        try:
            erase(conn, gone, "aassist", "assistant", 1, tombstones=stones)
            raise AssertionError("1: an assistant must not erase")
        except PermissionError:
            pass

        # 2. a full erasure leaves nothing in any store
        result = erase(conn, gone, "drossi", "dentist", 1, sorted_root, drop, undo, collection,
                       tombstones=stones)
        assert result["hold_reason"] is None and not any(result["left"].values()), f"2: {result}"
        assert conn.execute("SELECT COUNT(*) FROM patients WHERE patient_id = ?",
                            (gone,)).fetchone()[0] == 0, "2: the patient row goes"
        assert not (sorted_root / gone).exists(), "2: the directory goes"
        assert "zzer800101010101" not in " ".join(p.name for p in drop.iterdir()), "2: drop"
        assert "Ezio" not in undo.read_text(), "2: the undo log"
        assert collection.get(where={"patient_id": gone})["ids"] == [], "2: the index"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE target LIKE ?",
                            (f"sorted/{gone}/%",)).fetchone()[0] == 0, "2: audit paths redacted"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'erase_patient'"
                            " AND target = ? AND allowed = 1", (gone,)).fetchone()[0] == 1

        # 3. a held erasure keeps invoices and the identity on them, nothing else
        result = erase(conn, held, "drossi", "dentist", 2, sorted_root, drop, undo, collection,
                       tombstones=stones)
        assert result["held"] == ["invoices"] and "demo template" in result["hold_reason"], "3"
        assert not any(result["left"].values()), f"3: {result['left']}"
        row = conn.execute("SELECT patient_name, phone FROM patients WHERE patient_id = ?",
                           (held,)).fetchone()
        assert row[0] == "Hilda Held" and row[1] is None, "3: name kept for the invoice, phone gone"
        assert conn.execute("SELECT COUNT(*) FROM invoices WHERE patient_id = ?",
                            (held,)).fetchone()[0] == 1, "3: the invoice is held"
        visit = conn.execute("SELECT visit_date, clinical_notes, procedures FROM visits"
                             " WHERE patient_id = ?", (held,)).fetchone()
        assert tuple(visit) == ("2026-05-01", "", "[]"), f"3: visit shell {tuple(visit)}"
        assert [p.name for p in (sorted_root / held).iterdir()] == ["records"], "3: records kept"
        assert ledger.summary(conn, hinv)["paid_cents"] == 4000, "3: the ledger is held with them"
        assert conn.execute("SELECT COUNT(*) FROM billing_events WHERE patient_id = ?",
                            (held,)).fetchone()[0] == 0, "3: queued reminders go"
        assert conn.execute("SELECT COUNT(*) FROM reminder_jobs WHERE patient_id = ?",
                            (held,)).fetchone()[0] == 0, "3: the reminder queue goes too"
        for table in ("patient_agent_actions", "handoff_requests"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE patient_id = ?",
                                (held,)).fetchone()[0] == 0, f"3: {table} goes too"

        # 3b. with no invoice hold, the ledger goes too: payments, allocations,
        # plans, invoices, events - through the unlock, nothing left
        free = seed("ZZER800101010104", "Fabio Free", invoice=True)
        fv = conn.execute("SELECT visit_id FROM invoices WHERE patient_id = ?", (free,)).fetchone()[0]
        conn.execute("UPDATE invoices SET amount_cents = 6000 WHERE patient_id = ?", (free,))
        finv = ledger.ensure_invoice(conn, free, fv)
        conn.commit()
        ledger.issue(conn, finv, "drossi", "dentist")
        fp, _ = ledger.record_payment(conn, finv, "30,00", "cash", "er-2", "drossi", "dentist")
        ledger.refund(conn, fp, "10,00", "er-3", "drossi", "dentist", "goodwill")
        ledger.plan_installments(conn, finv, 2, "2030-02-01", "drossi", "dentist")
        policy = root / "no-hold.json"
        types = json.loads(RETENTION.read_text())["types"]
        types["invoices"]["hold_on_erasure"] = False
        policy.write_text(json.dumps({"types": types}))
        result = erase(conn, free, "drossi", "dentist", 4, sorted_root, drop, undo, collection,
                       tombstones=stones, policy_path=policy)
        assert result["held"] == [] and not any(result["left"].values()), f"3b: {result}"
        for table in ("payments", "billing_invoices", "billing_events", "invoices", "patients"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE patient_id = ?",
                                (free,)).fetchone()[0] == 0, f"3b: {table} kept a row"
        assert conn.execute("SELECT COUNT(*) FROM payment_allocations WHERE invoice_id = ?",
                            (finv,)).fetchone()[0] == 0, "3b: allocations kept"
        assert conn.execute("SELECT COUNT(*) FROM installment_plans WHERE invoice_id = ?",
                            (finv,)).fetchone()[0] == 0, "3b: plan kept"

        # 4. another patient is untouched
        assert conn.execute("SELECT phone FROM patients WHERE patient_id = ?",
                            (stay,)).fetchone()[0] == "3330000000", "4"
        assert (sorted_root / stay / "notes").is_dir() and "Stan" in undo.read_text(), "4"
        assert collection.get(where={"patient_id": stay})["ids"] == ["ZZER800101010103"], "4"

        # 5. the tombstone holds no codice fiscale and no name
        text = stones.read_text()
        assert "ZZER" not in text.upper() and "Ezio" not in text, f"5: {text}"
        assert len(text.splitlines()) == 3, "5: one tombstone per erasure"

        # 6. a copy taken before the erasure is cleaned again by reapply
        conn.close()
        copy = root / "restored"
        copy.mkdir()
        shutil.copy(root / "clinic.sqlite", copy / "before.sqlite")
        conn2 = storage.init_db(str(copy / "before.sqlite"))
        pid_back = patient_id.seed_patient(conn2, "ZZER800101010101", "Ezio Gone", "3331112222")
        (copy / "sorted" / pid_back / "notes").mkdir(parents=True)
        (copy / "sorted" / pid_back / "notes" / "n.txt").write_text("Ezio Gone secret")
        conn2.execute("UPDATE patients SET phone = '3339998888' WHERE patient_id = ?", (held,))
        conn2.commit()
        done = reapply(conn2, copy / "sorted", copy / "drop", copy / "undo.jsonl", stones)
        assert sorted(done) == sorted([pid_back, held]), f"6: reapply should find both, got {done}"
        assert conn2.execute("SELECT phone FROM patients WHERE patient_id = ?",
                             (held,)).fetchone()[0] is None, "6: the held shell is restored"
        assert conn2.execute("SELECT COUNT(*) FROM patients WHERE codice_fiscale ="
                             " 'ZZER800101010101'").fetchone()[0] == 0, "6: revived patient"
        assert not (copy / "sorted" / pid_back).exists(), "6: revived files"
        assert reapply(conn2, copy / "sorted", copy / "drop", copy / "undo.jsonl", stones) == [], \
            "6: a second reapply has nothing left to do"
        conn2.close()
        del os.environ[KEY_ENV]

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    if len(sys.argv) > 3 and sys.argv[1] == "reapply" and sys.argv[2] == "--root":
        # for a restored copy that had to be migrated before erasures applied
        import backup
        problems = []
        print(json.dumps({"reapplied": backup.reapply_erasures(Path(sys.argv[3]),
                                                              problems=problems),
                          "problems": problems}, indent=2))
        return
    print("usage: python erasure.py --selftest | reapply --root <restored dir>")
    sys.exit(1)


if __name__ == "__main__":
    main()
