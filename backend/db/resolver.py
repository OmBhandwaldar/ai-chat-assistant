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

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime
from typing import Any

import duckdb

from backend.models.schemas import Conflict, WellState

# Words in an inspector's free-text note that imply the well is stopped.
# Used to flag the "override-flipped-to-producing-but-note-says-stopped"
# conflict in Q12.
_UPCOMING_NOTE_DAYS = 90

_STOPPED_WORDS = ("not moving", "stopped", "shut in", "shutdown", "down", "offline")


_DOWN_RE = re.compile(
    r"\b(shut[- ]?in|shutdown|shut down|down|stopped|offline|off[- ]line|not (?:\w+ )?"
    r"(?:running|producing|operating|working|online|fine|ok)|isn't (?:running|producing|"
    r"operating|working|online|fine)|is not (?:running|producing|operating|working|online|"
    r"fine))\b",
    re.IGNORECASE,
)
_UP_RE = re.compile(
    r"\b(running|online|producing|operating|working|fine|up and running)\b", re.IGNORECASE
)
_BACK_ONLINE_RE = re.compile(r"\b(back online|back up|restarted|resumed)\b", re.IGNORECASE)


def classify_claim(text: str) -> tuple[bool, bool]:
    """Return (implies_producing, implies_down) for a free-text claim.

    Negations ("not running", "isn't producing") count as *down*, not as the
    positive keyword they contain. A claim that reads both ways round ("shut in
    until back online") or neither way implies nothing, so it never raises a
    conflict by itself; the agent still sees the raw text.
    """
    down = bool(_DOWN_RE.search(text))
    positive_text = _DOWN_RE.sub(" ", text)
    up = bool(_UP_RE.search(positive_text)) or bool(_BACK_ONLINE_RE.search(text))
    if up and down:
        return False, False
    return up, down


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
        inspection_id, inspected_at, _wellhead_c, _ambient_c, delta_c,
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
        SELECT id, field, author_user_id, reason, new_value, original_value
        FROM overrides
        WHERE scope_type = 'inspection' AND scope_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        [inspection_id],
    ).fetchall()
    override_fields: set[str] = set()
    for override_id, field, author_id, reason_text, new_value, original_value in override_rows:
        override_fields.add(field)
        author = _user_name(conn, author_id)
        snippet = f"'{reason_text}'" if reason_text else "no reason given"
        was = f", was {original_value}" if original_value is not None else ""
        reasons.append(
            f"Reading corrected by {author} (override #{override_id}): "
            f"{field} set to {new_value}{was} ({snippet})."
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

    # 3b. Notes that start in the near future (Q16: "is any downtime planned?").
    # They do not change today's state but must be visible to everyone asking.
    upcoming_rows = conn.execute(
        """
        SELECT note_id, note_type, author_user_id, text, valid_from, valid_to
        FROM context_notes
        WHERE asset_id = ?
          AND valid_from > ?
          AND valid_from <= ? + INTERVAL (?) DAY
        ORDER BY valid_from
        """,
        [asset_id, as_of.date(), as_of.date(), _UPCOMING_NOTE_DAYS],
    ).fetchall()
    for note_id, note_type, author_id, text, vfrom, vto in upcoming_rows:
        author = _user_name(conn, author_id)
        window = f"from {_fmt_date(vfrom)}" + (f" to {_fmt_date(vto)}" if vto else "")
        reasons.append(
            f"Upcoming note {note_id} by {author} ({note_type}, {window}): {text}"
        )

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
        claim_implies_producing, claim_implies_down = classify_claim(text)
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


# ---------------------------------------------------------------------------
# Spill detection verdict (Q18: people disagree about the AI's analysis)
# ---------------------------------------------------------------------------

_DISMISS_RE = re.compile(
    r"produced water|rain ?water|only water|water only|washout|"
    r"not (?:a )?(?:crude|oil|spill|real)",
    re.IGNORECASE,
)
_CONFIRM_RE = re.compile(
    r"\bcrude\b|detection stands|\bconfirmed\b|\bis oil\b|real (?:oil|spill)", re.IGNORECASE
)


@dataclass
class InspectionNotesVerdict:
    """What the notes on one spill inspection say, and whether the detection stands."""

    inspection_id: str
    detection_stands: bool
    notes: list[dict[str, Any]] = dc_field(default_factory=list)
    conflicts: list[Conflict] = dc_field(default_factory=list)
    basis: list[str] = dc_field(default_factory=list)
    repair_ids: list[str] = dc_field(default_factory=list)


def resolve_inspection_notes(
    conn: duckdb.DuckDBPyConnection, inspection_id: str
) -> InspectionNotesVerdict:
    """Decide whether a spill detection stands and surface disagreeing notes.

    Rules (Q4 / Q18):
      - A *formal* correction (context note of type 'correction' on this
        inspection, or a spill_detected=false override) removes the detection.
      - Informal notes (operator_feedback / operator_note that mention the
        inspection id) never remove it. When two of them take opposite sides
        ("produced water" vs "crude oil") both are returned as a
        `note_vs_note` conflict; neither is silently preferred.
      - A spill repair triggered by this inspection is further evidence that
        the detection was acted on as real.
    """
    row = conn.execute(
        "SELECT asset_id FROM inspections WHERE inspection_id = ?", [inspection_id]
    ).fetchone()
    if row is None:
        return InspectionNotesVerdict(inspection_id, detection_stands=False,
                                      basis=[f"No inspection {inspection_id} in the data."])
    asset_id = row[0]

    stands = conn.execute(
        "SELECT 1 FROM v_corrected_spills WHERE inspection_id = ?", [inspection_id]
    ).fetchone() is not None

    note_rows = conn.execute(
        """
        SELECT note_id, note_type, author_user_id, text, created_at
        FROM context_notes
        WHERE asset_id = ? AND (inspection_id = ? OR text LIKE '%' || ? || '%')
        ORDER BY created_at, note_id
        """,
        [asset_id, inspection_id, inspection_id],
    ).fetchall()

    notes: list[dict[str, Any]] = []
    dismissers: list[dict[str, Any]] = []
    confirmers: list[dict[str, Any]] = []
    formal: list[str] = []
    for note_id, note_type, author_id, text, created_at in note_rows:
        if note_type == "correction":
            formal.append(note_id)
        stance = (
            "dismisses" if _DISMISS_RE.search(text)
            else "confirms" if _CONFIRM_RE.search(text)
            else "neutral"
        )
        entry = {
            "note_id": note_id, "note_type": note_type, "author": _user_name(conn, author_id),
            "author_user_id": author_id, "text": text, "stance": stance,
            "created_at": str(created_at),
        }
        notes.append(entry)
        if note_type != "correction":
            if stance == "dismisses":
                dismissers.append(entry)
            elif stance == "confirms":
                confirmers.append(entry)

    conflicts = [
        Conflict(
            type="note_vs_note",
            left=f'{d["author"]} ({d["note_id"]}): "{d["text"]}"',
            right=f'{c["author"]} ({c["note_id"]}): "{c["text"]}"',
        )
        for d in dismissers for c in confirmers
    ]

    repair_ids = [
        r[0] for r in conn.execute(
            "SELECT maintenance_id FROM maintenance WHERE triggered_by_inspection_id = ? "
            "ORDER BY started_at",
            [inspection_id],
        ).fetchall()
    ]

    basis: list[str] = []
    if formal:
        basis.append(f"Formal correction note(s) {', '.join(formal)} remove this detection.")
    elif not stands:
        basis.append("A spill_detected=false override removes this detection.")
    else:
        basis.append(
            f"There is no formal correction for {inspection_id} (no 'correction' note, "
            "no spill_detected override), so the detection stands."
        )
    if repair_ids and stands:
        basis.append(f"Spill repair {', '.join(repair_ids)} was done for this inspection.")

    return InspectionNotesVerdict(
        inspection_id=inspection_id, detection_stands=stands, notes=notes,
        conflicts=conflicts, basis=basis, repair_ids=repair_ids,
    )
