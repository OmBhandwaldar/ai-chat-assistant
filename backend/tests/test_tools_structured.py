"""Tests for backend/tools/{state,trends,downtime,spills,costs,rca}.py
(Phase 3.1). One round-trip test per tool against known answers from
Q1-Q11 and Q14 in 03-evaluation-queries.md.
"""

from datetime import date, datetime

import duckdb
import pytest

from backend.tools.costs import repair_costs
from backend.tools.downtime import downtime_days
from backend.tools.rca import rca_due
from backend.tools.spills import spill_heatmap, spill_map, spills
from backend.tools.state import well_status_now, wells_not_producing
from backend.tools.trends import temp_timeseries, top_downtime

DB_PATH = __import__("pathlib").Path(__file__).parent.parent.parent / "backend" / "data.duckdb"
AS_OF_DT = datetime(2026, 10, 16)
AS_OF_DATE = date(2026, 10, 16)


def _conn():
    if not DB_PATH.exists():
        pytest.skip("ingested DB not found — run scripts/ingest.py first")
    con = duckdb.connect(str(DB_PATH), read_only=False)
    con.execute(open(DB_PATH.parent / "db" / "views.sql").read())
    return con


# Q1 -------------------------------------------------------------------

def test_well_status_now_q1():
    con = _conn()
    result = well_status_now(con, "WELL-0001", as_of=AS_OF_DT)
    data = result.data
    assert data["state"] == "not_producing"
    joined = " ".join(data["reasons"])
    assert "INS-0001-0203" in joined
    assert "CTX-0001-003" in joined
    evidence_ids = {e.source_id for e in result.evidence}
    assert "INS-0001-0203" in evidence_ids
    assert "CTX-0001-003" in evidence_ids
    con.close()


# Q2 -------------------------------------------------------------------

def test_wells_not_producing_q2():
    con = _conn()
    result = wells_not_producing(con, area_id="NE-08", as_of=AS_OF_DATE)
    wells = {w["asset_id"]: w for w in result.data["wells"]}
    assert set(wells) == {"WELL-0164", "WELL-0170", "WELL-0172", "WELL-0175"}

    assert "No recorded reason" in wells["WELL-0164"]["reason"]
    assert "not moving" in wells["WELL-0164"]["reason"]

    assert "MNT-0170-031" in wells["WELL-0170"]["reason"]
    assert "still open" in wells["WELL-0170"]["reason"]

    assert "MNT-0172-028" in wells["WELL-0172"]["reason"]
    con.close()


# Q3 -------------------------------------------------------------------

def test_temp_timeseries_q3():
    con = _conn()
    result = temp_timeseries(con, "WELL-0001", weeks=13, as_of=AS_OF_DT)
    points = result.data["points"]
    assert len(points) == 13
    not_producing_dates = {p["inspected_at"][:10] for p in points if not p["is_producing"]}
    assert "2026-10-08" in not_producing_dates
    assert "2026-10-15" in not_producing_dates
    con.close()


# Q4 -------------------------------------------------------------------

def test_spills_q4():
    con = _conn()
    result = spills(con, "WELL-0001", years=4, as_of=AS_OF_DT)
    data = result.data
    assert data["count"] == 6
    assert data["total_bbl"] == pytest.approx(15.1, abs=0.01)
    inspection_ids = {s["inspection_id"] for s in data["spills"]}
    assert "INS-0001-0070" not in inspection_ids  # corrected as rainwater
    con.close()


# Q5 -------------------------------------------------------------------

def test_downtime_days_q5():
    con = _conn()
    result = downtime_days(con, "WELL-0001", start=date(2025, 1, 1), end=date(2025, 12, 31))
    assert result.data["days"] == 21
    con.close()


# Q6 -------------------------------------------------------------------

def test_top_downtime_q6():
    con = _conn()
    result = top_downtime(
        con, start=date(2026, 6, 16), end=date(2026, 10, 16), area_id="NE-07", limit=3,
    )
    wells = result.data["wells"]
    assert wells[0]["asset_id"] == "WELL-0121"
    assert wells[0]["downtime_days"] == 49
    assert {w["asset_id"] for w in wells} == {"WELL-0121", "WELL-0138", "WELL-0107"}
    con.close()


# Q7 -------------------------------------------------------------------

def test_spill_map_q7():
    con = _conn()
    result = spill_map(con, area_id="NE-08", days=90, as_of=AS_OF_DT)
    pins = {p["asset_id"] for p in result.data["pins"]}
    assert pins == {"WELL-0151", "WELL-0165", "WELL-0175", "WELL-0186"}
    con.close()


# Q8 -------------------------------------------------------------------

def test_spill_heatmap_q8():
    con = _conn()
    result = spill_heatmap(con, year=2026)
    totals = result.data["totals"]
    assert totals["Southwest Block 24"] == 43
    assert totals["Southwest Block 23"] == 35
    assert totals["Northeast Block 7"] == 37
    assert totals["Northeast Block 8"] == 26
    con.close()


# Q11 ------------------------------------------------------------------

def test_repair_costs_q11():
    con = _conn()
    result = repair_costs(con, ["WELL-0121", "WELL-0138", "WELL-0107"], year=2026)
    costs = result.data["costs"]
    assert costs["WELL-0121"] == pytest.approx(25794.0, abs=0.01)
    assert costs["WELL-0138"] == pytest.approx(46942.0, abs=0.01)
    assert costs["WELL-0107"] == pytest.approx(5176.0, abs=0.01)
    con.close()


# Q14 ------------------------------------------------------------------

def test_rca_due_q14_default_threshold():
    con = _conn()
    result = rca_due(con, equipment_kind="pump_jack", area_id="NE-07", as_of=AS_OF_DATE)
    assets = {w["asset_id"] for w in result.data["wells"]}
    assert assets == {"WELL-0108", "WELL-0121", "WELL-0138"}
    con.close()
