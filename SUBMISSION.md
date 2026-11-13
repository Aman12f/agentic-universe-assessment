# Submission

## Candidate
- **Name:** Aman Motghare
- **Assignment chosen:** A (Interaction Ledger & Reporting Pipeline)
- **Time spent (honest estimate):** ~5h

## How to run
```bash
# 1. (Optional) Create and activate virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux / macOS:
source .venv/bin/activate

# 2. Install dependencies (optional, solution runtime uses standard library; needed for pytest)
pip install -r requirements.txt

# 3. Run automated tests
pytest solution/tests
# (Alternatively, run with Python's built-in test runner without any external packages):
python -m unittest discover -s solution/tests

# 4. Ingest data drops
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2

# 5. Generate reports
python -m solution.cli report --out reports
```

## Design

The pipeline is structured into clean, decoupled layers following single-responsibility and append-only ledger principles:

1. **Domain Normalizers (`solution/normalizers.py`):**
   Strictly implements the spec rules:
   - **Header normalization:** lower-casing, trimming, and collapsing whitespace/underscores to a single space, matching schema field names and aliases.
   - **Phone parsing:** strips non-digits, handles `91` (12-digit) and `0` (11-digit) prefixes, enforces 10-digit Indian mobile numbers starting with 6–9, and outputs canonical `+91XXXXXXXXXX`.
   - **Date & Decimal parsing:** day-first multi-format date parser rejecting invalid dates (e.g., `31/02`); currency stripper handling `₹`, `Rs`, `INR`, thousands commas, rounding to 2 decimal places.
   - **Timestamps:** normalizes UTC, offset, and naive timestamps into aware IST (`+05:30`) datetimes.

2. **Cryptographic Protection & PII at Rest (`solution/crypto.py`):**
   - **Blind Indexing (HMAC-SHA256):** To meet Rule 4 (*"Fields marked PII must not be stored in plaintext. You still need to match events to leads by phone number"*), we compute a deterministic HMAC-SHA256 hash using a secret pepper. This `phone_hash` is indexed in SQL, allowing $O(1)$ exact-match queries and foreign key joins without ever storing raw phone numbers.
   - **Encrypted Payload:** Customer names and phone numbers are encrypted at rest using an authenticated CTR stream cipher with random IV and HMAC-SHA256 integrity tags.
   - **Masking:** Human-facing reports output masked numbers: `+91******` followed by the last 4 digits.

3. **Append-Only Interaction Ledger & Database (`solution/db.py`, `solution/event_ingest.py`):**
   - Implemented on SQLite with Write-Ahead Logging (WAL) and atomic transactions.
   - `events_ledger` is strictly append-only. Call overrides create new records and never mutate or delete previous events.
   - Late-arriving lead reconciliation: Unlinked events are quarantined as orphans (`is_orphan = 1`). Whenever a new lead batch arrives, orphan events and supervisor overrides sharing `call_id`s are atomically reconciled.

4. **Reporting Engine (`solution/reporter.py`):**
   - Evaluates call initiation, connectivity, and latest effective dispositions (`disposition.override` > `disposition.set`).
   - Derives lead buckets (`yet_to_reach_out` -> `completed` -> `in_progress_telephony_failed` -> `in_progress_dialed`).
   - Produces the 5 output files (`batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`, `rejections.csv`, `data_quality.json`) adhering strictly to formatting and sort contracts.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| **HMAC-SHA256 Blind Indexing for Phone Matching** | PII cannot be stored in plaintext, yet incoming events and lead duplicates must match by phone. A keyed HMAC hash provides secure deterministic lookups while keeping raw phone numbers encrypted. | Storing plaintext phone numbers (violates Rule 4), or random AES-GCM without an index (would require full-table decrypt on every query). |
| **Zero External Runtime Dependencies** | Implementing the pipeline with Python 3's built-in modules (`sqlite3`, `hashlib`, `hmac`, `csv`, `json`, `re`) ensures 100% portability, zero environment breakage, and fast cold-start performance. | Pandas / SQLAlchemy (heavy dependencies, slower startup, binary compilation requirements). |
| **Schema-Order Field Walk for Rejections** | The spec requires checking fields in schema order for the first failure. E.g., for `motor_renewal`, `customer_name` precedes `phone`. If both are missing/invalid, `missing_required:customer_name` is recorded. | Checking phone validity before name (would produce incorrect rejection reason order). |
| **Row Numbering on Non-Blank Rows Only** | Spec states blank rows are skipped and not counted. Row numbers count 1-based non-blank data rows. | Counting raw file line numbers (would misalign row numbers when blank lines exist). |
| **Optional Field Failure -> Field Warning** | If an optional field is present but invalid (e.g. invalid date or currency string), value is stored as null and a field warning is logged. Defaults apply post-merging. | Rejecting the lead row (violates spec). |
| **Atomic Re-linking of Late-Arriving Leads** | When a lead is inserted, existing orphan events in `events_ledger` with matching `phone_hash` are linked, and `disposition.override` events are linked via `call_id`. | Delaying reconciliation to report time (slower and more prone to state drift). |

## Failure modes considered

- **Malformed JSON / Non-event lines:** Intercepted during event parsing, recorded in `malformed_events` counter, and skipped without crashing.
- **Duplicate events / delivery spikes:** Checked against existing primary keys in `events_ledger`; duplicate deliveries increment `duplicate_events_dropped` and are skipped.
- **Idempotent re-runs:** Ingested files and drops are tracked in `ingested_files`. Re-running `ingest` on any drop is an idempotent no-op that leaves the database and reports identical.
- **Out-of-order events (events before leads):** Stored in `events_ledger` with `is_orphan = 1`. Retroactively linked upon lead arrival.
- **Database crash / process termination:** SQLite WAL mode with transactions (`commit` per file) guarantees consistency.

## Testing

- **Automated Test Suite:** 19 test cases in `solution/tests/` runnable via `pytest` or `python -m unittest`:
  - `test_normalizers.py`: Indian phone normalizer edge cases (prefixes `91`, `0`, invalid prefixes, landlines, formatting), day-first multi-format dates, currency strings, enum casing, IST timestamps.
  - `test_crypto.py`: Deterministic HMAC hashing, phone masking (`+91******XXXX`), PII authenticated encryption/decryption, tamper detection.
  - `test_lead_ingest.py`: Rejection reason schema-order, non-blank row numbering, duplicate lead merging, permanent `batch_id` retention, non-null field overwrites.
  - `test_event_ingest.py`: Malformed JSON handling, duplicate drop handling, orphan isolation, late-arriving lead reconciliation, call override linking.
  - `test_derived_facts.py`: Call counting, effective disposition override precedence, lead bucket evaluation.
- **End-to-End Verification:** Tested multi-drop ingestion and diffed reruns on independent seeds (seed 42 and seed 99) to verify zero diff in report outputs.

## Results

- **Small Dataset (Seed 42):**
  - Leads processed: 2,057 rows (108 rejected, 57 duplicate rows merged, 122 field warnings).
  - Events processed: 8,745 events (8 malformed, 237 duplicate dropped, 19 unknown disposition).
  - Runtime: Total ingestion + report generation completes in **under 4 seconds** on a laptop.
- **Idempotency:** Re-running ingestion of drops produces **byte-identical** reports (`Compare-Object` returned zero diffs across all 5 files).

## If I had another day

1. **Bulk Ingestion via SQLite `executemany` & CTEs:** For scaling to the `--scale large` dataset (140k+ leads, 600k+ events), stream records in chunks of 5,000 using `executemany` with temporary staging tables for bulk index updates.
2. **DuckDB / Parquet Analytics Layer:** For multi-million row analytics, export the append-only ledger to partitioned Parquet files queried via DuckDB.
3. **Async / Worker Queue Pipeline:** In production, decouple file upload notification from ingestion using Celery / temporal workflows with backoff retries and dead-letter queues.
