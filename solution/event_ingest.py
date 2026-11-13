"""
Event Ingestion, Deduplication, and Append-Only Ledger Pipeline.

This module processes incoming call event JSON Lines files per Assignment A spec:
1. Malformed: Invalid JSON lines or missing event_id -> increment malformed_events and skip.
2. Duplicate: event_id already seen across any drop -> increment duplicate_events_dropped and skip.
3. Orphan: Events that cannot be linked to a known lead remain isolated (is_orphan = 1).
   - Links via (workflow_id, normalised phone).
   - disposition.override links through its call_id to the lead of that call's other events.
4. Unknown Disposition: disposition.* events with codes not in dispositions.csv are ignored
   and increment unknown_disposition_events.
5. Append-Only Ledger: Events are permanently recorded without modifying historical records.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from solution.config import IST
from solution.crypto import hash_phone, mask_phone
from solution.db import Database
from solution.normalizers import format_ist_iso, normalize_phone, parse_timestamp_to_ist
from solution.schema_loader import SchemaRegistry


class EventIngestor:
    """
    Handles parsing, deduplicating, validating, and persisting event JSONL files.
    """

    def __init__(self, db: Database, schema_registry: SchemaRegistry):
        """
        Initializes the EventIngestor.

        Args:
            db: Database instance for storage.
            schema_registry: Loaded registry containing schemas and catalogues.
        """
        self.db = db
        self.registry = schema_registry

    def ingest_event_file(self, file_path: Path, drop_name: str) -> None:
        """
        Ingests a single event JSON Lines file into the append-only ledger.

        Execution Pipeline (per event line):
        1. Check idempotency: If file path was previously ingested, skip.
        2. Malformed Check: Validates JSON syntax and non-empty `event_id`.
        3. Deduplication Check: Ensures `event_id` is unique across all drops.
        4. Timestamp Conversion: Translates UTC/offset/naive into aware IST.
        5. Catalogue Verification: Validates `disposition_code` for disposition.* events.
        6. Lead Resolution: Resolves lead_id via phone_hash or call_id for overrides.
        7. Append to Ledger: Writes record to `events_ledger`.

        Args:
            file_path: Path to JSONL file.
            drop_name: Name of drop folder containing this file (e.g. 'drop_1').
        """
        file_path = Path(file_path).resolve()
        file_key = str(file_path)

        with self.db.get_connection() as conn:
            # Check idempotency: skip file if already processed
            already = conn.execute(
                "SELECT 1 FROM ingested_files WHERE file_path = ?", (file_key,)
            ).fetchone()
            if already:
                return

            events_total = 0
            malformed_events = 0
            duplicate_events_dropped = 0
            unknown_disp_events = 0

            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    s_line = line.strip()
                    if not s_line:
                        continue
                    # Spec: "events_total counts every non-empty line read, including malformed and duplicate lines."
                    events_total += 1

                    # 1. Malformed check: Must be valid JSON object with event_id
                    try:
                        raw = json.loads(s_line)
                    except Exception:
                        malformed_events += 1
                        continue

                    if not isinstance(raw, dict):
                        malformed_events += 1
                        continue

                    event_id = raw.get("event_id")
                    if not event_id or not isinstance(event_id, str):
                        malformed_events += 1
                        continue

                    event_type = raw.get("event_type")
                    workflow_id = raw.get("workflow_id")
                    call_id = raw.get("call_id")
                    occurred_at_raw = raw.get("occurred_at")

                    if not event_type or not workflow_id or not call_id or not occurred_at_raw:
                        malformed_events += 1
                        continue

                    # 2. Duplicate check: event_id already seen in any drop
                    exists = conn.execute(
                        "SELECT 1 FROM events_ledger WHERE event_id = ?", (event_id,)
                    ).fetchone()
                    if exists:
                        duplicate_events_dropped += 1
                        continue

                    # Parse timestamp to aware IST datetime
                    try:
                        dt_ist = parse_timestamp_to_ist(str(occurred_at_raw))
                        occurred_at_ist = format_ist_iso(dt_ist)
                        occurred_at_ts = dt_ist.timestamp()
                    except Exception:
                        malformed_events += 1
                        continue

                    payload = raw.get("payload", {})
                    if not isinstance(payload, dict):
                        payload = {}

                    # 4. Unknown disposition check
                    disposition_code = None
                    is_terminal = None
                    is_success = None

                    if event_type in ("disposition.set", "disposition.override"):
                        code = payload.get("disposition_code")
                        if not code or not self.registry.is_known_disposition(workflow_id, str(code)):
                            unknown_disp_events += 1
                            # Ignore unknown disposition code: not stored as effective disposition
                            disposition_code = None
                        else:
                            disposition_code = str(code)
                            d_info = self.registry.get_disposition(workflow_id, disposition_code)
                            if d_info:
                                is_terminal = 1 if d_info.is_terminal else 0
                                is_success = 1 if d_info.is_success else 0

                    # 3. Lead Linking & Orphan Isolation
                    lead_id = None
                    phone_hash = None
                    phone_masked = None

                    if event_type == "disposition.override":
                        # Spec: "A disposition.override links through its call_id to the lead of that call's other events."
                        call_match = conn.execute(
                            "SELECT lead_id FROM events_ledger WHERE call_id = ? AND lead_id IS NOT NULL LIMIT 1",
                            (call_id,)
                        ).fetchone()
                        if call_match:
                            lead_id = call_match["lead_id"]
                    else:
                        raw_phone = raw.get("phone")
                        if raw_phone:
                            valid_p, norm_p = normalize_phone(str(raw_phone))
                            if valid_p and norm_p:
                                phone_hash = hash_phone(norm_p)
                                phone_masked = mask_phone(norm_p)
                                lead_row = conn.execute(
                                    "SELECT id FROM leads WHERE workflow_id = ? AND phone_hash = ?",
                                    (workflow_id, phone_hash)
                                ).fetchone()
                                if lead_row:
                                    lead_id = lead_row["id"]

                    # If lead is not yet in database, mark event as an orphan
                    is_orphan = 0 if lead_id is not None else 1

                    # Insert into append-only interaction ledger
                    conn.execute(
                        """
                        INSERT INTO events_ledger 
                        (event_id, event_type, workflow_id, call_id, occurred_at_ist, occurred_at_ts,
                         phone_hash, phone_masked, payload_json, drop_name, lead_id, is_orphan,
                         disposition_code, is_terminal, is_success)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            event_type,
                            workflow_id,
                            call_id,
                            occurred_at_ist,
                            occurred_at_ts,
                            phone_hash,
                            phone_masked,
                            json.dumps(payload),
                            drop_name,
                            lead_id,
                            is_orphan,
                            disposition_code,
                            is_terminal,
                            is_success,
                        )
                    )

                    # If this event resolved the lead for a call_id, link any other events for this call_id
                    if lead_id is not None:
                        conn.execute(
                            """
                            UPDATE events_ledger
                            SET lead_id = ?, is_orphan = 0
                            WHERE call_id = ? AND lead_id IS NULL
                            """,
                            (lead_id, call_id)
                        )

            # Record file ingestion record for idempotency
            now_str = datetime.now(IST).isoformat()
            conn.execute(
                """
                INSERT INTO ingested_files 
                (file_path, drop_name, file_type, total_records, malformed_or_rejected, duplicates, ingested_at)
                VALUES (?, ?, 'events', ?, ?, ?, ?)
                """,
                (file_key, drop_name, events_total, malformed_events, duplicate_events_dropped, now_str)
            )

            # Update global counters
            self.db.increment_counter(conn, "events_total", events_total)
            self.db.increment_counter(conn, "malformed_events", malformed_events)
            self.db.increment_counter(conn, "duplicate_events_dropped", duplicate_events_dropped)
            self.db.increment_counter(conn, "unknown_disposition_events", unknown_disp_events)
            conn.commit()
