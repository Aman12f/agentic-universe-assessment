"""
Unit and Integration Tests for Event JSONL Ingestion.

Validates:
- Malformed JSON line filtering and error counting.
- Deduplication of identical event_id deliveries.
- Unknown disposition filtering per catalogue.
- Dynamic reconciliation of late-arriving leads with previously orphaned events.
- Association of disposition.override events via call_id.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from solution.db import Database
from solution.event_ingest import EventIngestor
from solution.lead_ingest import LeadIngestor
from solution.schema_loader import SchemaRegistry


class TestEventIngestion(unittest.TestCase):
    """Test suite for event JSONL ingestion and ledger storage."""

    def setUp(self) -> None:
        """Sets up a temporary SQLite database and schema registry for isolation."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.db = Database(self.db_path)
        self.registry = SchemaRegistry()
        self.lead_ingestor = LeadIngestor(self.db, self.registry)
        self.event_ingestor = EventIngestor(self.db, self.registry)

    def tearDown(self) -> None:
        """Cleans up temporary directory after each test."""
        self.temp_dir.cleanup()

    def _write_jsonl(self, filename: str, lines: list) -> Path:
        """Helper to write lines into a temporary JSONL file."""
        p = Path(self.temp_dir.name) / filename
        with open(p, "w", encoding="utf-8") as f:
            for line in lines:
                if isinstance(line, dict):
                    f.write(json.dumps(line) + "\n")
                else:
                    f.write(str(line) + "\n")
        return p

    def _write_lead_csv(self, filename: str, rows: list) -> Path:
        """Helper to write lead CSV in motor_renewal workflow."""
        wf_dir = Path(self.temp_dir.name) / "motor_renewal"
        wf_dir.mkdir(parents=True, exist_ok=True)
        csv_path = wf_dir / filename
        header = ["customer_name", "phone", "policy_number"]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return csv_path

    def test_malformed_and_duplicate_events(self) -> None:
        """Verifies counters for malformed JSON, duplicate event_ids, and unknown dispositions."""
        events = [
            # 1. Valid event
            {
                "event_id": "evt_001",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "call_001",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "9876543210",
                "payload": {},
            },
            # 2. Duplicate delivery of evt_001 -> dropped
            {
                "event_id": "evt_001",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "call_001",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "9876543210",
                "payload": {},
            },
            # 3. Malformed line (truncated JSON syntax)
            '{"event_id": "evt_002", "event_type": "call.initi',
            # 4. Malformed event (missing event_id key)
            {
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "call_003",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "9876543210",
            },
            # 5. Unknown disposition event (code not in dispositions.csv)
            {
                "event_id": "evt_004",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "call_001",
                "occurred_at": "2026-10-05T10:05:00Z",
                "phone": "9876543210",
                "payload": {"disposition_code": "unknown_foo_code"},
            },
        ]
        p = self._write_jsonl("events_001.jsonl", events)
        self.event_ingestor.ingest_event_file(p, "drop_1")

        self.assertEqual(self.db.get_counter("events_total"), 5)
        self.assertEqual(self.db.get_counter("malformed_events"), 2)
        self.assertEqual(self.db.get_counter("duplicate_events_dropped"), 1)
        self.assertEqual(self.db.get_counter("unknown_disposition_events"), 1)

    def test_late_arriving_lead_reconciles_orphans_and_overrides(self) -> None:
        """
        Verifies that out-of-order events arriving before their lead are quarantined as
        orphans in drop_1, and automatically reconciled when the lead arrives in drop_2.
        """
        # Drop 1: Events arrive first (lead does not exist yet)
        events = [
            {
                "event_id": "evt_101",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "call_101",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "9876543210",
                "payload": {},
            },
            {
                "event_id": "evt_102",
                "event_type": "call.connected",
                "workflow_id": "motor_renewal",
                "call_id": "call_101",
                "occurred_at": "2026-10-05T10:01:00Z",
                "phone": "9876543210",
                "payload": {},
            },
            # Supervisor override has NO phone, only call_id
            {
                "event_id": "evt_103",
                "event_type": "disposition.override",
                "workflow_id": "motor_renewal",
                "call_id": "call_101",
                "occurred_at": "2026-10-05T12:00:00Z",
                "payload": {
                    "disposition_code": "renewed",
                    "overridden_by": "supervisor_1",
                    "reason": "qa_review",
                },
            },
        ]
        p = self._write_jsonl("events_101.jsonl", events)
        self.event_ingestor.ingest_event_file(p, "drop_1")

        with self.db.get_connection() as conn:
            # Initially all 3 events are orphans because lead has not arrived
            orphans = conn.execute(
                "SELECT COUNT(*) as cnt FROM events_ledger WHERE is_orphan = 1"
            ).fetchone()["cnt"]
            self.assertEqual(orphans, 3)

        # Drop 2: Lead arrives!
        lead_csv = self._write_lead_csv(
            "batch_003.csv",
            [{"customer_name": "Aarav Sharma", "phone": "9876543210", "policy_number": "POL100"}],
        )
        self.lead_ingestor.ingest_lead_file(lead_csv, "drop_2")

        with self.db.get_connection() as conn:
            # All 3 events should now be linked to the newly arrived lead
            orphans_after = conn.execute(
                "SELECT COUNT(*) as cnt FROM events_ledger WHERE is_orphan = 1"
            ).fetchone()["cnt"]
            self.assertEqual(orphans_after, 0)

            # Check that override event is also linked to the lead via call_id
            override = conn.execute(
                "SELECT lead_id, is_orphan FROM events_ledger WHERE event_id = 'evt_103'"
            ).fetchone()
            self.assertIsNotNone(override["lead_id"])
            self.assertEqual(override["is_orphan"], 0)


if __name__ == "__main__":
    unittest.main()
