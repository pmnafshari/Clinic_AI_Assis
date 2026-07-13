import json
from pathlib import Path

from flask import Blueprint, g, render_template

import agent

dashboard_bp = Blueprint("dashboard", __name__)

# module-level, same as app/agent_routes.py's UNDO_LOG - lets a selftest
# patch dashboard_routes.UNDO_LOG and have it take effect, since it's read
# fresh at call time below instead of frozen as a default argument
UNDO_LOG = agent.UNDO_LOG


def _user_undo_history(username, log_path=UNDO_LOG, limit=10):
    log_file = Path(log_path)
    if not log_file.exists():
        return []
    lines = log_file.read_text().strip().splitlines()
    if not lines:
        return []
    entries = [json.loads(line) for line in reversed(lines)]
    mine = [e for e in entries if e.get("username") == username]
    return mine[:limit]


@dashboard_bp.route("/")
def index():
    history = _user_undo_history(g.user["username"], log_path=UNDO_LOG)
    return render_template("dashboard.html", user=g.user, history=history)
