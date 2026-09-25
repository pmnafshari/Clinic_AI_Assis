"""the staff sidebar (P23): one list, each link carrying its route's own gate.

a link a role cannot follow is the same defect as a hidden withhold, read from the other side - so every entry names
the capability its route checks, and the sidebar shows exactly the entries the role holds. counts are shown only to a
role that may open the page they count.
"""

from flask import g, request

from auth import authorize

# (group, label, endpoint, path, icon, gate or None, count key or None, active prefix)
ITEMS = [
    ("Workspace", "Home", "dashboard.index", "/", "bi-house", "read_notes", None, "dashboard."),
    ("Workspace", "Appointments", "appointments.index", "/appointments", "bi-calendar3", "manage_appointments", "requests", "appointments."),
    ("Workspace", "Patients", "patients.list_view", "/patients", "bi-people", "read_notes", None, "patients.list_view|patients.detail_view|patients.search|patients.edit|patients.visit|patients.files|patients.issue|patients.revoke|patients.consent|documents.|summary.|similar."),
    ("Workspace", "Notes to review", "review.queue", "/reviews", "bi-clipboard-check", "review_upload", "reviews", "review."),
    ("Workspace", "Ask records", "qa.qa_page", "/qa", "bi-chat-square-text", "read_notes", None, "qa."),
    ("Workspace", "Add note", "notes.new_note", "/notes/new", "bi-journal-plus", "read_notes", None, "notes."),
    ("Operations", "Billing", "billing.index", "/billing", "bi-receipt", "view_billing", None, "billing."),
    ("Operations", "Stock", "stock.index", "/stock", "bi-box-seam", "use_inventory", None, "stock."),
    ("Operations", "Call-backs", "handoff.index", "/handoffs", "bi-telephone", "handle_handoff", None, "handoff."),
    ("Operations", "Reminders", "reminders.index", "/reminders", "bi-bell", "view_reminders", None, "reminders."),
    ("Operations", "Data requests", "data_requests.index", "/data-requests", "bi-shield-lock", "manage_data_requests", None, "data_requests."),
    ("Operations", "Reports", "reports.index", "/reports", "bi-graph-up", "read_clinical", None, "reports."),
    ("Operations", "Connections", "providers.index", "/providers", "bi-plug", "view_providers", None, "providers."),
    ("Admin", "Staff accounts", "admin.users_view", "/admin/users", "bi-person-gear", "manage_users", None, "admin."),
    ("Admin", "Duplicates", "patients.duplicates_view", "/patients/duplicates", "bi-people-fill", "manage_users", None, "patients.duplicates"),
]


def _counts(conn, role):
    out = {}
    if authorize(role, "manage_appointments"):
        out["requests"] = conn.execute("SELECT COUNT(*) FROM appointments WHERE status = 'requested'").fetchone()[0]
    if authorize(role, "review_upload"):
        out["reviews"] = conn.execute("SELECT COUNT(*) FROM note_reviews WHERE status IN"
                                      " ('pending', 'extraction_failed', 'confirming')").fetchone()[0]
    return out


def sidebar():
    """-> [(group, [item dict])] for the signed-in user, or [] on a chromeless page."""
    user = getattr(g, "user", None)
    if not user:
        return []
    from .db import get_db
    role = user["role"]
    counts = _counts(get_db(), role)
    endpoint = request.endpoint or ""
    groups = []
    for group, label, _ep, path, icon, gate, count_key, prefixes in ITEMS:
        if gate and not authorize(role, gate):
            continue
        active = any(endpoint.startswith(p) for p in prefixes.split("|"))
        item = {"label": label, "path": path, "icon": icon, "active": active,
                "count": counts.get(count_key) if count_key else None}
        if not groups or groups[-1][0] != group:
            groups.append((group, []))
        groups[-1][1].append(item)
    return groups
