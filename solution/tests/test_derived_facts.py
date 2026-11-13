"""
Unit Tests for Derived Metrics, Effective Dispositions, and Lead Bucketing.

Validates:
- Bucket logic:
  1. yet_to_reach_out (0 calls).
  2. completed (any call has terminal disposition).
  3. in_progress_telephony_failed (every call has telephony_failed).
  4. in_progress_dialed (all other cases).
- Effective disposition resolution: disposition.override takes precedence over disposition.set.
- Ever success flag calculation.
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
from solution.reporter import ReportGenerator
from solution.schema_loader import SchemaRegistry


class TestDerivedFacts(unittest.TestCase):
    """Test suite for derived metrics, effective dispositions, and bucketing rules."""

    def setUp(self) -> None:
        """Sets up a temporary SQLite database and schema registry for isolation."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.db = Database(self.db_path)
        self.registry = SchemaRegistry()
        self.lead_ingestor = LeadIngestor(self.db, self.registry)
        self.event_ingestor = EventIngestor(self.db, self.registry)
        self.reporter = ReportGenerator(self.db)

    def tearDown(self) -> None:
        """Cleans up temporary directory after each test."""
        self.temp_dir.cleanup()

    def _setup_leads_and_events(self, leads: list, events: list) -> None:
        """Helper to create and ingest test lead CSV and event JSONL files."""
        # Write lead file
        wf_dir = Path(self.temp_dir.name) / "motor_renewal"
        wf_dir.mkdir(parents=True, exist_ok=True)
        csv_path = wf_dir / "batch_001.csv"
        header = ["customer_name", "phone", "policy_number"]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for r in leads:
                w.writerow(r)
        self.lead_ingestor.ingest_lead_file(csv_path, "drop_1")

        # Write events file
        ev_path = Path(self.temp_dir.name) / "events.jsonl"
        with open(ev_path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        self.event_ingestor.ingest_event_file(ev_path, "drop_1")

    def test_bucket_yet_to_reach_out(self) -> None:
        """Verifies that leads with zero call attempts fall into 'yet_to_reach_out' bucket."""
        leads = [{"customer_name": "Lead Zero", "phone": "9876543210", "policy_number": "POL000"}]
        self._setup_leads_and_events(leads, [])
        out_dir = Path(self.temp_dir.name) / "reports"
        self.reporter.generate_all(out_dir)

        with open(out_dir / "lead_report.csv", "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["bucket"], "yet_to_reach_out")
            self.assertEqual(rows[0]["total_calls"], "0")

    def test_bucket_completed_terminal(self) -> None:
        """Verifies that a call classified with a terminal disposition places lead in 'completed'."""
        leads = [{"customer_name": "Lead Term", "phone": "9876543210", "policy_number": "POL001"}]
        events = [
            {
                "event_id": "e1",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "9876543210",
            },
            {
                "event_id": "e2",
                "event_type": "call.connected",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:01:00Z",
                "phone": "9876543210",
            },
            {
                "event_id": "e3",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:05:00Z",
                "phone": "9876543210",
                "payload": {"disposition_code": "not_interested"},
            },
        ]
        self._setup_leads_and_events(leads, events)
        out_dir = Path(self.temp_dir.name) / "reports"
        self.reporter.generate_all(out_dir)

        with open(out_dir / "lead_report.csv", "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["bucket"], "completed")
            self.assertEqual(rows[0]["current_disposition"], "not_interested")
            self.assertEqual(rows[0]["ever_success"], "false")

    def test_bucket_in_progress_telephony_failed(self) -> None:
        """Verifies that leads where every call attempt failed telephony become 'in_progress_telephony_failed'."""
        leads = [{"customer_name": "Lead Fail", "phone": "9876543210", "policy_number": "POL002"}]
        events = [
            {
                "event_id": "e1",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "9876543210",
            },
            {
                "event_id": "e2",
                "event_type": "call.telephony_failed",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:00:05Z",
                "phone": "9876543210",
            },
        ]
        self._setup_leads_and_events(leads, events)
        out_dir = Path(self.temp_dir.name) / "reports"
        self.reporter.generate_all(out_dir)

        with open(out_dir / "lead_report.csv", "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["bucket"], "in_progress_telephony_failed")

    def test_effective_disposition_override_precedence(self) -> None:
        """
        Verifies that disposition.override supersedes earlier disposition.set events,
        correctly calculating effective disposition, bucket, and ever_success.
        """
        leads = [{"customer_name": "Lead Over", "phone": "9876543210", "policy_number": "POL003"}]
        events = [
            {
                "event_id": "e1",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "9876543210",
            },
            {
                "event_id": "e2",
                "event_type": "call.connected",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:01:00Z",
                "phone": "9876543210",
            },
            # AI agent sets 'payment_link_sent' (non-terminal, non-success)
            {
                "event_id": "e3",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T10:05:00Z",
                "phone": "9876543210",
                "payload": {"disposition_code": "payment_link_sent"},
            },
            # Supervisor overrides outcome to 'renewed' (terminal, success)
            {
                "event_id": "e4",
                "event_type": "disposition.override",
                "workflow_id": "motor_renewal",
                "call_id": "c1",
                "occurred_at": "2026-10-05T11:00:00Z",
                "payload": {
                    "disposition_code": "renewed",
                    "overridden_by": "sup_1",
                    "reason": "qa",
                },
            },
        ]
        self._setup_leads_and_events(leads, events)
        out_dir = Path(self.temp_dir.name) / "reports"
        self.reporter.generate_all(out_dir)

        with open(out_dir / "lead_report.csv", "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["current_disposition"], "renewed")
            self.assertEqual(rows[0]["bucket"], "completed")
            self.assertEqual(rows[0]["ever_success"], "true")


if __name__ == "__main__":
    unittest.main()
