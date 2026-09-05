#!/bin/sh
# Start the whole demo: ollama plus the three apps, on 5000, 5001 and 5002.
#
# The startup sequence used to live only in notes that are not in this repo, so
# cloning it got you a README and a checklist. This is the checklist.
#
#   ./demo.sh              staff + patient + public site
#   ./demo.sh --voice      also arm the fenced demo voice (needs .env.voice)
#   ./demo.sh --no-ollama  public site only - it reads no model
#
# Ctrl+C stops everything this script started, and nothing it did not.
set -e

VOICE=0
NO_OLLAMA=0
for arg in "$@"; do
    case "$arg" in
        --voice) VOICE=1 ;;
        --no-ollama) NO_OLLAMA=1 ;;
        -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg (try --help)"; exit 2 ;;
    esac
done

PY=.venv/bin/python
LOGS=$(mktemp -d "${TMPDIR:-/tmp}/clinic-demo.XXXXXX")
STARTED=""          # pids we own, so Ctrl+C does not kill a borrowed ollama
OLLAMA_WAS_UP=0

say()  { printf '%s\n' "$*"; }
fail() { printf 'demo: %s\n' "$*" >&2; exit 1; }

# --- shutdown -------------------------------------------------------------
# armed BEFORE anything starts. a preflight that fails halfway through startup
# would otherwise leave a half-demo running on some of the ports, which is the
# exact mess this script exists to avoid. only what we started: an ollama that
# was already serving belongs to whoever started it.
cleanup() {
    # disarm first: TERM and EXIT both fire on a signalled shutdown and this
    # ran twice without it. no `exit` in here either - this runs on the EXIT
    # trap, and exiting from it would overwrite the real status. measured: a
    # failed preflight reported 0.
    trap - INT TERM EXIT
    if [ -n "$STARTED" ]; then
        printf '\nstopping...\n'
        for pid in $STARTED; do kill "$pid" 2>/dev/null || true; done
        wait 2>/dev/null || true
        if [ "$OLLAMA_WAS_UP" -eq 1 ]; then
            printf 'left the ollama that was already running\n'
        fi
        printf 'logs kept at %s\n' "$LOGS"
    fi
}
trap cleanup INT TERM EXIT

# --- preflight ------------------------------------------------------------
# every one of these is a real failure someone hit, reported before anything
# starts rather than as a traceback in a log file three terminals away.

[ -x "$PY" ] || fail "no .venv here. run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"

"$PY" -c "import flask, pydantic, yaml" 2>/dev/null \
    || fail "dependencies missing. run:  .venv/bin/pip install -r requirements.txt"

# only the staff and patient apps read it - the public site holds no database
# connection at all, so a site-only demo must not be blocked on one
if [ "$NO_OLLAMA" -eq 0 ] && [ ! -f db/clinic.sqlite ]; then
    fail "no database yet. run:  $PY seed_users.py"
fi

port_busy() { lsof -ti:"$1" >/dev/null 2>&1; }

for port in 5000 5001 5002; do
    if port_busy "$port"; then
        fail "port $port is already in use. stop it first:  lsof -ti:$port | xargs kill"
    fi
done

if [ "$NO_OLLAMA" -eq 0 ]; then
    command -v ollama >/dev/null 2>&1 \
        || fail "ollama not installed (https://ollama.com/download), or use --no-ollama for the site alone"
fi

if [ "$VOICE" -eq 1 ] && [ ! -f .env.voice ]; then
    fail "--voice needs .env.voice with the demo keys. see voice_config.py for why it is fenced"
fi

# --- ollama ---------------------------------------------------------------
# the staff extraction path and the patient chat need it. the public site does
# not touch a model at all, which is why --no-ollama is still a usable demo.
if [ "$NO_OLLAMA" -eq 0 ]; then
    if curl -s -m 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
        OLLAMA_WAS_UP=1
        say "ollama      already running - leaving it alone"
    else
        ollama serve >"$LOGS/ollama.log" 2>&1 &
        STARTED="$STARTED $!"
        printf 'ollama      starting'
        i=0
        while [ $i -lt 30 ]; do
            curl -s -m 2 http://localhost:11434/api/tags >/dev/null 2>&1 && break
            printf '.'; sleep 1; i=$((i + 1))
        done
        printf '\n'
        curl -s -m 2 http://localhost:11434/api/tags >/dev/null 2>&1 \
            || fail "ollama did not come up - see $LOGS/ollama.log"
    fi

    if ! ollama list 2>/dev/null | grep -q '^dental-notes'; then
        say ""
        say "  !! the dental-notes model is not registered."
        say "     note extraction and the staff Q&A will fail; everything else works."
        say "     its weights are ~2GB and not in this repo - see the README."
        say ""
    fi
fi

# --- the three apps -------------------------------------------------------
# started in one place so the flags cannot drift from the README again.

start_app() {
    name=$1; port=$2; log=$3; shift 3
    "$@" >"$LOGS/$log" 2>&1 &
    STARTED="$STARTED $!"
    printf '%-11s starting' "$name"
    i=0
    while [ $i -lt 25 ]; do
        code=$(curl -s -o /dev/null -w '%{http_code}' -m 2 "http://127.0.0.1:$port/" 2>/dev/null || true)
        case "$code" in 2??|3??) printf ' -> http://127.0.0.1:%s\n' "$port"; return 0 ;; esac
        printf '.'; sleep 1; i=$((i + 1))
    done
    printf '\n'
    # the two guards exit before flask binds, so the log is the only place that
    # says why. surfacing it here saves reading three logs to find the one line.
    say ""
    say "  $name did not come up. last lines of $LOGS/$log:"
    tail -5 "$LOGS/$log" 2>/dev/null | sed 's/^/    /' || true
    if grep -qi "filevault\|disk guard" "$LOGS/$log" 2>/dev/null; then
        say ""
        say "  that is disk_guard: it refuses an unencrypted disk because this app"
        say "  reads patient data. turn FileVault on, or for a fake-data demo:"
        say "      DISK_GUARD_DISARMED=1 ./demo.sh"
    fi
    fail "$name failed to start"
}

say ""
if [ "$NO_OLLAMA" -eq 0 ]; then
    start_app "staff"   5000 staff.log   "$PY" run.py
    start_app "patient" 5001 patient.log env PATIENT_COOKIE_SECURE=0 "$PY" patient_run.py
fi

if [ "$VOICE" -eq 1 ]; then
    start_app "site" 5002 site.log env VOICE_DEMO=1 "$PY" site_run.py
else
    start_app "site" 5002 site.log "$PY" site_run.py
fi

# --- ready ----------------------------------------------------------------
say ""
say "  public site     http://127.0.0.1:5002"
if [ "$NO_OLLAMA" -eq 0 ]; then
    say "  staff CRM       http://127.0.0.1:5000     dentist / assistant / admin"
    say "  patient portal  http://127.0.0.1:5001     codice fiscale + PIN issued by staff"
    say ""
    say "  staff passwords are the dev ones in seed_users.py - fake data only."
fi
if [ "$VOICE" -eq 1 ]; then
    say ""
    say "  voice ARMED. audio leaves this machine to Deepgram and ElevenLabs."
    say "  demo only - never with real patient data. see voice_config.py."
fi
say ""
say "  logs: $LOGS"
say "  Ctrl+C to stop."
say ""

wait
