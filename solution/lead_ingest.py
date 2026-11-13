"""
Lead CSV Ingestion, Validation, Rejection, and Merging Engine.

This module processes incoming lead batch CSV files according to Assignment A rules:
1. Header Mapping: Matches CSV headers against schema field names and aliases.
2. Blank Rows: Completely empty or whitespace rows are skipped and not counted.
3. Row Numbering: 1-based indexing counting only non-blank data rows.
4. Schema-Order Rejection:
   - Evaluates fields strictly in schema order.
   - First failure becomes the single rejection reason ('missing_required:<field>' or 'invalid_phone').
   - Rejected rows are excluded from leads and logged to rejections.csv.
5. Field Warnings: Optional fields present but invalid log a warning and store null.
6. Lead Identity & Merging:
   - Identity is (workflow_id, canonical phone).
   - First valid row permanently assigns batch_id from its file stem.
   - Later valid rows merge by overwriting only non-null fields (last write wins).
7. Late-Arriving Event Reconciliation:
   - Immediately links any prior orphan events in the ledger matching this lead's phone or call_id.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from solution.config import IST
from solution.crypto import encrypt_pii, hash_phone, mask_phone
from solution.db import Database
from solution.normalizers import (
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_phone,
    normalize_string,
)
from solution.schema_loader import SchemaRegistry


class LeadIngestor:
    """
    Handles parsing, validating, normalizing, and persisting lead CSV uploads.
    """

    def __init__(self, db: Database, schema_registry: SchemaRegistry):
        """
        Initializes the LeadIngestor.

        Args:
            db: Database instance for storage.
            schema_registry: Loaded registry containing schemas and catalogues.
        """
        self.db = db
        self.registry = schema_registry

    def ingest_lead_file(self, file_path: Path, drop_name: str) -> None:
        """
        Ingests a single lead CSV file into the database.

        Processing Order:
        - Checks idempotency: if file was already ingested, skips immediately.
        - Maps headers to schema fields.
        - Iterates non-blank data rows (1-indexed).
        - Walks fields in schema order to validate and check required constraints.
        - Rejects row upon first error; otherwise inserts new lead or merges duplicate.
        - Reconciles orphan events in events_ledger that arrived in earlier drops.

        Args:
            file_path: Absolute or relative Path to CSV file.
            drop_name: Name of drop folder containing this file (e.g., 'drop_1').

        Raises:
            ValueError: If workflow schema or reference field cannot be resolved.
        """
        file_path = Path(file_path).resolve()
        batch_id = file_path.stem  # e.g., 'batch_001'
        workflow_id = file_path.parent.name  # directory name represents workflow_id

        schema = self.registry.workflows.get(workflow_id)
        if not schema:
            raise ValueError(f"Unknown workflow schema for workflow_id='{workflow_id}'")

        ref_field = schema.get_reference_field()
        if not ref_field:
            raise ValueError(f"No reference field defined in schema for {workflow_id}")

        with self.db.get_connection() as conn:
            # Check idempotency: skip file if already ingested
            file_key = str(file_path)
            already_ingested = conn.execute(
                "SELECT 1 FROM ingested_files WHERE file_path = ?", (file_key,)
            ).fetchone()
            if already_ingested:
                return

            rows_total = 0
            rows_rejected = 0
            duplicates_merged = 0

            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                try:
                    raw_headers = next(reader)
                except StopIteration:
                    # Empty file (0 lines)
                    return

                col_mapping = schema.map_headers(raw_headers)

                row_number = 0
                for raw_row in reader:
                    # Skip completely blank rows (all cells empty or whitespace)
                    # Spec: "Blank rows are skipped and not counted."
                    if not raw_row or all(c.strip() == "" for c in raw_row):
                        continue

                    # 1-based index counting only non-blank data rows
                    row_number += 1
                    rows_total += 1

                    # Extract raw trimmed values into a field-keyed dictionary
                    field_values: Dict[str, Optional[str]] = {}
                    for col_idx, field_def in col_mapping.items():
                        if col_idx < len(raw_row):
                            val = raw_row[col_idx].strip()
                            field_values[field_def.name] = val if val != "" else None
                        else:
                            field_values[field_def.name] = None

                    # Walk fields in strict schema order to determine validity
                    rejection_reason = None
                    cleaned_values: Dict[str, Any] = {}
                    row_warnings: List[str] = []

                    for f_def in schema.fields:
                        fname = f_def.name
                        raw_val = field_values.get(fname)

                        # Check required constraint
                        if f_def.required and raw_val is None:
                            rejection_reason = f"missing_required:{fname}"
                            break

                        # Missing optional field: stores None
                        if raw_val is None:
                            cleaned_values[fname] = None
                            continue

                        # Field is present: validate according to data type
                        if f_def.type == "phone":
                            valid, norm_phone = normalize_phone(raw_val)
                            if not valid:
                                rejection_reason = "invalid_phone"
                                break
                            cleaned_values[fname] = norm_phone

                        elif f_def.type == "string":
                            valid, norm_s = normalize_string(raw_val)
                            cleaned_values[fname] = norm_s

                        elif f_def.type == "date":
                            valid, norm_d = normalize_date(raw_val)
                            if not valid:
                                cleaned_values[fname] = None
                                if not f_def.required:
                                    row_warnings.append(fname)
                                else:
                                    rejection_reason = f"invalid_date:{fname}"
                                    break
                            else:
                                cleaned_values[fname] = norm_d

                        elif f_def.type == "decimal":
                            valid, norm_dec = normalize_decimal(raw_val)
                            if not valid:
                                cleaned_values[fname] = None
                                if not f_def.required:
                                    row_warnings.append(fname)
                                else:
                                    rejection_reason = f"invalid_decimal:{fname}"
                                    break
                            else:
                                cleaned_values[fname] = norm_dec

                        elif f_def.type == "enum":
                            valid, norm_e = normalize_enum(raw_val, f_def.values)
                            if not valid:
                                cleaned_values[fname] = None
                                if not f_def.required:
                                    row_warnings.append(fname)
                                else:
                                    rejection_reason = f"invalid_enum:{fname}"
                                    break
                            else:
                                cleaned_values[fname] = norm_e

                    # If row failed validation, log to rejections and proceed to next row
                    if rejection_reason:
                        rows_rejected += 1
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO rejections 
                            (workflow_id, batch_id, row_number, reason, drop_name)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (workflow_id, batch_id, row_number, rejection_reason, drop_name)
                        )
                        continue

                    # Non-rejected row: record any field warnings for invalid optional values
                    for w_fname in row_warnings:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO field_warnings
                            (workflow_id, batch_id, row_number, field_name, drop_name)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (workflow_id, batch_id, row_number, w_fname, drop_name)
                        )

                    # Lead Identity: (workflow_id, canonical phone)
                    canonical_phone = cleaned_values["phone"]
                    p_hash = hash_phone(canonical_phone)
                    p_masked = mask_phone(canonical_phone)
                    ext_ref = str(cleaned_values.get(ref_field.name) or "")

                    # Separate PII fields (to encrypt) and non-PII fields
                    non_pii_fields: Dict[str, Any] = {}
                    pii_fields: Dict[str, Any] = {}
                    for f_def in schema.fields:
                        val = cleaned_values.get(f_def.name)
                        if f_def.pii:
                            pii_fields[f_def.name] = val
                        else:
                            non_pii_fields[f_def.name] = val

                    # Check if lead identity already exists in database
                    existing = conn.execute(
                        "SELECT id, batch_id, fields_json, pii_payload_enc FROM leads WHERE workflow_id = ? AND phone_hash = ?",
                        (workflow_id, p_hash)
                    ).fetchone()

                    now_iso = datetime.now(IST).isoformat()

                    if existing is None:
                        # Apply schema defaults to fields that are still null
                        for f_def in schema.fields:
                            if f_def.default and not f_def.pii and non_pii_fields.get(f_def.name) is None:
                                non_pii_fields[f_def.name] = f_def.default

                        # New Lead Creation:
                        # Permanent batch_id is established from this first file stem.
                        pii_enc = encrypt_pii(json.dumps(pii_fields))
                        cursor = conn.execute(
                            """
                            INSERT INTO leads 
                            (workflow_id, phone_hash, phone_masked, batch_id, external_ref,
                             fields_json, pii_payload_enc, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                workflow_id,
                                p_hash,
                                p_masked,
                                batch_id,
                                ext_ref,
                                json.dumps(non_pii_fields),
                                pii_enc,
                                now_iso,
                                now_iso,
                            )
                        )
                        new_lead_id = cursor.lastrowid

                        # Retroactive Orphan Event Linking:
                        # If events arrived in an earlier drop before this lead, link them now!
                        conn.execute(
                            """
                            UPDATE events_ledger
                            SET lead_id = ?, is_orphan = 0
                            WHERE workflow_id = ? AND phone_hash = ? AND is_orphan = 1
                            """,
                            (new_lead_id, workflow_id, p_hash)
                        )
                        # Cascade lead_id to any disposition.override events sharing call_ids
                        conn.execute(
                            """
                            UPDATE events_ledger
                            SET lead_id = ?, is_orphan = 0
                            WHERE call_id IN (
                                SELECT call_id FROM events_ledger WHERE lead_id = ?
                            ) AND is_orphan = 1
                            """,
                            (new_lead_id, new_lead_id)
                        )
                    else:
                        # Merged Duplicate:
                        # Spec: "Each later valid row for the same identity counts as one merged
                        # duplicate and overwrites only the fields where its value is non-null
                        # (last write wins). Original batch_id remains permanent."
                        duplicates_merged += 1
                        old_non_pii = json.loads(existing["fields_json"])

                        # Overwrite only non-null values
                        for k, v in non_pii_fields.items():
                            if v is not None:
                                old_non_pii[k] = v

                        # Apply schema defaults to fields that are still null
                        for f_def in schema.fields:
                            if f_def.default and not f_def.pii and old_non_pii.get(f_def.name) is None:
                                old_non_pii[f_def.name] = f_def.default

                        # Update external_ref if provided
                        new_ext_ref = ext_ref if ext_ref else existing["external_ref"]

                        # Handle PII updates if any new PII value is non-null
                        if any(v is not None for v in pii_fields.values()):
                            from solution.crypto import decrypt_pii
                            old_pii_raw = decrypt_pii(existing["pii_payload_enc"])
                            old_pii = json.loads(old_pii_raw) if old_pii_raw else {}
                            for k, v in pii_fields.items():
                                if v is not None:
                                    old_pii[k] = v
                            new_pii_enc = encrypt_pii(json.dumps(old_pii))
                        else:
                            new_pii_enc = existing["pii_payload_enc"]

                        conn.execute(
                            """
                            UPDATE leads
                            SET external_ref = ?, fields_json = ?, pii_payload_enc = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (new_ext_ref, json.dumps(old_non_pii), new_pii_enc, now_iso, existing["id"])
                        )

            # Persist file ingestion record for idempotency
            now_str = datetime.now(IST).isoformat()
            conn.execute(
                """
                INSERT INTO ingested_files 
                (file_path, drop_name, file_type, total_records, malformed_or_rejected, duplicates, ingested_at)
                VALUES (?, ?, 'leads', ?, ?, ?, ?)
                """,
                (file_key, drop_name, rows_total, rows_rejected, duplicates_merged, now_str)
            )

            # Update global counters
            self.db.increment_counter(conn, "lead_rows_total", rows_total)
            self.db.increment_counter(conn, "duplicate_lead_rows_merged", duplicates_merged)
            conn.commit()
