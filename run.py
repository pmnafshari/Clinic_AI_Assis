import upload_worker
from app import create_app

app = create_app()
# anything left in drop/ by a previous run was uploaded but never processed
upload_worker.resume_pending()
# no port argument, so this binds flask's default 5000 - tunnel_guard.STAFF_PORT mirrors it, change both together
app.run(host="127.0.0.1", threaded=True)
