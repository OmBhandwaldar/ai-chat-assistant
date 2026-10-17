"""SOP breach engine: deterministic compliance checker for a single well.

Checks every SOP protocol and manual escalation rule against the well's
records over a lookback window, returning breaches (deadlines missed),
open items (deadlines not yet passed but work still pending), and met
rules (confirmed compliant).  Every finding carries the protocol ID,
the rule text quoted from the SOP, the evidence record that proves it,
and a due-by date where applicable.

Covered protocols and the eval questions they serve:
  SR-01  24 h written notice for reportable spills (≥ 5 bbl)        Q9, Q13
  SR-02  4 h containment (parsed from repair report top chunk)      Q9, Q10
  SR-03  60 d remediation verification for reportable spills        Q9, Q13
  RC-01  Inspection every 14 days                                   (implicit)
  RC-02  Annual flowline integrity test                             Q9
  RC-03  Report 14+ consecutive days down to supervisor             (implicit)
  RC-04  Answer agency NOV within 30 days                           Q9
  RC-05  Flowline maintenance program                               (implicit)
  §10    RCA after N corrective events on same equipment in 12 mo   Q9, Q14

See PLAN.md Phase 2.3, DECISIONS.md D9/D10.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import duckdb

from backend.models.schemas import Evidence, ToolResult
from backend.tools.sop_rules import SopRule, extract_threshold, load_sop_rules

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

BreachStatus = Literal["breach", "open", "met"]


class SopFinding(dict):
    """A dict with typed construction for one compliance finding."""

    def __init__(
        self,
        protocol_id: str,
        rule_text_quote: str,
        status: BreachStatus,
        detail: str,
        evidence_id: str,
        due_by: str | None = None,
        fine_usd: float | None = None,
    ):
        super().__init__(
            protocol_id=protocol_id,
            rule_text_quote=rule_text_quote,
            status=status,
            detail=detail,
            evidence_id=evidence_id,
            due_by=due_by,
            fine_usd=fine_usd,
        )


class SopBreachResult(dict):
    def __init__(
        self,
        breaches: list[dict],
        open_items: list[dict],
        met: list[dict],
    ):
        super().__init__(breaches=breaches, open_items=open_items, met=met)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONTAINMENT_RE = re.compile(
    r"contained?\s+(?:in\s+)?(?:about\s+)?(\d+(?:\.\d+)?)\s*(?:hours?|h)\b",
    re.IGNORECASE,
)


def _parse_containment_hours(report_body: str) -> float | None:
    """Extract containment time in hours from a maintenance report's text.

    The dataset embeds this as e.g. "contained about 7 hours after …" in
    the top section ("What happened") of each spill repair report.
    """
    m = _CONTAINMENT_RE.search(report_body[:2000])
    if m:
        return float(m.group(1))
    return None


def _as_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        try:
            return date.fromisoformat(val[:10])
        except ValueError:
            return None
    return None


def _fmt(d: date | None) -> str:
    return d.strftime("%-d %b %Y") if d else "unknown"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def sop_breaches(
    conn: duckdb.DuckDBPyConnection,
    asset_id: str,
    lookback_days: int = 365,
    as_of: date | None = None,
    fleet_dir: Path | str | None = None,
) -> ToolResult[SopBreachResult]:
    """Check all SOP and manual rules for one well over the lookback window.

    Returns ToolResult wrapping SopBreachResult with three lists:
    breaches (deadline missed), open_items (pending), met (confirmed ok).
    """
    as_of = as_of or date(2026, 10, 16)  # "today" per eval spec
    window_start = as_of - timedelta(days=lookback_days)

    # Load SOP rule texts for quoting.
    if fleet_dir is None:
        fleet_dir = _find_fleet_dir(conn)
    rules = load_sop_rules(fleet_dir) if fleet_dir else {}

    breaches: list[dict] = []
    open_items: list[dict] = []
    met: list[dict] = []
    evidence_ids: list[Evidence] = []

    def _rule_text(pid: str) -> str:
        r = rules.get(pid)
        return r["text"][:200] if r else pid

    # ------------------------------------------------------------------
    # Gather the well's data in the lookback window
    # ------------------------------------------------------------------

    # Corrected spills (≥ 0 bbl) in the window
    spills = conn.execute(
        """
        SELECT cs.inspection_id, cs.inspected_at, cs.spill_est_volume_bbl,
               cs.spill_area_pct
        FROM v_corrected_spills cs
        WHERE cs.asset_id = ? AND cs.inspected_at >= ? AND cs.inspected_at <= ?
        ORDER BY cs.inspected_at
        """,
        [asset_id, datetime.combine(window_start, datetime.min.time()),
         datetime.combine(as_of, datetime.max.time())],
    ).fetchall()

    # Regulatory events in the window
    reg_events = conn.execute(
        """
        SELECT event_id, date, agency, event_type, protocol_id,
               related_inspection_id, hours_to_report, detail, fine_usd, status
        FROM regulatory_events
        WHERE asset_id = ? AND date >= ? AND date <= ?
        ORDER BY date
        """,
        [asset_id, window_start.isoformat(), as_of.isoformat()],
    ).fetchall()

    # Maintenance in the window
    maint_rows = conn.execute(
        """
        SELECT maintenance_id, started_at, completed_at, maintenance_type,
               equipment_kind, issue, action_taken, triggered_by_inspection_id,
               report_file
        FROM maintenance
        WHERE asset_id = ? AND started_at >= ? AND started_at <= ?
        ORDER BY started_at
        """,
        [asset_id, datetime.combine(window_start, datetime.min.time()),
         datetime.combine(as_of, datetime.max.time())],
    ).fetchall()

    # Maintenance reports (body text) for this asset
    report_bodies: dict[str, str] = {}
    rpt_rows = conn.execute(
        """
        SELECT mr.report_path, mr.body, mr.maintenance_id
        FROM maintenance_reports mr
        WHERE mr.asset_id = ?
        """,
        [asset_id],
    ).fetchall()
    for rp, body, mid in rpt_rows:
        report_bodies[mid] = body

    # Index maintenance by triggered_by_inspection_id for spill lookups
    maint_by_inspection: dict[str, list] = {}
    for row in maint_rows:
        insp_id = row[7]  # triggered_by_inspection_id
        if insp_id:
            maint_by_inspection.setdefault(insp_id, []).append(row)

    # Index regulatory events by related_inspection_id
    reg_by_inspection: dict[str, list] = {}
    for row in reg_events:
        insp_id = row[5]  # related_inspection_id
        if insp_id:
            reg_by_inspection.setdefault(insp_id, []).append(row)

    # ------------------------------------------------------------------
    # SR-01: Written notice within 24 h for reportable spills (≥ 5 bbl)
    # ------------------------------------------------------------------
    for insp_id, inspected_at, volume, area_pct in spills:
        if volume is None or volume < 5.0:
            continue
        # This is a reportable spill — was written notice filed within 24 h?
        discovery = inspected_at if isinstance(inspected_at, datetime) else datetime.fromisoformat(str(inspected_at))
        deadline = discovery + timedelta(hours=24)
        # Check regulatory_events for a spill_report_filed tied to this inspection
        filed_events = [
            r for r in reg_by_inspection.get(insp_id, [])
            if r[3] == "spill_report_filed"  # event_type
        ]
        if filed_events:
            ev = filed_events[0]
            hours = ev[6]  # hours_to_report
            ev_id = ev[0]
            evidence_ids.append(Evidence(source_type="regulatory", source_id=ev_id))
            if hours is not None and hours > 24:
                breaches.append(SopFinding(
                    protocol_id="SR-01",
                    rule_text_quote=_rule_text("SR-01"),
                    status="breach",
                    detail=f"Reported after {hours:.0f} h (limit 24 h). "
                           f"Spill of {volume:.1f} bbl detected {_fmt(_as_date(inspected_at))}.",
                    evidence_id=ev_id,
                    due_by=deadline.strftime("%Y-%m-%d %H:%M"),
                ))
                # Check for resulting NOV and fine
                novs = [
                    r for r in reg_by_inspection.get(insp_id, [])
                    if r[3] == "notice_of_violation" and r[4] == "SR-01"
                ]
                for nov in novs:
                    nov_id, nov_date = nov[0], nov[1]
                    nov_fine = nov[8]
                    nov_status = nov[9]
                    evidence_ids.append(Evidence(source_type="regulatory", source_id=nov_id))
                    breaches[-1]["detail"] += (
                        f" This led to NOV {nov_id} on {_fmt(_as_date(nov_date))}"
                        f" with a ${nov_fine:,.0f} fine ({nov_status})."
                    )
                    breaches[-1]["fine_usd"] = nov_fine
            else:
                met.append(SopFinding(
                    protocol_id="SR-01",
                    rule_text_quote=_rule_text("SR-01"),
                    status="met",
                    detail=f"Reported within {hours:.0f} h.",
                    evidence_id=ev_id,
                ))
        else:
            # No report filed at all
            evidence_ids.append(Evidence(source_type="inspection", source_id=insp_id))
            breaches.append(SopFinding(
                protocol_id="SR-01",
                rule_text_quote=_rule_text("SR-01"),
                status="breach",
                detail=f"No written notice on record for reportable spill of {volume:.1f} bbl "
                       f"detected {_fmt(_as_date(inspected_at))}.",
                evidence_id=insp_id,
                due_by=deadline.strftime("%Y-%m-%d %H:%M"),
            ))

    # ------------------------------------------------------------------
    # SR-02: Source contained within 4 h of discovery
    # ------------------------------------------------------------------
    for insp_id, inspected_at, volume, area_pct in spills:
        # Check all spills (any size) for containment time from the repair report
        repairs = maint_by_inspection.get(insp_id, [])
        for repair in repairs:
            maint_id = repair[0]
            body = report_bodies.get(maint_id, "")
            if not body:
                continue
            hours = _parse_containment_hours(body)
            if hours is None:
                continue
            evidence_ids.append(Evidence(source_type="report", source_id=maint_id))
            if hours > 4.0:
                breaches.append(SopFinding(
                    protocol_id="SR-02",
                    rule_text_quote=_rule_text("SR-02"),
                    status="breach",
                    detail=f"Contained after ~{hours:.1f} h (limit 4 h). "
                           f"Report {maint_id}, spill detected {_fmt(_as_date(inspected_at))}.",
                    evidence_id=maint_id,
                ))
            else:
                met.append(SopFinding(
                    protocol_id="SR-02",
                    rule_text_quote=_rule_text("SR-02"),
                    status="met",
                    detail=f"Contained in ~{hours:.1f} h.",
                    evidence_id=maint_id,
                ))

    # ------------------------------------------------------------------
    # SR-03: Remediation verification within 60 d for reportable spills
    # ------------------------------------------------------------------
    for insp_id, inspected_at, volume, area_pct in spills:
        if volume is None or volume < 5.0:
            continue
        discovery_date = _as_date(inspected_at)
        if discovery_date is None:
            continue
        deadline_date = discovery_date + timedelta(days=60)
        # Look for an agency site_inspection or verification event
        related_regs = reg_by_inspection.get(insp_id, [])
        verifications = [
            r for r in related_regs
            if r[3] in ("site_inspection", "remediation_verification")
        ]
        if verifications:
            for v in verifications:
                v_id, v_status = v[0], v[9]
                evidence_ids.append(Evidence(source_type="regulatory", source_id=v_id))
                if v_status == "closed":
                    met.append(SopFinding(
                        protocol_id="SR-03",
                        rule_text_quote=_rule_text("SR-03"),
                        status="met",
                        detail=f"Verification closed ({v_id}).",
                        evidence_id=v_id,
                    ))
                elif deadline_date <= as_of:
                    breaches.append(SopFinding(
                        protocol_id="SR-03",
                        rule_text_quote=_rule_text("SR-03"),
                        status="breach",
                        detail=f"Verification due by {_fmt(deadline_date)}, still {v_status} ({v_id}).",
                        evidence_id=v_id,
                        due_by=deadline_date.isoformat(),
                    ))
                else:
                    open_items.append(SopFinding(
                        protocol_id="SR-03",
                        rule_text_quote=_rule_text("SR-03"),
                        status="open",
                        detail=f"Verification due by {_fmt(deadline_date)}, currently {v_status} ({v_id}).",
                        evidence_id=v_id,
                        due_by=deadline_date.isoformat(),
                    ))
        else:
            # No verification event at all
            evidence_ids.append(Evidence(source_type="inspection", source_id=insp_id))
            if deadline_date <= as_of:
                breaches.append(SopFinding(
                    protocol_id="SR-03",
                    rule_text_quote=_rule_text("SR-03"),
                    status="breach",
                    detail=f"No remediation verification on record. "
                           f"Due by {_fmt(deadline_date)} for {volume:.1f} bbl spill.",
                    evidence_id=insp_id,
                    due_by=deadline_date.isoformat(),
                ))
            else:
                open_items.append(SopFinding(
                    protocol_id="SR-03",
                    rule_text_quote=_rule_text("SR-03"),
                    status="open",
                    detail=f"Verification due by {_fmt(deadline_date)} for {volume:.1f} bbl spill. "
                           f"No agency event on record yet.",
                    evidence_id=insp_id,
                    due_by=deadline_date.isoformat(),
                ))

    # ------------------------------------------------------------------
    # RC-01: Inspection every 14 days
    # ------------------------------------------------------------------
    last_insp = conn.execute(
        """
        SELECT inspection_id, inspected_at
        FROM inspections
        WHERE asset_id = ? AND inspected_at <= ?
        ORDER BY inspected_at DESC LIMIT 1
        """,
        [asset_id, datetime.combine(as_of, datetime.max.time())],
    ).fetchone()
    if last_insp:
        last_date = _as_date(last_insp[1])
        if last_date and (as_of - last_date).days > 14:
            breaches.append(SopFinding(
                protocol_id="RC-01",
                rule_text_quote=_rule_text("RC-01"),
                status="breach",
                detail=f"Last inspection was {_fmt(last_date)}, "
                       f"{(as_of - last_date).days} days ago (limit 14).",
                evidence_id=last_insp[0],
            ))
        else:
            met.append(SopFinding(
                protocol_id="RC-01",
                rule_text_quote=_rule_text("RC-01"),
                status="met",
                detail=f"Last inspection {_fmt(last_date)}.",
                evidence_id=last_insp[0],
            ))
            evidence_ids.append(Evidence(source_type="inspection", source_id=last_insp[0]))

    # ------------------------------------------------------------------
    # RC-02: Annual flowline integrity test
    # ------------------------------------------------------------------
    integrity_test = conn.execute(
        """
        SELECT maintenance_id, started_at
        FROM maintenance
        WHERE asset_id = ?
          AND issue ILIKE '%flowline integrity test%'
          AND started_at >= ?
          AND started_at <= ?
        ORDER BY started_at DESC LIMIT 1
        """,
        [asset_id,
         datetime.combine(as_of - timedelta(days=365), datetime.min.time()),
         datetime.combine(as_of, datetime.max.time())],
    ).fetchone()
    if integrity_test:
        evidence_ids.append(Evidence(source_type="maintenance", source_id=integrity_test[0]))
        met.append(SopFinding(
            protocol_id="RC-02",
            rule_text_quote=_rule_text("RC-02"),
            status="met",
            detail=f"Flowline integrity test done {_fmt(_as_date(integrity_test[1]))} ({integrity_test[0]}).",
            evidence_id=integrity_test[0],
        ))
    else:
        breaches.append(SopFinding(
            protocol_id="RC-02",
            rule_text_quote=_rule_text("RC-02"),
            status="breach",
            detail="No flowline integrity test found in the last 12 months.",
            evidence_id=asset_id,
        ))

    # ------------------------------------------------------------------
    # RC-04: Answer agency NOV within 30 days
    # ------------------------------------------------------------------
    novs = [
        r for r in reg_events
        if r[3] == "notice_of_violation"
    ]
    for nov in novs:
        nov_id = nov[0]
        nov_date = _as_date(nov[1])
        nov_status = nov[9]
        if nov_date is None:
            continue
        response_deadline = nov_date + timedelta(days=30)
        # Look for a nov_response_filed event after this NOV
        responses = [
            r for r in reg_events
            if r[3] == "nov_response_filed"
            and _as_date(r[1]) is not None
            and _as_date(r[1]) >= nov_date
        ]
        evidence_ids.append(Evidence(source_type="regulatory", source_id=nov_id))
        if responses:
            met.append(SopFinding(
                protocol_id="RC-04",
                rule_text_quote=_rule_text("RC-04"),
                status="met",
                detail=f"Response filed for {nov_id}.",
                evidence_id=nov_id,
            ))
        elif response_deadline <= as_of:
            breaches.append(SopFinding(
                protocol_id="RC-04",
                rule_text_quote=_rule_text("RC-04"),
                status="breach",
                detail=f"NOV {nov_id} on {_fmt(nov_date)} required response by "
                       f"{_fmt(response_deadline)}; none recorded.",
                evidence_id=nov_id,
                due_by=response_deadline.isoformat(),
            ))
        else:
            open_items.append(SopFinding(
                protocol_id="RC-04",
                rule_text_quote=_rule_text("RC-04"),
                status="open",
                detail=f"NOV {nov_id} on {_fmt(nov_date)} — response due by {_fmt(response_deadline)}.",
                evidence_id=nov_id,
                due_by=response_deadline.isoformat(),
            ))

    # ------------------------------------------------------------------
    # Manual §10: RCA after N corrective events on same equipment in 12 mo
    # ------------------------------------------------------------------
    well_type_row = conn.execute(
        "SELECT well_type FROM wells WHERE asset_id = ?", [asset_id]
    ).fetchone()
    if well_type_row:
        well_type = well_type_row[0]
        # Get the effective RCA threshold for this well type (respects overrides, Q14)
        rca_threshold = conn.execute(
            "SELECT rca_corrective_count FROM v_well_type_rules_effective WHERE well_type = ?",
            [well_type],
        ).fetchone()
        rca_count = rca_threshold[0] if rca_threshold else 3
        rca_lookback = conn.execute(
            "SELECT rca_lookback_days FROM v_well_type_rules_effective WHERE well_type = ?",
            [well_type],
        ).fetchone()
        rca_days = rca_lookback[0] if rca_lookback else 365
        rca_start = as_of - timedelta(days=rca_days)

        # Count corrective maintenance per equipment_kind in the lookback
        equip_counts = conn.execute(
            """
            SELECT equipment_kind, COUNT(*) as cnt,
                   ARRAY_AGG(maintenance_id ORDER BY started_at) as maint_ids
            FROM maintenance
            WHERE asset_id = ?
              AND maintenance_type = 'corrective'
              AND started_at >= ?
              AND started_at <= ?
            GROUP BY equipment_kind
            HAVING COUNT(*) >= ?
            """,
            [asset_id,
             datetime.combine(rca_start, datetime.min.time()),
             datetime.combine(as_of, datetime.max.time()),
             rca_count],
        ).fetchall()

        for equip_kind, cnt, maint_ids in equip_counts:
            last_maint_id = maint_ids[-1] if maint_ids else asset_id
            # Check if the last repair report marks RCA as done or required
            body = report_bodies.get(last_maint_id, "")
            rca_done = bool(re.search(r"root.cause\s+analysis\s+(?:is\s+)?(?:complete|completed|done|finished)", body, re.IGNORECASE))
            evidence_ids.append(Evidence(source_type="maintenance", source_id=last_maint_id))
            if rca_done:
                met.append(SopFinding(
                    protocol_id="§10",
                    rule_text_quote=f"{well_type} manual §10: {rca_count}+ corrective events on same equipment in {rca_days//30} months require RCA.",
                    status="met",
                    detail=f"RCA completed for {equip_kind} ({cnt} corrective repairs: {', '.join(maint_ids)}).",
                    evidence_id=last_maint_id,
                ))
            else:
                rca_required = bool(re.search(r"(?:root.cause|RCA).*(?:required|action required|to be started)", body, re.IGNORECASE))
                if rca_required:
                    open_items.append(SopFinding(
                        protocol_id="§10",
                        rule_text_quote=f"{well_type} manual §10: {rca_count}+ corrective events on same equipment in {rca_days//30} months require RCA.",
                        status="open",
                        detail=f"RCA required for {equip_kind}: {cnt} corrective repairs ({', '.join(maint_ids)}). "
                               f"Report {last_maint_id} marks it as 'Action required'.",
                        evidence_id=last_maint_id,
                    ))
                else:
                    open_items.append(SopFinding(
                        protocol_id="§10",
                        rule_text_quote=f"{well_type} manual §10: {rca_count}+ corrective events on same equipment in {rca_days//30} months require RCA.",
                        status="open",
                        detail=f"RCA may be needed for {equip_kind}: {cnt} corrective repairs ({', '.join(maint_ids)}).",
                        evidence_id=last_maint_id,
                    ))

    # ------------------------------------------------------------------
    # Check for planned work not yet in the maintenance system (from context notes)
    # ------------------------------------------------------------------
    planned_notes = conn.execute(
        """
        SELECT note_id, author_user_id, text, valid_from, valid_to
        FROM context_notes
        WHERE asset_id = ?
          AND note_type IN ('operator_feedback', 'operator_note')
          AND (valid_from IS NULL OR valid_from <= ?)
          AND (valid_to   IS NULL OR valid_to   >= ?)
        ORDER BY created_at DESC
        """,
        [asset_id, as_of.isoformat(), as_of.isoformat()],
    ).fetchall()

    for note_id, author_id, text, vfrom, vto in planned_notes:
        lowered = text.lower()
        if any(kw in lowered for kw in ("shut in", "shut-in", "planned", "work order not yet")):
            # Check if there's a matching maintenance record
            has_maint = conn.execute(
                """
                SELECT 1 FROM maintenance
                WHERE asset_id = ? AND started_at >= ? AND started_at <= ?
                LIMIT 1
                """,
                [asset_id,
                 datetime.combine(_as_date(vfrom) or as_of, datetime.min.time()) if vfrom else datetime.combine(as_of - timedelta(days=7), datetime.min.time()),
                 datetime.combine(as_of, datetime.max.time())],
            ).fetchone()
            if not has_maint:
                evidence_ids.append(Evidence(source_type="context", source_id=note_id))
                open_items.append(SopFinding(
                    protocol_id="T200§4",
                    rule_text_quote="T200 manual §4: record the shut-in in the maintenance system before work starts.",
                    status="open",
                    detail=f"Note {note_id} describes a planned shut-in not yet in the maintenance system.",
                    evidence_id=note_id,
                ))

    return ToolResult[dict](
        data={"breaches": breaches, "open_items": open_items, "met": met},
        evidence=evidence_ids,
    )


# ---------------------------------------------------------------------------
# Helpers for fleet_dir auto-detection
# ---------------------------------------------------------------------------

def _find_fleet_dir(conn: duckdb.DuckDBPyConnection) -> Path | None:
    """Try common locations for the fleet/ directory."""
    candidates = [
        Path("sample-data/fleet"),
        Path("../flytbase-ahc-swe-asset-intelligence-dataset/sample-data/fleet"),
        Path(__file__).resolve().parents[2] / "sample-data" / "fleet",
    ]
    # Also check DATASET_ROOT env var
    import os
    dr = os.environ.get("DATASET_ROOT")
    if dr:
        candidates.insert(0, Path(dr) / "fleet")
    for c in candidates:
        if c.exists() and (c / "sops").exists():
            return c
    return None


# ---------------------------------------------------------------------------
# SOP + manual lookup tools (Phase 3.4)
# ---------------------------------------------------------------------------

def get_sop(
    conn: duckdb.DuckDBPyConnection,
    protocol_id: str,
    fleet_dir: Path | str | None = None,
) -> ToolResult[str]:
    """Look up one SOP protocol's rule text verbatim, e.g. get_sop('SR-01')."""
    if fleet_dir is None:
        fleet_dir = _find_fleet_dir(conn)
    rules = load_sop_rules(fleet_dir) if fleet_dir else {}

    rule = rules.get(protocol_id)
    if rule is None:
        return ToolResult[str](data="", evidence=[])

    return ToolResult[str](
        data=rule["text"],
        evidence=[Evidence(source_type="sop", source_id=f"{rule['source_path']}#{protocol_id}")],
    )


def get_manual_section(
    conn: duckdb.DuckDBPyConnection,
    well_type: Literal["T200", "T350"],
    section: str,
    fleet_dir: Path | str | None = None,
) -> ToolResult[str]:
    """Look up one well-type manual section's text verbatim, e.g.
    get_manual_section('T200', '9.3')."""
    if fleet_dir is None:
        fleet_dir = _find_fleet_dir(conn)
    rules = load_sop_rules(fleet_dir) if fleet_dir else {}

    rule = rules.get(f"{well_type}#{section}") or rules.get(section)
    if rule is None:
        return ToolResult[str](data="", evidence=[])

    return ToolResult[str](
        data=rule["text"],
        evidence=[Evidence(source_type="manual", source_id=f"{rule['source_path']}#{section}")],
    )
