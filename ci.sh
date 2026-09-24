#!/bin/sh
# local, reproducible ci (P17.02). run from the repo root:
#     ./ci.sh            pins, pip check, fast suite, migrations, secret scan, dependency audit
#     ./ci.sh --full     also the browser/model gate: needs ollama and the three apps (see README)
#     ./ci.sh --offline  skip the dependency audit (it asks pypi/osv about package names, never data)
#     ./ci.sh --only <step>   one step: pins, pip-check, fast-suite, migrations, secrets, audit
# nothing here runs on anyone else's machine: hosted ci is not enabled (owner decision D11).
PY=.venv/bin/python
FAILED=""
ONLY=$(echo " $* " | sed -n 's/.* --only \([a-z-]*\) .*/\1/p')
step() {
  name=$1; shift
  [ -z "$ONLY" ] || [ "$ONLY" = "$name" ] || return 0
  printf '\n== %s\n' "$name"
  if "$@"; then echo "-- $name ok"; else echo "-- $name FAILED"; FAILED="$FAILED $name"; fi
}

pins() {
  # every runtime and dev requirement pinned to one version
  bad=$(grep -hvE '^\s*(#|$)' requirements.txt requirements-dev.txt | grep -v '==')
  [ -z "$bad" ] || { echo "not pinned: $bad"; return 1; }
}

migrations() {
  # a new empty database, twice (every migration must be a no-op the second time)
  tmp=$(mktemp -d) && $PY - "$tmp" <<'PYEOF'
import sys, storage
path = sys.argv[1] + "/ci.sqlite"
storage.init_db(path).close()
conn = storage.init_db(path)
assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
print(len(conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()), "tables")
PYEOF
  code=$?; rm -rf "$tmp"; return $code
}

secrets() {
  # fails on any finding not in the reviewed baseline
  git ls-files -z | xargs -0 .venv/bin/detect-secrets-hook --baseline .secrets.baseline
}

audit() {
  out=$(mktemp)
  .venv/bin/pip-audit -r requirements.txt --format json > "$out" 2>/dev/null
  $PY - "$out" <<'PYEOF'
import json, sys
accepted = {l.split()[0] for l in open("ci_accepted_vulns.txt") if l.strip() and not l.startswith("#")}
data = json.load(open(sys.argv[1]))
found = [(d["name"], d["version"], v["id"]) for d in data["dependencies"] for v in d.get("vulns", [])]
new = [f for f in found if f[2] not in accepted]
print(f"{len(found)} advisories, {len(found) - len(new)} accepted, {len(new)} new")
for f in new:
    print("NEW:", *f)
sys.exit(1 if new else 0)
PYEOF
  code=$?; rm -f "$out"; return $code
}

step pins pins
step pip-check .venv/bin/pip check
step fast-suite ./run_selftests.sh
step migrations migrations
step secrets secrets
case " $* " in *" --offline "*) echo "\n== audit SKIPPED (--offline)" ;; *) step audit audit ;; esac

case " $* " in *" --full "*)
  step intake-walk $PY e2e_intake_walk.py
  step chat-walk $PY e2e_chat_walk.py
  step eval-chat $PY eval_chat.py
  step flow $PY e2e_flow_appt.py
  step calendar-keys $PY e2e_cal_keys.py
  step shots $PY shot_pages.py --width 390 --width 1440
  step a11y-1440 $PY a11y_audit.py
  step a11y-390 $PY a11y_audit.py --width 390
  step portal $PY e2e_portal_preview.py 1440 390
  step load $PY load_check.py
  ;;
esac

if [ -n "$FAILED" ]; then echo "\nci FAILED:$FAILED"; exit 1; fi
echo "\nci ok"
