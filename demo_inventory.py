"""DEMO stock fixtures - synthetic consumables for a small Italian dental studio.

NOT A REAL CLINIC'S STOCK. Made through inventory.py's own functions, so every
row is audited like real use, with the actor demo_fixture. A few items sit at or
under their threshold so the low-stock alert has something to show.
Idempotent: an item that already exists is left alone.

    python demo_inventory.py --apply
"""
import json
import sys

import inventory
import storage

DB_PATH = "db/clinic.sqlite"
ACTOR = ("demo_fixture", "dentist")
NOTE = "DEMO fixture"

# name, unit, threshold, received, used
ITEMS = (
    ("Guanti nitrile M (conf. 100)", "conf", 4, 12, 7),
    ("Mascherine chirurgiche (conf. 50)", "conf", 3, 8, 3),
    ("Carpule articaina 4% 1:100.000", "pz", 50, 100, 62),
    ("Composito A2 (siringa)", "pz", 3, 6, 4),
    ("Rulli di cotone (conf. 300)", "conf", 2, 5, 1),
    ("Buste sterilizzazione 90x230", "conf", 2, 4, 1),
    ("Aghi 30G corti (conf. 100)", "conf", 1, 3, 1),
    ("Clorexidina colluttorio 0,2%", "ml", 1000, 5000, 1500),
)


def apply(conn):
    done = []
    existing = {r[0] for r in conn.execute("SELECT name FROM inventory_items")}
    for name, unit, threshold, received, used in ITEMS:
        if name in existing:
            continue
        item_id = inventory.create_item(conn, name, unit, threshold, *ACTOR)
        inventory.move(conn, item_id, "receipt", received, *ACTOR, reason=NOTE,
                       key=f"demo-stock-in-{item_id}")
        inventory.move(conn, item_id, "consumption", used, *ACTOR, reason=NOTE,
                       key=f"demo-stock-out-{item_id}")
        done.append(name)
    return done


def selftest():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.init_db(str(Path(tmp) / "clinic.sqlite"))
        assert len(apply(conn)) == len(ITEMS) and apply(conn) == [], "1: once, then nothing"
        low = sorted(i["name"] for i in inventory.items(conn, low_only=True))
        expected = sorted(n for n, _, t, r, u in ITEMS if r - u <= t)
        assert low == expected and len(inventory.open_alerts(conn)) == len(expected), (low, expected)
        assert all(r[0] == NOTE for r in conn.execute("SELECT reason FROM inventory_movements"))
    print("selftest ok")


def main(argv):
    if "--selftest" in argv:
        selftest()
        return
    if "--apply" not in argv:
        print("dry run - pass --apply to create the demo stock")
        return
    conn = storage.init_db(DB_PATH)
    try:
        print(json.dumps(apply(conn), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
