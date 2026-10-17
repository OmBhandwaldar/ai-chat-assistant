"""Tools: repair_costs.

Sum of maintenance.cost_total per well for a given year — the field
already bundles labor + parts per job (Q11).

See PLAN.md Phase 3.1.
"""

from __future__ import annotations

import duckdb

from backend.models.schemas import Evidence, ToolResult


def repair_costs(
    conn: duckdb.DuckDBPyConnection,
    asset_ids: list[str],
    year: int,
) -> ToolResult[dict]:
    """What did repairs cost each of these wells in `year`? (Q11)"""
    rows = conn.execute(
        """
        SELECT asset_id, maintenance_id, cost_total
        FROM maintenance
        WHERE asset_id = ANY(?) AND date_part('year', started_at) = ?
        ORDER BY asset_id, started_at
        """,
        [asset_ids, year],
    ).fetchall()

    costs: dict[str, float] = {asset_id: 0.0 for asset_id in asset_ids}
    evidence: list[Evidence] = []
    for asset_id, maintenance_id, cost_total in rows:
        costs[asset_id] = round(costs[asset_id] + (cost_total or 0.0), 2)
        evidence.append(Evidence(source_type="maintenance", source_id=maintenance_id))

    return ToolResult[dict](
        data={"year": year, "costs": costs},
        evidence=evidence,
    )
