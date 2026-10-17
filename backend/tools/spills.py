"""Tools: spills, spill_heatmap.

Both read v_corrected_spills exclusively, so a formal correction or a
spill_detected=false override removes a detection exactly once, in the
view, for every caller (Q4, Q7, Q8).

See PLAN.md Phase 3.1.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from backend.db.resolver import resolve_inspection_notes
from backend.db.scope import resolve_scope, unresolved_fields
from backend.models.schemas import Evidence, ToolResult

# Spill Response SOP SR-01: spills of at least this many barrels are
# reportable (24 h written notice) and trigger SR-03 verification. Mirrors the
# threshold used by the SOP breach engine (backend/tools/sop.py).
REPORTABLE_BBL = 5.0


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

    spill_list: list[dict] = []
    evidence: list[Evidence] = []
    conflicts: list[dict] = []
    for iid, ts, bbl in rows:
        verdict = resolve_inspection_notes(conn, iid)
        spill_conflicts = [c.model_dump() for c in verdict.conflicts]
        spill_list.append({
            "inspection_id": iid,
            "date": ts.date().isoformat(),
            "bbl": bbl,
            # Q18: informal notes on the detection, whether it stands, and why.
            "detection_stands": verdict.detection_stands,
            "notes": [
                {k: n[k] for k in ("note_id", "author", "text", "stance")} for n in verdict.notes
            ],
            "conflicts": spill_conflicts,
            "repair_ids": verdict.repair_ids,
            "basis": verdict.basis,
        })
        conflicts.extend(spill_conflicts)
        evidence.append(Evidence(source_type="inspection", source_id=iid))
        for n in verdict.notes:
            evidence.append(Evidence(source_type="context", source_id=n["note_id"]))
        if verdict.notes:
            for mid in verdict.repair_ids:
                evidence.append(Evidence(source_type="maintenance", source_id=mid))

    return ToolResult[dict](
        data={
            "asset_id": asset_id,
            "count": len(spill_list),
            "total_bbl": round(sum(s["bbl"] or 0 for s in spill_list), 2),
            "spills": spill_list,
            "conflicts": conflicts,
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
    scope = resolve_scope(conn, area_id=area_id)
    if not scope.ok:
        return ToolResult[dict](
            data={
                "start": window_start.date().isoformat(),
                "end": as_of.date().isoformat(),
                "reportable_threshold_bbl": REPORTABLE_BBL,
                "pins": [],
                **unresolved_fields(scope),
            },
            evidence=[],
        )
    area_id = scope.area_id

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
        """,
        params,
    ).fetchall()

    pins: list[dict] = []
    evidence: list[Evidence] = []
    for iid, asset_id, name, ts, bbl, lat, lon in rows:
        pin = {
            "inspection_id": iid,
            "asset_id": asset_id,
            "name": name,
            "date": ts.date().isoformat(),
            "bbl": bbl,
            "lat": lat,
            "lon": lon,
            "reportable": bbl is not None and bbl >= REPORTABLE_BBL,
        }
        evidence.append(Evidence(source_type="inspection", source_id=iid))

        # A user-corrected volume (Q13) is shown with the original and the author.
        override = conn.execute(
            """
            SELECT o.id, u.name, o.original_value
            FROM overrides o LEFT JOIN users u ON u.user_id = o.author_user_id
            WHERE o.scope_type = 'inspection' AND o.scope_id = ?
              AND o.field = 'spill_est_volume_bbl'
            ORDER BY o.created_at DESC, o.id DESC LIMIT 1
            """,
            [iid],
        ).fetchone()
        if override is not None:
            override_id, author, original = override
            pin["override_id"] = override_id
            pin["corrected_by"] = author
            pin["original_bbl"] = float(original) if original is not None else None
            evidence.append(Evidence(source_type="override", source_id=str(override_id)))

        if pin["reportable"]:
            # SR-01: written notice within 24 h of discovery.
            filed = conn.execute(
                """
                SELECT event_id, hours_to_report FROM regulatory_events
                WHERE related_inspection_id = ? AND event_type = 'spill_report_filed'
                ORDER BY date LIMIT 1
                """,
                [iid],
            ).fetchone()
            due = ts + timedelta(hours=24)
            pin["notice_due_by"] = due.strftime("%Y-%m-%d %H:%M")
            pin["notice_on_record"] = filed is not None
            pin["notice_overdue"] = filed is None and as_of > due
            if filed is not None:
                pin["notice_event_id"] = filed[0]
                pin["notice_hours_to_report"] = filed[1]
                evidence.append(Evidence(source_type="regulatory", source_id=filed[0]))
        pins.append(pin)

    return ToolResult[dict](
        data={
            "start": window_start.date().isoformat(),
            "end": as_of.date().isoformat(),
            "reportable_threshold_bbl": REPORTABLE_BBL,
            "pins": pins,
        },
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
