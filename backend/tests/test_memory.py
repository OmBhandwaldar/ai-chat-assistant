"""Tests for backend/tools/memory.py (Phase 3.3).

Each test inserts runtime rows (overrides / context_notes / shared_claims /
conversations) against the real ingested DB and deletes them again at the
end, mirroring the cleanup convention in test_overrides.py.
"""

from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from backend.tools.memory import (
    recall_user_context,
    record_claim,
    record_feedback,
    record_shared_note,
)
from backend.tools.state import wells_not_producing

DB_PATH = Path(__file__).parent.parent.parent / "backend" / "data.duckdb"
AS_OF_DATE = date(2026, 10, 16)


def _conn():
    if not DB_PATH.exists():
        pytest.skip(f"ingested DB not found at {DB_PATH} — run scripts/ingest.py first")
    con = duckdb.connect(str(DB_PATH), read_only=False)
    con.execute(open(DB_PATH.parent / "db" / "views.sql").read())
    return con


def test_record_feedback_q12_then_q2():
    con = _conn()
    before = wells_not_producing(con, area_id="NE-08", as_of=AS_OF_DATE)
    assert {"WELL-0164", "WELL-0170", "WELL-0172", "WELL-0175"} == {
        w["asset_id"] for w in before.data["wells"]
    }

    result = record_feedback(
        con, "inspection", "INS-0164-0205", "wellhead_temp_c", 41.0, "U-201",
        reason="faulty thermal gun; field crew re-measured",
    )
    assert result.data["override_id"] > 0
    assert result.evidence[0].source_type == "override"

    after = wells_not_producing(con, area_id="NE-08", as_of=AS_OF_DATE)
    assert {"WELL-0170", "WELL-0172", "WELL-0175"} == {w["asset_id"] for w in after.data["wells"]}

    con.execute("DELETE FROM overrides WHERE id = ?", [result.data["override_id"]])
    con.close()


def test_record_shared_note_surfaces_in_resolver():
    from backend.db.resolver import resolve_well_state

    con = _conn()
    result = record_shared_note(
        con, "WELL-0007", "Shut in from 17 Oct for a 10-day planned workover.", "U-SOUTHWEST",
        valid_from=date(2026, 10, 17), valid_to=date(2026, 10, 27),
    )
    note_id = result.data["note_id"]
    assert note_id.startswith("CTX-RT-")

    state = resolve_well_state(con, "WELL-0007", as_of=datetime(2026, 10, 20))
    joined = " ".join(state.reasons)
    assert note_id in joined
    assert "10-day planned workover" in joined

    con.execute("DELETE FROM context_notes WHERE note_id = ?", [note_id])
    con.close()


def test_record_claim_flags_conflict_q17():
    con = _conn()
    result = record_claim(con, "U-TYLER", "well", "WELL-0175", "WELL-0175 is running fine.")
    assert result.data["status"] == "unconfirmed"
    assert len(result.data["conflicts"]) >= 1
    assert result.data["conflicts"][0]["type"] == "claim_vs_data"

    con.execute("DELETE FROM shared_claims WHERE id = ?", [result.data["claim_id"]])
    con.close()


def test_recall_user_context_is_private_per_user():
    con = _conn()
    con.execute(
        """
        INSERT INTO conversations (user_id, conversation_id, turn_id, role, text)
        VALUES
            ('U-MICHAEL', 'conv-1', 1, 'user', 'Which 3 wells in Northeast Block 7 had the most downtime?'),
            ('U-MICHAEL', 'conv-1', 2, 'assistant', 'WELL-0121, WELL-0138, WELL-0107.'),
            ('U-JESSICA', 'conv-2', 1, 'user', 'Is WELL-0164 producing oil right now?')
        """
    )

    michael = recall_user_context(con, "U-MICHAEL")
    assert set(michael.data["well_ids"]) == {"WELL-0121", "WELL-0138", "WELL-0107"}
    assert "Northeast Block 7" in michael.data["area_names"]

    jessica = recall_user_context(con, "U-JESSICA")
    assert jessica.data["well_ids"] == ["WELL-0164"]
    assert "WELL-0121" not in jessica.data["well_ids"]

    stranger = recall_user_context(con, "U-NOBODY")
    assert stranger.data["has_history"] is False

    con.execute("DELETE FROM conversations WHERE user_id IN ('U-MICHAEL', 'U-JESSICA')")
    con.close()
