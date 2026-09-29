import csv
import io
import json
from datetime import date

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import services as svc
from .db import get_db

bp = Blueprint("main", __name__)


# ---------------------------------------------------------------- helpers

@bp.before_app_request
def require_operator():
    if request.endpoint in ("main.operator", "static") or session.get("operator"):
        return None
    return redirect(url_for("main.operator", next=request.full_path))


@bp.app_context_processor
def inject_globals():
    return {"operator": session.get("operator"), "today": date.today().isoformat()}


@bp.record_once
def register_template_globals(state):
    # Globals (unlike context-processor values) are also visible inside imported macros.
    state.app.jinja_env.globals.update(
        MOVEMENT_TYPES=svc.MOVEMENT_TYPES, INBOUND=svc.INBOUND, OUTBOUND=svc.OUTBOUND, PARTY_TYPES=svc.PARTY_TYPES
    )


@bp.app_template_filter("fromjson")
def fromjson(value):
    try:
        return json.loads(value or "{}")
    except ValueError:
        return {}


def user():
    return session["operator"]


def all_parties():
    return get_db().execute("SELECT id, name, party_type FROM parties ORDER BY name").fetchall()


def all_samples():
    return get_db().execute(
        f"SELECT s.id, s.code, s.name, s.unit, {svc.ON_HAND_SQL} AS on_hand"
        " FROM samples s LEFT JOIN movements m ON m.sample_id = s.id"
        " GROUP BY s.id ORDER BY s.code DESC"
    ).fetchall()


MOVEMENT_SELECT = """
    SELECT m.*, s.code AS sample_code, s.name AS sample_name, s.unit,
           p.name AS party_name, p.party_type,
           src.movement_date AS source_date, sp.name AS source_party
    FROM movements m
    JOIN samples s ON s.id = m.sample_id
    LEFT JOIN parties p ON p.id = m.party_id
    LEFT JOIN movements src ON src.id = m.source_movement_id
    LEFT JOIN parties sp ON sp.id = src.party_id
"""


def movement_filters(args):
    """Build a WHERE clause from query-string filters shared by the list and CSV export."""
    where, params = [], []
    if args.get("sample_id"):
        where.append("m.sample_id = ?")
        params.append(args["sample_id"])
    if args.get("party_id"):
        where.append("m.party_id = ?")
        params.append(args["party_id"])
    if args.get("movement_type"):
        where.append("m.movement_type = ?")
        params.append(args["movement_type"])
    elif args.get("direction") == "in":
        where.append(f"m.movement_type IN ({','.join('?' * len(svc.INBOUND))})")
        params.extend(svc.INBOUND)
    elif args.get("direction") == "out":
        where.append(f"m.movement_type IN ({','.join('?' * len(svc.OUTBOUND))})")
        params.extend(svc.OUTBOUND)
    if args.get("date_from"):
        where.append("m.movement_date >= ?")
        params.append(args["date_from"])
    if args.get("date_to"):
        where.append("m.movement_date <= ?")
        params.append(args["date_to"])
    if args.get("q"):
        like = f"%{args['q']}%"
        where.append(
            "(s.code LIKE ? OR s.name LIKE ? OR m.reference LIKE ? OR m.tracking_number LIKE ? OR m.notes LIKE ?)"
        )
        params.extend([like] * 5)
    if not args.get("include_voided"):
        where.append("m.voided = 0")
    return (" WHERE " + " AND ".join(where)) if where else "", params


def csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------- operator

@bp.route("/operator", methods=["GET", "POST"])
def operator():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if name:
            session["operator"] = name
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("main.dashboard"))
        flash("Please enter your name.", "error")
    return render_template("operator.html")


# ---------------------------------------------------------------- dashboard

@bp.route("/")
def dashboard():
    db = get_db()
    month_start = date.today().replace(day=1).isoformat()
    totals = db.execute(
        f"SELECT {svc.QTY_IN_SQL} AS qty_in, {svc.QTY_OUT_SQL} AS qty_out FROM movements m"
    ).fetchone()
    month = db.execute(
        f"SELECT {svc.QTY_IN_SQL} AS qty_in, {svc.QTY_OUT_SQL} AS qty_out FROM movements m WHERE m.movement_date >= ?",
        (month_start,),
    ).fetchone()
    stats = {
        "samples": db.execute("SELECT COUNT(*) FROM samples").fetchone()[0],
        "parties": db.execute("SELECT COUNT(*) FROM parties").fetchone()[0],
        "on_hand": totals["qty_in"] - totals["qty_out"],
        "total_in": totals["qty_in"],
        "total_out": totals["qty_out"],
        "month_in": month["qty_in"],
        "month_out": month["qty_out"],
    }
    recent = db.execute(MOVEMENT_SELECT + " WHERE m.voided = 0 ORDER BY m.movement_date DESC, m.id DESC LIMIT 10").fetchall()
    top_sources = db.execute(
        "SELECT p.id, p.name, SUM(m.quantity) AS qty FROM movements m JOIN parties p ON p.id = m.party_id"
        " WHERE m.voided = 0 AND m.movement_type = 'RECEIVED' GROUP BY p.id ORDER BY qty DESC LIMIT 5"
    ).fetchall()
    top_destinations = db.execute(
        "SELECT p.id, p.name, SUM(m.quantity) AS qty FROM movements m JOIN parties p ON p.id = m.party_id"
        " WHERE m.voided = 0 AND m.movement_type IN ('SENT', 'REDIRECTED') GROUP BY p.id ORDER BY qty DESC LIMIT 5"
    ).fetchall()
    return render_template(
        "dashboard.html", stats=stats, recent=recent, top_sources=top_sources, top_destinations=top_destinations
    )


# ---------------------------------------------------------------- receive (main intake workflow)

@bp.route("/receive", methods=["GET", "POST"])
def receive():
    form = request.form if request.method == "POST" else {"sample_id": request.args.get("sample_id", "")}
    if request.method == "POST":
        db = get_db()
        try:
            data = dict(request.form)
            if data.get("sample_id") == "new" or not data.get("sample_id"):
                data["sample_id"] = svc.create_sample(
                    {k[4:]: v for k, v in request.form.items() if k.startswith("new_")}, user()
                )
            data["movement_type"] = "RECEIVED"
            receipt_id = svc.record_movement(data, user())
            if request.form.get("redirect_to"):
                svc.record_movement(
                    {
                        "movement_type": "REDIRECTED",
                        "sample_id": data["sample_id"],
                        "quantity": request.form.get("redirect_quantity") or data["quantity"],
                        "party_id": request.form["redirect_to"],
                        "movement_date": data.get("movement_date"),
                        "source_movement_id": receipt_id,
                        "carrier": request.form.get("redirect_carrier"),
                        "tracking_number": request.form.get("redirect_tracking"),
                        "reference": data.get("reference"),
                        "notes": request.form.get("redirect_notes"),
                    },
                    user(),
                )
            db.commit()
            flash("Receipt recorded.", "success")
            return redirect(url_for("main.sample_detail", sample_id=data["sample_id"]))
        except svc.ValidationError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template(
        "receive.html",
        form=form,
        samples=all_samples(),
        parties=all_parties(),
        next_code=svc.next_sample_code(),
    )


# ---------------------------------------------------------------- movements

@bp.route("/movements")
def movements():
    where, params = movement_filters(request.args)
    rows = get_db().execute(
        MOVEMENT_SELECT + where + " ORDER BY m.movement_date DESC, m.id DESC LIMIT 500", params
    ).fetchall()
    return render_template(
        "movements.html", rows=rows, args=request.args, samples=all_samples(), parties=all_parties()
    )


@bp.route("/movements/export.csv")
def movements_csv():
    where, params = movement_filters(request.args)
    rows = get_db().execute(MOVEMENT_SELECT + where + " ORDER BY m.movement_date, m.id", params).fetchall()
    return csv_response(
        "sample_movements.csv",
        ["ID", "Date", "Type", "Direction", "Sample code", "Sample", "Quantity", "Unit", "Party", "Party type",
         "Reference", "Carrier", "Tracking #", "Redirected from receipt #", "Handled by", "Notes",
         "Recorded at", "Voided", "Void reason"],
        [
            [r["id"], r["movement_date"], svc.MOVEMENT_TYPES[r["movement_type"]][0],
             "IN" if r["movement_type"] in svc.INBOUND else "OUT", r["sample_code"], r["sample_name"],
             r["quantity"], r["unit"], r["party_name"], r["party_type"], r["reference"], r["carrier"],
             r["tracking_number"], r["source_movement_id"], r["handled_by"], r["notes"], r["created_at"],
             "yes" if r["voided"] else "", r["void_reason"]]
            for r in rows
        ],
    )


@bp.route("/movements/new", methods=["GET", "POST"])
def movement_new():
    form = request.form if request.method == "POST" else request.args
    if request.method == "POST":
        db = get_db()
        try:
            svc.record_movement(request.form, user())
            db.commit()
            flash("Movement recorded.", "success")
            return redirect(url_for("main.sample_detail", sample_id=request.form["sample_id"]))
        except svc.ValidationError as exc:
            db.rollback()
            flash(str(exc), "error")
    open_receipts = []
    if form.get("sample_id"):
        open_receipts = [
            r for r in get_db().execute(
                "SELECT m.id, m.movement_date, m.quantity, p.name AS party_name FROM movements m"
                " LEFT JOIN parties p ON p.id = m.party_id"
                " WHERE m.sample_id = ? AND m.movement_type = 'RECEIVED' AND m.voided = 0 ORDER BY m.movement_date DESC",
                (form["sample_id"],),
            ).fetchall()
            if svc.receipt_remaining(r["id"]) > 0
        ]
    return render_template(
        "movement_form.html", form=form, samples=all_samples(), parties=all_parties(), open_receipts=open_receipts
    )


@bp.route("/movements/void", methods=["POST"])
def movement_void():
    db = get_db()
    movement_id = request.form.get("movement_id", type=int)
    mv = db.execute("SELECT sample_id FROM movements WHERE id = ?", (movement_id,)).fetchone()
    if mv is None:
        abort(404)
    try:
        svc.void_movement(movement_id, request.form.get("reason"), user())
        db.commit()
        flash(f"Entry #{movement_id} voided.", "success")
    except svc.ValidationError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("main.sample_detail", sample_id=mv["sample_id"]))


# ---------------------------------------------------------------- samples

@bp.route("/samples")
def samples():
    q = request.args.get("q", "").strip()
    stock = request.args.get("stock", "")
    sql = (
        f"SELECT s.*, {svc.QTY_IN_SQL} AS qty_in, {svc.QTY_OUT_SQL} AS qty_out, {svc.ON_HAND_SQL} AS on_hand,"
        " MAX(CASE WHEN m.voided = 0 THEN m.movement_date END) AS last_activity"
        " FROM samples s LEFT JOIN movements m ON m.sample_id = s.id"
    )
    params = []
    if q:
        sql += " WHERE s.code LIKE ? OR s.name LIKE ? OR s.category LIKE ? OR s.part_number LIKE ? OR s.storage_location LIKE ?"
        params = [f"%{q}%"] * 5
    sql += " GROUP BY s.id"
    if stock == "in":
        sql += f" HAVING {svc.ON_HAND_SQL} > 0"
    elif stock == "out":
        sql += f" HAVING {svc.ON_HAND_SQL} <= 0"
    sql += " ORDER BY s.code DESC"
    rows = get_db().execute(sql, params).fetchall()
    if request.args.get("format") == "csv":
        return csv_response(
            "sample_inventory.csv",
            ["Code", "Name", "Category", "Part #", "Unit", "Location", "Total received", "Total out", "On hand",
             "Last activity"],
            [[r["code"], r["name"], r["category"], r["part_number"], r["unit"], r["storage_location"], r["qty_in"],
              r["qty_out"], r["on_hand"], r["last_activity"]] for r in rows],
        )
    return render_template("samples.html", rows=rows, q=q, stock=stock)


@bp.route("/samples/new", methods=["GET", "POST"])
def sample_new():
    if request.method == "POST":
        db = get_db()
        try:
            sample_id = svc.create_sample(request.form, user())
            db.commit()
            flash("Sample created. Record a receipt to add stock.", "success")
            return redirect(url_for("main.sample_detail", sample_id=sample_id))
        except svc.ValidationError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("sample_form.html", sample=dict(request.form), next_code=svc.next_sample_code(), is_new=True)


@bp.route("/samples/<int:sample_id>")
def sample_detail(sample_id):
    db = get_db()
    sample = db.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()
    if sample is None:
        abort(404)
    stock = db.execute(
        f"SELECT {svc.QTY_IN_SQL} AS qty_in, {svc.QTY_OUT_SQL} AS qty_out, {svc.ON_HAND_SQL} AS on_hand"
        " FROM movements m WHERE m.sample_id = ?",
        (sample_id,),
    ).fetchone()
    history = db.execute(
        MOVEMENT_SELECT + " WHERE m.sample_id = ? ORDER BY m.movement_date, m.id", (sample_id,)
    ).fetchall()
    # Running balance for the trail (voided rows don't count).
    balance, trail = 0, []
    for row in history:
        if not row["voided"]:
            balance += row["quantity"] * svc.MOVEMENT_TYPES[row["movement_type"]][1]
        trail.append((row, balance))
    trail.reverse()
    audit = db.execute(
        "SELECT * FROM audit_log WHERE (entity = 'sample' AND entity_id = ?)"
        " OR (entity = 'movement' AND entity_id IN (SELECT id FROM movements WHERE sample_id = ?))"
        " ORDER BY id DESC",
        (sample_id, sample_id),
    ).fetchall()
    return render_template("sample_detail.html", sample=sample, stock=stock, trail=trail, audit=audit)


@bp.route("/samples/<int:sample_id>/edit", methods=["GET", "POST"])
def sample_edit(sample_id):
    db = get_db()
    sample = db.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()
    if sample is None:
        abort(404)
    if request.method == "POST":
        try:
            svc.update_sample(sample_id, request.form, user())
            db.commit()
            flash("Sample updated.", "success")
            return redirect(url_for("main.sample_detail", sample_id=sample_id))
        except svc.ValidationError as exc:
            db.rollback()
            flash(str(exc), "error")
            sample = {**dict(sample), **request.form}
    return render_template("sample_form.html", sample=dict(sample), is_new=False)


# ---------------------------------------------------------------- parties

@bp.route("/parties")
def parties():
    rows = get_db().execute(
        "SELECT p.*,"
        " COALESCE(SUM(CASE WHEN m.movement_type IN ('RECEIVED','RETURNED') AND m.voided = 0 THEN m.quantity END), 0) AS qty_from,"
        " COALESCE(SUM(CASE WHEN m.movement_type IN ('SENT','REDIRECTED') AND m.voided = 0 THEN m.quantity END), 0) AS qty_to,"
        " MAX(CASE WHEN m.voided = 0 THEN m.movement_date END) AS last_activity"
        " FROM parties p LEFT JOIN movements m ON m.party_id = p.id GROUP BY p.id ORDER BY p.name"
    ).fetchall()
    return render_template("parties.html", rows=rows)


@bp.route("/parties/new", methods=["GET", "POST"])
def party_new():
    if request.method == "POST":
        db = get_db()
        try:
            party_id = svc.create_party(request.form, user())
            db.commit()
            flash("Saved.", "success")
            nxt = request.args.get("next") or ""
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("main.party_detail", party_id=party_id))
        except svc.ValidationError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("party_form.html", party=dict(request.form), is_new=True)


@bp.route("/parties/<int:party_id>")
def party_detail(party_id):
    db = get_db()
    party = db.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
    if party is None:
        abort(404)
    by_sample = db.execute(
        "SELECT s.id, s.code, s.name, s.unit,"
        " COALESCE(SUM(CASE WHEN m.movement_type IN ('RECEIVED','RETURNED') THEN m.quantity END), 0) AS qty_from,"
        " COALESCE(SUM(CASE WHEN m.movement_type IN ('SENT','REDIRECTED') THEN m.quantity END), 0) AS qty_to"
        " FROM movements m JOIN samples s ON s.id = m.sample_id"
        " WHERE m.party_id = ? AND m.voided = 0 GROUP BY s.id ORDER BY s.code DESC",
        (party_id,),
    ).fetchall()
    history = db.execute(
        MOVEMENT_SELECT + " WHERE m.party_id = ? ORDER BY m.movement_date DESC, m.id DESC", (party_id,)
    ).fetchall()
    return render_template("party_detail.html", party=party, by_sample=by_sample, history=history)


@bp.route("/parties/<int:party_id>/edit", methods=["GET", "POST"])
def party_edit(party_id):
    db = get_db()
    party = db.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
    if party is None:
        abort(404)
    if request.method == "POST":
        try:
            svc.update_party(party_id, request.form, user())
            db.commit()
            flash("Saved.", "success")
            return redirect(url_for("main.party_detail", party_id=party_id))
        except svc.ValidationError as exc:
            db.rollback()
            flash(str(exc), "error")
            party = {**dict(party), **request.form}
    return render_template("party_form.html", party=dict(party), is_new=False)


# ---------------------------------------------------------------- reports & audit

@bp.route("/reports")
def reports():
    args = request.args
    date_from = args.get("date_from") or date.today().replace(month=1, day=1).isoformat()
    date_to = args.get("date_to") or date.today().isoformat()
    db = get_db()
    by_type = db.execute(
        "SELECT movement_type, COUNT(*) AS entries, SUM(quantity) AS qty FROM movements"
        " WHERE voided = 0 AND movement_date BETWEEN ? AND ? GROUP BY movement_type",
        (date_from, date_to),
    ).fetchall()
    by_party = db.execute(
        "SELECT p.id, p.name, p.party_type,"
        " COALESCE(SUM(CASE WHEN m.movement_type = 'RECEIVED' THEN m.quantity END), 0) AS received,"
        " COALESCE(SUM(CASE WHEN m.movement_type = 'RETURNED' THEN m.quantity END), 0) AS returned,"
        " COALESCE(SUM(CASE WHEN m.movement_type = 'SENT' THEN m.quantity END), 0) AS sent,"
        " COALESCE(SUM(CASE WHEN m.movement_type = 'REDIRECTED' THEN m.quantity END), 0) AS redirected"
        " FROM movements m JOIN parties p ON p.id = m.party_id"
        " WHERE m.voided = 0 AND m.movement_date BETWEEN ? AND ? GROUP BY p.id ORDER BY p.name",
        (date_from, date_to),
    ).fetchall()
    by_month = db.execute(
        f"SELECT substr(m.movement_date, 1, 7) AS month, {svc.QTY_IN_SQL} AS qty_in, {svc.QTY_OUT_SQL} AS qty_out"
        " FROM movements m WHERE m.movement_date BETWEEN ? AND ? GROUP BY month ORDER BY month",
        (date_from, date_to),
    ).fetchall()
    if args.get("format") == "csv":
        return csv_response(
            "party_summary.csv",
            ["Party", "Type", "Received from", "Returned by", "Sent to", "Redirected to"],
            [[r["name"], r["party_type"], r["received"], r["returned"], r["sent"], r["redirected"]] for r in by_party],
        )
    return render_template(
        "reports.html", date_from=date_from, date_to=date_to, by_type=by_type, by_party=by_party, by_month=by_month
    )


@bp.route("/audit")
def audit_log():
    rows = get_db().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 500").fetchall()
    return render_template("audit.html", rows=rows)
