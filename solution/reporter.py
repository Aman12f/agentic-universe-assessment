"""
Report Generator Module Implementing the Exact Output Contract.

This module aggregates the interaction ledger and lead stores to produce the five
deliverable report files:
1. batch_summary.csv: Lead counts grouped by workflow and batch across buckets.
2. daily_overview.csv: Daily campaign calling metrics (calls, connectivity_rate, success_leads).
3. lead_report.csv: Granular per-lead call history, masked phone, current disposition, and bucket.
4. rejections.csv: Audit log of discarded lead rows and reason (strictly zero PII).
5. data_quality.json: Comprehensive pipeline quality counters.

All sorting, formatting, rounding, and boolean representations strictly conform to the spec.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from solution.db import Database


@dataclass
class EffectiveDisposition:
    """
    Resolved call outcome metadata.

    Attributes:
        code: Disposition code string.
        is_terminal: True if outcome terminates campaign calling.
        is_success: True if outcome constitutes a business conversion.
    """
    code: str
    is_terminal: bool
    is_success: bool


@dataclass
class CallInfo:
    """
    Aggregated interaction facts for a single call_id.

    Spec Rules:
        - A call is counted only if its call.initiated event is present and linked.
        - The call's date is the IST date of call.initiated.
        - Connected if call.connected exists.
        - Effective disposition: latest override by occurred_at, else latest set.
    """
    call_id: str
    lead_id: int
    workflow_id: str
    initiated_at_ist: str
    initiated_at_ts: float
    call_date: str
    connected: bool
    telephony_failed: bool
    effective_disp: Optional[EffectiveDisposition] = None


class ReportGenerator:
    """
    Engine that queries the ledger and produces the 5 reporting files.
    """

    def __init__(self, db: Database):
        """
        Initializes the ReportGenerator.

        Args:
            db: Database instance containing ledger and lead records.
        """
        self.db = db

    def generate_all(self, output_dir: Path) -> Dict[str, Path]:
        """
        Executes aggregations and writes all 5 reports to `output_dir`.

        Args:
            output_dir: Destination directory for report CSVs and JSON.

        Returns:
            Dictionary mapping report name -> generated Path.
        """
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        with self.db.get_connection() as conn:
            # 1. Fetch all calls that have a linked call.initiated event
            # Spec: "A call is counted only if its call.initiated event is present (and linked)."
            initiated_rows = conn.execute(
                """
                SELECT call_id, lead_id, workflow_id, occurred_at_ist, occurred_at_ts,
                       substr(occurred_at_ist, 1, 10) as call_date
                FROM events_ledger
                WHERE event_type = 'call.initiated' AND lead_id IS NOT NULL
                ORDER BY occurred_at_ts ASC
                """
            ).fetchall()

            # Connected call IDs: calls with a call.connected event
            connected_call_ids: Set[str] = {
                r["call_id"]
                for r in conn.execute(
                    "SELECT DISTINCT call_id FROM events_ledger WHERE event_type = 'call.connected' AND lead_id IS NOT NULL"
                ).fetchall()
            }

            # Telephony failed call IDs: calls with a call.telephony_failed event
            failed_call_ids: Set[str] = {
                r["call_id"]
                for r in conn.execute(
                    "SELECT DISTINCT call_id FROM events_ledger WHERE event_type = 'call.telephony_failed' AND lead_id IS NOT NULL"
                ).fetchall()
            }

            # Disposition events for effective disposition resolution
            # Ordered by occurred_at_ts ASC, rowid ASC
            disp_rows = conn.execute(
                """
                SELECT call_id, event_type, disposition_code, is_terminal, is_success, occurred_at_ts
                FROM events_ledger
                WHERE disposition_code IS NOT NULL AND lead_id IS NOT NULL
                ORDER BY occurred_at_ts ASC, rowid ASC
                """
            ).fetchall()

            # Map call_id -> latest override, or latest set
            # Spec: "A call's effective disposition is the code of its latest disposition.override
            # by occurred_at. If it has no override, it's the latest disposition.set."
            call_overrides: Dict[str, EffectiveDisposition] = {}
            call_sets: Dict[str, EffectiveDisposition] = {}

            for r in disp_rows:
                cid = r["call_id"]
                etype = r["event_type"]
                edisp = EffectiveDisposition(
                    code=r["disposition_code"],
                    is_terminal=bool(r["is_terminal"]),
                    is_success=bool(r["is_success"]),
                )
                if etype == "disposition.override":
                    call_overrides[cid] = edisp
                elif etype == "disposition.set":
                    call_sets[cid] = edisp

            # Construct structured CallInfo objects
            calls_by_lead: Dict[int, List[CallInfo]] = {}
            calls_by_date_wf: Dict[Tuple[str, str], List[CallInfo]] = {}  # (workflow_id, date) -> [CallInfo]

            for r in initiated_rows:
                cid = r["call_id"]
                lid = r["lead_id"]
                wfid = r["workflow_id"]
                cdate = r["call_date"]

                # Effective disposition: latest override takes precedence over latest set
                eff = call_overrides.get(cid) or call_sets.get(cid)

                cinfo = CallInfo(
                    call_id=cid,
                    lead_id=lid,
                    workflow_id=wfid,
                    initiated_at_ist=r["occurred_at_ist"],
                    initiated_at_ts=r["occurred_at_ts"],
                    call_date=cdate,
                    connected=(cid in connected_call_ids),
                    telephony_failed=(cid in failed_call_ids),
                    effective_disp=eff,
                )

                calls_by_lead.setdefault(lid, []).append(cinfo)
                calls_by_date_wf.setdefault((wfid, cdate), []).append(cinfo)

            # Load all leads sorted by workflow_id ASC, external_ref ASC per contract
            leads_rows = conn.execute(
                """
                SELECT id, workflow_id, batch_id, external_ref, phone_masked
                FROM leads
                ORDER BY workflow_id ASC, external_ref ASC
                """
            ).fetchall()

            # Process per-lead derived properties and bucket categorization
            lead_results = []
            batch_counts: Dict[Tuple[str, str], Dict[str, int]] = {}

            for l in leads_rows:
                lid = l["id"]
                wfid = l["workflow_id"]
                bid = l["batch_id"]
                ext_ref = l["external_ref"]
                p_masked = l["phone_masked"]

                l_calls = calls_by_lead.get(lid, [])
                l_calls.sort(key=lambda c: c.initiated_at_ts)

                total_calls = len(l_calls)
                connected_calls = sum(1 for c in l_calls if c.connected)
                last_call_at = l_calls[-1].initiated_at_ist if l_calls else ""

                # Current disposition: effective disposition of most recent call (by initiated time) with one
                current_disp = ""
                for c in reversed(l_calls):
                    if c.effective_disp is not None:
                        current_disp = c.effective_disp.code
                        break

                ever_success = any(c.effective_disp and c.effective_disp.is_success for c in l_calls)

                # Lead Bucket Resolution Order (first matching rule wins):
                # 1. yet_to_reach_out: no calls
                # 2. completed: any call's effective disposition is terminal
                # 3. in_progress_telephony_failed: every call has a call.telephony_failed event
                # 4. in_progress_dialed: everything else
                if total_calls == 0:
                    bucket = "yet_to_reach_out"
                elif any(c.effective_disp and c.effective_disp.is_terminal for c in l_calls):
                    bucket = "completed"
                elif all(c.telephony_failed for c in l_calls):
                    bucket = "in_progress_telephony_failed"
                else:
                    bucket = "in_progress_dialed"

                lead_results.append({
                    "workflow_id": wfid,
                    "external_ref": ext_ref,
                    "batch_id": bid,
                    "phone_masked": p_masked,
                    "bucket": bucket,
                    "total_calls": total_calls,
                    "connected_calls": connected_calls,
                    "last_call_at": last_call_at,
                    "current_disposition": current_disp,
                    "ever_success": "true" if ever_success else "false",
                })

                # Aggregate counts for batch_summary.csv
                b_key = (wfid, bid)
                if b_key not in batch_counts:
                    batch_counts[b_key] = {
                        "total_leads": 0,
                        "yet_to_reach_out": 0,
                        "in_progress_telephony_failed": 0,
                        "in_progress_dialed": 0,
                        "completed": 0,
                    }
                batch_counts[b_key]["total_leads"] += 1
                batch_counts[b_key][bucket] += 1

            # -------------------------------------------------------------
            # Output 1: batch_summary.csv (sorted by workflow_id, batch_id)
            # -------------------------------------------------------------
            batch_summary_path = output_dir / "batch_summary.csv"
            with open(batch_summary_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "workflow_id",
                    "batch_id",
                    "total_leads",
                    "yet_to_reach_out",
                    "in_progress_telephony_failed",
                    "in_progress_dialed",
                    "completed",
                ])
                for (wfid, bid) in sorted(batch_counts.keys()):
                    bc = batch_counts[(wfid, bid)]
                    writer.writerow([
                        wfid,
                        bid,
                        bc["total_leads"],
                        bc["yet_to_reach_out"],
                        bc["in_progress_telephony_failed"],
                        bc["in_progress_dialed"],
                        bc["completed"],
                    ])

            # -------------------------------------------------------------
            # Output 2: daily_overview.csv (sorted by workflow_id, date)
            # -------------------------------------------------------------
            daily_overview_path = output_dir / "daily_overview.csv"
            with open(daily_overview_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "workflow_id",
                    "date",
                    "total_calls",
                    "connected_calls",
                    "connectivity_rate",
                    "success_leads",
                ])
                for (wfid, dt_str) in sorted(calls_by_date_wf.keys()):
                    d_calls = calls_by_date_wf[(wfid, dt_str)]
                    total_c = len(d_calls)
                    conn_c = sum(1 for c in d_calls if c.connected)
                    # Exactly 4 decimals per contract
                    conn_rate = f"{conn_c / total_c:.4f}" if total_c > 0 else "0.0000"

                    # Distinct leads with at least one call on that date with effective disposition is_success=true
                    success_lead_ids = {
                        c.lead_id for c in d_calls if c.effective_disp and c.effective_disp.is_success
                    }

                    writer.writerow([
                        wfid,
                        dt_str,
                        total_c,
                        conn_c,
                        conn_rate,
                        len(success_lead_ids),
                    ])

            # -------------------------------------------------------------
            # Output 3: lead_report.csv (sorted by workflow_id, external_ref)
            # -------------------------------------------------------------
            lead_report_path = output_dir / "lead_report.csv"
            with open(lead_report_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "workflow_id",
                    "external_ref",
                    "batch_id",
                    "phone_masked",
                    "bucket",
                    "total_calls",
                    "connected_calls",
                    "last_call_at",
                    "current_disposition",
                    "ever_success",
                ])
                for lr in lead_results:
                    writer.writerow([
                        lr["workflow_id"],
                        lr["external_ref"],
                        lr["batch_id"],
                        lr["phone_masked"],
                        lr["bucket"],
                        lr["total_calls"],
                        lr["connected_calls"],
                        lr["last_call_at"],
                        lr["current_disposition"],
                        lr["ever_success"],
                    ])

            # -------------------------------------------------------------
            # Output 4: rejections.csv (sorted by workflow_id, batch_id, row_number)
            # -------------------------------------------------------------
            rejections_path = output_dir / "rejections.csv"
            rejections_rows = conn.execute(
                """
                SELECT workflow_id, batch_id, row_number, reason
                FROM rejections
                ORDER BY workflow_id ASC, batch_id ASC, row_number ASC
                """
            ).fetchall()

            with open(rejections_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["workflow_id", "batch_id", "row_number", "reason"])
                for r in rejections_rows:
                    writer.writerow([
                        r["workflow_id"],
                        r["batch_id"],
                        r["row_number"],
                        r["reason"],
                    ])

            # -------------------------------------------------------------
            # Output 5: data_quality.json
            # -------------------------------------------------------------
            quality_path = output_dir / "data_quality.json"

            lead_rows_total = self.db.get_counter("lead_rows_total")
            lead_rows_rejected = len(rejections_rows)

            rejections_by_reason: Dict[str, int] = {}
            for r in rejections_rows:
                reason = r["reason"]
                rejections_by_reason[reason] = rejections_by_reason.get(reason, 0) + 1

            dup_leads_merged = self.db.get_counter("duplicate_lead_rows_merged")

            field_warnings_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM field_warnings"
            ).fetchone()["cnt"]

            events_total = self.db.get_counter("events_total")
            malformed_events = self.db.get_counter("malformed_events")
            dup_events_dropped = self.db.get_counter("duplicate_events_dropped")

            orphan_events = conn.execute(
                "SELECT COUNT(*) as cnt FROM events_ledger WHERE is_orphan = 1"
            ).fetchone()["cnt"]

            unknown_disp_events = self.db.get_counter("unknown_disposition_events")

            dq_data = {
                "lead_rows_total": lead_rows_total,
                "lead_rows_rejected": lead_rows_rejected,
                "rejections_by_reason": dict(sorted(rejections_by_reason.items())),
                "duplicate_lead_rows_merged": dup_leads_merged,
                "field_warnings": field_warnings_count,
                "events_total": events_total,
                "malformed_events": malformed_events,
                "duplicate_events_dropped": dup_events_dropped,
                "orphan_events": orphan_events,
                "unknown_disposition_events": unknown_disp_events,
            }

            with open(quality_path, "w", encoding="utf-8") as f:
                json.dump(dq_data, f, indent=2)

        return {
            "batch_summary": batch_summary_path,
            "daily_overview": daily_overview_path,
            "lead_report": lead_report_path,
            "rejections": rejections_path,
            "data_quality": quality_path,
        }
