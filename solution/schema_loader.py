"""
Schema and Catalogue Loader Module.

This module loads:
1. Workflow Lead Schemas (JSON files under `schemas/`):
   - Field definitions: name, type, required status, PII flag, reference flag, aliases, defaults.
   - Dynamic header mapping logic using normalized aliases.
2. Disposition Catalogue (`dispositions.csv`):
   - Classified call outcomes, labels, terminal flags (`is_terminal`), and success flags (`is_success`).
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from solution.config import DISPOSITIONS_CSV, SCHEMAS_DIR
from solution.normalizers import normalize_header


@dataclass
class FieldDef:
    """
    Metadata definition for a single field in a workflow's lead schema.

    Attributes:
        name: Canonical field name (e.g. 'customer_name', 'phone', 'policy_number').
        type: Data type ('string', 'phone', 'date', 'decimal', 'enum').
        required: True if row must be rejected when this field is missing.
        pii: True if this field contains Personally Identifiable Information and must not be stored in plaintext.
        reference: True if this field serves as external_ref in client reports.
        aliases: List of alternative header names used in raw client CSVs.
        values: Allowed string values if type is 'enum'.
        default: Fallback value applied after merging if field remains null.
    """
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: List[str] = field(default_factory=list)
    values: List[str] = field(default_factory=list)
    default: Optional[str] = None


@dataclass
class WorkflowSchema:
    """
    Container for a workflow's complete schema configuration.

    Attributes:
        workflow_id: Unique campaign workflow identifier (e.g., 'motor_renewal', 'policy_feedback').
        description: Human-readable workflow description.
        fields: Ordered list of FieldDef instances.
    """
    workflow_id: str
    description: str
    fields: List[FieldDef]

    def get_reference_field(self) -> Optional[FieldDef]:
        """
        Retrieves the field marked with `"reference": true` (e.g. policy_number or order_id).
        Used to populate the `external_ref` column in `lead_report.csv`.
        """
        for f in self.fields:
            if f.reference:
                return f
        return None

    def map_headers(self, raw_headers: List[str]) -> Dict[int, FieldDef]:
        """
        Maps CSV column indices to schema FieldDefs based on normalized names and aliases.

        Spec Rule:
            "to normalise a header, lower-case it, trim it, and replace every run of '_'
             or whitespace with a single space. Match the result against the field name
             or any of its aliases (normalised the same way). Ignore columns that match nothing."

        Args:
            raw_headers: List of header strings as read from the CSV's first line.

        Returns:
            Dictionary mapping integer column index -> matching FieldDef.
        """
        mapping: Dict[int, FieldDef] = {}
        for idx, h in enumerate(raw_headers):
            norm_h = normalize_header(h)
            for f in self.fields:
                # Direct match against canonical field name
                if norm_h == normalize_header(f.name):
                    mapping[idx] = f
                    break
                # Match against registered aliases
                matched_alias = False
                for a in f.aliases:
                    if norm_h == normalize_header(a):
                        mapping[idx] = f
                        matched_alias = True
                        break
                if matched_alias:
                    break
        return mapping


@dataclass
class DispositionInfo:
    """
    Information regarding a classified call outcome from the disposition catalogue.

    Attributes:
        workflow_id: Campaign workflow identifier.
        code: Machine-readable disposition code (e.g. 'renewed', 'callback_requested').
        label: Human-readable description (e.g. 'Policy renewed').
        is_terminal: True if this outcome terminates further outreach for this lead.
        is_success: True if this outcome represents a positive business conversion.
    """
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


class SchemaRegistry:
    """
    Registry that discovers, parses, and provides lookups for all schemas and disposition catalogues.
    """

    def __init__(self, schemas_dir: Path = SCHEMAS_DIR, dispositions_csv: Path = DISPOSITIONS_CSV):
        """
        Initializes the schema registry.

        Args:
            schemas_dir: Directory containing workflow JSON schema files.
            dispositions_csv: Path to dispositions.csv file.
        """
        self.schemas_dir = schemas_dir
        self.dispositions_csv = dispositions_csv
        self.workflows: Dict[str, WorkflowSchema] = {}
        self.dispositions: Dict[Tuple[str, str], DispositionInfo] = {}
        self.load()

    def load(self) -> None:
        """
        Loads all workflow JSON schemas and the dispositions catalogue into memory.
        """
        # Load lead schemas from schemas/*.json
        for json_file in self.schemas_dir.glob("*.json"):
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            wf_id = data["workflow_id"]
            field_defs = []
            for fd in data.get("fields", []):
                field_defs.append(FieldDef(
                    name=fd["name"],
                    type=fd["type"],
                    required=fd.get("required", False),
                    pii=fd.get("pii", False),
                    reference=fd.get("reference", False),
                    aliases=fd.get("aliases", []),
                    values=fd.get("values", []),
                    default=fd.get("default"),
                ))
            self.workflows[wf_id] = WorkflowSchema(
                workflow_id=wf_id,
                description=data.get("description", ""),
                fields=field_defs
            )

        # Load dispositions from dispositions.csv
        if self.dispositions_csv.exists():
            with open(self.dispositions_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    wf = row["workflow_id"].strip()
                    code = row["code"].strip()
                    label = row["label"].strip()
                    is_term = row["is_terminal"].strip().lower() == "true"
                    is_succ = row["is_success"].strip().lower() == "true"
                    self.dispositions[(wf, code)] = DispositionInfo(
                        workflow_id=wf,
                        code=code,
                        label=label,
                        is_terminal=is_term,
                        is_success=is_succ
                    )

    def is_known_disposition(self, workflow_id: str, code: str) -> bool:
        """
        Checks whether a disposition code is recognized in the catalogue for the given workflow.

        Spec Rule:
            "Unknown disposition: a disposition.* event whose code is not in dispositions.csv
             for that workflow -> count it and ignore it."
        """
        return (workflow_id, code) in self.dispositions

    def get_disposition(self, workflow_id: str, code: str) -> Optional[DispositionInfo]:
        """
        Retrieves disposition metadata (terminal / success flags) for a given workflow and code.
        """
        return self.dispositions.get((workflow_id, code))
