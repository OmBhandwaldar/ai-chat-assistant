"""Tools: downtime_days.

A well's downtime over a date range: each weekly inspection that found it
not producing counts as a full 7-day week (Dataset Guide section on
downtime; a missed inspection adds no downtime). (Q5)

See PLAN.md Phase 3.1.
"""

from __future__ import annotations

from datetime import date

import duckdb

from backend.models.schemas import Evidence, ToolResult


def downtime_days(
    conn: duckdb.DuckDBPyConnection,
    asset_id: str,
    start: date,
    end: date,
) -> ToolResult[dict]:
    """How many days was this well down between start and end? (Q5)"""
    rows = conn.execute(
        """
        SELECT inspection_id, inspected_at
        FROM v_is_producing
        WHERE asset_id = ? AND inspected_at >= ? AND inspected_at <= ? AND is_producing = FALSE
        ORDER BY inspected_at
        """,
        [asset_id, start, end],
    ).fetchall()

    inspections = [{"inspection_id": iid, "inspected_at": ts.isoformat()} for iid, ts in rows]
    evidence = [Evidence(source_type="inspection", source_id=i["inspection_id"]) for i in inspections]

    return ToolResult[dict](
        data={
            "asset_id": asset_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": len(inspections) * 7,
            "inspections": inspections,
        },
        evidence=evidence,
    )
