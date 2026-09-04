from site_app import create_site_app

# its own port, alongside 5000 (staff) and 5001 (patient). loopback in
# development; this is the one app that is eventually meant to be public, but
# nothing exposes it yet.
SITE_PORT = 5002

if __name__ == "__main__":
    # same guard as patient_run.py - without it any import of this module
    # blocks forever inside the dev server (WR-11)
    app = create_site_app()
    # no disk_guard here, deliberately. run.py and patient_run.py refuse an
    # unencrypted host because they read patient data. this app reads none,
    # so there is nothing for encryption at rest to protect. if that ever
    # stops being true, the guard comes back with it.
    app.run(host="127.0.0.1", port=SITE_PORT, threaded=True)
