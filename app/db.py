import sqlite3

from flask import g

from storage import get_collection

DB_PATH = "db/clinic.sqlite"
CHROMA_PATH = "db/chroma"

_collection_cache = None


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def get_chroma():
    # one app-lifetime chroma handle shared by every blueprint (qa, agent,
    # notes) - the client is thread-safe, so it lives here, not per-request g,
    # and there is a single CHROMA_PATH for a selftest to patch
    global _collection_cache
    if _collection_cache is None:
        _collection_cache = get_collection(CHROMA_PATH)
    return _collection_cache


def close_db(app):
    @app.teardown_appcontext
    def _close(exception):
        db = g.pop("db", None)
        if db is not None:
            db.close()
