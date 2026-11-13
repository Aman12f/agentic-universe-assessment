"""
Database Layer: Schema Management, Connection Pooling, and Transaction Boundaries.

This module provides an SQLite database layer configured for:
1. ACID Transactions: File-level atomic commits ensuring consistency upon crashes.
2. High Concurrency: Write-Ahead Logging (WAL) and NORMAL synchronous mode.
3. Append-Only Ledger: Preserves all historical call events without in-place edits.
4. PII Protection: Indexed via deterministic HMAC-SHA256 blind index (phone_hash)
   with encrypted payload storage at rest.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from solution.config import DEFAULT_DB_PATH

# SQL DDL Script defining relational schema and optimized indexes
SCHEMA_SQL: str = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- Tracks ingested files to ensure idempotent re-runs across drops
CREATE TABLE IF NOT EXISTS ingested_files (
    file_path TEXT PRIMARY KEY,
    drop_name TEXT NOT NULL,
    file_type TEXT NOT NULL, -- 'leads' or 'events'
    total_records INTEGER NOT NULL DEFAULT 0,
    malformed_or_rejected INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT NOT NULL
);

-- Leads table: PII protected at rest, indexed deterministically by phone_hash
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    phone_hash TEXT NOT NULL,         -- HMAC-SHA256 blind index for O(1) matching
    phone_masked TEXT NOT NULL,       -- Masked phone (+91******XXXX) for safe display
    batch_id TEXT NOT NULL,           -- Permanently assigned from first valid file stem
    external_ref TEXT NOT NULL,       -- Primary business reference (policy_number / order_id)
    fields_json TEXT NOT NULL,        -- Non-PII fields stored as JSON
    pii_payload_enc TEXT,             -- Authenticated ciphertext of PII fields (name, phone)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workflow_id, phone_hash)
);

CREATE INDEX IF NOT EXISTS idx_leads_lookup ON leads(workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS idx_leads_ref ON leads(workflow_id, external_ref);
CREATE INDEX IF NOT EXISTS idx_leads_batch ON leads(workflow_id, batch_id);

-- Rejections audit log: strictly zero PII per specification
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,      -- 1-based index among non-blank data rows
    reason TEXT NOT NULL,             -- Single first failure reason (e.g. invalid_phone)
    drop_name TEXT NOT NULL,
    UNIQUE(workflow_id, batch_id, row_number)
);

CREATE INDEX IF NOT EXISTS idx_rejections_sort ON rejections(workflow_id, batch_id, row_number);

-- Field warnings log for optional fields present but failing validation
CREATE TABLE IF NOT EXISTS field_warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    drop_name TEXT NOT NULL,
    UNIQUE(workflow_id, batch_id, row_number, field_name)
);

-- Append-only interaction ledger for all telephony and disposition events
CREATE TABLE IF NOT EXISTS events_ledger (
    event_id TEXT PRIMARY KEY,        -- Deduplicated globally by event_id
    event_type TEXT NOT NULL,         -- call.initiated, call.connected, disposition.set, etc.
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    occurred_at_ist TEXT NOT NULL,    -- ISO-8601 formatted in IST (+05:30)
    occurred_at_ts REAL NOT NULL,     -- Unix epoch float for chronologically ordered resolution
    phone_hash TEXT,                  -- Blind index hash (NULL for overrides or bad phones)
    phone_masked TEXT,
    payload_json TEXT NOT NULL,
    drop_name TEXT NOT NULL,
    lead_id INTEGER REFERENCES leads(id),
    is_orphan INTEGER NOT NULL DEFAULT 1, -- 1 if unlinked, 0 once matched to lead
    disposition_code TEXT,            -- Validated catalogue outcome code
    is_terminal INTEGER,              -- 1 if terminal, 0 if not
    is_success INTEGER                -- 1 if business success, 0 if not
);

CREATE INDEX IF NOT EXISTS idx_events_call ON events_ledger(call_id);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events_ledger(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_wf_phone ON events_ledger(workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS idx_events_type ON events_ledger(event_type);
CREATE INDEX IF NOT EXISTS idx_events_call_occurred ON events_ledger(call_id, occurred_at_ts);

-- Quality counters for persistent pipeline-wide metrics
CREATE TABLE IF NOT EXISTS quality_counters (
    metric_name TEXT PRIMARY KEY,
    counter_value INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    """
    Database interface managing connection initialization, schema migration, and counters.
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        """
        Initializes the Database instance.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = Path(db_path)
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        """
        Creates and returns a new SQLite connection with foreign keys and Row factory enabled.
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init_db(self) -> None:
        """
        Applies schema DDL and pre-populates default quality counter rows.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.get_connection() as conn:
            conn.executescript(SCHEMA_SQL)
            # Initialize metrics for quality reporting
            metrics = [
                "lead_rows_total",
                "duplicate_lead_rows_merged",
                "events_total",
                "malformed_events",
                "duplicate_events_dropped",
                "unknown_disposition_events",
            ]
            for m in metrics:
                conn.execute(
                    "INSERT OR IGNORE INTO quality_counters (metric_name, counter_value) VALUES (?, 0)",
                    (m,)
                )
            conn.commit()

    def increment_counter(self, conn: sqlite3.Connection, metric_name: str, delta: int = 1) -> None:
        """
        Atomically increments a global quality metric counter within an active transaction.

        Args:
            conn: Active sqlite3.Connection.
            metric_name: Counter name in quality_counters.
            delta: Value to add (default 1).
        """
        if delta != 0:
            conn.execute(
                "UPDATE quality_counters SET counter_value = counter_value + ? WHERE metric_name = ?",
                (delta, metric_name)
            )

    def get_counter(self, metric_name: str) -> int:
        """
        Reads the current value of a global quality counter.

        Args:
            metric_name: Name of metric.

        Returns:
            Integer counter value (0 if not found).
        """
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT counter_value FROM quality_counters WHERE metric_name = ?",
                (metric_name,)
            ).fetchone()
            return row["counter_value"] if row else 0
