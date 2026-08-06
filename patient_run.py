from patient_app import create_patient_app

# its own port, chosen not locked. bound to loopback: the cloudflared tunnel
# is the only thing that should ever reach it, and not until phase 20.
PATIENT_PORT = 5001

if __name__ == "__main__":
    # without this guard, any import of this module - a future selftest, a
    # wsgi loader - blocks forever inside the dev server (WR-11)
    app = create_patient_app()
    app.run(host="127.0.0.1", port=PATIENT_PORT, threaded=True)
