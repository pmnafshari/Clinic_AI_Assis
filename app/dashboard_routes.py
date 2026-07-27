import json
from pathlib import Path

from flask import Blueprint, g, redirect, render_template, url_for

import agent
from auth import authorize

dashboard_bp = Blueprint("dashboard", __name__)

# module-level, same as app/agent_routes.py's UNDO_LOG - a selftest patches
# dashboard_routes.UNDO_LOG, so it must be read fresh (log_path=None below),
# not frozen into a default argument at def time
UNDO_LOG = agent.UNDO_LOG


def _user_undo_history(username, log_path=None, limit=10):
    log_path = log_path or UNDO_LOG
    log_file = Path(log_path)
    if not log_file.exists():
        return []
    lines = log_file.read_text().strip().splitlines()
    if not lines:
        return []
    mine = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # a truncated/hand-edited line must not 500 the whole dashboard
            continue
        if entry.get("username") == username:
            mine.append(entry)
    return mine[:limit]


@dashboard_bp.route("/")
def index():
    # someone who manages users and has no clinical access gets their own
    # landing page - the clinical dashboard is never rendered for them.
    # phrased as a capability pair so it survives a role being renamed.
    if authorize(g.user["role"], "manage_users") and not authorize(g.user["role"], "read_notes"):
        return redirect(url_for("admin.users_view"))

    history = _user_undo_history(g.user["username"])
    return render_template("dashboard.html", user=g.user, history=history)
