"""
Interaction Ledger & Reporting Pipeline Package (Assignment A).

This package implements an end-to-end data pipeline for:
- Ingesting messy lead CSV files across multiple campaigns (motor_renewal, policy_feedback).
- Normalizing headers, Indian phone numbers, day-first dates, and currency values.
- Enforcing PII protection at rest via HMAC-SHA256 blind indexing and authenticated encryption.
- Managing an append-only call interaction ledger.
- Producing 5 standardized business and data quality reports.
"""

__version__ = "1.0.0"
