"""Tests for backend/db/overrides.py against the real ingested dataset."""

from pathlib import Path

import duckdb
import pytest

from backend.db.overrides import (
    InvalidOverrideScope,
    apply_overrides_to_inspection,
    apply_overrides_to_rule,
    list_overrides,
    record_override,
)
from backend.db.resolver import resolve_well_state
from datetime import datetime

DB_PATH = Path(__file__).parent.parent.parent / "backend" / "data.duckdb"
AS_OF = datetime(2026, 10, 16)


def _conn():
    if not DB_PATH.exists():
        pytest.skip(f"ingested DB not found at {DB_PATH} — run scripts/ingest.py first")
    con = duckdb.connect(str(DB_PATH), read_only=False)
    con.execute(open(Path(__file__).parent.parent / "db" / "views.sql").read())
    return con


def test_record_override_captures_original_value():
    con = _conn()
    override_id = record_override(
        con, "inspection", "INS-0164-0205", "wellhead_temp_c",
        41.0, "U-201", "faulty thermal gun; re-measured",
    )
    assert override_id > 0

    rows = list_overrides(con, scope_type="inspection", scope_id="INS-0164-0205")
    assert len(rows) == 1
    assert rows[0]["original_value"] == "24.0"  # JSON-encoded original
    assert rows[0]["new_value"] == "41.0"

    con.execute("DELETE FROM overrides WHERE id = ?", [override_id])
    con.close()


def test_apply_overrides_to_inspection_reflects_correction():
    con = _conn()
    before = apply_overrides_to_inspection(con, "INS-0164-0205")
    assert before["wellhead_temp_c"] == 24.0
    assert before["has_override"] is False

    record_override(con, "inspection", "INS-0164-0205", "wellhead_temp_c", 41.0, "U-201", "re-measured")

    after = apply_overrides_to_inspection(con, "INS-0164-0205")
    assert after["wellhead_temp_c"] == 41.0
    assert after["has_override"] is True

    con.execute("DELETE FROM overrides WHERE scope_id = 'INS-0164-0205'")
    con.close()


def test_invalid_inspection_id_raises():
    con = _conn()
    with pytest.raises(InvalidOverrideScope):
        record_override(con, "inspection", "INS-NOPE-9999", "wellhead_temp_c", 41.0, "U-201", "bad id")
    con.close()


def test_well_type_scope_isolation_q14():
    con = _conn()
    before_t350 = apply_overrides_to_rule(con, "T350", "rca_corrective_count")
    before_t200 = apply_overrides_to_rule(con, "T200", "rca_corrective_count")
    assert before_t350 == 3
    assert before_t200 == 3

    record_override(con, "well_type", "T350", "rca_corrective_count", 2, "U-201",
                     "team wants earlier RCA for T350")

    after_t350 = apply_overrides_to_rule(con, "T350", "rca_corrective_count")
    after_t200 = apply_overrides_to_rule(con, "T200", "rca_corrective_count")
    assert after_t350 == 2       # changed
    assert after_t200 == 3       # untouched — scope isolation holds

    con.execute("DELETE FROM overrides WHERE scope_type = 'well_type'")
    con.close()


def test_q12_end_to_end_via_overrides_module():
    """Full Q12 flow using the overrides module's public API (not raw SQL),
    mirroring what record_feedback (Phase 3.3) will call."""
    con = _conn()
    before = resolve_well_state(con, "WELL-0164", as_of=AS_OF)
    assert before.state == "not_producing"

    record_override(
        con, "inspection", "INS-0164-0205", "wellhead_temp_c",
        41.0, "U-201", "faulty thermal gun; field crew re-measured at 41.0 C",
    )

    after = resolve_well_state(con, "WELL-0164", as_of=AS_OF)
    assert after.state == "producing"

    con.execute("DELETE FROM overrides WHERE scope_id = 'INS-0164-0205'")
    con.close()
