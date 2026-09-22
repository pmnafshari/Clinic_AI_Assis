"""Clinic stock: consumables, every movement, and a low-stock alert (P08).

The balance of an item is never stored - it is the sum of its movements, and
movements are append-only. A wrong number is fixed by a new movement with a
reason, so the history always explains the balance.

Policy (P08.02): a balance never goes below zero. A consumption larger than
what is in stock is refused; a physical count is recorded as the difference.

Alerts are in the app only - nothing is sent. At most one is open per item
(a partial unique index, so two workers cannot both open one). An alert opens
when the balance is at or below the item's threshold, closes by itself when
the balance goes back above it, and can be acknowledged by a person in
between; acknowledged is not resolved.

Lot numbers, expiry dates and automatic consumption from treatments are not
here: D09 is unanswered and its default keeps them out (P08.05).
"""
import sqlite3
import sys

import clinic_time
from auth import authorize, log_audit

UNITS = ("pz", "conf", "ml", "g", "paia")
KINDS = ("receipt", "consumption", "correction", "count")

SCHEMA = """
    CREATE TABLE IF NOT EXISTS inventory_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        unit TEXT NOT NULL CHECK (unit IN ('pz', 'conf', 'ml', 'g', 'paia')),
        threshold INTEGER NOT NULL CHECK (threshold >= 0),
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS inventory_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL REFERENCES inventory_items(id),
        kind TEXT NOT NULL CHECK (kind IN ('receipt', 'consumption', 'correction', 'count')),
        delta INTEGER NOT NULL,
        counted INTEGER,
        reason TEXT,
        idempotency_key TEXT UNIQUE,
        actor TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        ts TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_movements_item ON inventory_movements (item_id);
    CREATE TRIGGER IF NOT EXISTS movements_no_update BEFORE UPDATE ON inventory_movements
    BEGIN SELECT RAISE(ABORT, 'stock movements are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS movements_no_delete BEFORE DELETE ON inventory_movements
    BEGIN SELECT RAISE(ABORT, 'stock movements are append-only'); END;
    CREATE TABLE IF NOT EXISTS inventory_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL REFERENCES inventory_items(id),
        opened_at TEXT NOT NULL,
        balance_at_open INTEGER NOT NULL,
        threshold_at_open INTEGER NOT NULL,
        acknowledged_at TEXT,
        acknowledged_by TEXT,
        resolved_at TEXT,
        resolved_by TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_alert ON inventory_alerts (item_id)
        WHERE resolved_at IS NULL;
    CREATE TABLE IF NOT EXISTS inventory_job_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        checked INTEGER,
        opened INTEGER,
        resolved INTEGER,
        status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed'))
    );
"""


class StockError(ValueError):
    pass


def _gate(conn, actor, role, action, target, capability):
    if not authorize(role, capability):
        log_audit(conn, actor, role, action, target, allowed=0)
        raise PermissionError(f"{role} may not {action.replace('_', ' ')}")


def _quantity(value):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise StockError(f"a quantity is a whole number, not {value!r}")
    return number


# --- reading --------------------------------------------------------------


def balance(conn, item_id):
    return conn.execute("SELECT COALESCE(SUM(delta), 0) FROM inventory_movements"
                        " WHERE item_id = ?", (item_id,)).fetchone()[0]


def items(conn, low_only=False):
    """Every item with its balance and open alert - one grouped query."""
    rows = conn.execute(
        "SELECT i.*, COALESCE(SUM(m.delta), 0) AS balance,"
        " (SELECT id FROM inventory_alerts a WHERE a.item_id = i.id AND a.resolved_at IS NULL)"
        " AS alert_id,"
        " (SELECT acknowledged_at FROM inventory_alerts a WHERE a.item_id = i.id"
        "  AND a.resolved_at IS NULL) AS acknowledged_at"
        " FROM inventory_items i LEFT JOIN inventory_movements m ON m.item_id = i.id"
        " GROUP BY i.id ORDER BY i.name").fetchall()
    out = [dict(r, low=r["balance"] <= r["threshold"]) for r in rows]
    return [r for r in out if r["low"]] if low_only else out


def item(conn, item_id):
    row = conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    current = balance(conn, item_id)
    return dict(row, balance=current, low=current <= row["threshold"])


def history(conn, item_id, limit=100):
    return conn.execute("SELECT * FROM inventory_movements WHERE item_id = ?"
                        " ORDER BY id DESC LIMIT ?", (item_id, limit)).fetchall()


def open_alerts(conn):
    return conn.execute(
        "SELECT a.*, i.name, i.unit FROM inventory_alerts a JOIN inventory_items i"
        " ON i.id = a.item_id WHERE a.resolved_at IS NULL ORDER BY a.opened_at").fetchall()


def last_run(conn):
    return conn.execute("SELECT * FROM inventory_job_runs ORDER BY id DESC LIMIT 1").fetchone()


# --- items ----------------------------------------------------------------


def create_item(conn, name, unit, threshold, actor, role):
    _gate(conn, actor, role, "create_stock_item", name, "manage_inventory")
    name = (name or "").strip()[:120]
    if not name:
        raise StockError("an item needs a name")
    if unit not in UNITS:
        raise StockError(f"unit must be one of {', '.join(UNITS)}")
    threshold = _quantity(threshold)
    if threshold < 0:
        raise StockError("a threshold is zero or more")
    try:
        item_id = conn.execute(
            "INSERT INTO inventory_items (name, unit, threshold, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?)", (name, unit, threshold, actor, clinic_time.stamp())).lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        raise StockError("an item with that name already exists")
    conn.commit()
    log_audit(conn, actor, role, "create_stock_item", f"item:{item_id}", allowed=1)
    check(conn, item_id)
    return item_id


def set_threshold(conn, item_id, threshold, actor, role):
    _gate(conn, actor, role, "set_stock_threshold", f"item:{item_id}", "manage_inventory")
    threshold = _quantity(threshold)
    if threshold < 0:
        raise StockError("a threshold is zero or more")
    if conn.execute("UPDATE inventory_items SET threshold = ? WHERE id = ?",
                    (threshold, item_id)).rowcount == 0:
        raise StockError("no such item")
    conn.commit()
    log_audit(conn, actor, role, "set_stock_threshold", f"item:{item_id}", allowed=1,
              reason=str(threshold))
    check(conn, item_id)


# --- movements ------------------------------------------------------------


def move(conn, item_id, kind, quantity, actor, role, reason=None, key=None, now=None):
    """Record one stock movement. Returns (movement_id, created); the same key
    twice returns the first movement and records nothing.

    receipt and consumption take a positive quantity. correction takes a
    signed one and a reason. count takes what was physically counted and a
    reason, and records the difference."""
    if kind not in KINDS:
        raise StockError(f"kind must be one of {KINDS}")
    capability = "manage_inventory" if kind == "correction" else "use_inventory"
    _gate(conn, actor, role, f"stock_{kind}", f"item:{item_id}", capability)
    quantity = _quantity(quantity)
    reason = (reason or "").strip()[:300] or None
    if kind in ("receipt", "consumption") and quantity <= 0:
        raise StockError("a quantity is more than zero")
    if kind in ("correction", "count") and not reason:
        raise StockError(f"a {kind} needs a reason")
    if kind == "correction" and quantity == 0:
        raise StockError("a correction changes the balance")
    if kind == "count" and quantity < 0:
        raise StockError("a count is zero or more")

    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if key:
            found = conn.execute("SELECT id FROM inventory_movements WHERE idempotency_key = ?",
                                 (key,)).fetchone()
            if found:
                conn.execute("ROLLBACK")
                return found[0], False
        if conn.execute("SELECT 1 FROM inventory_items WHERE id = ?", (item_id,)).fetchone() is None:
            raise StockError("no such item")
        current = balance(conn, item_id)
        if kind == "receipt":
            delta = quantity
        elif kind == "consumption":
            delta = -quantity
        elif kind == "correction":
            delta = quantity
        else:
            delta = quantity - current
        if current + delta < 0:
            raise StockError(f"only {current} in stock - the balance cannot go below zero")
        movement_id = conn.execute(
            "INSERT INTO inventory_movements (item_id, kind, delta, counted, reason,"
            " idempotency_key, actor, actor_role, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, kind, delta, quantity if kind == "count" else None, reason, key, actor,
             role, clinic_time.to_storage(now) if now else clinic_time.stamp())).lastrowid
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    log_audit(conn, actor, role, f"stock_{kind}", f"item:{item_id}", allowed=1, reason=str(delta))
    check(conn, item_id, now)
    return movement_id, True


# --- alerts ---------------------------------------------------------------


def check(conn, item_id, now=None):
    """Open or resolve this item's alert from its balance now.
    Returns 'opened', 'resolved' or None. Safe to run any number of times,
    from any number of workers: the unique index keeps one open alert."""
    stamp = clinic_time.to_storage(now) if now else clinic_time.stamp()
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT threshold FROM inventory_items WHERE id = ?",
                           (item_id,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        current = balance(conn, item_id)
        open_id = conn.execute("SELECT id FROM inventory_alerts WHERE item_id = ?"
                               " AND resolved_at IS NULL", (item_id,)).fetchone()
        outcome = None
        if current <= row[0] and open_id is None:
            cur = conn.execute(
                "INSERT OR IGNORE INTO inventory_alerts (item_id, opened_at, balance_at_open,"
                " threshold_at_open) VALUES (?, ?, ?, ?)", (item_id, stamp, current, row[0]))
            outcome = "opened" if cur.rowcount else None
        elif current > row[0] and open_id is not None:
            conn.execute("UPDATE inventory_alerts SET resolved_at = ?, resolved_by = 'system'"
                         " WHERE id = ?", (stamp, open_id[0]))
            outcome = "resolved"
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return outcome


def acknowledge(conn, alert_id, actor, role):
    _gate(conn, actor, role, "acknowledge_stock_alert", f"alert:{alert_id}", "use_inventory")
    changed = conn.execute("UPDATE inventory_alerts SET acknowledged_at = ?, acknowledged_by = ?"
                           " WHERE id = ? AND resolved_at IS NULL AND acknowledged_at IS NULL",
                           (clinic_time.stamp(), actor, alert_id)).rowcount
    conn.commit()
    if changed:
        log_audit(conn, actor, role, "acknowledge_stock_alert", f"alert:{alert_id}", allowed=1)
    return bool(changed)


# --- the daily job --------------------------------------------------------


def run_job(conn, now=None):
    """Check every item once. Recorded in inventory_job_runs, so the screen can
    say when it last ran - and a run that failed says so."""
    started = clinic_time.to_storage(now) if now else clinic_time.stamp()
    run_id = conn.execute("INSERT INTO inventory_job_runs (started_at, status) VALUES (?, 'running')",
                          (started,)).lastrowid
    conn.commit()
    opened = resolved = 0
    ids = [r[0] for r in conn.execute("SELECT id FROM inventory_items ORDER BY id")]
    try:
        for item_id in ids:
            outcome = check(conn, item_id, now)
            opened += outcome == "opened"
            resolved += outcome == "resolved"
    except Exception:
        conn.execute("UPDATE inventory_job_runs SET status = 'failed', finished_at = ? WHERE id = ?",
                     (clinic_time.stamp(), run_id))
        conn.commit()
        raise
    conn.execute("UPDATE inventory_job_runs SET status = 'ok', finished_at = ?, checked = ?,"
                 " opened = ?, resolved = ? WHERE id = ?",
                 (clinic_time.stamp(), len(ids), opened, resolved, run_id))
    conn.commit()
    return {"run": run_id, "checked": len(ids), "opened": opened, "resolved": resolved}


def selftest():
    import tempfile
    import threading
    from datetime import datetime, timezone
    from pathlib import Path

    from storage import init_db

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "clinic.sqlite")
        conn = init_db(db_path)
        D, A = ("drossi", "dentist"), ("aassist", "assistant")
        t0 = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)

        # 1. items: unit from the list, threshold zero or more, names unique
        gloves = create_item(conn, "Guanti nitrile M", "conf", 5, *D)
        for bad in (("Anestetico", "bottle", 5), ("Anestetico", "pz", -1), ("", "pz", 1),
                    ("Guanti nitrile M", "conf", 1)):
            try:
                create_item(conn, *bad, *D)
                raise AssertionError(f"1: {bad} should be refused")
            except StockError:
                pass
        try:
            create_item(conn, "Frese", "pz", 3, *A)
            raise AssertionError("1: reception does not create items")
        except PermissionError:
            pass

        # 2. P08.T1 - receipt, consumption, the threshold boundary, the zero floor
        assert check(conn, gloves) is None and open_alerts(conn)[0]["item_id"] == gloves, \
            "2: a new item with nothing in stock is already low"
        move(conn, gloves, "receipt", 12, *A, now=t0)
        assert balance(conn, gloves) == 12 and not open_alerts(conn), "2: restocked resolves"
        move(conn, gloves, "consumption", 6, *A)
        assert balance(conn, gloves) == 6 and not open_alerts(conn), "2: 6 > 5 is not low"
        move(conn, gloves, "consumption", 1, *A)
        assert balance(conn, gloves) == 5 and len(open_alerts(conn)) == 1, \
            "2: at the threshold exactly, the item is low"
        for bad in (("consumption", 6), ("consumption", 0), ("receipt", -3), ("receipt", "2.5"),
                    ("consumption", "tre")):
            try:
                move(conn, gloves, *bad, *A)
                raise AssertionError(f"2: {bad} should be refused")
            except StockError:
                pass
        assert balance(conn, gloves) == 5, "2: a refused movement changes nothing"

        # 3. corrections and counts: new rows with a reason; a correction is the dentist's
        try:
            move(conn, gloves, "correction", -1, *A, reason="broken box")
            raise AssertionError("3: reception does not correct")
        except PermissionError:
            pass
        try:
            move(conn, gloves, "count", 3, *A)
            raise AssertionError("3: a count needs a reason")
        except StockError:
            pass
        move(conn, gloves, "count", 3, *A, reason="monthly count")
        move(conn, gloves, "correction", 2, *D, reason="box found in the store room")
        rows = history(conn, gloves)
        assert [(r["kind"], r["delta"]) for r in rows[:2]] == [("correction", 2), ("count", -2)], \
            [tuple(r) for r in rows[:2]]
        assert balance(conn, gloves) == 5, "3: 5 - 2 + 2"
        for sql in ("UPDATE inventory_movements SET delta = 99", "DELETE FROM inventory_movements"):
            try:
                conn.execute(sql)
                raise AssertionError(f"3: {sql} should be refused")
            except sqlite3.IntegrityError:
                conn.rollback()

        # 4. the same key twice is one movement
        m, made = move(conn, gloves, "receipt", 10, *A, key="k-1")
        assert made and move(conn, gloves, "receipt", 10, *A, key="k-1") == (m, False)
        assert balance(conn, gloves) == 15, "4: a double submit counts once"

        # 5. acknowledged is not resolved; a restock resolves it
        move(conn, gloves, "consumption", 10, *A)
        alert = open_alerts(conn)[0]
        assert acknowledge(conn, alert["id"], *A) and not acknowledge(conn, alert["id"], *A)
        assert open_alerts(conn)[0]["acknowledged_by"] == "aassist", "5: still open"
        move(conn, gloves, "receipt", 1, *A)
        assert not open_alerts(conn), "5: 6 > 5 resolves it"
        closed = conn.execute("SELECT resolved_by FROM inventory_alerts WHERE id = ?",
                              (alert["id"],)).fetchone()[0]
        assert closed == "system", "5: resolved by the check, not by a person"

        # 6. P08.T2 - the job re-run, and two workers at once, open one alert
        cotton = create_item(conn, "Rulli di cotone", "conf", 2, *D)
        move(conn, cotton, "receipt", 2, *A)
        conn.execute("UPDATE inventory_alerts SET resolved_at = 'x', resolved_by = 'test'"
                     " WHERE item_id = ? AND resolved_at IS NULL", (cotton,))
        conn.commit()
        results = []

        def worker():
            c = init_db(db_path)
            try:
                results.append(run_job(c, now=t0))
            finally:
                c.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sum(r["opened"] for r in results) == 1, f"6: two workers opened {results}"
        assert run_job(conn, now=t0)["opened"] == 0, "6: a re-run opens nothing new"
        assert conn.execute("SELECT COUNT(*) FROM inventory_alerts WHERE item_id = ? AND"
                            " resolved_at IS NULL", (cotton,)).fetchone()[0] == 1
        assert last_run(conn)["status"] == "ok" and last_run(conn)["checked"] == 2
        # the threads above may not overlap on a given run; the guarantee they
        # rest on is the database's, so it is asserted directly: a second open
        # alert for the same item cannot be written by anyone
        try:
            conn.execute("INSERT INTO inventory_alerts (item_id, opened_at, balance_at_open,"
                         " threshold_at_open) VALUES (?, 'y', 0, 2)", (cotton,))
            raise AssertionError("6: a second open alert for one item must be refused")
        except sqlite3.IntegrityError:
            conn.rollback()

        # 7. P08.T3 - four people consume the last 2 at once: 2 succeed, the floor holds
        errors = []

        def take():
            c = init_db(db_path)
            try:
                move(c, cotton, "consumption", 1, *A)
            except StockError as e:
                errors.append(e)
            finally:
                c.close()

        threads = [threading.Thread(target=take) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert balance(conn, cotton) == 0 and len(errors) == 2, (balance(conn, cotton), errors)

        # 8. a failure half way leaves no movement and no alert change
        before = conn.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()[0]
        conn.execute("CREATE TRIGGER boom BEFORE INSERT ON inventory_movements"
                     " BEGIN SELECT RAISE(ABORT, 'boom'); END")
        try:
            move(conn, cotton, "receipt", 5, *A)
            raise AssertionError("8: the injected failure should surface")
        except sqlite3.IntegrityError:
            pass
        conn.execute("DROP TRIGGER boom")
        assert conn.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()[0] == before
        assert balance(conn, cotton) == 0 and open_alerts(conn), "8: consistent after failure"

        # 9. no patient data anywhere near this, and admin holds nothing here
        for role in ("admin",):
            try:
                move(conn, cotton, "receipt", 1, "admin", role)
                raise AssertionError("9: admin must not move stock")
            except PermissionError:
                pass
        audit = conn.execute("SELECT target FROM audit_log WHERE action LIKE 'stock_%'").fetchall()
        assert audit and all(r[0].startswith("item:") for r in audit), "9: audited by item"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python inventory.py --selftest")
    sys.exit(1)


if __name__ == "__main__":
    main()
