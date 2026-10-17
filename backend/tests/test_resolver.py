"""Tests for backend/db/resolver.py.

Uses the already-ingested backend/data.duckdb (sample dataset). Tests cover:
- Q1  base case: WELL-0001 is not producing, CTX-0001-003 appears, recorded_status flagged
- Q12 override case: inspection-level temp correction flips WELL-0164 to producing,
      inspector's "Pump jack not moving." note surfaces as a conflict
- Q17 claim case: a user claim contradicting state becomes a conflict
"""

from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from backend.db.resolver import resolve_well_state

DB_PATH = Path(__file__).parent.parent.parent / "backend" / "data.duckdb"
AS_OF = datetime(2026, 10, 16)  # "today" in the dataset


def _conn():
    if not DB_PATH.exists():
        pytest.skip(f"ingested DB not found at {DB_PATH} — run scripts/ingest.py first")
    con = duckdb.connect(str(DB_PATH), read_only=False)
    con.execute((Path(__file__).parent.parent / "db" / "views.sql").read_text())
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


# ---------------------------------------------------------------------------
# Phase 7.3: edge cases on an isolated in-memory DB (schema.sql + views.sql, hand-made rows).
# These never touch backend/data.duckdb, so they run anywhere and in parallel with other agents.
# ---------------------------------------------------------------------------

_DB_DIR = Path(__file__).parent.parent / "db"


def _iso_db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute((_DB_DIR / "schema.sql").read_text())
    con.execute("INSERT INTO teams VALUES ('TEAM-X', 'Team X', NULL)")
    con.execute("INSERT INTO areas VALUES ('AREA-X', 'Area X', 'TEAM-X', 0, 0)")
    con.execute(
        "INSERT INTO users VALUES ('U-1', 'Alice Author', 'field_operator', 'TEAM-X'), "
        "('U-2', 'Bob Builder', 'field_inspector', 'TEAM-X')"
    )
    con.execute("INSERT INTO well_type_rules VALUES ('T200', 15, 85, 3, 365), ('T350', 20, 95, 3, 365)")
    con.execute((_DB_DIR / "views.sql").read_text())
    return con


def _iso_well(con, asset_id="WELL-A", well_type="T200", recorded_status=None):
    con.execute(
        "INSERT INTO wells (asset_id, name, well_type, area_id, team_id, recorded_status) "
        "VALUES (?, ?, ?, 'AREA-X', 'TEAM-X', ?)",
        [asset_id, asset_id, well_type, recorded_status],
    )


def _iso_insp(con, asset_id, n, ts, wellhead, ambient, notes=None):
    con.execute(
        "INSERT INTO inspections (inspection_id, asset_id, inspected_at, inspector_user_id, method, "
        "ambient_temp_c, wellhead_temp_c, spill_detected, notes) "
        "VALUES (?, ?, ?, 'U-2', 'manual', ?, ?, FALSE, ?)",
        [f"INS-{asset_id[-1]}-{n:04d}", asset_id, ts, ambient, wellhead, notes],
    )
    return f"INS-{asset_id[-1]}-{n:04d}"


def _iso_note(con, note_id, asset_id, vfrom, vto, text="shut in for work", note_type="operator_note",
              inspection_id=None):
    con.execute(
        "INSERT INTO context_notes (note_id, asset_id, created_at, author_user_id, note_type, "
        "inspection_id, text, valid_from, valid_to) VALUES (?, ?, ?, 'U-1', ?, ?, ?, ?, ?)",
        [note_id, asset_id, datetime(2026, 9, 1), note_type, inspection_id, text, vfrom, vto],
    )


def _iso_override(con, inspection_id, field, new_value, created, author="U-1"):
    con.execute(
        "INSERT INTO overrides (scope_type, scope_id, field, new_value, author_user_id, reason, "
        "created_at) VALUES ('inspection', ?, ?, ?, ?, 'fixture', ?)",
        [inspection_id, field, str(new_value), author, created],
    )


@pytest.mark.parametrize(
    "well_type, wellhead, ambient, expected_state",
    [
        ("T200", 40.0, 25.0, "producing"),       # delta == 15.0, exactly at the T200 limit
        ("T200", 39.9, 25.0, "not_producing"),   # 14.9
        ("T350", 45.0, 25.0, "producing"),       # delta == 20.0, exactly at the T350 limit
        ("T350", 44.9, 25.0, "not_producing"),   # 19.9
        ("T200", 45.0, 25.0, "producing"),       # T200 well at a T350-style delta is fine
        ("T350", 40.0, 25.0, "not_producing"),   # 15.0 is enough for T200 but NOT for T350
    ],
)
def test_threshold_boundary_is_inclusive_and_type_specific(well_type, wellhead, ambient, expected_state):
    con = _iso_db()
    _iso_well(con, well_type=well_type)
    _iso_insp(con, "WELL-A", 1, datetime(2026, 10, 15, 9), wellhead, ambient)
    assert resolve_well_state(con, "WELL-A", as_of=AS_OF).state == expected_state
    con.close()


def test_absolute_temperature_never_decides_state():
    con = _iso_db()
    _iso_well(con)
    _iso_insp(con, "WELL-A", 1, datetime(2026, 7, 24, 15), 51.6, 43.1)  # hot but only +8.5
    assert resolve_well_state(con, "WELL-A", as_of=datetime(2026, 7, 25)).state == "not_producing"
    con.close()


def test_existing_well_without_any_inspection_is_unknown_not_a_crash():
    con = _iso_db()
    _iso_well(con, recorded_status="active")
    state = resolve_well_state(con, "WELL-A", as_of=AS_OF)
    assert state.state == "unknown"
    assert "No inspections" in " ".join(state.reasons)
    assert state.conflicts == []
    con.close()


def test_missed_inspection_falls_back_to_the_latest_earlier_one():
    con = _iso_db()
    _iso_well(con)
    wk1 = _iso_insp(con, "WELL-A", 1, datetime(2026, 10, 1, 10), 45.0, 20.0)   # producing
    # week of 8 Oct was missed
    wk3 = _iso_insp(con, "WELL-A", 3, datetime(2026, 10, 15, 10), 22.0, 20.0)  # not producing

    between = resolve_well_state(con, "WELL-A", as_of=datetime(2026, 10, 10))
    assert between.state == "producing" and wk1 in " ".join(between.reasons)

    exact = resolve_well_state(con, "WELL-A", as_of=datetime(2026, 10, 15, 10))   # == inspected_at
    assert exact.state == "not_producing" and wk3 in " ".join(exact.reasons)

    just_before = resolve_well_state(con, "WELL-A", as_of=datetime(2026, 10, 15, 9, 59, 59))
    assert just_before.state == "producing"                                       # wk3 not yet visible
    con.close()


def test_as_of_before_the_first_inspection_is_unknown():
    con = _iso_db()
    _iso_well(con)
    _iso_insp(con, "WELL-A", 1, datetime(2026, 10, 1, 10), 45.0, 20.0)
    assert resolve_well_state(con, "WELL-A", as_of=datetime(2026, 9, 30)).state == "unknown"
    con.close()


@pytest.mark.parametrize(
    "as_of, vfrom, vto, active",
    [
        (datetime(2026, 10, 7), date(2026, 10, 7), date(2026, 10, 20), True),            # == valid_from
        (datetime(2026, 10, 6, 23, 59, 59), date(2026, 10, 7), date(2026, 10, 20), False),  # day before
        (datetime(2026, 10, 20), date(2026, 10, 7), date(2026, 10, 20), True),           # == valid_to
        (datetime(2026, 10, 20, 23, 59, 59), date(2026, 10, 7), date(2026, 10, 20), True),  # end of valid_to day
        (datetime(2026, 10, 21), date(2026, 10, 7), date(2026, 10, 20), False),          # day after valid_to
        (datetime(2027, 6, 1), date(2026, 10, 7), None, True),                           # open-ended
        (datetime(2020, 1, 1), None, date(2026, 10, 20), True),                          # no start
        (datetime(2026, 10, 7), date(2026, 10, 7), date(2026, 10, 7), True),             # one-day note
    ],
)
def test_shared_note_validity_window_boundaries(as_of, vfrom, vto, active):
    con = _iso_db()
    _iso_well(con)
    _iso_insp(con, "WELL-A", 1, datetime(2020, 1, 1), 45.0, 20.0)
    _iso_note(con, "CTX-X-001", "WELL-A", vfrom, vto)
    joined = " ".join(resolve_well_state(con, "WELL-A", as_of=as_of).reasons)
    assert ("Note CTX-X-001 by Alice Author" in joined) is active
    con.close()


def test_active_note_adds_a_reason_but_does_not_flip_the_derived_state():
    con = _iso_db()
    _iso_well(con)
    _iso_insp(con, "WELL-A", 1, datetime(2026, 10, 15), 45.0, 20.0)  # producing
    _iso_note(con, "CTX-X-001", "WELL-A", date(2026, 10, 7), date(2026, 10, 20))
    state = resolve_well_state(con, "WELL-A", as_of=AS_OF)
    assert state.state == "producing"
    assert "CTX-X-001" in " ".join(state.reasons)
    con.close()


def test_notes_are_scoped_to_their_own_well():
    con = _iso_db()
    _iso_well(con, "WELL-A")
    _iso_well(con, "WELL-B")
    _iso_insp(con, "WELL-A", 1, datetime(2026, 10, 15), 45.0, 20.0)
    _iso_insp(con, "WELL-B", 1, datetime(2026, 10, 15), 45.0, 20.0)
    _iso_note(con, "CTX-B-001", "WELL-B", date(2026, 10, 1), None)
    assert "CTX-B-001" not in " ".join(resolve_well_state(con, "WELL-A", as_of=AS_OF).reasons)
    assert "CTX-B-001" in " ".join(resolve_well_state(con, "WELL-B", as_of=AS_OF).reasons)
    con.close()


def test_correction_note_is_skipped_on_latest_inspection_but_shown_on_older_ones():
    con = _iso_db()
    _iso_well(con)
    old = _iso_insp(con, "WELL-A", 1, datetime(2026, 10, 1), 45.0, 20.0)
    latest = _iso_insp(con, "WELL-A", 2, datetime(2026, 10, 15), 45.0, 20.0)
    _iso_note(con, "CTX-X-OLD", "WELL-A", None, None, "was rainwater", "correction", old)
    _iso_note(con, "CTX-X-NEW", "WELL-A", None, None, "was rainwater", "correction", latest)
    joined = " ".join(resolve_well_state(con, "WELL-A", as_of=AS_OF).reasons)
    assert "CTX-X-OLD" in joined and "CTX-X-NEW" not in joined
    con.close()


def test_override_on_an_older_inspection_does_not_change_the_latest_state():
    con = _iso_db()
    _iso_well(con)
    old = _iso_insp(con, "WELL-A", 1, datetime(2026, 10, 1), 22.0, 20.0)
    _iso_insp(con, "WELL-A", 2, datetime(2026, 10, 15), 22.0, 20.0)  # not producing
    _iso_override(con, old, "wellhead_temp_c", 45.0, datetime(2026, 10, 16))
    state = resolve_well_state(con, "WELL-A", as_of=AS_OF)
    assert state.state == "not_producing"
    assert "corrected by" not in " ".join(state.reasons).lower()
    con.close()


def test_most_recent_override_on_the_same_inspection_wins():
    con = _iso_db()
    _iso_well(con)
    ins = _iso_insp(con, "WELL-A", 1, datetime(2026, 10, 15), 22.0, 20.0, notes="Pump jack not moving.")
    _iso_override(con, ins, "wellhead_temp_c", 45.0, datetime(2026, 10, 16, 8), author="U-1")
    assert resolve_well_state(con, "WELL-A", as_of=AS_OF).state == "producing"
    _iso_override(con, ins, "wellhead_temp_c", 21.0, datetime(2026, 10, 16, 9), author="U-2")
    state = resolve_well_state(con, "WELL-A", as_of=AS_OF)
    assert state.state == "not_producing"
    assert not any(c.type == "override_vs_inspector_note" for c in state.conflicts)
    con.close()


def test_inspector_note_only_conflicts_when_an_override_flipped_the_state():
    con = _iso_db()
    _iso_well(con)
    ins = _iso_insp(con, "WELL-A", 1, datetime(2026, 10, 15), 45.0, 20.0, notes="Pump jack not moving.")
    assert resolve_well_state(con, "WELL-A", as_of=AS_OF).conflicts == []  # no override => no conflict
    _iso_override(con, ins, "wellhead_temp_c", 46.0, datetime(2026, 10, 16))
    conflicts = resolve_well_state(con, "WELL-A", as_of=AS_OF).conflicts
    assert [c.type for c in conflicts] == ["override_vs_inspector_note"]
    con.close()


@pytest.mark.parametrize(
    "recorded, wellhead, expect_stale",
    [
        ("active", 22.0, True),    # says active, inspection says not producing
        ("down", 45.0, True),      # says down, inspection says producing
        ("active", 45.0, False),
        ("down", 22.0, False),
        (None, 22.0, False),       # nothing recorded => nothing to compare
    ],
)
def test_recorded_status_only_flagged_when_it_disagrees(recorded, wellhead, expect_stale):
    con = _iso_db()
    _iso_well(con, recorded_status=recorded)
    _iso_insp(con, "WELL-A", 1, datetime(2026, 10, 15), wellhead, 20.0)
    joined = " ".join(resolve_well_state(con, "WELL-A", as_of=AS_OF).reasons)
    assert ("out of date" in joined) is expect_stale
    con.close()


def test_claims_are_scoped_to_their_well_and_only_unconfirmed_ones_count():
    con = _iso_db()
    _iso_well(con, "WELL-A")
    _iso_well(con, "WELL-B")
    _iso_insp(con, "WELL-A", 1, datetime(2026, 10, 15), 22.0, 20.0)  # not producing
    _iso_insp(con, "WELL-B", 1, datetime(2026, 10, 15), 22.0, 20.0)

    def claim(well, text, status="unconfirmed"):
        con.execute(
            "INSERT INTO shared_claims (user_id, scope_type, scope_id, text, status) "
            "VALUES ('U-2', 'well', ?, ?, ?)", [well, text, status],
        )

    claim("WELL-B", "WELL-B is running fine")                      # other well: must not leak
    claim("WELL-A", "WELL-A is running fine", status="rejected")   # not unconfirmed
    claim("WELL-A", "WELL-A is running fine", status="confirmed")
    assert resolve_well_state(con, "WELL-A", as_of=AS_OF).conflicts == []

    claim("WELL-A", "WELL-A is running fine")
    conflicts = resolve_well_state(con, "WELL-A", as_of=AS_OF).conflicts
    assert [c.type for c in conflicts] == ["claim_vs_data"] and "Bob Builder" in conflicts[0].left
    assert len(resolve_well_state(con, "WELL-B", as_of=AS_OF).conflicts) == 1
    con.close()


def test_claim_that_agrees_with_the_data_is_not_a_conflict():
    con = _iso_db()
    _iso_well(con)
    _iso_insp(con, "WELL-A", 1, datetime(2026, 10, 15), 22.0, 20.0)  # not producing
    con.execute("INSERT INTO shared_claims (user_id, scope_type, scope_id, text) "
                "VALUES ('U-1', 'well', 'WELL-A', 'WELL-A has been shut in since 14 Oct')")
    assert resolve_well_state(con, "WELL-A", as_of=AS_OF).conflicts == []
    con.close()
