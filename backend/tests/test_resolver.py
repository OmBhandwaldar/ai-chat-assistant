"""Tests for backend/db/resolver.py.

Uses the already-ingested backend/data.duckdb (sample dataset). Tests cover:
- Q1  base case: WELL-0001 is not producing, CTX-0001-003 appears, recorded_status flagged
- Q12 override case: inspection-level temp correction flips WELL-0164 to producing,
      inspector's "Pump jack not moving." note surfaces as a conflict
- Q17 claim case: a user claim contradicting state becomes a conflict
"""

from pathlib import Path
from datetime import datetime

import duckdb
import pytest

from backend.db.resolver import resolve_well_state

DB_PATH = Path(__file__).parent.parent.parent / "backend" / "data.duckdb"
AS_OF = datetime(2026, 10, 16)  # "today" in the dataset


def _conn():
    if not DB_PATH.exists():
        pytest.skip(f"ingested DB not found at {DB_PATH} — run scripts/ingest.py first")
    con = duckdb.connect(str(DB_PATH), read_only=False)
    con.execute(open(Path(__file__).parent.parent / "db" / "views.sql").read())
    return con


def test_well_0001_not_producing_with_planned_shutin_note():
    con = _conn()
    state = resolve_well_state(con, "WELL-0001", as_of=AS_OF)
    assert state.state == "not_producing"
    joined = " ".join(state.reasons)
    assert "INS-0001-0203" in joined
    assert "CTX-0001-003" in joined                 # the planned-shutin note
    assert "out of date" in joined                  # recorded_status mismatch
    assert "T200" in joined                         # threshold explanation
    assert state.conflicts == []
    con.close()


def test_well_0164_override_flips_to_producing_with_inspector_note_conflict():
    con = _conn()
    # Baseline: raw reading says not producing.
    baseline = resolve_well_state(con, "WELL-0164", as_of=AS_OF)
    assert baseline.state == "not_producing"

    # Apply Michael's Q12 correction: wellhead was actually 41.0 C.
    con.execute(
        """
        INSERT INTO overrides (scope_type, scope_id, field,
                               original_value, new_value, author_user_id, reason)
        VALUES ('inspection', 'INS-0164-0205', 'wellhead_temp_c',
                '24.0', '41.0', 'U-201', 'faulty thermal gun; field crew re-measured')
        """
    )

    after = resolve_well_state(con, "WELL-0164", as_of=AS_OF)
    assert after.state == "producing"
    joined = " ".join(after.reasons)
    assert "corrected by Michael Turner" in joined.lower() or "Michael Turner" in joined
    # Inspector's "Pump jack not moving." should become a conflict.
    assert any(c.type == "override_vs_inspector_note" for c in after.conflicts)

    # Clean up so other tests see pristine data.
    con.execute("DELETE FROM overrides WHERE scope_id = 'INS-0164-0205'")
    con.close()


def test_user_claim_contradicting_state_becomes_conflict():
    con = _conn()
    # WELL-0175 is not producing on 15 Oct (0.4 C delta).
    # Tyler claims "running fine" — should become a claim_vs_data conflict.
    con.execute(
        """
        INSERT INTO shared_claims (user_id, scope_type, scope_id, text)
        VALUES ('U-204', 'well', 'WELL-0175', 'WELL-0175 is running fine')
        """
    )
    state = resolve_well_state(con, "WELL-0175", as_of=AS_OF)
    assert state.state == "not_producing"
    assert any(c.type == "claim_vs_data" for c in state.conflicts)
    assert any("Tyler Hughes" in c.left for c in state.conflicts)

    con.execute("DELETE FROM shared_claims WHERE scope_id = 'WELL-0175'")
    con.close()


def test_unknown_when_no_inspections():
    con = _conn()
    state = resolve_well_state(con, "WELL-9999-NONEXISTENT", as_of=AS_OF)
    assert state.state == "unknown"
    con.close()
