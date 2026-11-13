"""
Automated Test Suite for Interaction Ledger & Reporting Pipeline.

Covers:
- Normalizers: Phone, date, decimal, enum, header, and timestamp parsing.
- Crypto: Masking, blind indexing (HMAC-SHA256), and PII encryption/tamper resistance.
- Lead Ingestion: Schema-order rejection, blank row handling, duplicate merging.
- Event Ingestion: Deduplication, orphan isolation, late lead linking, overrides.
- Derived Metrics: Effective disposition resolution and lead bucketing logic.
"""
