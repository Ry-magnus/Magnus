"""Business rules: stock calculation, movement recording, audit trail."""
import json
from datetime import date

from .db import get_db

PARTY_TYPES = ["vendor", "customer", "internal", "other"]

# movement_type -> (label, direction)  direction: +1 adds stock, -1 removes it
MOVEMENT_TYPES = {
    "RECEIVED": ("Received", 1),
    "RETURNED": ("Returned to us", 1),
    "ADJUST_IN": ("Adjustment (+)", 1),
    "SENT": ("Sent out", -1),
    "REDIRECTED": ("Redirected / forwarded", -1),
    "CONSUMED": ("Consumed / tested / disposed", -1),
    "ADJUST_OUT": ("Adjustment (-)", -1),
}
INBOUND = [k for k, (_, d) in MOVEMENT_TYPES.items() if d > 0]
OUTBOUND = [k for k, (_, d) in MOVEMENT_TYPES.items() if d < 0]

_IN_SQL = ",".join(f"'{t}'" for t in INBOUND)
_OUT_SQL = ",".join(f"'{t}'" for t in OUTBOUND)

# Reusable SQL fragments (only constant values are interpolated).
QTY_IN_SQL = f"COALESCE(SUM(CASE WHEN m.movement_type IN ({_IN_SQL}) AND m.voided = 0 THEN m.quantity END), 0)"
QTY_OUT_SQL = f"COALESCE(SUM(CASE WHEN m.movement_type IN ({_OUT_SQL}) AND m.voided = 0 THEN m.quantity END), 0)"
ON_HAND_SQL = f"({QTY_IN_SQL} - {QTY_OUT_SQL})"


class ValidationError(Exception):
    pass


def audit(user, entity, entity_id, action, details=None):
    get_db().execute(
        "INSERT INTO audit_log (user_name, entity, entity_id, action, details) VALUES (?, ?, ?, ?, ?)",
        (user, entity, entity_id, action, json.dumps(details or {}, default=str)),
    )


def _clean(value):
    value = (value or "").strip()
    return value or None


# ---------------------------------------------------------------- parties

def create_party(data, user):
    name = _clean(data.get("name"))
    party_type = data.get("party_type")
    if not name:
        raise ValidationError("Name is required.")
    if party_type not in PARTY_TYPES:
        raise ValidationError("Choose a valid party type.")
    db = get_db()
    if db.execute("SELECT 1 FROM parties WHERE name = ?", (name,)).fetchone():
        raise ValidationError(f"A vendor/customer named '{name}' already exists.")
    fields = {k: _clean(data.get(k)) for k in ("contact_person", "email", "phone", "address", "notes")}
    cur = db.execute(
        "INSERT INTO parties (name, party_type, contact_person, email, phone, address, notes, created_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, party_type, *fields.values(), user),
    )
    audit(user, "party", cur.lastrowid, "create", {"name": name, "party_type": party_type, **fields})
    return cur.lastrowid


def update_party(party_id, data, user):
    db = get_db()
    old = db.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
    if old is None:
        raise ValidationError("Not found.")
    name = _clean(data.get("name"))
    if not name:
        raise ValidationError("Name is required.")
    if data.get("party_type") not in PARTY_TYPES:
        raise ValidationError("Choose a valid party type.")
    if db.execute("SELECT 1 FROM parties WHERE name = ? AND id != ?", (name, party_id)).fetchone():
        raise ValidationError(f"A vendor/customer named '{name}' already exists.")
    new = {"name": name, "party_type": data["party_type"]}
    new.update({k: _clean(data.get(k)) for k in ("contact_person", "email", "phone", "address", "notes")})
    changes = {k: {"from": old[k], "to": v} for k, v in new.items() if old[k] != v}
    if not changes:
        return
    db.execute(
        f"UPDATE parties SET {', '.join(f'{k} = ?' for k in new)} WHERE id = ?",
        (*new.values(), party_id),
    )
    audit(user, "party", party_id, "update", changes)


# ---------------------------------------------------------------- samples

def next_sample_code():
    year = date.today().year
    prefix = f"SMP-{year}-"
    row = get_db().execute(
        "SELECT code FROM samples WHERE code LIKE ? ORDER BY code DESC LIMIT 1", (prefix + "%",)
    ).fetchone()
    seq = int(row["code"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}{seq:04d}"


SAMPLE_FIELDS = ("name", "category", "part_number", "unit", "storage_location", "description")


def create_sample(data, user):
    name = _clean(data.get("name"))
    if not name:
        raise ValidationError("Sample name is required.")
    fields = {k: _clean(data.get(k)) for k in SAMPLE_FIELDS}
    fields["unit"] = fields["unit"] or "pcs"
    code = _clean(data.get("code")) or next_sample_code()
    db = get_db()
    if db.execute("SELECT 1 FROM samples WHERE code = ?", (code,)).fetchone():
        raise ValidationError(f"Sample code '{code}' is already in use.")
    cur = db.execute(
        f"INSERT INTO samples (code, {', '.join(SAMPLE_FIELDS)}, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (code, *fields.values(), user),
    )
    audit(user, "sample", cur.lastrowid, "create", {"code": code, **fields})
    return cur.lastrowid


def update_sample(sample_id, data, user):
    db = get_db()
    old = db.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()
    if old is None:
        raise ValidationError("Not found.")
    new = {k: _clean(data.get(k)) for k in SAMPLE_FIELDS}
    if not new["name"]:
        raise ValidationError("Sample name is required.")
    new["unit"] = new["unit"] or "pcs"
    changes = {k: {"from": old[k], "to": v} for k, v in new.items() if old[k] != v}
    if not changes:
        return
    db.execute(
        f"UPDATE samples SET {', '.join(f'{k} = ?' for k in new)} WHERE id = ?",
        (*new.values(), sample_id),
    )
    audit(user, "sample", sample_id, "update", changes)


def on_hand(sample_id):
    row = get_db().execute(
        f"SELECT {ON_HAND_SQL} AS qty FROM movements m WHERE m.sample_id = ?", (sample_id,)
    ).fetchone()
    return row["qty"]


def receipt_remaining(receipt_id):
    """Units of a receipt not yet redirected onward."""
    row = get_db().execute(
        "SELECT r.quantity - COALESCE((SELECT SUM(quantity) FROM movements"
        "  WHERE source_movement_id = r.id AND voided = 0), 0) AS remaining"
        " FROM movements r WHERE r.id = ?",
        (receipt_id,),
    ).fetchone()
    return row["remaining"] if row else 0


# ---------------------------------------------------------------- movements

def record_movement(data, user):
    db = get_db()
    mtype = data.get("movement_type")
    if mtype not in MOVEMENT_TYPES:
        raise ValidationError("Choose a valid movement type.")
    try:
        sample_id = int(data.get("sample_id"))
    except (TypeError, ValueError):
        raise ValidationError("Choose a sample.")
    sample = db.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()
    if sample is None:
        raise ValidationError("Choose a sample.")
    try:
        qty = int(data.get("quantity"))
    except (TypeError, ValueError):
        raise ValidationError("Quantity must be a whole number.")
    if qty <= 0:
        raise ValidationError("Quantity must be greater than zero.")

    party_id = data.get("party_id") or None
    if party_id is not None:
        party_id = int(party_id)
        if not db.execute("SELECT 1 FROM parties WHERE id = ?", (party_id,)).fetchone():
            raise ValidationError("Unknown vendor/customer.")
    if mtype in ("RECEIVED", "RETURNED") and party_id is None:
        raise ValidationError("Select who the sample was received from.")
    if mtype in ("SENT", "REDIRECTED") and party_id is None:
        raise ValidationError("Select who the sample was sent to.")

    movement_date = _clean(data.get("movement_date")) or date.today().isoformat()

    source_id = data.get("source_movement_id") or None
    if source_id is not None:
        source_id = int(source_id)
        src = db.execute("SELECT * FROM movements WHERE id = ?", (source_id,)).fetchone()
        if src is None or src["sample_id"] != sample_id or src["movement_type"] != "RECEIVED" or src["voided"]:
            raise ValidationError("The linked receipt is not valid for this sample.")
        if mtype != "REDIRECTED":
            raise ValidationError("Only redirects can be linked to a receipt.")
        if qty > receipt_remaining(source_id):
            raise ValidationError(
                f"Only {receipt_remaining(source_id)} {sample['unit']} of that receipt are left to redirect."
            )

    if MOVEMENT_TYPES[mtype][1] < 0:
        available = on_hand(sample_id)
        if qty > available:
            raise ValidationError(
                f"Cannot move out {qty} {sample['unit']} — only {available} on hand for {sample['code']}."
            )

    fields = {
        "sample_id": sample_id,
        "movement_type": mtype,
        "quantity": qty,
        "party_id": party_id,
        "movement_date": movement_date,
        "reference": _clean(data.get("reference")),
        "carrier": _clean(data.get("carrier")),
        "tracking_number": _clean(data.get("tracking_number")),
        "source_movement_id": source_id,
        "notes": _clean(data.get("notes")),
        "handled_by": _clean(data.get("handled_by")) or user,
    }
    cur = db.execute(
        f"INSERT INTO movements ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
        tuple(fields.values()),
    )
    audit(user, "movement", cur.lastrowid, "create", {**fields, "sample_code": sample["code"]})
    return cur.lastrowid


def void_movement(movement_id, reason, user):
    db = get_db()
    mv = db.execute("SELECT * FROM movements WHERE id = ?", (movement_id,)).fetchone()
    if mv is None:
        raise ValidationError("Not found.")
    if mv["voided"]:
        raise ValidationError("This entry is already voided.")
    reason = _clean(reason)
    if not reason:
        raise ValidationError("A reason is required to void an entry.")
    if MOVEMENT_TYPES[mv["movement_type"]][1] > 0:
        # Voiding an inbound entry must not push stock negative.
        if on_hand(mv["sample_id"]) - mv["quantity"] < 0:
            raise ValidationError(
                "Cannot void this receipt: the units have already been sent out. Void the outbound entries first."
            )
        linked = db.execute(
            "SELECT COUNT(*) FROM movements WHERE source_movement_id = ? AND voided = 0", (movement_id,)
        ).fetchone()[0]
        if linked:
            raise ValidationError("Cannot void this receipt while redirects linked to it are active.")
    db.execute(
        "UPDATE movements SET voided = 1, void_reason = ?, voided_by = ?, voided_at = datetime('now') WHERE id = ?",
        (reason, user, movement_id),
    )
    audit(user, "movement", movement_id, "void", {"reason": reason})
