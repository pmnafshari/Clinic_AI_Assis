import sqlite3

from flask import g

DB_PATH = "db/clinic.sqlite"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(app):
    @app.teardown_appcontext
    def _close(exception):
        db = g.pop("db", None)
        if db is not None:
            db.close()
