"""P21: the support loop and the period run. written before support.py and period_run.py.

temporary database and folders; health, backup, drill and evals are stubbed where the real ones would touch
the dev install. nothing is sent to anyone.
"""

import json
import sys
import tempfile
from pathlib import Path

import period_run as pr
import support
from storage import init_db


def refused(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except support.SupportError as e:
        return str(e)
    raise AssertionError(f"{fn.__name__} did not refuse")


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = init_db(str(tmp / "ops.sqlite"))

        # 1. a ticket: open -> triaged -> closed with a resolution; every step kept
        tid = support.open_ticket(conn, "backup older than 26 h", "medium", "dentist", "dentist")
        assert "not allowed" in refused(support.open_ticket, conn, "x", "low", "assistant", "assistant")
        assert "not allowed" in refused(support.open_ticket, conn, "x", "low", "admin", "admin"), "1: admin stays manage_users only"
        assert "resolution" in refused(support.close, conn, tid, "", "dentist", "dentist"), "1: closed without a resolution"
        support.triage(conn, tid, "dentist", "2026-09-25", "dentist", "dentist")
        support.close(conn, tid, "ran backup.py create; verified", "dentist", "dentist")
        assert "already closed" in refused(support.close, conn, tid, "again", "dentist", "dentist")
        assert [e["event"] for e in support.events(conn, tid)] == ["opened", "triaged", "closed"]
        for sql in ("UPDATE support_ticket_events SET detail = 'x'", "DELETE FROM support_ticket_events"):
            try:
                conn.execute(sql)
                raise AssertionError(f"1: {sql.split()[0]} on the ticket history was allowed")
            except Exception as e:
                assert "append-only" in str(e), e

        # 2. tickets carry no patient data
        for text in ("patient RSSMRA80A01H501U called", "mail mario.rossi@example.it", "call 3331234567"):
            assert "identifying" in refused(support.open_ticket, conn, text, "low", "dentist", "dentist"), text
        assert "severity" in refused(support.open_ticket, conn, "disk", "urgent!!", "dentist", "dentist")

        # 3. the artificial failure: an alert line becomes exactly one ticket, however often it is read
        log = tmp / "alerts.log"
        log.write_text("2026-09-24T21:00:00+00:00 | 1 alert(s) | app.patient\n")
        made = support.from_alerts(conn, log)
        assert len(made) == 1 and support.from_alerts(conn, log) == [], "3: an alert made two tickets"
        t = support.ticket(conn, made[0])
        assert t["source"] == "alert" and "app.patient" in t["summary"] and t["status"] == "open", t
        with open(log, "a") as f:
            f.write("2026-09-24T21:05:00+00:00 | xray demo incident | open: false_negative 1\n")
        assert len(support.from_alerts(conn, log)) == 1, "3: a new alert line made no ticket"
        # only lines from a given moment on: old history is not turned into a flood of tickets
        with open(log, "a") as f:
            f.write("2026-09-20T08:00:00+00:00 | 1 alert(s) | disk.free_gib\n")
            f.write("2026-09-24T22:00:00+00:00 | 1 alert(s) | backup.newest_age_h\n")
        fresh = support.from_alerts(conn, log, since="2026-09-24T21:30:00+00:00")
        assert [support.ticket(conn, t)["summary"] for t in fresh] == ["alert: 1 alert(s) | backup.newest_age_h"], fresh

        # 4. the period run: a health sample, the steps it was told to run, and a record on disk
        def health_ok(_conn):
            return [("app.staff", {"up": True, "ms": 40}, None), ("app.patient", {"up": True, "ms": 40}, None)]

        def health_down(_conn):
            return [("app.staff", {"up": True, "ms": 40}, None),
                    ("app.patient", {"up": False, "ms": None}, "patient not reachable")]

        stubs = {"backup": lambda: {"archive": "clinic-x.cbk", "problems": []},
                 "drill": lambda: {"problems": [], "rto_seconds": 1.5, "rpo_h_at_start": 0.2},
                 "evals": lambda: {"agent_rate": 1.0, "agent_cases": 31}}
        ops = tmp / "ops"
        first = pr.run(conn, ops, health=health_ok, steps=stubs, stamp="20260917T090000Z")
        assert first["health"] == {"all_up": True, "alerts": 0} and first["drill"]["rto_seconds"] == 1.5, first
        assert (ops / "period-20260917T090000Z.json").exists()
        assert first["drops"] == [] and first["tickets_opened"] == [], "4: the first run has nothing to compare"

        # 5. a metric drop against the previous period opens a ticket with the action to take
        worse = {**stubs, "evals": lambda: {"agent_rate": 0.9, "agent_cases": 31}}
        second = pr.run(conn, ops, health=health_down, steps=worse, stamp="20260924T090000Z")
        assert second["drops"] == [{"metric": "agent_rate", "before": 1.0, "after": 0.9}], second["drops"]
        drop_ticket = support.ticket(conn, second["tickets_opened"][0])
        assert drop_ticket["source"] == "eval" and "agent_rate" in drop_ticket["summary"], drop_ticket
        assert "re-run" in drop_ticket["summary"], "5: the drop ticket does not say what to do"
        # a failed drill is a ticket too
        broken = {**stubs, "drill": lambda: {"problems": ["the erased patient is back"], "rto_seconds": 2}}
        third = pr.run(conn, ops, health=health_ok, steps=broken, stamp="20260925T090000Z")
        assert any(support.ticket(conn, t)["summary"].startswith("restore drill failed") for t in third["tickets_opened"])

        # 6. the period report: availability from samples, tickets, restore age, cost, progress from the matrix
        matrix = tmp / "steps.md"
        matrix.write_text("## P20 Pilot\n- [x] P20.T1: a\n- [ ] P20.01: b\n## P21 Support\n- [ ] P21.01: c\n"
                          "- [ ] P21.02: d\n- [x] P21.T5: e\n- [x] P21.T1: f\n")
        rep = pr.report(conn, ops, matrix=matrix)
        assert rep["availability"] == {"samples": 3, "all_up": 2, "rate": 0.6667}, rep["availability"]
        assert rep["error_budget"] == "no SLO approved", rep["error_budget"]
        assert rep["cost_cents"] == 0 and rep["restore_age_h"] is not None, rep
        assert rep["progress"] == {"P20": {"done": 1, "of": 2, "pct": 50.0}, "P21": {"done": 2, "of": 4, "pct": 50.0}}, rep["progress"]
        assert rep["tickets"]["open"] >= 3 and rep["tickets"]["closed"] == 1, rep["tickets"]
        assert rep["programme"] == "ACTIVE", "6: support must never be reported as finished"
        text = json.dumps(rep)
        assert "RSSMRA" not in text and "@" not in text, "6: identifying data in the report"
        conn.close()
    print("selftest ok")


def main():
    if "--selftest" not in sys.argv:
        print("usage: python support_selftest.py --selftest")
        return
    selftest()


if __name__ == "__main__":
    main()
