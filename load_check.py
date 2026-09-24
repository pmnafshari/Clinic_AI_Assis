"""P17.T2 on a disposable staff instance: concurrent load, then a restart.

    .venv/bin/python load_check.py [--users 20] [--rounds 10] [--out FILE]

the instance is populated_pages' (temporary database, documents and index on
port 5003); the dev database is never opened. the volume is a labelled demo
volume - the clinic's real volume is unknown (TBC), so this is not a sizing.

passes when: no request fails, p95 stays under health.SLOW_MS, and after a
restart on the same database the same signed-in session still works and sees
the same patients. exit 1 otherwise.
"""

import http.cookiejar
import json
import re
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import health
import populated_pages as pp


def session():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                         urllib.request.ProxyHandler({}))
    page = opener.open(f"{pp.POPULATED_URL}/login", timeout=10).read().decode()
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    body = urllib.parse.urlencode({"csrf_token": token, "username": pp.USER, "password": pp.PASS}).encode()
    landed = opener.open(f"{pp.POPULATED_URL}/login", data=body, timeout=10)
    if "/login" in landed.url:
        raise RuntimeError("load: login failed")
    return opener


def hit(opener, path):
    """-> (ok, ms)"""
    started = time.monotonic()
    try:
        with opener.open(f"{pp.POPULATED_URL}{path}", timeout=30) as r:
            r.read()
            ok = r.status == 200 and "/login" not in r.url
    except (urllib.error.URLError, OSError):
        ok = False
    return ok, (time.monotonic() - started) * 1000


def run_load(opener, paths, users, rounds):
    results, lock = [], threading.Lock()

    def worker():
        for _ in range(rounds):
            for path in paths:
                r = hit(opener, path)
                with lock:
                    results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(users)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.monotonic() - started
    return summarise(results, wall)


def summarise(results, wall):
    ms = sorted(m for _ok, m in results)
    errors = sum(1 for ok, _m in results if not ok)
    return {"requests": len(results), "errors": errors,
            "p50_ms": round(statistics.median(ms)) if ms else None,
            "p95_ms": round(ms[int(len(ms) * 0.95) - 1]) if ms else None,
            "max_ms": round(ms[-1]) if ms else None,
            "per_second": round(len(results) / wall, 1) if wall else None}


def patients_seen(opener):
    ok, _ms = hit(opener, "/patients")
    page = opener.open(f"{pp.POPULATED_URL}/patients", timeout=10).read().decode() if ok else ""
    return ok, len(re.findall(r'href="/patients/[A-Z0-9]{16}"', page))


def restart(tmp, server):
    """stop the app and start a fresh one on the same temporary stores - no reseed."""
    import app.db as app_db
    from werkzeug.serving import make_server
    from app import create_app
    server.shutdown()
    server.server_close()
    assert app_db.DB_PATH.startswith(str(tmp)), "load: refusing to restart on a non-temporary database"
    fresh = make_server("127.0.0.1", pp.POPULATED_PORT, create_app(), threaded=True)
    threading.Thread(target=fresh.serve_forever, daemon=True).start()
    return fresh


def check(users, rounds):
    report = {"volume": f"{users} concurrent streams x {rounds} rounds (demo volume; real volume TBC)",
              "problems": []}
    with tempfile.TemporaryDirectory(prefix="load-") as tmp:
        server, pages = pp.start(tmp)
        try:
            opener = session()
            paths = ["/"] + [p for _n, p, _m in pages]
            report["pages"] = len(paths)
            report["load"] = run_load(opener, paths, users, rounds)
            if report["load"]["errors"]:
                report["problems"].append(f"{report['load']['errors']} failed requests under load")
            if report["load"]["p95_ms"] > health.SLOW_MS:
                report["problems"].append(f"p95 {report['load']['p95_ms']} ms over {health.SLOW_MS} ms")
            ok, before = patients_seen(opener)
            server = restart(tmp, server)
            ok_after, after = patients_seen(opener)
            report["restart"] = {"session_kept": ok_after, "patients_before": before, "patients_after": after}
            if not (ok and ok_after and before == after and before > 0):
                report["problems"].append("the restart lost the session or the data")
        finally:
            server.shutdown()
            server.server_close()
    return report


def selftest():
    # the counting, without a server: a failed request must count as an error
    s = summarise([(True, 100), (False, 3000), (True, 200), (True, 300)], 2)
    assert (s["requests"], s["errors"], s["max_ms"], s["per_second"]) == (4, 1, 3000, 2.0), s
    assert summarise([], 1)["p95_ms"] is None
    print("selftest ok")


def main(argv):
    if "--selftest" in argv:
        selftest()
        return 0
    users = int(argv[argv.index("--users") + 1]) if "--users" in argv else 20
    rounds = int(argv[argv.index("--rounds") + 1]) if "--rounds" in argv else 10
    report = check(users, rounds)
    text = json.dumps(report, indent=1)
    if "--out" in argv:
        with open(argv[argv.index("--out") + 1], "w") as f:
            f.write(text + "\n")
    print(text)
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
