"""State resolver: single source of truth for well state + reasons + conflicts.

The resolver is the correctness contract (DECISIONS.md D9). Instead of the
LLM reasoning over scattered records, this one function joins every signal
in a fixed priority and returns a structured answer the LLM only narrates.

Priority of signals (highest wins, lower ones add context/conflicts):
  1. v_well_latest_inspection: producing flag derived from (wellhead - ambient)
     against the well-type threshold. v_effective_inspections already applies
     per-inspection temperature overrides (Q12 flow), so this reflects
     corrections automatically.
  2. Formal `correction` notes on that inspection: can flip spill detection
     (handled in v_corrected_spills) but do NOT flip the producing flag on
     their own — temperature corrections flow through step 1's override.
  3. Active context notes on the well (operator_feedback / operator_note /
     correction) whose [valid_from, valid_to] covers `as_of`: added as
     reasons (e.g. "planned shut-in by Jake Morales until 20 Oct") and,
     when they contradict the derived state, as conflicts.
  4. inspections.notes free-text on the latest inspection row: surfaced
     verbatim as a reason, and if an override flipped the state TO
     producing while the inspector wrote "not moving" / "stopped" / "down",
     surfaced as a conflict (Q12: inspector's "Pump jack not moving."
     disagrees with Michael's corrected 41 C reading).
  5. wells.recorded_status: never used as the source of truth; emitted as
     "recorded status says X, which is out of date" when it disagrees.
  6. shared_claims with status='unconfirmed' on this well that contradict
     the derived state (Q17: Tyler's "running fine" vs the 0.4 C delta).

See PLAN.md Phase 2.1, REQUIREMENTS_COVERAGE.md section C.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import duckdb

from backend.models.schemas import Conflict, WellState

# Words in an inspector's free-text note that imply the well is stopped.
# Used to flag the "override-flipped-to-producing-but-note-says-stopped"
# conflict in Q12.
_STOPPED_WORDS = ("not moving", "stopped", "shut in", "shutdown", "down", "offline")


def _fmt_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%-d %b %Y")
    return str(value)


def _user_name(conn: duckdb.DuckDBPyConnection, user_id: str | None) -> str:
    if not user_id:
        return "unknown"
    row = conn.execute("SELECT name FROM users WHERE user_id = ?", [user_id]).fetchone()
    return row[0] if row else user_id


def resolve_well_state(
    conn: duckdb.DuckDBPyConnection,
    asset_id: str,
    as_of: datetime | None = None,
) -> WellState:
    """Resolve the producing/not-producing state of a well at a point in time."""
    as_of = as_of or datetime.now()

    # 1. Latest effective inspection at or before as_of.
    row = conn.execute(
        """
        SELECT p.inspection_id, p.inspected_at, p.wellhead_temp_c,
               p.ambient_temp_c, p.delta_c, p.producing_threshold_c,
               p.is_producing, p.well_type,
               i.inspector_user_id, i.method, i.notes
        FROM v_is_producing p
        JOIN inspections i ON i.inspection_id = p.inspection_id
        WHERE p.asset_id = ? AND p.inspected_at <= ?
        ORDER BY p.inspected_at DESC
        LIMIT 1
        """,
        [asset_id, as_of],
    ).fetchone()

    if row is None:
        return WellState(
            state="unknown",
            reasons=[f"No inspections recorded for {asset_id} at or before {_fmt_date(as_of)}."],
            conflicts=[],
        )

    (
        inspection_id, inspected_at, wellhead_c, ambient_c, delta_c,
        threshold_c, is_producing, well_type, inspector_id, method, insp_notes,
    ) = row

    reasons: list[str] = []
    conflicts: list[Conflict] = []
    state = "producing" if is_producing else "not_producing"

    # Primary reason: who inspected, when, how, and the delta vs threshold.
    inspector_name = _user_name(conn, inspector_id)
    reasons.append(
        f"Latest inspection {inspection_id} on {_fmt_date(inspected_at)}"
        f" by {inspector_name} ({method or 'manual'})."
    )
    reasons.append(
        f"Wellhead was {delta_c:+.1f} C vs. ambient, "
        f"{'at or above' if is_producing else 'below'} the {well_type} limit of {threshold_c:.0f} C."
    )

    # 2. Any inspection-scoped override that was applied — mention the author.
    override_rows = conn.execute(
        """
        SELECT field, author_user_id, reason, new_value
        FROM overrides
        WHERE scope_type = 'inspection' AND scope_id = ?
        ORDER BY created_at DESC
        """,
        [inspection_id],
    ).fetchall()
    override_fields: set[str] = set()
    for field, author_id, reason_text, new_value in override_rows:
        override_fields.add(field)
        author = _user_name(conn, author_id)
        snippet = f"'{reason_text}'" if reason_text else "no reason given"
        reasons.append(
            f"Reading corrected by {author}: {field} set to {new_value} ({snippet})."
        )

    # 3. Active context notes on the well (valid at as_of).
    note_rows = conn.execute(
        """
        SELECT note_id, note_type, author_user_id, text, inspection_id,
               valid_from, valid_to
        FROM context_notes
        WHERE asset_id = ?
          AND (valid_from IS NULL OR valid_from <= ?)
          AND (valid_to   IS NULL OR valid_to   >= ?)
        ORDER BY created_at DESC
        """,
        [asset_id, as_of.date(), as_of.date()],
    ).fetchall()
    for note_id, note_type, author_id, text, note_inspection, vfrom, vto in note_rows:
        # Formal corrections on the LATEST inspection are already handled by
        # the overrides pipeline; skip them here to avoid double-counting.
        if note_type == "correction" and note_inspection == inspection_id:
            continue
        author = _user_name(conn, author_id)
        reasons.append(f"Note {note_id} by {author} ({note_type}): {text}")

    # 4. Inspector's free-text note on this inspection.
    if insp_notes:
        reasons.append(f'Inspector note on {inspection_id}: "{insp_notes}"')
        # Q12 conflict: override flipped state TO producing, but the
        # inspector observed the well is stopped. Surface the contradiction
        # so the UI can ask the correcting user to confirm.
        if (
            is_producing
            and "wellhead_temp_c" in override_fields
            and any(w in insp_notes.lower() for w in _STOPPED_WORDS)
        ):
            conflicts.append(
                Conflict(
                    type="override_vs_inspector_note",
                    left=f"Corrected reading implies producing (delta {delta_c:+.1f} C)",
                    right=f'Inspector wrote "{insp_notes}" on the same inspection',
                )
            )

    # 5. recorded_status comparison. "active" means producing, "down" means not.
    rec_row = conn.execute(
        "SELECT recorded_status FROM wells WHERE asset_id = ?", [asset_id]
    ).fetchone()
    if rec_row and rec_row[0]:
        recorded = rec_row[0].lower()
        implied_producing = recorded == "active"
        if implied_producing != is_producing:
            reasons.append(f"Recorded status is '{rec_row[0]}', which is out of date.")

    # 6. Unconfirmed claims that contradict the derived state.
    claim_rows = conn.execute(
        """
        SELECT id, user_id, text, created_at
        FROM shared_claims
        WHERE scope_type = 'well' AND scope_id = ? AND status = 'unconfirmed'
        ORDER BY created_at DESC
        """,
        [asset_id],
    ).fetchall()
    for _claim_id, user_id, text, _created_at in claim_rows:
        claimer = _user_name(conn, user_id)
        lowered = text.lower()
        claim_implies_producing = any(
            kw in lowered for kw in ("running", "online", "producing", "back online", "fine")
        )
        claim_implies_down = any(
            kw in lowered for kw in ("shut in", "down", "stopped", "offline", "shutdown")
        )
        if is_producing and claim_implies_down:
            conflicts.append(
                Conflict(
                    type="claim_vs_data",
                    left=f'{claimer} says: "{text}"',
                    right=f"Latest inspection {inspection_id} shows the well is producing.",
                )
            )
        elif (not is_producing) and claim_implies_producing:
            conflicts.append(
                Conflict(
                    type="claim_vs_data",
                    left=f'{claimer} says: "{text}"',
                    right=f"Latest inspection {inspection_id} shows the well is not producing"
                          f" (delta {delta_c:+.1f} C).",
                )
            )

    return WellState(state=state, reasons=reasons, conflicts=conflicts)
