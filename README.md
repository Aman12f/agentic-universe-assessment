# Agentic Universe — Interaction Ledger & Reporting Pipeline (Assignment A)

A robust, idempotent, append-only data pipeline for outbound voice campaigns across India & APAC:
- Ingests messy, multi-format lead uploads and telephony event streams.
- Protects PII at rest using deterministic HMAC-SHA256 blind indexing and authenticated encryption.
- Dynamically reconciles late-arriving leads and out-of-order call events.
- Generates 5 verified contract reports with 100% idempotency.

---

## Quickstart

### 1. (Optional) Virtual Environment Setup
```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux / macOS:
source .venv/bin/activate

# Install test runner:
pip install -r requirements.txt
```

### 2. Run Automated Tests
```bash
# Run with Python's built-in test runner (no external dependencies):
python -m unittest discover -s solution/tests

# Or with pytest:
pytest solution/tests
```

### 3. Run Pipeline Ingestion & Reporting
```bash
# 1. Ingest drops in sequence
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2

# 2. Generate final reports
python -m solution.cli report --out reports
```

---

## Project Structure

```
agentic-universe-assessment/
├── solution/               # Complete implementation & test suite
│   ├── cli.py              # CLI entrypoint (ingest, report)
│   ├── config.py           # Paths, IST timezone, encryption keys
│   ├── crypto.py           # HMAC-SHA256 blind index & PII encryption
│   ├── normalizers.py      # Phone, date, decimal, enum, header sanitizers
│   ├── schema_loader.py    # Workflow JSON schemas & disposition catalogue
│   ├── db.py               # SQLite schema, WAL mode, transaction management
│   ├── lead_ingest.py      # Lead CSV parser, rejection engine, duplicate merging
│   ├── event_ingest.py     # Event JSONL parser, deduplication, orphan reconciliation
│   ├── reporter.py         # Metrics calculation & 5 report generators
│   └── tests/              # 19 automated unit and integration tests
├── assignment-a-data/      # Schemas and synthetic data generator
│   ├── schemas/            # motor_renewal.json, policy_feedback.json, dispositions.csv
│   └── tools/              # generate_data.py
├── SUBMISSION.md           # Architecture, design decisions, trade-offs, and benchmarks
├── AI_USAGE.md             # AI usage disclosure
├── requirements.txt        # Optional test dependencies (pytest)
└── .gitignore              # Ignores generated data, databases, and caches
```

---

## Documentation Links

- [SUBMISSION.md](SUBMISSION.md) — Detailed architecture write-up, database schema, PII design, decisions & trade-offs, failure modes, and benchmarks.
- [AI_USAGE.md](AI_USAGE.md) — Transparent AI usage disclosure.
- [GLOSSARY.md](docs/GLOSSARY.md) — Domain vocabulary (Tenant, Workflow, Lead, Batch, Disposition, Overrides, Ledger).
