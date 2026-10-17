"""Tools: record_feedback, record_shared_note, record_claim, recall_user_context.

Four ways data enters the system at runtime, each at an explicit scope so
later reads apply them consistently (DECISIONS.md D9):
  - record_feedback: a correction to raw data or a well-type-wide rule
    (Q12, Q13, Q14) — writes `overrides`, applied by the views in views.sql.
  - record_shared_note: a fact anyone can see (Q16) — writes `context_notes`
    with source='runtime', picked up by resolve_well_state() like a
    dataset-provided note.
  - record_claim: an unconfirmed statement that may contradict the data
    (Q17, Q18) — writes `shared_claims`, then immediately re-resolves the
    well so the caller gets back any conflict the claim creates.
  - recall_user_context: per-user memory (Q11, Q16 — must NOT leak between
    users) — scans that user's own conversation turns for well/area
    mentions, most recent first.

See PLAN.md Phase 3.3.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

import duckdb

from backend.db.overrides import record_override
from backend.db.resolver import resolve_well_state
from backend.models.schemas import Evidence, ToolResult

_WELL_ID_RE = re.compile(r"\bWELL-\d{4}\b")


def record_feedback(
    conn: duckdb.DuckDBPyConnection,
    scope_type: Literal["inspection", "well", "well_type", "global"],
    scope_id: str,
    field: str,
    new_value: Any,
    user_id: str,
    reason: str | None = None,
) -> ToolResult[dict]:
    """Correct a raw value or set a well-type-wide rule (Q12, Q13, Q14)."""
    override_id = record_override(conn, scope_type, scope_id, field, new_value, user_id, reason)
    return ToolResult[dict](
        data={
            "override_id": override_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "field": field,
            "new_value": new_value,
        },
        evidence=[Evidence(source_type="override", source_id=str(override_id))],
    )


def record_shared_note(
    conn: duckdb.DuckDBPyConnection,
    well_id: str,
    text: str,
    user_id: str,
    valid_from: date | None = None,
    valid_to: date | None = None,
    note_type: str = "operator_feedback",
) -> ToolResult[dict]:
    """Share a fact about a well with every user (Q16)."""
    conn.execute("CREATE SEQUENCE IF NOT EXISTS context_notes_runtime_id_seq START 1")
    seq = conn.execute("SELECT nextval('context_notes_runtime_id_seq')").fetchone()[0]
    note_id = f"CTX-RT-{seq:06d}"

    conn.execute(
        """
        INSERT INTO context_notes
            (note_id, asset_id, created_at, author_user_id, note_type, text, valid_from, valid_to, source)
        VALUES (?, ?, current_timestamp, ?, ?, ?, ?, ?, 'runtime')
        """,
        [note_id, well_id, user_id, note_type, text, valid_from, valid_to],
    )
    return ToolResult[dict](
        data={"note_id": note_id, "asset_id": well_id, "text": text},
        evidence=[Evidence(source_type="context", source_id=note_id)],
    )


def record_claim(
    conn: duckdb.DuckDBPyConnection,
    user_id: str,
    scope_type: Literal["well"],
    scope_id: str,
    text: str,
) -> ToolResult[dict]:
    """Record a statement that may or may not match the data; flag any
    conflict against the current resolved state immediately (Q17, Q18)."""
    row = conn.execute(
        """
        INSERT INTO shared_claims (user_id, scope_type, scope_id, text)
        VALUES (?, ?, ?, ?)
        RETURNING id
        """,
        [user_id, scope_type, scope_id, text],
    ).fetchone()
    claim_id = row[0]

    conflicts: list[dict] = []
    evidence = [Evidence(source_type="claim", source_id=str(claim_id))]
    if scope_type == "well":
        state = resolve_well_state(conn, scope_id)
        conflicts = [c.model_dump() for c in state.conflicts]

    return ToolResult[dict](
        data={"claim_id": claim_id, "status": "unconfirmed", "conflicts": conflicts},
        evidence=evidence,
    )


def recall_user_context(
    conn: duckdb.DuckDBPyConnection,
    user_id: str,
    hint: str | None = None,
    limit: int = 5,
) -> ToolResult[dict]:
    """What wells/areas has this user been talking about recently? Private
    to this user — never reads another user's conversations (Q11, Q16)."""
    if hint:
        rows = conn.execute(
            """
            SELECT conversation_id, turn_id, text, ts
            FROM conversations
            WHERE user_id = ? AND text ILIKE ?
            ORDER BY ts DESC
            LIMIT 50
            """,
            [user_id, f"%{hint}%"],
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT conversation_id, turn_id, text, ts
            FROM conversations
            WHERE user_id = ?
            ORDER BY ts DESC
            LIMIT 50
            """,
            [user_id],
        ).fetchall()

    area_names = [r[0] for r in conn.execute("SELECT name FROM areas").fetchall()]

    seen_wells: dict[str, datetime] = {}
    seen_areas: dict[str, datetime] = {}
    for _conv_id, _turn_id, text, ts in rows:
        for well_id in _WELL_ID_RE.findall(text):
            seen_wells.setdefault(well_id, ts)
        for area_name in area_names:
            if area_name in text:
                seen_areas.setdefault(area_name, ts)

    wells = sorted(seen_wells, key=lambda w: seen_wells[w], reverse=True)[:limit]
    areas = sorted(seen_areas, key=lambda a: seen_areas[a], reverse=True)[:limit]

    return ToolResult[dict](
        data={"user_id": user_id, "has_history": len(rows) > 0, "well_ids": wells, "area_names": areas},
        evidence=[],
    )
