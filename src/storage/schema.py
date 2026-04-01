"""SQLite schema and initialization helpers."""

from __future__ import annotations

import sqlite3


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pairs (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_token                  TEXT UNIQUE NOT NULL,
    direction                   TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'PENDING',
    entry_time                  REAL NOT NULL,
    entry_spread                REAL,

    mt5_ticket                  INTEGER,
    mt5_action                  TEXT,
    mt5_volume                  REAL,
    mt5_entry_price             REAL,
    mt5_close_price             REAL,
    mt5_profit                  REAL DEFAULT 0,
    mt5_entry_latency_ms        REAL,
    mt5_close_latency_ms        REAL,

    exchange_order_id           TEXT,
    exchange_close_order_id     TEXT,
    exchange_volume             INTEGER,
    exchange_side               INTEGER,
    exchange_entry_latency_ms   REAL,
    exchange_close_latency_ms   REAL,

    close_time                  REAL,
    close_spread                REAL,
    close_reason                TEXT,

    conf_dev_entry              REAL,
    conf_dev_close              REAL,

    recon_status                TEXT DEFAULT 'PENDING',
    recon_note                  TEXT,
    last_recon_at               REAL,

    created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS pair_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_token  TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    event_data  TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS recon_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at              REAL NOT NULL,
    mt5_positions       INTEGER,
    exchange_positions  INTEGER,
    local_pairs         INTEGER,
    mismatches          INTEGER DEFAULT 0,
    actions_taken       TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_pairs_status ON pairs(status);
CREATE INDEX IF NOT EXISTS idx_pairs_token ON pairs(pair_token);
CREATE INDEX IF NOT EXISTS idx_events_token ON pair_events(pair_token);
CREATE INDEX IF NOT EXISTS idx_events_type ON pair_events(event_type);
"""


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
