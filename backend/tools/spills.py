"""Tools: spills, spill_heatmap.

Both read v_corrected_spills exclusively, so a formal correction or a
spill_detected=false override removes a detection exactly once, in the
view, for every caller (Q4, Q7, Q8).

See PLAN.md Phase 3.1.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from backend.models.schemas import Evidence, ToolResult


def spills(
    conn: duckdb.DuckDBPyConnection,
    asset_id: str,
    years: int = 4,
    as_of: datetime | None = None,
) -> ToolResult[dict]:
    """How many real spills has this well had, and how much oil in total? (Q4)"""
    as_of = as_of or datetime(2026, 10, 16)
    window_start = as_of.replace(year=as_of.year - years)

    rows = conn.execute(
        """
        SELECT inspection_id, inspected_at, spill_est_volume_bbl
        FROM v_corrected_spills
        WHERE asset_id = ? AND inspected_at >= ?
        ORDER BY inspected_at
        """,
        [asset_id, window_start],
    ).fetchall()

    spill_list = [
        {"inspection_id": iid, "date": ts.date().isoformat(), "bbl": bbl}
        for iid, ts, bbl in rows
    ]
    evidence = [Evidence(source_type="inspection", source_id=s["inspection_id"]) for s in spill_list]

    return ToolResult[dict](
        data={
            "asset_id": asset_id,
            "count": len(spill_list),
            "total_bbl": round(sum(s["bbl"] for s in spill_list), 2),
            "spills": spill_list,
        },
        evidence=evidence,
    )


def spill_map(
    conn: duckdb.DuckDBPyConnection,
    area_id: str | None = None,
    days: int = 90,
    as_of: datetime | None = None,
) -> ToolResult[dict]:
    """Every well in this scope with a real spill in the last N days, for a map (Q7)."""
    as_of = as_of or datetime(2026, 10, 16)
    window_start = as_of - timedelta(days=days)

    conditions = ["cs.inspected_at >= ?", "cs.inspected_at <= ?"]
    params: list = [window_start, as_of]
    if area_id is not None:
        conditions.append("cs.area_id = ?")
        params.append(area_id)
    where_clause = " AND ".join(conditions)

    rows = conn.execute(
        f"""
        SELECT cs.inspection_id, w.asset_id, w.name, cs.inspected_at,
               cs.spill_est_volume_bbl, w.lat, w.lon
        FROM v_corrected_spills cs
        JOIN wells w ON w.asset_id = cs.asset_id
        WHERE {where_clause}
        ORDER BY cs.inspected_at
        """,  # noqa: S608 (conditions built from a fixed allow-list of columns above)
        params,
    ).fetchall()

    pins = [
        {
            "asset_id": asset_id,
            "name": name,
            "date": ts.date().isoformat(),
            "bbl": bbl,
            "lat": lat,
            "lon": lon,
        }
        for _iid, asset_id, name, ts, bbl, lat, lon in rows
    ]
    evidence = [Evidence(source_type="inspection", source_id=r[0]) for r in rows]

    return ToolResult[dict](
        data={"start": window_start.date().isoformat(), "end": as_of.date().isoformat(), "pins": pins},
        evidence=evidence,
    )


def spill_heatmap(
    conn: duckdb.DuckDBPyConnection,
    year: int,
) -> ToolResult[dict]:
    """2026 spills by area and month, for a heatmap (Q8)."""
    rows = conn.execute(
        """
        SELECT a.name AS area_name, date_part('month', cs.inspected_at) AS month,
               count(*) AS n, list(cs.inspection_id) AS inspection_ids
        FROM v_corrected_spills cs
        JOIN areas a ON a.area_id = cs.area_id
        WHERE date_part('year', cs.inspected_at) = ?
        GROUP BY a.name, month
        ORDER BY a.name, month
        """,
        [year],
    ).fetchall()

    matrix: dict[str, dict[int, int]] = {}
    totals: dict[str, int] = {}
    evidence: list[Evidence] = []
    for area_name, month, n, inspection_ids in rows:
        matrix.setdefault(area_name, {})[int(month)] = n
        totals[area_name] = totals.get(area_name, 0) + n
        for insp_id in inspection_ids:
            evidence.append(Evidence(source_type="inspection", source_id=insp_id))

    return ToolResult[dict](
        data={
            "year": year,
            "areas": sorted(matrix.keys()),
            "months": list(range(1, 13)),
            "matrix": matrix,
            "totals": totals,
        },
        evidence=evidence,
    )
