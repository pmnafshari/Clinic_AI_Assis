import disk_guard
import tunnel_guard
from patient_app import create_patient_app

# its own port, chosen not locked. bound to loopback: the cloudflared tunnel
# is the only thing that should ever reach it, and not until phase 20.
PATIENT_PORT = 5001

if __name__ == "__main__":
    # without this guard, any import of this module - a future selftest, a
    # wsgi loader - blocks forever inside the dev server (WR-11)
    app = create_patient_app()
    # here, not in create_patient_app: the factory runs in every selftest that
    # builds an app, and the guard must never fire there (D-06). before
    # app.run, so a refusal never leaves a socket listening on the port. after
    # create_patient_app, so a config error and a construction error cannot
    # mask each other - construction is the cheaper failure.
    # disk first: encryption at rest is the more fundamental precondition, so
    # on a misconfigured host it should be the first refusal the operator sees.
    disk_guard.guard_or_exit()
    tunnel_guard.guard_or_exit(PATIENT_PORT)
    app.run(host="127.0.0.1", port=PATIENT_PORT, threaded=True)
