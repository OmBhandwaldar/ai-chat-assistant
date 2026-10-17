"""Tools: temp_timeseries, top_downtime.

temp_timeseries returns a well's recent wellhead/ambient readings for a
line chart, marking which ones were not producing (Q3).
top_downtime ranks wells in a scope by downtime days over a window for a
bar chart (Q6). Downtime days = count of weekly inspections where the well
was not producing, times 7 (Dataset Guide: a missed inspection adds no
downtime, so this is a plain count, not a day-span calculation).

See PLAN.md Phase 3.1.
"""

from __future__ import annotations

from datetime import date, datetime

import duckdb

from backend.db.scope import resolve_scope, unresolved_fields
from backend.models.schemas import Evidence, ToolResult


def temp_timeseries(
    conn: duckdb.DuckDBPyConnection,
    asset_id: str,
    weeks: int = 13,
    as_of: datetime | None = None,
) -> ToolResult[dict]:
    """Wellhead vs. ambient temperature over the last `weeks` inspections (Q3)."""
    as_of = as_of or datetime(2026, 10, 16)

    rows = conn.execute(
        """
        SELECT inspection_id, inspected_at, wellhead_temp_c, ambient_temp_c, is_producing
        FROM v_is_producing
        WHERE asset_id = ? AND inspected_at <= ?
        ORDER BY inspected_at DESC
        LIMIT ?
        """,
        [asset_id, as_of, weeks],
    ).fetchall()
    rows.reverse()  # ascending, for a line chart

    points = [
        {
            "inspection_id": inspection_id,
            "inspected_at": inspected_at.isoformat(),
            "wellhead_temp_c": wellhead_c,
            "ambient_temp_c": ambient_c,
            "is_producing": bool(is_producing),
        }
        for inspection_id, inspected_at, wellhead_c, ambient_c, is_producing in rows
    ]
    evidence = [Evidence(source_type="inspection", source_id=p["inspection_id"]) for p in points]

    return ToolResult[dict](
        data={"asset_id": asset_id, "points": points},
        evidence=evidence,
    )


def top_downtime(
    conn: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    area_id: str | None = None,
    team_id: str | None = None,
    well_type: str | None = None,
    limit: int = 3,
) -> ToolResult[dict]:
    """Which wells in this scope had the most downtime in [start, end]? (Q6)"""
    scope = resolve_scope(conn, area_id=area_id, team_id=team_id)
    if not scope.ok:
        return ToolResult[dict](
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "wells": [],
                **unresolved_fields(scope),
            },
            evidence=[],
        )
    area_id, team_id = scope.area_id, scope.team_id

    conditions = ["p.inspected_at >= ?", "p.inspected_at <= ?", "p.is_producing = FALSE"]
    params: list = [start, end]
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
        SELECT w.asset_id, w.name, w.well_type,
               count(*) * 7 AS downtime_days,
               list(p.inspection_id) AS inspection_ids
        FROM v_is_producing p
        JOIN wells w ON w.asset_id = p.asset_id
        WHERE {where_clause}
        GROUP BY w.asset_id, w.name, w.well_type
        ORDER BY downtime_days DESC, w.asset_id
        LIMIT ?
        """,  # conditions built from a fixed allow-list of columns above
        [*params, limit],
    ).fetchall()

    wells: list[dict] = []
    evidence: list[Evidence] = []
    for asset_id, name, w_type, downtime_days, inspection_ids in rows:
        wells.append({
            "asset_id": asset_id,
            "name": name,
            "well_type": w_type,
            "downtime_days": downtime_days,
        })
        for insp_id in inspection_ids:
            evidence.append(Evidence(source_type="inspection", source_id=insp_id))

    return ToolResult[dict](
        data={"start": start.isoformat(), "end": end.isoformat(), "wells": wells},
        evidence=evidence,
    )
