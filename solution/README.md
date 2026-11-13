# Solution: Interaction Ledger & Reporting Pipeline (Assignment A)

This folder contains the complete implementation of Assignment A: Interaction Ledger & Reporting Pipeline.

## Architecture & Structure

- `solution/cli.py` & `solution/__main__.py`: Unified CLI interface (`ingest` and `report`).
- `solution/config.py`: Environment configuration, timezone (IST), pepper keys, and paths.
- `solution/crypto.py`: PII masking (`+91******XXXX`), deterministic HMAC-SHA256 blind indexing for fast database lookups, and authenticated CTR stream cipher for PII encryption at rest.
- `solution/normalizers.py`: Header fuzzy matching, Indian mobile number parsing (`+91XXXXXXXXXX`), day-first multi-format date parsing, currency/decimal cleaning, and IST timezone conversion.
- `solution/schema_loader.py`: Schema registry for workflow JSON schemas and the disposition catalogue (`dispositions.csv`).
- `solution/db.py`: SQLite database layer with WAL mode, strict indexes, and transactional idempotency.
- `solution/lead_ingest.py`: Lead CSV parser, schema-ordered rejection engine, non-blank row numbering, duplicate merging (retaining original `batch_id` and overwriting non-null fields), and retroactive orphan event linking.
- `solution/event_ingest.py`: Event JSONL parser, deduplication, orphan isolation, unknown disposition filtering, and append-only ledger storage.
- `solution/reporter.py`: Aggregation engine producing the 5 contract output files (`batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`, `rejections.csv`, `data_quality.json`).
- `solution/tests/`: Comprehensive unit and integration test suite runnable with `pytest` or `python -m unittest`.
