import disk_guard
import upload_worker
from app import create_app

app = create_app()
# before resume_pending, not merely before app.run: that sweep reads and moves
# filed patient notes, so an unencrypted host must be refused ahead of it, not
# after it has already processed a backlog. after create_app for the same
# reason patient_run.py gives - construction is the cheaper failure and the two
# errors should not mask each other.
disk_guard.guard_or_exit()
# anything left in drop/ by a previous run was uploaded but never processed
upload_worker.resume_pending()
# no port argument, so this binds flask's default 5000 - tunnel_guard.STAFF_PORT mirrors it, change both together
app.run(host="127.0.0.1", threaded=True)
