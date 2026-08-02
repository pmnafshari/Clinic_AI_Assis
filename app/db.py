import sqlite3

from flask import g

from storage import get_shared_collection

DB_PATH = "db/clinic.sqlite"
CHROMA_PATH = "db/chroma"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def get_chroma():
    # the single app-lifetime chroma handle now lives in storage, shared with
    # non-Flask callers (upload_worker, watcher). CHROMA_PATH stays here as
    # the app's patch point for selftests
    return get_shared_collection(CHROMA_PATH)


def close_db(app):
    @app.teardown_appcontext
    def _close(exception):
        db = g.pop("db", None)
        if db is not None:
            db.close()
