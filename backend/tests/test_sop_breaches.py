"""Tests for backend/tools/sop.sop_breaches (Phase 2.3).

Validates against Q9 expected answers: WELL-0001 over the last 12 months
should have 4 breaches (SR-01, SR-02, SR-03, RC-04), 2 open items
(§10 RCA + planned shut-in not in maintenance system), and RC-02 met.
"""

from datetime import date
from pathlib import Path

import duckdb
import pytest

from backend.tools.sop import get_manual_section, get_sop, sop_breaches

DB_PATH = Path(__file__).parent.parent.parent / "backend" / "data.duckdb"
FLEET_DIR = Path(__file__).resolve().parents[3] / "flytbase-ahc-swe-asset-intelligence-dataset" / "sample-data" / "fleet"
AS_OF = date(2026, 10, 16)


def _conn():
    if not DB_PATH.exists():
        pytest.skip("ingested DB not found — run scripts/ingest.py first")
    con = duckdb.connect(str(DB_PATH), read_only=False)
    con.execute(open(Path(__file__).parent.parent / "db" / "views.sql").read())
    return con


def test_well_0001_q9_breaches():
    con = _conn()
    fleet = FLEET_DIR if FLEET_DIR.exists() else None
    result = sop_breaches(con, "WELL-0001", lookback_days=365, as_of=AS_OF, fleet_dir=fleet)
    data = result.data

    breach_ids = {b["protocol_id"] for b in data["breaches"]}
    assert "SR-01" in breach_ids, f"SR-01 breach missing; got {breach_ids}"
    assert "SR-02" in breach_ids, f"SR-02 breach missing; got {breach_ids}"
    assert "SR-03" in breach_ids, f"SR-03 breach missing; got {breach_ids}"
    assert "RC-04" in breach_ids, f"RC-04 breach missing; got {breach_ids}"

    # SR-01 detail: 41 h, NOV, $5000 fine
    sr01 = [b for b in data["breaches"] if b["protocol_id"] == "SR-01"][0]
    assert "41" in sr01["detail"]
    assert "5,000" in sr01["detail"] or "5000" in sr01["detail"]

    # SR-02 detail: ~7 hours
    sr02 = [b for b in data["breaches"] if b["protocol_id"] == "SR-02"][0]
    assert "7" in sr02["detail"]

    # SR-03: verification due 10 Aug 2026, still open
    sr03 = [b for b in data["breaches"] if b["protocol_id"] == "SR-03"][0]
    assert "REG-0001-003" in sr03["evidence_id"] or "REG-0001-003" in sr03["detail"]

    # RC-04: NOV response overdue
    rc04 = [b for b in data["breaches"] if b["protocol_id"] == "RC-04"][0]
    assert "REG-0001-002" in rc04["evidence_id"] or "REG-0001-002" in rc04["detail"]

    con.close()


def test_well_0001_q9_open_items():
    con = _conn()
    fleet = FLEET_DIR if FLEET_DIR.exists() else None
    result = sop_breaches(con, "WELL-0001", lookback_days=365, as_of=AS_OF, fleet_dir=fleet)
    data = result.data

    open_ids = {o["protocol_id"] for o in data["open_items"]}
    # RCA open item for flowline
    assert "§10" in open_ids, f"§10 open item missing; got {open_ids}"
    # Planned shut-in not in maintenance system
    assert "T200§4" in open_ids, f"T200§4 open item missing; got {open_ids}"

    con.close()


def test_well_0001_q9_met():
    con = _conn()
    fleet = FLEET_DIR if FLEET_DIR.exists() else None
    result = sop_breaches(con, "WELL-0001", lookback_days=365, as_of=AS_OF, fleet_dir=fleet)
    data = result.data

    met_ids = {m["protocol_id"] for m in data["met"]}
    assert "RC-02" in met_ids, f"RC-02 met missing; got {met_ids}"

    con.close()


def test_evidence_ids_populated():
    con = _conn()
    fleet = FLEET_DIR if FLEET_DIR.exists() else None
    result = sop_breaches(con, "WELL-0001", lookback_days=365, as_of=AS_OF, fleet_dir=fleet)
    assert len(result.evidence) > 0
    con.close()


def test_get_sop_sr01():
    con = _conn()
    fleet = FLEET_DIR if FLEET_DIR.exists() else None
    result = get_sop(con, "SR-01", fleet_dir=fleet)
    assert "24 hours" in result.data
    assert result.evidence[0].source_type == "sop"
    con.close()


def test_get_manual_section_t200():
    con = _conn()
    fleet = FLEET_DIR if FLEET_DIR.exists() else None
    result = get_manual_section(con, "T200", "9.3", fleet_dir=fleet)
    assert "15" in result.data
    assert result.evidence[0].source_type == "manual"
    con.close()
