"""Expired staff-session sweep, run by the retention workflow.

The rule is the one the app already enforces when a token is presented:
a staff session idle for more than SESSION_IDLE_MINUTES is dead. The sweep
removes those rows without waiting for someone to present the token - and
never one that became active again after it was picked.
"""
import json
import sqlite3
import sys
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

import clinic_time
import retention
import web_session as ws

IDLE = timedelta(minutes=ws.SESSION_IDLE_MINUTES)


def session(conn, username, role, last_seen):
    token = ws.create_session(conn, username, role, now=last_seen)
    return token


def count(conn, table="sessions"):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def selftest():
    from storage import init_db
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "s.sqlite")
        conn = init_db(db)
        for name, role in (("drossi", "dentist"), ("aassist", "assistant"), ("anadmin", "admin")):
            conn.execute("INSERT INTO users (username, password_hash, role, active)"
                         " VALUES (?, 'x', ?, 1)", (name, role))
        conn.commit()
        now = clinic_time.read_instant("2026-09-23T12:00:00+00:00")

        # 1. the timeout is the app's own, unchanged
        assert ws.SESSION_IDLE_MINUTES == 30, "1: the idle rule changed"

        # 2. the boundary matches load_session: exactly 30 minutes idle is
        # still alive, a microsecond more is expired
        at_limit = session(conn, "drossi", "dentist", now - IDLE)
        past = session(conn, "aassist", "assistant", now - IDLE - timedelta(microseconds=1))
        picked = ws.select_expired(conn, now)
        assert len(picked) == 1, f"2: boundary wrong, picked {len(picked)}"
        assert ws.load_session(conn, at_limit, now=now) is not None, "2: load_session disagrees"

        # 3. live sessions of every role survive the sweep and still work;
        # a purged one can no longer authenticate
        live = {r: session(conn, u, r, now - timedelta(minutes=5))
                for u, r in (("drossi", "dentist"), ("aassist", "assistant"), ("anadmin", "admin"))}
        dead = session(conn, "ghost_user", "dentist", now - timedelta(days=20))
        out = ws.expire_idle(conn, now=now, actor="retention")
        assert out["deleted"] == 2 and out["skipped_active"] == 0, out
        for role, token in live.items():
            assert ws.load_session(conn, token, now=now) is not None, f"3: live {role} session lost"
        assert ws.load_session(conn, past, now=now) is None and ws.load_session(conn, dead, now=now) is None

        # 4. dry run changes nothing and reports what it would do; repeats are
        # idempotent
        session(conn, "drossi", "dentist", now - timedelta(hours=3))
        before = count(conn)
        dry = ws.expire_idle(conn, now=now, dry_run=True, actor="retention")
        assert dry["selected"] == 1 and dry["deleted"] == 0 and count(conn) == before, dry
        assert ws.expire_idle(conn, now=now, dry_run=True, actor="retention") == dry, "4: dry run drift"
        first = ws.expire_idle(conn, now=now, actor="retention")
        again = ws.expire_idle(conn, now=now, actor="retention")
        assert first["deleted"] == 1 and again["selected"] == 0 and again["deleted"] == 0, (first, again)

        # 5. clock anomalies: a last-seen in the future, or unreadable, is
        # never deleted - it is counted and left for a person to look at
        future = session(conn, "drossi", "dentist", now + timedelta(hours=2))
        conn.execute("INSERT INTO sessions (token_hash, username, role, created_at, last_seen_at)"
                     " VALUES ('h-bad', 'drossi', 'dentist', 'x', 'not a time')")
        conn.commit()
        out = ws.expire_idle(conn, now=now, actor="retention")
        assert out["future"] == 1 and out["unreadable"] == 1 and out["deleted"] == 0, out
        assert ws.load_session(conn, future, now=now + timedelta(hours=2)) is not None
        conn.execute("DELETE FROM sessions WHERE token_hash = 'h-bad'")
        conn.commit()

        # 6. the race: a session picked as expired becomes active before the
        # delete. it must survive - the delete matches the last-seen it read
        racer = session(conn, "aassist", "assistant", now - timedelta(hours=1))
        picked = ws.select_expired(conn, now)
        assert len(picked) == 1
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                     (now.isoformat(), ws._hash_token(racer)))
        conn.commit()
        deleted, skipped = ws.delete_selected(conn, picked)
        assert (deleted, skipped) == (0, 1), f"6: A SESSION THAT CAME BACK WAS DELETED {deleted}"
        assert ws.load_session(conn, racer, now=now) is not None, "6: the racer was logged out"

        # 6b. the same race with real threads: activity during the sweep
        for i in range(30):
            session(conn, "drossi", "dentist", now - timedelta(hours=1, minutes=i))
        tokens_alive = [session(conn, "drossi", "dentist", now - timedelta(hours=2)) for _ in range(5)]
        gate = threading.Barrier(2)
        results = {}

        def sweeper():
            c = sqlite3.connect(db, timeout=15)
            c.row_factory = sqlite3.Row
            gate.wait()
            results["sweep"] = ws.expire_idle(c, now=now, actor="retention")
            c.close()

        def activity():
            c = sqlite3.connect(db, timeout=15)
            c.row_factory = sqlite3.Row
            gate.wait()
            for t in tokens_alive:
                c.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                          (now.isoformat(), ws._hash_token(t)))
                c.commit()
            c.close()

        threads = [threading.Thread(target=sweeper), threading.Thread(target=activity)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        for t in tokens_alive:
            row = conn.execute("SELECT last_seen_at FROM sessions WHERE token_hash = ?",
                               (ws._hash_token(t),)).fetchone()
            # either the sweep deleted it before the activity landed (the
            # session was dead at that moment) or it survived with fresh
            # activity - never deleted AFTER being refreshed
            if row is not None:
                assert row[0] == now.isoformat()
        s = results["sweep"]
        assert s["selected"] == s["deleted"] + s["skipped_active"], s

        # 7. scope: patient portal sessions and every other table are untouched
        conn.execute("INSERT INTO patients (patient_id, codice_fiscale, patient_name)"
                     " VALUES ('pid_sweep', 'ZZSW000000000001', 'Sweep Test')")
        conn.execute("INSERT INTO patient_sessions (token_hash, patient_id, created_at, last_seen_at)"
                     " VALUES ('p-h', 'pid_sweep', '2020-01-01T00:00:00+00:00',"
                     " '2020-01-01T00:00:00+00:00')")
        conn.commit()
        session(conn, "drossi", "dentist", now - timedelta(days=1))
        before_patient = count(conn, "patient_sessions")
        ws.expire_idle(conn, now=now, actor="retention")
        assert count(conn, "patient_sessions") == before_patient, "7: portal sessions swept"

        # 8. audit: aggregate numbers only, never a token or its hash
        rows = conn.execute("SELECT username, role, action, target, reason FROM audit_log"
                            " WHERE action = 'session_sweep'").fetchall()
        assert rows, "8: the sweep is not audited"
        hashes = {r[0] for r in conn.execute("SELECT token_hash FROM sessions")} | {"h-bad", "p-h"}
        for r in rows:
            detail = json.loads(r["reason"])
            assert set(detail) == {"cutoff", "run_at", "selected", "deleted", "skipped_active",
                                   "future", "unreadable", "dry_run", "outcome"}, detail
            blob = json.dumps(tuple(r))
            assert not any(h in blob for h in hashes if h), "8: a token hash reached the audit"
            assert r["username"] == "retention" and r["target"] is None

        # 9. retention runs it: the plan counts, --apply deletes, and nothing
        # else in the policy moves
        session(conn, "drossi", "dentist", now - timedelta(days=2))
        plan = {r["type"]: r for r in retention.plan(conn, retention.policy(), now=now)}
        assert plan["staff_sessions"]["sweep"] == "delete" and plan["staff_sessions"]["past_period"] == 1
        done = retention.apply(conn, retention.policy(), now=now)
        assert done.get("staff_sessions") == 1, done
        assert retention.plan(conn, retention.policy(), now=now)[-1] is not None
        conn.close()
    print("selftest ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    else:
        print("usage: python session_sweep_selftest.py --selftest")
        sys.exit(1)
