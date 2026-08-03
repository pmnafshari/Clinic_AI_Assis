import upload_worker
from app import create_app

app = create_app()
# anything left in drop/ by a previous run was uploaded but never processed
upload_worker.resume_pending()
app.run(host="127.0.0.1", threaded=True)
