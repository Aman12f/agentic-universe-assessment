"""
Configuration Module for Interaction Ledger & Reporting Pipeline.

This module centralizes filesystem paths, timezone definitions (IST = UTC+05:30),
and cryptographic salt/pepper keys used across the pipeline for deterministic
PII blind indexing and secure storage at rest.
"""

from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path

# Indian Standard Time (IST): UTC+05:30.
# Per specification, all naive timestamps are interpreted as IST, and all final
# reporting timestamps/dates must be represented in IST.
IST: timezone = timezone(timedelta(hours=5, minutes=30))

# Base directories:
# SOLUTION_DIR points to the directory containing this solution code.
# REPO_ROOT points to the root of the take-home repository.
SOLUTION_DIR: Path = Path(__file__).resolve().parent
REPO_ROOT: Path = SOLUTION_DIR.parent

# Schema & Catalogue File Paths:
# Workflow JSON schemas define accepted fields, required flags, PII flags, and aliases.
# Dispositions CSV defines terminal and success classifications.
SCHEMAS_DIR: Path = REPO_ROOT / "assignment-a-data" / "schemas"
MOTOR_RENEWAL_SCHEMA: Path = SCHEMAS_DIR / "motor_renewal.json"
POLICY_FEEDBACK_SCHEMA: Path = SCHEMAS_DIR / "policy_feedback.json"
DISPOSITIONS_CSV: Path = SCHEMAS_DIR / "dispositions.csv"

# Default SQLite database path for persistent storage across multi-drop ingestions.
DEFAULT_DB_PATH: Path = REPO_ROOT / "solution_ledger.db"

# Secret pepper key used for HMAC-SHA256 blind indexing of phone numbers and
# authenticated PII encryption at rest. In production, this is supplied via an
# environment variable or a secure secrets manager (e.g., AWS KMS / HashiCorp Vault).
PII_PEPPER: str = os.environ.get(
    "PII_PEPPER",
    "agentic-universe-takehome-sde-2026-secret-pepper-key"
)
