"""SQLite storage for the sample tracker."""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS parties (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    party_type     TEXT NOT NULL,              -- vendor | customer | internal | other
    contact_person TEXT,
    email          TEXT,
    phone          TEXT,
    address        TEXT,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    created_by     TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    code             TEXT NOT NULL UNIQUE,     -- e.g. SMP-2026-0001
    name             TEXT NOT NULL,
    category         TEXT,
    part_number      TEXT,
    unit             TEXT NOT NULL DEFAULT 'pcs',
    storage_location TEXT,
    description      TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    created_by       TEXT
);

-- Every change in quantity is a movement. Movements are never deleted,
-- only voided, so the history is always complete.
CREATE TABLE IF NOT EXISTS movements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    movement_type   TEXT NOT NULL,
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    party_id        INTEGER REFERENCES parties(id),
    movement_date   TEXT NOT NULL,
    reference       TEXT,                      -- PO / invoice / request no.
    carrier         TEXT,
    tracking_number TEXT,
    source_movement_id INTEGER REFERENCES movements(id),  -- receipt a redirect came from
    notes           TEXT,
    handled_by      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    voided          INTEGER NOT NULL DEFAULT 0,
    void_reason     TEXT,
    voided_by       TEXT,
    voided_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_movements_sample ON movements(sample_id);
CREATE INDEX IF NOT EXISTS idx_movements_party ON movements(party_id);
CREATE INDEX IF NOT EXISTS idx_movements_date ON movements(movement_date);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    user_name   TEXT,
    entity      TEXT NOT NULL,                 -- sample | party | movement
    entity_id   INTEGER,
    action      TEXT NOT NULL,                 -- create | update | void
    details     TEXT                           -- JSON
);

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    get_db().executescript(SCHEMA)
