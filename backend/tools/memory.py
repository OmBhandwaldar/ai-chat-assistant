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

import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import duckdb

from backend.db.overrides import InvalidOverrideScope, record_override
from backend.db.resolver import resolve_inspection_notes, resolve_well_state
from backend.models.schemas import Evidence, ToolResult
from backend.tools.sop import sop_breaches
from backend.tools.spills import REPORTABLE_BBL
from backend.tools.state import _evidence_from_reasons

_WELL_ID_RE = re.compile(r"\bWELL-\d{4}\b")

# "Today" in the dataset; every tool defaults to it (see state.py / spills.py).
DEFAULT_AS_OF = datetime(2026, 10, 16)

_SHARED_NOTE_TYPES = ("operator_feedback", "operator_note")
# Models sometimes "close" a turn by calling a write tool with a dummy argument.
# Those writes must not land: they crash on unknown scopes and pollute later turns.
_JUNK_TOKENS = frozenset({
    "", "noop", "none", "placeholder", "null", "n/a", "na", "nil", "test", "dummy",
})


def _is_junk(value: Any) -> bool:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return False
    return str(value if value is not None else "").strip().casefold() in _JUNK_TOKENS


def _rejected(error: str, **fields: Any) -> ToolResult[dict]:
    return ToolResult[dict](data={"recorded": False, "error": error, **fields}, evidence=[])


def _user_name(conn: duckdb.DuckDBPyConnection, user_id: str | None) -> str:
    row = conn.execute("SELECT name FROM users WHERE user_id = ?", [user_id]).fetchone()
    return row[0] if row else (user_id or "unknown")


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


# ---------------------------------------------------------------------------
# record_feedback — corrections and well-type rules (Q12, Q13, Q14)
# ---------------------------------------------------------------------------

def record_feedback(
    conn: duckdb.DuckDBPyConnection,
    scope_type: Literal["inspection", "well", "well_type", "global"],
    scope_id: str,
    field: str,
    new_value: Any,
    user_id: str,
    reason: str | None = None,
    as_of: datetime | None = None,
    fleet_dir: Path | str | None = None,
) -> ToolResult[dict]:
    """Correct a raw value or set a well-type-wide rule (Q12, Q13, Q14).

    The original value is kept. The result also carries the immediate impact
    so the agent can narrate it (Q12: new state + disagreeing inspector note,
    Q13: reportable? SR-01/SR-02/SR-03 outcome, Q14: which wells the rule
    covers). Bad scope ids / unsupported fields come back as
    `recorded=False` + `error` instead of raising, so the agent can retry.
    """
    as_of = as_of or DEFAULT_AS_OF
    if _is_junk(scope_type) or _is_junk(scope_id) or _is_junk(field) or _is_junk(new_value):
        return _rejected(
            "Refusing a placeholder correction. Pass a real scope_type, scope_id, field and value.",
            scope_type=scope_type, scope_id=scope_id, field=field,
        )
    try:
        override_id = record_override(conn, scope_type, scope_id, field, new_value, user_id, reason)
    except (InvalidOverrideScope, ValueError) as exc:
        return ToolResult[dict](
            data={"recorded": False, "error": str(exc), "scope_type": scope_type,
                  "scope_id": scope_id, "field": field},
            evidence=[],
        )

    stored_scope_id, original, stored_new = conn.execute(
        "SELECT scope_id, original_value, new_value FROM overrides WHERE id = ?", [override_id]
    ).fetchone()
    evidence = [Evidence(source_type="override", source_id=str(override_id))]
    data: dict[str, Any] = {
        "recorded": True,
        "override_id": override_id,
        "scope_type": scope_type,
        "scope_id": stored_scope_id,
        "field": field,
        "original_value": _loads(original),
        "new_value": _loads(stored_new),
        "author": _user_name(conn, user_id),
        "reason": reason,
    }

    if scope_type == "inspection":
        row = conn.execute(
            "SELECT asset_id FROM inspections WHERE inspection_id = ?", [stored_scope_id]
        ).fetchone()
        asset_id = row[0]
        data["asset_id"] = asset_id
        evidence.append(Evidence(source_type="inspection", source_id=stored_scope_id))
        if field == "wellhead_temp_c":
            state = resolve_well_state(conn, asset_id, as_of=as_of)
            data["resulting_state"] = {
                "state": state.state,
                "reasons": state.reasons,
                "conflicts": [c.model_dump() for c in state.conflicts],
            }
            # Q12: a correction that disagrees with the inspector's own note must be
            # confirmed by the person who made it.
            data["confirmation_needed"] = any(
                c.type == "override_vs_inspector_note" for c in state.conflicts
            )
        else:
            data["impact"], extra = _spill_impact(conn, stored_scope_id, as_of, fleet_dir)
            evidence.extend(extra)
    elif scope_type == "well_type":
        data["applies_to"] = f"every {stored_scope_id} well (and only {stored_scope_id} wells)"
        data["manual_value"] = _loads(original)

    return ToolResult[dict](data=data, evidence=evidence)


def _spill_impact(
    conn: duckdb.DuckDBPyConnection,
    inspection_id: str,
    as_of: datetime,
    fleet_dir: Path | str | None,
) -> tuple[dict, list[Evidence]]:
    """What a spill-volume correction changes: reportability and SOP outcomes (Q13)."""
    asset_id, inspected_at, original_volume, volume = conn.execute(
        """
        SELECT i.asset_id, i.inspected_at, i.spill_est_volume_bbl, e.spill_est_volume_bbl
        FROM inspections i JOIN v_effective_inspections e USING (inspection_id)
        WHERE i.inspection_id = ?
        """,
        [inspection_id],
    ).fetchone()
    verdict = resolve_inspection_notes(conn, inspection_id)
    stands = verdict.detection_stands
    reportable = bool(stands and volume is not None and volume >= REPORTABLE_BBL)
    was_reportable = bool(original_volume is not None and original_volume >= REPORTABLE_BBL)

    impact: dict[str, Any] = {
        "asset_id": asset_id,
        "inspection_id": inspection_id,
        "inspected_at": str(inspected_at),
        "detection_stands": stands,
        "volume_bbl": volume,
        "original_volume_bbl": original_volume,
        "reportable_threshold_bbl": REPORTABLE_BBL,
        "reportable": reportable,
        "newly_reportable": reportable and not was_reportable,
        "related_repair_ids": verdict.repair_ids,
        "sop_findings": [],
    }
    evidence: list[Evidence] = []
    if not stands:
        return impact, evidence

    reg_ids = {
        r[0] for r in conn.execute(
            "SELECT event_id FROM regulatory_events WHERE related_inspection_id = ?",
            [inspection_id],
        ).fetchall()
    }
    related = {inspection_id, *verdict.repair_ids, *reg_ids}
    days_back = max(365, (as_of - inspected_at).days + 1)
    result = sop_breaches(
        conn, asset_id, lookback_days=days_back, as_of=as_of.date(), fleet_dir=fleet_dir
    )
    for status_key in ("breaches", "open_items"):
        for finding in result.data[status_key]:
            if finding["evidence_id"] in related:
                impact["sop_findings"].append(dict(finding))
    evidence.extend(e for e in result.evidence if e.source_id in related)
    if impact["newly_reportable"]:
        # Repair reports written from the old estimate (e.g. "moderate", no reporting
        # duty) no longer hold; flag them rather than silently trusting them.
        impact["repair_reports_based_on_old_estimate"] = verdict.repair_ids
    return impact, evidence


# ---------------------------------------------------------------------------
# record_shared_note — facts for everyone (Q16)
# ---------------------------------------------------------------------------

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
    if _is_junk(well_id) or _is_junk(text):
        return _rejected("Refusing a placeholder shared note.", asset_id=well_id)
    if conn.execute("SELECT 1 FROM wells WHERE asset_id = ?", [well_id]).fetchone() is None:
        return ToolResult[dict](
            data={"recorded": False, "error": f"Unknown well {well_id!r}."}, evidence=[]
        )
    if note_type not in _SHARED_NOTE_TYPES:
        return ToolResult[dict](
            data={"recorded": False,
                  "error": f"note_type must be one of {list(_SHARED_NOTE_TYPES)}; formal "
                           "corrections are made with record_feedback."},
            evidence=[],
        )
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
        data={
            "recorded": True,
            "note_id": note_id,
            "asset_id": well_id,
            "text": text,
            "author": _user_name(conn, user_id),
            "valid_from": valid_from.isoformat() if valid_from else None,
            "valid_to": valid_to.isoformat() if valid_to else None,
            "visible_to": "all users",
        },
        evidence=[Evidence(source_type="context", source_id=note_id)],
    )


# ---------------------------------------------------------------------------
# record_claim — statements that may contradict the data (Q17)
# ---------------------------------------------------------------------------

def record_claim(
    conn: duckdb.DuckDBPyConnection,
    user_id: str,
    scope_type: Literal["well"],
    scope_id: str,
    text: str,
    as_of: datetime | None = None,
) -> ToolResult[dict]:
    """Record a statement that may or may not match the data; flag any
    conflict against the current resolved state immediately (Q17, Q18).

    The claim is always stored with status='unconfirmed' (both it and the data
    stay on record, nothing is overwritten). `conflicts` holds only the
    conflicts this claim itself creates; `resolved_state` is the data side the
    agent should quote back ("the 15 Oct inspection shows ...").
    """
    as_of = as_of or DEFAULT_AS_OF
    if scope_type != "well" or _is_junk(scope_type) or _is_junk(scope_id) or _is_junk(text):
        return _rejected(
            "record_claim only accepts scope_type 'well' and a real statement.",
            scope_type=scope_type, scope_id=scope_id,
        )
    if scope_type == "well" and conn.execute(
        "SELECT 1 FROM wells WHERE asset_id = ?", [scope_id]
    ).fetchone() is None:
        return ToolResult[dict](
            data={"recorded": False, "error": f"Unknown well {scope_id!r}."}, evidence=[]
        )

    def _keys(state) -> Counter:
        return Counter((c.type, c.left, c.right) for c in state.conflicts)

    before = _keys(resolve_well_state(conn, scope_id, as_of=as_of)) if scope_type == "well" else Counter()

    claim_id = conn.execute(
        """
        INSERT INTO shared_claims (user_id, scope_type, scope_id, text, status)
        VALUES (?, ?, ?, ?, 'unconfirmed')
        RETURNING id
        """,
        [user_id, scope_type, scope_id, text],
    ).fetchone()[0]

    evidence = [Evidence(source_type="claim", source_id=str(claim_id))]
    conflicts: list[dict] = []
    resolved: dict[str, Any] | None = None
    if scope_type == "well":
        state = resolve_well_state(conn, scope_id, as_of=as_of)
        new = _keys(state) - before
        for c in state.conflicts:
            key = (c.type, c.left, c.right)
            if new[key] > 0:
                new[key] -= 1
                conflicts.append(c.model_dump())
        resolved = {"state": state.state, "reasons": state.reasons}
        evidence.extend(_evidence_from_reasons(state.reasons))

    return ToolResult[dict](
        data={
            "recorded": True,
            "claim_id": claim_id,
            "author": _user_name(conn, user_id),
            "status": "unconfirmed",
            "contradicts_data": bool(conflicts),
            "conflicts": conflicts,
            "resolved_state": resolved,
            "needs_confirmation": bool(conflicts),
        },
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# recall_user_context — private memory (Q11, Q16)
# ---------------------------------------------------------------------------

def recall_user_context(
    conn: duckdb.DuckDBPyConnection,
    user_id: str,
    hint: str | None = None,
    limit: int = 5,
) -> ToolResult[dict]:
    """What wells/areas has this user been talking about recently? Private
    to this user — every read is filtered by user_id, so one user's chats are
    never visible to another (Q11, Q16).

    `hint` narrows to turns containing that phrase when those turns name a well
    or area. A free-form hint, or one that only matches the question just stored
    for this turn, falls back to the user's most recent turns. `has_history` is
    True only when this user's own earlier turns mention at least one well or area.
    """
    area_names = [r[0] for r in conn.execute("SELECT name FROM areas").fetchall()]

    def _recent(hint_filter: str | None) -> list[tuple]:
        if not user_id:
            return []
        if hint_filter:
            return conn.execute(
                """
                SELECT c.conversation_id, c.turn_id, c.text, c.ts, c.role
                FROM conversations c
                WHERE c.user_id = ?
                  AND EXISTS (
                      SELECT 1 FROM conversations m
                      WHERE m.user_id = c.user_id
                        AND m.conversation_id = c.conversation_id
                        AND m.turn_id = c.turn_id
                        AND m.text ILIKE ?
                  )
                ORDER BY c.ts DESC
                LIMIT 50
                """,
                [user_id, f"%{hint_filter}%"],
            ).fetchall()
        return conn.execute(
            """
            SELECT conversation_id, turn_id, text, ts, role
            FROM conversations
            WHERE user_id = ?
            ORDER BY ts DESC
            LIMIT 50
            """,
            [user_id],
        ).fetchall()

    def _mentions(candidates: list[tuple]) -> bool:
        for _conv_id, _turn_id, text, _ts, _role in candidates:
            if _WELL_ID_RE.search(text) or any(name and name in text for name in area_names):
                return True
        return False

    rows = _recent(hint) if hint else _recent(None)
    # The current question is stored before the agent runs, so a hint copied from
    # it matches that row and would hide the earlier chat the hint was meant to find.
    if hint and not _mentions(rows):
        rows = _recent(None)

    seen_wells: dict[str, datetime] = {}
    seen_areas: dict[str, datetime] = {}
    for _conv_id, _turn_id, text, ts, _role in rows:
        for well_id in _WELL_ID_RE.findall(text):
            seen_wells.setdefault(well_id, ts)
        for area_name in area_names:
            if area_name in text:
                seen_areas.setdefault(area_name, ts)

    wells = sorted(seen_wells, key=lambda w: seen_wells[w], reverse=True)[:limit]
    areas = sorted(seen_areas, key=lambda a: seen_areas[a], reverse=True)[:limit]
    found = bool(wells or areas)

    return ToolResult[dict](
        data={
            "user_id": user_id,
            "has_history": found,
            "turn_count": len(rows),
            "well_ids": wells,
            "area_names": areas,
            "note": None if found else (
                "No earlier conversation of this user mentions any well or area."
            ),
        },
        evidence=[],
    )
