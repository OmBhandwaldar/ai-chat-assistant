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

from backend.db.scope import resolve_scope, unresolved_fields
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
    scope = resolve_scope(conn, area_id=area_id, team_id=team_id)
    if not scope.ok:
        return ToolResult[dict](
            data={
                "as_of": as_of.isoformat(),
                "equipment_kind": equipment_kind,
                "wells": [],
                "rules": [],
                **unresolved_fields(scope),
            },
            evidence=[],
        )
    area_id, team_id = scope.area_id, scope.team_id

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
        """,
        [equipment_kind, as_of, as_of, *params],
    ).fetchall()

    rules = _threshold_rules(conn, equipment_kind)
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
            "threshold_source": rules.get(w_type, {}).get("source", "manual"),
        })
        for mid in maintenance_ids:
            evidence.append(Evidence(source_type="maintenance", source_id=mid))

    for rule in rules.values():
        if rule["source"] == "override":
            evidence.append(Evidence(source_type="override", source_id=str(rule["override_id"])))

    return ToolResult[dict](
        data={
            "as_of": as_of.isoformat(),
            "equipment_kind": equipment_kind,
            "wells": wells,
            # Per well type: the threshold applied and whether it comes from the manual or
            # from a user's well_type-scoped rule (Q14) — say which in the answer.
            "rules": list(rules.values()),
        },
        evidence=evidence,
    )


def _threshold_rules(conn: duckdb.DuckDBPyConnection, equipment_kind: str) -> dict[str, dict]:
    """Effective RCA threshold per well type, with the override that set it (if any)."""
    rows = conn.execute(
        """
        SELECT r.well_type, r.rca_corrective_count AS manual, e.rca_corrective_count AS effective,
               e.rca_lookback_days
        FROM well_type_rules r
        JOIN v_well_type_rules_effective e ON e.well_type = r.well_type
        ORDER BY r.well_type
        """
    ).fetchall()
    rules: dict[str, dict] = {}
    for well_type, manual, effective, lookback_days in rows:
        rule = {
            "well_type": well_type,
            "equipment_kind": equipment_kind,
            "threshold": effective,
            "manual_threshold": manual,
            "lookback_days": lookback_days,
            "source": "manual",
            "set_by": None,
            "reason": None,
            "override_id": None,
        }
        override = conn.execute(
            """
            SELECT o.id, u.name, o.reason
            FROM overrides o LEFT JOIN users u ON u.user_id = o.author_user_id
            WHERE o.scope_type = 'well_type' AND o.scope_id = ?
              AND o.field = 'rca_corrective_count'
            ORDER BY o.created_at DESC, o.id DESC LIMIT 1
            """,
            [well_type],
        ).fetchone()
        if override is not None:
            rule.update(source="override", override_id=override[0], set_by=override[1],
                        reason=override[2])
        rules[well_type] = rule
    return rules
