"""Overrides engine: record and apply scoped corrections/feedback.

The actual *application* of overrides to reads happens in SQL views
(v_effective_inspections, v_corrected_spills, v_well_type_rules_effective —
see views.sql). This module is the *write* side: it validates a scope,
looks up the original value so it's never lost, and inserts the override
row. Every tool that lets a user correct data or set a scoped rule
(record_feedback in Phase 3.3) goes through record_override().

Scopes (must match views.sql's expectations):
  - 'inspection' + inspection_id : fields 'wellhead_temp_c',
    'ambient_temp_c', 'spill_est_volume_bbl', 'spill_detected'
  - 'well_type'  + 'T200'|'T350' : field 'rca_corrective_count' (Q14)
  - 'well'       + asset_id      : reserved for future well-scoped fields
  - 'global'     + '*'           : reserved for fleet-wide rules

See PLAN.md Phase 2.2, DECISIONS.md D9.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import duckdb

ScopeType = Literal["inspection", "well", "well_type", "global"]

# Which (scope_type, field) pairs are known and which table/column they
# read the "original" value from, so record_override() can auto-populate
# original_value without the caller having to fetch it first.
_ORIGINAL_VALUE_LOOKUP: dict[tuple[str, str], tuple[str, str, str]] = {
    # (scope_type, field): (table, id_column, value_column)
    ("inspection", "wellhead_temp_c"): ("inspections", "inspection_id", "wellhead_temp_c"),
    ("inspection", "ambient_temp_c"): ("inspections", "inspection_id", "ambient_temp_c"),
    ("inspection", "spill_est_volume_bbl"): ("inspections", "inspection_id", "spill_est_volume_bbl"),
    ("inspection", "spill_detected"): ("inspections", "inspection_id", "spill_detected"),
    ("well_type", "rca_corrective_count"): ("well_type_rules", "well_type", "rca_corrective_count"),
}


# Fields that the views in views.sql actually honour, per scope. An override on
# any other field would be stored but silently ignored by every read, so
# record_override() rejects it up front (the caller / agent can retry).
_SUPPORTED_FIELDS: dict[str, frozenset[str]] = {
    "inspection": frozenset(
        {"wellhead_temp_c", "spill_est_volume_bbl", "spill_detected"}
    ),
    "well_type": frozenset({"rca_corrective_count"}),
}


def _coerce_value(scope_type: str, field: str, value: Any) -> Any:
    """Normalise an LLM-supplied value to the type the views CAST it to."""
    try:
        if field in ("wellhead_temp_c", "spill_est_volume_bbl"):
            return float(value)
        if field == "rca_corrective_count":
            number = float(value)
            if number != int(number) or number < 1:
                raise ValueError(number)
            return int(number)
        if field == "spill_detected":
            if isinstance(value, str):
                return value.strip().lower() in ("true", "yes", "1")
            return bool(value)
    except (TypeError, ValueError) as exc:
        raise InvalidOverrideScope(
            f"Invalid value {value!r} for {scope_type} field {field!r}."
        ) from exc
    return value


class InvalidOverrideScope(ValueError):
    """Raised when (scope_type, scope_id, field) doesn't resolve to a real row."""


def _fetch_original_value(
    conn: duckdb.DuckDBPyConnection, scope_type: str, scope_id: str, field: str
) -> Any:
    key = (scope_type, field)
    if key not in _ORIGINAL_VALUE_LOOKUP:
        # Unknown (scope_type, field) combination — allowed, but we can't
        # look up an original value, so the caller must supply one or we
        # record None.
        return None
    table, id_col, value_col = _ORIGINAL_VALUE_LOOKUP[key]
    row = conn.execute(
        f"SELECT {value_col} FROM {table} WHERE {id_col} = ?", [scope_id]
    ).fetchone()
    if row is None:
        raise InvalidOverrideScope(
            f"No row in {table} with {id_col} = {scope_id!r}; cannot record an override against it."
        )
    return row[0]


def record_override(
    conn: duckdb.DuckDBPyConnection,
    scope_type: ScopeType,
    scope_id: str,
    field: str,
    new_value: Any,
    author_user_id: str,
    reason: str | None = None,
) -> int:
    """Record a correction or feedback rule. Returns the new override's id.

    The original value is looked up automatically when the (scope_type,
    field) pair is known (see _ORIGINAL_VALUE_LOOKUP); otherwise it is
    recorded as null. Raises InvalidOverrideScope if scope_id doesn't
    resolve to a real row for a known field, so a typo'd well or
    inspection ID fails loudly instead of silently being ignored by the
    views later.
    """
    if scope_type not in ("inspection", "well", "well_type", "global"):
        raise ValueError(f"Unknown scope_type: {scope_type!r}")

    scope_id = scope_id.strip()
    if scope_type == "well_type":
        scope_id = scope_id.upper()  # 't350' -> 'T350': scope ids are case-sensitive in SQL
    supported = _SUPPORTED_FIELDS.get(scope_type)
    if supported is not None and field not in supported:
        raise InvalidOverrideScope(
            f"Field {field!r} cannot be overridden at scope {scope_type!r}; "
            f"supported fields: {sorted(supported)}."
        )
    new_value = _coerce_value(scope_type, field, new_value)
    if scope_type == "well_type":
        known = {r[0] for r in conn.execute("SELECT well_type FROM well_type_rules").fetchall()}
        if scope_id not in known:
            raise InvalidOverrideScope(
                f"Unknown well type {scope_id!r}; known well types: {sorted(known)}."
            )

    original = _fetch_original_value(conn, scope_type, scope_id, field)

    row = conn.execute(
        """
        INSERT INTO overrides
            (scope_type, scope_id, field, original_value, new_value, author_user_id, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        [
            scope_type,
            scope_id,
            field,
            json.dumps(original) if original is not None else None,
            json.dumps(new_value),
            author_user_id,
            reason,
        ],
    ).fetchone()
    return row[0]


def list_overrides(
    conn: duckdb.DuckDBPyConnection,
    scope_type: str | None = None,
    scope_id: str | None = None,
) -> list[dict]:
    """List overrides, optionally filtered by scope. Most recent first."""
    query = "SELECT id, scope_type, scope_id, field, original_value, new_value, author_user_id, reason, created_at FROM overrides"
    conditions = []
    params: list[Any] = []
    if scope_type is not None:
        conditions.append("scope_type = ?")
        params.append(scope_type)
    if scope_id is not None:
        conditions.append("scope_id = ?")
        params.append(scope_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC"

    cols = ["id", "scope_type", "scope_id", "field", "original_value", "new_value",
            "author_user_id", "reason", "created_at"]
    return [dict(zip(cols, row)) for row in conn.execute(query, params).fetchall()]


def apply_overrides_to_inspection(conn: duckdb.DuckDBPyConnection, inspection_id: str) -> dict[str, Any]:
    """Return the effective (post-override) row for one inspection, as a
    plain dict. Convenience wrapper around v_effective_inspections for
    tools that need a single row rather than writing SQL themselves.
    """
    row = conn.execute(
        """
        SELECT inspection_id, asset_id, inspected_at, inspector_user_id, method,
               ambient_temp_c, wellhead_temp_c, spill_detected, spill_area_pct,
               spill_est_volume_bbl, notes, has_override
        FROM v_effective_inspections
        WHERE inspection_id = ?
        """,
        [inspection_id],
    ).fetchone()
    if row is None:
        raise InvalidOverrideScope(f"No inspection {inspection_id!r}")
    cols = ["inspection_id", "asset_id", "inspected_at", "inspector_user_id", "method",
            "ambient_temp_c", "wellhead_temp_c", "spill_detected", "spill_area_pct",
            "spill_est_volume_bbl", "notes", "has_override"]
    return dict(zip(cols, row))


def apply_overrides_to_rule(
    conn: duckdb.DuckDBPyConnection, well_type: str, rule_name: str, default: Any = None
) -> Any:
    """Return the effective value of a well_type_rules field for one well
    type, applying any well_type-scoped override (Q14). `rule_name` must
    be a column on well_type_rules (e.g. 'rca_corrective_count',
    'producing_threshold_c', 'max_wellhead_c').
    """
    allowed_columns = {
        "producing_threshold_c", "max_wellhead_c",
        "rca_corrective_count", "rca_lookback_days",
    }
    if rule_name not in allowed_columns:
        raise ValueError(f"Unknown well_type_rules column: {rule_name!r}")

    row = conn.execute(
        f"SELECT {rule_name} FROM v_well_type_rules_effective WHERE well_type = ?",
        [well_type],
    ).fetchone()
    if row is None:
        return default
    return row[0]
