"""
Command-Line Interface (CLI) for the Interaction Ledger & Reporting Pipeline.

Provides the two standard CLI commands mandated by Assignment A:
    1. Ingestion:
       python -m solution.cli ingest <drop_path>
       Example:
       python -m solution.cli ingest data/drop_1

    2. Reporting:
       python -m solution.cli report --out <output_directory>
       Example:
       python -m solution.cli report --out reports
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Tuple

from solution.config import DEFAULT_DB_PATH
from solution.db import Database
from solution.event_ingest import EventIngestor
from solution.lead_ingest import LeadIngestor
from solution.reporter import ReportGenerator
from solution.schema_loader import SchemaRegistry


def get_pipeline(
    db_path: Path = DEFAULT_DB_PATH,
) -> Tuple[Database, SchemaRegistry, LeadIngestor, EventIngestor, ReportGenerator]:
    """
    Factory helper that instantiates and connects all pipeline components.

    Args:
        db_path: Path to target SQLite database file.

    Returns:
        Tuple of (db, registry, lead_ingestor, event_ingestor, reporter).
    """
    db = Database(db_path)
    registry = SchemaRegistry()
    lead_ingestor = LeadIngestor(db, registry)
    event_ingestor = EventIngestor(db, registry)
    reporter = ReportGenerator(db)
    return db, registry, lead_ingestor, event_ingestor, reporter


def handle_ingest(drop_path_str: str, db_path: Path) -> None:
    """
    Discovers and executes ingestion for all lead and event files within a drop directory.

    Order of Operations:
    1. Discovers lead files in `leads/<workflow_id>/*.csv` and sorts them in filename order.
    2. Ingests all lead files first (updating lead identity store and permanent batch_ids).
    3. Discovers event files in `events/*.jsonl` and sorts them in filename order.
    4. Ingests all event files into the append-only ledger and links them to leads.

    Args:
        drop_path_str: Path string pointing to drop folder (e.g., 'data/drop_1') or specific file.
        db_path: Destination SQLite database path.
    """
    drop_path = Path(drop_path_str).resolve()
    if not drop_path.exists():
        print(f"Error: Path '{drop_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    db, registry, lead_ingestor, event_ingestor, _ = get_pipeline(db_path)
    drop_name = drop_path.name if drop_path.is_dir() else drop_path.parent.name

    # Discover lead CSV files
    lead_files = []
    leads_dir = drop_path / "leads" if drop_path.is_dir() else None
    if leads_dir and leads_dir.exists():
        # Iterate over workflow subdirectories (e.g. motor_renewal, policy_feedback)
        for wf_dir in sorted(leads_dir.iterdir()):
            if wf_dir.is_dir():
                for f in sorted(wf_dir.glob("*.csv")):
                    lead_files.append(f)
    elif drop_path.is_file() and drop_path.suffix == ".csv":
        lead_files.append(drop_path)

    # Discover event JSONL files
    event_files = []
    events_dir = drop_path / "events" if drop_path.is_dir() else None
    if events_dir and events_dir.exists():
        for f in sorted(events_dir.glob("*.jsonl")):
            event_files.append(f)
    elif drop_path.is_file() and drop_path.suffix == ".jsonl":
        event_files.append(drop_path)

    print(f"Ingesting drop '{drop_name}' from {drop_path}:")
    print(f"  Found {len(lead_files)} lead files, {len(event_files)} event files.")

    # 1. Ingest leads first (in filename order per spec)
    for lf in lead_files:
        rel_name = lf.relative_to(drop_path) if drop_path.is_dir() else lf.name
        print(f"  Ingesting leads: {rel_name} ...")
        lead_ingestor.ingest_lead_file(lf, drop_name=drop_name)

    # 2. Ingest events second (in filename order per spec)
    for ef in event_files:
        rel_name = ef.relative_to(drop_path) if drop_path.is_dir() else ef.name
        print(f"  Ingesting events: {rel_name} ...")
        event_ingestor.ingest_event_file(ef, drop_name=drop_name)

    print(f"Successfully finished ingestion of '{drop_name}'.")


def handle_report(out_dir_str: str, db_path: Path) -> None:
    """
    Triggers generation of the 5 deliverable reporting files to the designated directory.

    Args:
        out_dir_str: Destination directory path string.
        db_path: Source SQLite database path.
    """
    out_dir = Path(out_dir_str).resolve()
    db, _, _, _, reporter = get_pipeline(db_path)

    print(f"Generating reports to {out_dir} ...")
    generated = reporter.generate_all(out_dir)
    print("Reports generated successfully:")
    for name, path in generated.items():
        print(f"  - {name}: {path}")


def main() -> None:
    """
    Main CLI entrypoint parsing arguments and dispatching to ingest or report handlers.
    """
    parser = argparse.ArgumentParser(
        prog="interaction-pipeline",
        description="Agentic Universe Take-Home: Interaction Ledger & Reporting Pipeline (Assignment A)",
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=str(DEFAULT_DB_PATH),
        help=f"Path to SQLite database (default: {DEFAULT_DB_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Ingest subcommand
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a data drop folder or file")
    ingest_parser.add_argument("path", help="Path to drop directory (e.g., data/drop_1)")

    # Report subcommand
    report_parser = subparsers.add_parser("report", help="Generate final reporting files")
    report_parser.add_argument("--out", required=True, help="Directory to save report files")

    args = parser.parse_args()
    db_path = Path(args.db_path)

    if args.command == "ingest":
        handle_ingest(args.path, db_path)
    elif args.command == "report":
        handle_report(args.out, db_path)


if __name__ == "__main__":
    main()
