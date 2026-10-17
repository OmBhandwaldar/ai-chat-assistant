"""Tools: well_status_now, wells_not_producing.

well_status_now wraps the Phase 2.1 resolver (backend/db/resolver.py) for a
single well's current producing state, reasons and conflicts (Q1).
wells_not_producing lists every well in a filtered scope that is currently
not producing, with its temperature delta and a best-effort reason: an
open repair, an active context note, or "No recorded reason." (Q2).

See PLAN.md Phase 3.1.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import duckdb

from backend.db.resolver import resolve_well_state
from backend.models.schemas import Evidence, ToolResult

_ID_PREFIX_TO_SOURCE_TYPE = {
    "INS": "inspection",
    "CTX": "context",
    "MNT": "maintenance",
    "REG": "regulatory",
}

_ID_RE = re.compile(r"\b(INS|CTX|MNT|REG)-[\w-]+\b")


def _evidence_from_reasons(reasons: list[str]) -> list[Evidence]:
    """Pull evidence IDs out of the resolver's free-text reasons by prefix."""
    seen: set[str] = set()
    evidence: list[Evidence] = []
    for reason in reasons:
        for m in _ID_RE.finditer(reason):
            full_id = m.group(0)
            if full_id in seen:
                continue
            seen.add(full_id)
            prefix = m.group(1)
            evidence.append(Evidence(source_type=_ID_PREFIX_TO_SOURCE_TYPE[prefix], source_id=full_id))
    return evidence


def well_status_now(
    conn: duckdb.DuckDBPyConnection,
    asset_id: str,
    as_of: datetime | None = None,
) -> ToolResult[dict]:
    """Is this well producing oil right now, and why? (Q1)"""
    as_of = as_of or datetime(2026, 10, 16)
    state = resolve_well_state(conn, asset_id, as_of=as_of)

    data = {
        "asset_id": asset_id,
        "state": state.state,
        "reasons": state.reasons,
        "conflicts": [c.model_dump() for c in state.conflicts],
    }
    return ToolResult[dict](data=data, evidence=_evidence_from_reasons(state.reasons))


def wells_not_producing(
    conn: duckdb.DuckDBPyConnection,
    area_id: str | None = None,
    team_id: str | None = None,
    well_type: str | None = None,
    as_of: date | None = None,
) -> ToolResult[dict]:
    """Which wells in this scope are not producing oil right now, and why? (Q2)"""
    as_of = as_of or date(2026, 10, 16)

    conditions = ["p.is_producing = FALSE"]
    params: list = []
    if area_id is not None:
        conditions.append("w.area_id = ?")
        params.append(area_id)
    if team_id is not None:
        conditions.append("w.team_id = ?")
        params.append(team_id)
    if well_type is not None:
        conditions.append("w.well_type = ?")
        params.append(well_type)
    where_clause = " AND ".join(conditions)

    rows = conn.execute(
        f"""
        SELECT w.asset_id, w.name, w.well_type, w.recorded_status,
               p.inspection_id, p.delta_c, i.notes
        FROM v_well_latest_inspection p
        JOIN wells w ON w.asset_id = p.asset_id
        JOIN inspections i ON i.inspection_id = p.inspection_id
        WHERE {where_clause}
        ORDER BY w.asset_id
        """,  # noqa: S608 (conditions built from a fixed allow-list of columns above)
        params,
    ).fetchall()

    wells: list[dict] = []
    evidence: list[Evidence] = []

    for asset_id, name, w_type, recorded_status, inspection_id, delta_c, insp_notes in rows:
        evidence.append(Evidence(source_type="inspection", source_id=inspection_id))

        # 1. An open repair job: not completed, or completed after as_of.
        maint = conn.execute(
            """
            SELECT maintenance_id, issue, started_at
            FROM maintenance
            WHERE asset_id = ?
              AND started_at <= ?
              AND (completed_at IS NULL OR completed_at > ?)
            ORDER BY started_at DESC
            LIMIT 1
            """,
            [asset_id, as_of, as_of],
        ).fetchone()

        reason: str
        if maint is not None:
            maintenance_id, issue, started_at = maint
            reason = f"Repair {maintenance_id}, {issue}, started {started_at.date()}, still open"
            evidence.append(Evidence(source_type="maintenance", source_id=maintenance_id))
        else:
            # 2. An active context note (not a correction) covering as_of.
            note = conn.execute(
                """
                SELECT note_id, text
                FROM context_notes
                WHERE asset_id = ?
                  AND note_type != 'correction'
                  AND (valid_from IS NULL OR valid_from <= ?)
                  AND (valid_to   IS NULL OR valid_to   >= ?)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [asset_id, as_of, as_of],
            ).fetchone()
            if note is not None:
                note_id, text = note
                reason = text
                evidence.append(Evidence(source_type="context", source_id=note_id))
            else:
                reason = "No recorded reason."
                if insp_notes:
                    reason += f' The inspector noted "{insp_notes}"'

        recorded_status_display = recorded_status or "unknown"
        implied_producing = (recorded_status or "").lower() == "active"
        if implied_producing:
            recorded_status_display = f"{recorded_status} (out of date)"

        wells.append({
            "asset_id": asset_id,
            "name": name,
            "well_type": w_type,
            "delta_c": delta_c,
            "recorded_status": recorded_status_display,
            "reason": reason,
        })

    return ToolResult[dict](
        data={"as_of": as_of.isoformat(), "wells": wells},
        evidence=evidence,
    )
