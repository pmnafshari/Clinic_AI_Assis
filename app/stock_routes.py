import secrets

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import inventory
import inventory_job
from auth import authorize, log_audit

from .db import get_db

stock_bp = Blueprint("stock", __name__)


def _refuse(action, target=None):
    log_audit(get_db(), g.user["username"], g.user["role"], action, target, allowed=0)
    flash("You don't have permission to do that with stock.", "danger")
    return redirect(url_for("dashboard.index"))


@stock_bp.route("/stock")
def index():
    if not authorize(g.user["role"], "use_inventory"):
        return _refuse("view_stock")
    conn = get_db()
    low_only = request.args.get("show") == "low"
    return render_template("stock.html", items=inventory.items(conn, low_only), low_only=low_only,
                           alerts=inventory.open_alerts(conn), last_run=inventory.last_run(conn),
                           schedule=inventory_job.SCHEDULE, units=inventory.UNITS,
                           can_manage=authorize(g.user["role"], "manage_inventory"))


@stock_bp.route("/stock/<int:item_id>")
def item(item_id):
    if not authorize(g.user["role"], "use_inventory"):
        return _refuse("view_stock", f"item:{item_id}")
    conn = get_db()
    found = inventory.item(conn, item_id)
    if found is None:
        abort(404)
    return render_template("stock_item.html", item=found, history=inventory.history(conn, item_id),
                           key=lambda: secrets.token_urlsafe(16),
                           can_manage=authorize(g.user["role"], "manage_inventory"))


@stock_bp.route("/stock/<int:item_id>/move", methods=["POST"])
def move(item_id):
    if not authorize(g.user["role"], "use_inventory"):
        return _refuse("stock_move", f"item:{item_id}")
    form = request.form
    try:
        _, created = inventory.move(get_db(), item_id, form.get("kind", ""), form.get("quantity"),
                                    g.user["username"], g.user["role"], form.get("reason"),
                                    form.get("key") or None)
        flash("Recorded." if created else "That was already recorded.", "success")
    except PermissionError:
        flash("A correction is for a dentist. Use a count to record what is on the shelf.", "danger")
    except inventory.StockError as e:
        flash(f"Not recorded: {e}.", "danger")
    return redirect(url_for("stock.item", item_id=item_id))


@stock_bp.route("/stock/items", methods=["POST"])
def create():
    if not authorize(g.user["role"], "manage_inventory"):
        return _refuse("create_stock_item")
    try:
        item_id = inventory.create_item(get_db(), request.form.get("name"), request.form.get("unit"),
                                        request.form.get("threshold"), g.user["username"],
                                        g.user["role"])
        flash("Item added.", "success")
        return redirect(url_for("stock.item", item_id=item_id))
    except inventory.StockError as e:
        flash(f"Not added: {e}.", "danger")
        return redirect(url_for("stock.index"))


@stock_bp.route("/stock/<int:item_id>/threshold", methods=["POST"])
def threshold(item_id):
    if not authorize(g.user["role"], "manage_inventory"):
        return _refuse("set_stock_threshold", f"item:{item_id}")
    try:
        inventory.set_threshold(get_db(), item_id, request.form.get("threshold"),
                                g.user["username"], g.user["role"])
        flash("Threshold changed.", "success")
    except inventory.StockError as e:
        flash(f"Not changed: {e}.", "danger")
    return redirect(url_for("stock.item", item_id=item_id))


@stock_bp.route("/stock/alerts/<int:alert_id>/acknowledge", methods=["POST"])
def acknowledge(alert_id):
    if not authorize(g.user["role"], "use_inventory"):
        return _refuse("acknowledge_stock_alert", f"alert:{alert_id}")
    inventory.acknowledge(get_db(), alert_id, g.user["username"], g.user["role"])
    return redirect(url_for("stock.index"))
