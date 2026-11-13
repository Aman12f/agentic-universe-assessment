"""
Unit and Integration Tests for Lead CSV Ingestion.

Validates:
- Schema-order evaluation of rejection rules (e.g. checking customer_name before phone).
- Non-blank row numbering excluding skipped blank rows.
- Duplicate lead merging: permanent batch_id retention from first upload and non-null field overwrite.
- Metric counter tracking for rejections and merged duplicates.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from solution.db import Database
from solution.lead_ingest import LeadIngestor
from solution.schema_loader import SchemaRegistry


class TestLeadIngestion(unittest.TestCase):
    """Test suite for lead CSV ingestion logic."""

    def setUp(self) -> None:
        """Sets up a temporary SQLite database and schema registry for isolation."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.db = Database(self.db_path)
        self.registry = SchemaRegistry()
        self.ingestor = LeadIngestor(self.db, self.registry)

    def tearDown(self) -> None:
        """Cleans up temporary directory after each test."""
        self.temp_dir.cleanup()

    def _write_csv(
        self,
        filename: str,
        rows: list,
        header: list,
        workflow: str = "motor_renewal"
    ) -> Path:
        """Helper to write CSV files within temporary workflow directory."""
        wf_dir = Path(self.temp_dir.name) / workflow
        wf_dir.mkdir(parents=True, exist_ok=True)
        csv_path = wf_dir / filename
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for r in rows:
                if r is None:
                    f.write("," * (len(header) - 1) + "\r\n")  # Blank row
                else:
                    w.writerow(r)
        return csv_path

    def test_rejection_schema_order_missing_name_before_phone(self) -> None:
        """
        Verifies that schema order decides the single rejection reason when multiple fields fail.
        In motor_renewal.json, customer_name appears before phone.
        """
        header = ["customer_name", "phone", "policy_number", "policy_expiry"]
        rows = [
            # Row 1: Valid row
            {
                "customer_name": "Aarav Sharma",
                "phone": "9876543210",
                "policy_number": "POL100",
                "policy_expiry": "2026-10-10",
            },
            # Row 2: Missing name AND invalid phone -> customer_name checked first!
            {
                "customer_name": "",
                "phone": "12345",
                "policy_number": "POL101",
                "policy_expiry": "2026-10-10",
            },
            # Row 3: Name present, but invalid phone -> rejected with invalid_phone
            {
                "customer_name": "Diya Patel",
                "phone": "5123456789",
                "policy_number": "POL102",
                "policy_expiry": "2026-10-10",
            },
            # Row 4: Blank row -> must be skipped and NOT increment row_number
            None,
            # Row 5: Name and phone present, missing policy_number -> row 4 in 1-based non-blank count
            {
                "customer_name": "Rohan Gupta",
                "phone": "9876543211",
                "policy_number": "",
                "policy_expiry": "2026-10-10",
            },
        ]
        csv_path = self._write_csv("batch_001.csv", rows, header)
        self.ingestor.ingest_lead_file(csv_path, "drop_1")

        with self.db.get_connection() as conn:
            rejections = conn.execute(
                "SELECT row_number, reason FROM rejections ORDER BY row_number ASC"
            ).fetchall()

            # Expecting exactly 3 rejections
            self.assertEqual(len(rejections), 3)

            # Row 2: missing_required:customer_name
            self.assertEqual(rejections[0]["row_number"], 2)
            self.assertEqual(rejections[0]["reason"], "missing_required:customer_name")

            # Row 3: invalid_phone
            self.assertEqual(rejections[1]["row_number"], 3)
            self.assertEqual(rejections[1]["reason"], "invalid_phone")

            # Row 4 (following the skipped blank line): missing_required:policy_number
            self.assertEqual(rejections[2]["row_number"], 4)
            self.assertEqual(rejections[2]["reason"], "missing_required:policy_number")

            # Only 1 valid lead row should exist in the database
            leads = conn.execute("SELECT * FROM leads").fetchall()
            self.assertEqual(len(leads), 1)

    def test_duplicate_lead_merging(self) -> None:
        """
        Verifies duplicate merging retains permanent batch_id and overwrites non-null fields.
        """
        header = ["customer_name", "phone", "policy_number", "preferred_language", "premium_amount"]

        # Batch 1: Initial upload creates the lead with batch_id = 'batch_001'
        rows_1 = [
            {
                "customer_name": "Aarav Sharma",
                "phone": "9876543210",
                "policy_number": "POL100",
                "preferred_language": "hi",
                "premium_amount": "5000",
            }
        ]
        f1 = self._write_csv("batch_001.csv", rows_1, header)
        self.ingestor.ingest_lead_file(f1, "drop_1")

        # Batch 2: Duplicate upload with same canonical phone (+919876543210), updated premium, null language
        rows_2 = [
            {
                "customer_name": "Aarav Sharma",
                "phone": "+91 98765 43210",
                "policy_number": "POL100",
                "preferred_language": "",
                "premium_amount": "6500",
            }
        ]
        f2 = self._write_csv("batch_002.csv", rows_2, header)
        self.ingestor.ingest_lead_file(f2, "drop_1")

        with self.db.get_connection() as conn:
            leads = conn.execute("SELECT * FROM leads").fetchall()
            self.assertEqual(len(leads), 1)
            lead = leads[0]

            # Permanent batch_id must remain 'batch_001'
            self.assertEqual(lead["batch_id"], "batch_001")

            fields = json.loads(lead["fields_json"])
            # Premium amount overwritten to 6500.00 (last write wins)
            self.assertEqual(fields["premium_amount"], "6500.00")
            # Preferred language was empty in row 2, so it kept old non-null value 'hi'
            self.assertEqual(fields["preferred_language"], "hi")

            # Duplicate counter must reflect 1 merged duplicate
            self.assertEqual(self.db.get_counter("duplicate_lead_rows_merged"), 1)


if __name__ == "__main__":
    unittest.main()
