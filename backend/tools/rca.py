"""Tools: rca_due.

Which wells need a root-cause analysis on a given equipment kind: N or
more corrective repairs on it within the well type's RCA lookback window.
Reads the threshold from v_well_type_rules_effective, so a well_type-scoped
override (Q14: "pump jack RCA after 2 repairs, not 3, for T350") is applied
automatically and the answer explains it followed the override, not the
manual, when one is present.

See PLAN.md Phase 3.1.
"""

from __future__ import annotations

from datetime import date

import duckdb

from backend.models.schemas import Evidence, ToolResult


def rca_due(
    conn: duckdb.DuckDBPyConnection,
    equipment_kind: str = "pump_jack",
    area_id: str | None = None,
    team_id: str | None = None,
    well_type: str | None = None,
    as_of: date | None = None,
) -> ToolResult[dict]:
    """Which wells need an RCA on this equipment kind right now? (Q14)"""
    as_of = as_of or date(2026, 10, 16)

    conditions = ["w.well_type IS NOT NULL"]
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
        SELECT w.asset_id, w.name, w.well_type,
               r.rca_corrective_count AS threshold, r.rca_lookback_days AS lookback_days,
               count(m.maintenance_id) AS corrective_count,
               list(m.maintenance_id) AS maintenance_ids
        FROM wells w
        JOIN v_well_type_rules_effective r ON r.well_type = w.well_type
        LEFT JOIN maintenance m
               ON m.asset_id = w.asset_id
              AND m.equipment_kind = ?
              AND m.maintenance_type = 'corrective'
              AND m.started_at >= ? - INTERVAL (r.rca_lookback_days) DAY
              AND m.started_at <= ?
        WHERE {where_clause}
        GROUP BY w.asset_id, w.name, w.well_type, r.rca_corrective_count, r.rca_lookback_days
        HAVING count(m.maintenance_id) >= r.rca_corrective_count
        ORDER BY w.asset_id
        """,  # noqa: S608 (conditions built from a fixed allow-list of columns above)
        [equipment_kind, as_of, as_of, *params],
    ).fetchall()

    wells: list[dict] = []
    evidence: list[Evidence] = []
    for asset_id, name, w_type, threshold, lookback_days, corrective_count, maintenance_ids in rows:
        wells.append({
            "asset_id": asset_id,
            "name": name,
            "well_type": w_type,
            "equipment_kind": equipment_kind,
            "corrective_count": corrective_count,
            "threshold": threshold,
            "lookback_days": lookback_days,
        })
        for mid in maintenance_ids:
            evidence.append(Evidence(source_type="maintenance", source_id=mid))

    return ToolResult[dict](
        data={"as_of": as_of.isoformat(), "equipment_kind": equipment_kind, "wells": wells},
        evidence=evidence,
    )
