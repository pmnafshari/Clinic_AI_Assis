from pathlib import Path

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import sqlite3

import agent
import ask
import consent
import data_rights
import patient_auth
import patient_identity
import pending_actions
from auth import authorize, log_audit
from codice_fiscale import is_valid as is_valid_cf
from storage import lookup_clinical, lookup_patient

from .db import get_chroma, get_db

patients_bp = Blueprint("patients", __name__)

SORTED_ROOT = Path("sorted")


def patient_identity_pid(key):
    """Resolve a route's identifier to a patient_id (Phase 51)."""
    import patient_id as _pid

    return _pid.resolve(get_db(), key)

# newest by visit_date, not by insert order - a bulk load_from_sorted re-import
# assigns visit ids by filename, so the highest id is not the latest visit.
# same ordering as agent.pick_target_visit; nulls sort last.
LATEST_VISIT = "ORDER BY v.visit_date IS NULL, v.visit_date DESC, v.id DESC LIMIT 1"


@patients_bp.route("/patients")
def list_view():
    if not authorize(g.user["role"], "read_notes"):
        log_audit(get_db(), g.user["username"], g.user["role"], "read_notes", None, allowed=0)
        flash("You don't have permission to view patient records.")
        return redirect(url_for("dashboard.index"))

    patients = get_db().execute(f"""
        SELECT p.patient_id, p.codice_fiscale, p.patient_name, p.phone,
            (SELECT visit_date FROM visits v WHERE v.patient_id = p.patient_id
             {LATEST_VISIT}) AS last_visit
        FROM patients p
        ORDER BY p.patient_name
    """).fetchall()
    # the next appointment is a booking (appointments.next_booked), never the
    # free-text recall a visit note carries - that one is shown on the record (P22)
    import appointments
    conn = get_db()
    patients = [{**dict(p), "next_appointment": appointments.next_booked_local(conn, p["patient_id"])}
                for p in patients]
    return render_template("patients_list.html", patients=patients)


@patients_bp.route("/patients/search")
def search_fragment():
    # HTMX target - a denied fragment returns a bare status, not a redirect
    if not authorize(g.user["role"], "read_notes"):
        return "", 403

    query = request.args.get("q", "")
    candidates = ask.fuzzy_lookup(query, get_db())
    return render_template("_patient_candidates.html", candidates=candidates, query=query)


# --- duplicate review (P04) ------------------------------------------------
#
# REGISTERED BEFORE /patients/<cf> ON PURPOSE. Werkzeug sorts static rules
# ahead of dynamic ones so the order in this file does not actually decide it,
# but the two rules do overlap, and `patients_routes_selftest` asserts that
# /patients/duplicates reaches this view rather than being read as a codice
# fiscale. Do not merge the two into one dynamic rule.
#
# manage_users, not read_notes: merging two patients is the most destructive
# operation in the product, and reception must not hold it. Dismissing a pair
# is the same judgement in the other direction - a wrong dismissal hides a real
# duplicate from everyone who looks after it - so it is gated identically.

@patients_bp.route("/patients/duplicates")
def duplicates_view():
    if not authorize(g.user["role"], "manage_users"):
        log_audit(get_db(), g.user["username"], g.user["role"],
                  "review_duplicates", None, allowed=0)
        flash("You don't have permission to review duplicate records.")
        return redirect(url_for("dashboard.index"))

    conn = get_db()
    pairs = patient_identity.candidates(conn)
    log_audit(conn, g.user["username"], g.user["role"],
              "review_duplicates", str(len(pairs)), allowed=1)
    return render_template("patients_duplicates.html", pairs=pairs)


@patients_bp.route("/patients/duplicates/dismiss", methods=["POST"])
def duplicates_dismiss():
    # the gate lives in patient_identity.dismiss() as well as here. the module
    # is callable from a CLI and from a future agent action, and a capability
    # check that only exists in a route is a capability check that gets skipped.
    ok, message = patient_identity.dismiss(
        get_db(),
        request.form.get("cf_a", ""),
        request.form.get("cf_b", ""),
        g.user["username"],
        g.user["role"],
        request.form.get("reason"),
    )
    if not ok:
        flash(message, "error")
        return redirect(url_for("dashboard.index"))
    flash(message, "success")
    return redirect(url_for("patients.duplicates_view"))


@patients_bp.route("/patients/duplicates/merge", methods=["POST"])
def duplicates_merge():
    # the capability check lives in patient_identity.merge() too. this is the
    # most destructive operation in the product, and a gate that exists only in
    # a route is a gate that a CLI or a future agent action walks straight past.
    conn = get_db()
    cf_a = request.form.get("cf_a", "")
    cf_b = request.form.get("cf_b", "")
    keep = request.form.get("keep", "")
    if request.form.get("confirm") != "yes":
        flash("Tick the confirmation to merge two records.", "error")
        return redirect(url_for("patients.duplicates_view"))
    # the form sends both records and which one survives; the other is derived
    # here rather than posted, so the page cannot disagree with itself about
    # which record is being folded away.
    #
    # this is NOT what stops a malicious post: cf_a and cf_b are form fields,
    # so anyone who holds manage_users can name any two records either way
    # round. The capability IS the protection, and merge() re-checks it. What
    # this guard buys is an incoherent or half-filled form getting a clear
    # answer here instead of a confusing one from three layers down.
    if keep not in (cf_a, cf_b) or cf_a == cf_b or not cf_a or not cf_b:
        flash("Choose which record to keep.", "error")
        return redirect(url_for("patients.duplicates_view"))
    target = keep
    source = cf_b if keep == cf_a else cf_a

    ok, message = patient_identity.merge(
        conn, source, target, g.user["username"], g.user["role"],
        collection=get_collection_or_none(),
    )
    flash(message, "success" if ok else "error")
    if not ok and "not permitted" in message:
        return redirect(url_for("dashboard.index"))
    return redirect(url_for("patients.duplicates_view"))


def get_collection_or_none():
    # the merge repoints chunk metadata so staff Q&A stops citing a patient
    # who no longer exists. if the index is unreachable the SQLite merge still
    # stands and is re-runnable against the mapping, which is better than
    # refusing the merge outright - but it must not fail silently, so the
    # count of repointed chunks is recorded on the merge row either way.
    try:
        return get_chroma()
    except Exception:
        return None


@patients_bp.route("/patients/<cf>")
def detail_view(cf):
    if not authorize(g.user["role"], "read_notes"):
        log_audit(get_db(), g.user["username"], g.user["role"], "read_notes", cf, allowed=0)
        flash("You don't have permission to view patient records.")
        return redirect(url_for("dashboard.index"))

    # validate before any db/filesystem access - cf is a raw path segment
    if not is_valid_cf(cf):
        abort(404)

    conn = get_db()
    pid = patient_identity_pid(cf)
    if pid is None:
        abort(404)
    # A FOLDED CODICE FISCALE REDIRECTS RATHER THAN RENDERING. lookup_patient
    # would happily serve the survivor's record under the old identifier, and
    # that is worse than it sounds: two URLs would show one record, so a link
    # copied out of the address bar would keep the dead identifier alive. The
    # redirect makes the canonical URL the one people actually pass around.
    current = conn.execute(
        "SELECT codice_fiscale FROM patients WHERE patient_id = ?", (pid,)).fetchone()
    if current is not None and cf not in (pid, current["codice_fiscale"]):
        flash(f"{cf} was merged into this record.")
        return redirect(url_for("patients.detail_view", cf=current["codice_fiscale"]))

    patient = lookup_patient(pid, conn)
    if patient is None:
        abort(404)

    # dentist-only gate for the clinical card - never read_notes, which
    # assistant also holds (RBAC-03)
    show_clinical = authorize(g.user["role"], "read_clinical")
    clinical = lookup_clinical(cf, conn) if show_clinical else None

    # an authorized view is audited too, not only a refusal (P02.04): who
    # opened whose record, and when - never what it said. written BEFORE the
    # page renders, so if the audit write fails the request fails with it and
    # no record is served unlogged (fail-closed). the 5s files poll is not
    # logged per poll; this view, which starts it, is.
    log_audit(conn, g.user["username"], g.user["role"],
              "view_record_clinical" if show_clinical else "view_record", cf, allowed=1)

    return render_template(
        "patients_detail.html",
        cf=cf,
        patient=patient,
        clinical=clinical,
        show_clinical=show_clinical,
        # the timeline follows the SAME gate as the clinical card above, rather
        # than carrying a gate of its own - an assistant sees that a visit
        # happened without reading what it said (RBAC-03)
        timeline=patient_identity.timeline(conn, cf, show_clinical=show_clinical),
        consents=consent.state(conn, pid, "en"),
        requests=data_rights.for_patient(conn, pid),
    )


@patients_bp.route("/patients/<cf>/files")
def files_fragment(cf):
    # HTMX-polled fragment - a denied fragment returns a bare status, never
    # a redirect (an HX-swap would otherwise get a whole login/dashboard page
    # every 5 seconds). filenames are clinical data too (notes/, images/,
    # records/), so this stays read_clinical, not read_notes (CR-01/RBAC-03)
    if not authorize(g.user["role"], "read_clinical"):
        return "", 403

    if not is_valid_cf(cf):
        abort(404)

    # ONE DIRECTORY PER PATIENT, named by the surrogate (Phase 51). A merge now
    # moves the folded patient's files into the survivor's directory, so this no
    # longer has to union several roots the way it did while MERGE-1 was open.
    #
    # The legacy names are still consulted: a migration or merge file-move op
    # that has not completed yet leaves a directory under the old codice
    # fiscale, and a file that exists must not vanish from this list because a
    # background step is still pending.
    pid = patient_identity_pid(cf)
    roots = [pid] if pid else []
    roots += [cf] + patient_identity.merged_sources_of(get_db(), cf)
    files = []
    seen = set()
    for root in roots:
        if not root or root in seen:
            continue
        seen.add(root)
        patient_dir = SORTED_ROOT / root
        if patient_dir.is_dir():
            files.extend(
                str(f.relative_to(SORTED_ROOT)) for f in patient_dir.rglob("*") if f.is_file()
            )
    return render_template("_patient_files.html", files=sorted(files))


@patients_bp.route("/patients/<cf>/issue-pin", methods=["POST"])
def issue_pin_submit(cf):
    # HTMX target - a denied fragment returns a bare status, not a redirect.
    # there is deliberately no GET counterpart: only the hash is stored, so a
    # pin cannot be re-displayed even if someone wanted to build one.
    if not authorize(g.user["role"], "issue_patient_pin"):
        log_audit(get_db(), g.user["username"], g.user["role"], "issue_patient_pin",
                  cf, allowed=0)
        return "", 403

    if not is_valid_cf(cf):
        abort(404)

    conn = get_db()
    if lookup_patient(cf, conn) is None:
        abort(404)

    try:
        pin = patient_auth.issue_pin(cf, conn, g.user["username"], g.user["role"])
    except sqlite3.IntegrityError:
        # the patient row went away between the lookup and the write
        abort(404)

    # the plaintext goes into this one response body and nowhere else: not a
    # flash (which survives a redirect), not the url, not the log, not the
    # audit row. issue_pin already recorded that an issuance happened.
    return render_template("_patient_pin.html", cf=cf, pin=pin)


@patients_bp.route("/patients/<cf>/revoke-pin", methods=["POST"])
def revoke_pin_submit(cf):
    # HTMX target - a denied fragment returns a bare status, not a redirect.
    # there is deliberately no GET counterpart and no un-revoke route:
    # reissuing a PIN already sets active = 1 and is the documented recovery
    # path, so a second way back would just be a second thing to get wrong.
    if not authorize(g.user["role"], "revoke_patient_pin"):
        log_audit(get_db(), g.user["username"], g.user["role"], "revoke_patient_pin",
                  cf, allowed=0)
        return "", 403

    if not is_valid_cf(cf):
        abort(404)

    conn = get_db()
    if lookup_patient(cf, conn) is None:
        abort(404)

    cur = conn.execute(
        "UPDATE patient_credentials SET active = 0 WHERE patient_id = ?",
        (patient_identity_pid(cf),)
    )
    conn.commit()
    if cur.rowcount == 0:
        # no credential to revoke - don't report a revocation that never happened
        abort(404)

    log_audit(conn, g.user["username"], g.user["role"], "revoke_patient_pin", cf, allowed=1)

    # deactivating the credential alone would leave a live session sliding
    # its own idle window forward on every request - the loader's
    # c.active = 1 clause plus this call are what make revocation immediate
    # rather than eventual
    patient_auth.destroy_patient_sessions(conn, cf)

    return render_template("_patient_pin_revoked.html", cf=cf)


@patients_bp.route("/patients/<cf>/consent", methods=["POST"])
def consent_submit(cf):
    # staff record what the patient said at the desk; the patient can also
    # grant or withdraw from the portal profile. either way it is a new row.
    if not authorize(g.user["role"], "record_consent"):
        log_audit(get_db(), g.user["username"], g.user["role"], "record_consent", cf, allowed=0)
        flash("You don't have permission to record consent.", "danger")
        return redirect(url_for("dashboard.index"))

    if not is_valid_cf(cf):
        abort(404)
    pid = patient_identity_pid(cf)
    if pid is None:
        abort(404)

    note = request.form.get("note", "").strip()[:200] or None
    ok, message = consent.record(get_db(), pid, request.form.get("purpose", ""),
                                 request.form.get("granted") == "1",
                                 g.user["username"], g.user["role"], note)
    flash("Consent recorded." if ok else f"Not recorded: {message}.", "success" if ok else "danger")
    return redirect(url_for("patients.detail_view", cf=cf) + "#consent-title")


@patients_bp.route("/patients/<cf>/edit-form")
def edit_form_fragment(cf):
    # HTMX target - a denied fragment returns a bare status, not a redirect
    if not authorize(g.user["role"], "read_notes"):
        return "", 403

    if not is_valid_cf(cf):
        abort(404)

    field = request.args.get("field", "")
    if field not in agent.EDITABLE_FIELDS:
        abort(400)

    patient = lookup_patient(cf, get_db())
    if patient is None:
        abort(404)

    value = patient[agent.EDITABLE_FIELDS[field]]
    return render_template("_edit_form.html", cf=cf, field=field, value=value)


@patients_bp.route("/patients/<cf>/edit", methods=["POST"])
def edit_submit(cf):
    if not authorize(g.user["role"], "read_notes"):
        return "", 403

    if not is_valid_cf(cf):
        abort(404)

    field = request.form.get("field", "")
    value = request.form.get("value", "")

    conn = get_db()
    patient = lookup_patient(cf, conn)
    if patient is None:
        abort(404)

    # build_pending_action resolves by name, not cf - choose_cf pins the
    # resolution to the cf this route already validated, so a duplicate
    # name elsewhere can never redirect the edit to the wrong patient
    call = agent.ToolCall(
        tool="update_field",
        args={"patient": patient["patient_name"], "field": field, "value": value},
    )
    try:
        call.parsed_args()
    except Exception as e:
        return render_template(
            "_edit_form.html", cf=cf, field=field, value=value, error=f"invalid field: {e}"
        )

    pending, reason = agent.build_pending_action(
        call, conn, g.user["role"], g.user["username"],
        choose_cf=lambda candidates, cf=cf: cf if cf in candidates else None,
    )
    if pending is None:
        return render_template("_edit_form.html", cf=cf, field=field, value=value, error=reason)

    token = pending_actions.create_pending_action(conn, g.user["username"], g.user["role"], pending)
    return render_template("_confirm_diff.html", diff_line=pending["diff_line"], token=token)


@patients_bp.route("/patients/<cf>/visits/<int:visit_id>/edit-form")
def visit_edit_form_fragment(cf, visit_id):
    # HTMX target - a denied fragment returns a bare status, not a redirect.
    # gated on read_clinical (dentist-only) since this exposes clinical data
    # (visit date, next appointment), not on read_notes (RBAC-03 / CR-01)
    if not authorize(g.user["role"], "read_clinical"):
        return "", 403

    if not is_valid_cf(cf):
        abort(404)

    row = get_db().execute(
        "SELECT visit_date, next_appointment FROM visits WHERE id = ? AND patient_id = ?",
        (visit_id, patient_identity_pid(cf)),
    ).fetchone()
    if row is None:
        abort(404)

    return render_template(
        "_visit_edit_form.html", cf=cf, visit_id=visit_id,
        value=row["next_appointment"], visit_date=row["visit_date"],
    )


@patients_bp.route("/patients/<cf>/visits/<int:visit_id>/edit", methods=["POST"])
def visit_edit_submit(cf, visit_id):
    if not authorize(g.user["role"], "read_clinical"):
        return "", 403

    if not is_valid_cf(cf):
        abort(404)

    value = request.form.get("value", "")

    conn = get_db()
    patient = lookup_patient(cf, conn)
    if patient is None:
        abort(404)

    call = agent.ToolCall(
        tool="update_visit_field",
        args={"patient": patient["patient_name"], "visit_id": visit_id, "value": value},
    )
    try:
        call.parsed_args()
    except Exception as e:
        return render_template(
            "_visit_edit_form.html", cf=cf, visit_id=visit_id, value=value,
            error=f"invalid value: {e}",
        )

    pending, reason = agent.build_pending_action(
        call, conn, g.user["role"], g.user["username"], sorted_root=SORTED_ROOT,
        choose_cf=lambda candidates, cf=cf: cf if cf in candidates else None,
    )
    if pending is None:
        return render_template(
            "_visit_edit_form.html", cf=cf, visit_id=visit_id, value=value, error=reason
        )

    token = pending_actions.create_pending_action(conn, g.user["username"], g.user["role"], pending)
    return render_template("_confirm_diff.html", diff_line=pending["diff_line"], token=token)
