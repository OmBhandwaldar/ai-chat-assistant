"""Tests for backend/tools/sop.sop_breaches (Phase 2.3).

Validates against Q9 expected answers: WELL-0001 over the last 12 months
should have 4 breaches (SR-01, SR-02, SR-03, RC-04), 2 open items
(§10 RCA + planned shut-in not in maintenance system), and RC-02 met.
"""

from datetime import date, datetime, timedelta
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
    con.execute((Path(__file__).parent.parent / "db" / "views.sql").read_text())
    return con


def test_sop_breaches_datetime_as_of_does_not_compare_date_to_datetime():
    """Eval passes Deps.as_of as datetime; SR-03/RC-04 compare deadlines to as_of."""
    con = _iso_db()
    _well(con)
    _quiet_baseline(con)
    _insp(con, "INS-A-0001", datetime(2026, 6, 1, 10), volume=6.0)
    _reg(con, "REG-A-001", date(2026, 6, 2), "spill_report_filed", "INS-A-0001", hours=5)
    empty_fleet = Path(__file__).parent / "__no_fleet__"
    result = sop_breaches(
        con,
        "WELL-A",
        lookback_days=365,
        as_of=datetime(2026, 10, 16, 0, 0),
        fleet_dir=empty_fleet,
    )
    sr03 = [f for f in result.data["breaches"] + result.data["open_items"] if f["protocol_id"] == "SR-03"]
    assert len(sr03) == 1
    con.close()


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
    sr01 = next(b for b in data["breaches"] if b["protocol_id"] == "SR-01")
    assert "41" in sr01["detail"]
    assert "5,000" in sr01["detail"] or "5000" in sr01["detail"]

    # SR-02 detail: ~7 hours
    sr02 = next(b for b in data["breaches"] if b["protocol_id"] == "SR-02")
    assert "7" in sr02["detail"]

    # SR-03: verification due 10 Aug 2026, still open
    sr03 = next(b for b in data["breaches"] if b["protocol_id"] == "SR-03")
    assert "REG-0001-003" in sr03["evidence_id"] or "REG-0001-003" in sr03["detail"]

    # RC-04: NOV response overdue
    rc04 = next(b for b in data["breaches"] if b["protocol_id"] == "RC-04")
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


# ---------------------------------------------------------------------------
# Phase 7.3: boundary / edge cases on an isolated in-memory DB (schema.sql + views.sql, hand-made
# rows). No dependency on backend/data.duckdb; rule texts come from an empty fleet dir so only the
# deterministic logic is under test.
# ---------------------------------------------------------------------------

_DB_DIR = Path(__file__).parent.parent / "db"
ASOF = date(2026, 10, 16)


def _iso_db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute((_DB_DIR / "schema.sql").read_text())
    con.execute("INSERT INTO teams VALUES ('TEAM-X', 'Team X', NULL)")
    con.execute("INSERT INTO areas VALUES ('AREA-X', 'Area X', 'TEAM-X', 0, 0)")
    con.execute("INSERT INTO users VALUES ('U-1', 'Alice Author', 'field_operator', 'TEAM-X')")
    con.execute("INSERT INTO well_type_rules VALUES ('T200', 15, 85, 3, 365), ('T350', 20, 95, 3, 365)")
    con.execute((_DB_DIR / "views.sql").read_text())
    return con


def _well(con, asset_id="WELL-A", well_type="T200"):
    con.execute(
        "INSERT INTO wells (asset_id, name, well_type, area_id, team_id) "
        "VALUES (?, ?, ?, 'AREA-X', 'TEAM-X')", [asset_id, asset_id, well_type],
    )


def _insp(con, ins_id, ts, asset_id="WELL-A", volume=None, wellhead=45.0, ambient=20.0):
    con.execute(
        "INSERT INTO inspections (inspection_id, asset_id, inspected_at, inspector_user_id, method, "
        "ambient_temp_c, wellhead_temp_c, spill_detected, spill_area_pct, spill_est_volume_bbl) "
        "VALUES (?, ?, ?, 'U-1', 'drone', ?, ?, ?, ?, ?)",
        [ins_id, asset_id, ts, ambient, wellhead, volume is not None,
         2.0 if volume is not None else None, volume],
    )


def _maint(con, mnt_id, started, asset_id="WELL-A", kind="flowline", mtype="corrective",
           issue="repair", trigger=None, body=None):
    con.execute(
        "INSERT INTO maintenance (maintenance_id, asset_id, started_at, completed_at, maintenance_type, "
        "equipment_kind, issue, triggered_by_inspection_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [mnt_id, asset_id, started, started + timedelta(hours=8), mtype, kind, issue, trigger],
    )
    if body is not None:
        con.execute(
            "INSERT INTO maintenance_reports (report_path, asset_id, maintenance_id, body) "
            "VALUES (?, ?, ?, ?)", [f"{mnt_id}.md", asset_id, mnt_id, body],
        )


def _reg(con, ev_id, d, etype, ins=None, hours=None, status="open", protocol="SR-01", fine=None,
         asset_id="WELL-A"):
    con.execute(
        "INSERT INTO regulatory_events (event_id, asset_id, date, agency, event_type, protocol_id, "
        "related_inspection_id, hours_to_report, detail, fine_usd, status) "
        "VALUES (?, ?, ?, 'Agency', ?, ?, ?, ?, 'fixture', ?, ?)",
        [ev_id, asset_id, d, etype, protocol, ins, hours, fine, status],
    )


def _run(con, asset="WELL-A", lookback=365, as_of=ASOF, tmp=None):
    empty_fleet = Path(__file__).parent / "__no_fleet__"  # non-existent => no rule texts, no autodetect
    return sop_breaches(con, asset, lookback_days=lookback, as_of=as_of, fleet_dir=empty_fleet).data


def _hits(data, bucket, protocol):
    return [f for f in data[bucket] if f["protocol_id"] == protocol]


def _quiet_baseline(con, asset_id="WELL-A"):
    """A recent inspection + an in-window flowline test so RC-01/RC-02 stay quiet."""
    _insp(con, f"INS-{asset_id[-1]}-BASE", datetime(2026, 10, 15, 9), asset_id)
    _maint(con, f"MNT-{asset_id[-1]}-RC02", datetime(2026, 3, 7, 9), asset_id,
           mtype="scheduled", issue="Annual flowline integrity test")


# ---- SR-01 ------------------------------------------------------------------

@pytest.mark.parametrize("volume, reportable", [(5.0, True), (4.99, False), (5.01, True), (0.5, False)])
def test_sr01_reportable_volume_threshold_is_inclusive_at_5_bbl(volume, reportable):
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-0001", datetime(2026, 6, 11, 15, 30), volume=volume)
    data = _run(con)
    assert bool(_hits(data, "breaches", "SR-01")) is reportable
    con.close()


@pytest.mark.parametrize("hours, bucket", [(24.0, "met"), (23.99, "met"), (24.01, "breaches"), (41, "breaches")])
def test_sr01_24h_report_boundary_is_strictly_greater_than_24(hours, bucket):
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-0001", datetime(2026, 6, 11, 15, 30), volume=6.8)
    _reg(con, "REG-A-001", date(2026, 6, 12), "spill_report_filed", "INS-A-0001", hours=hours)
    data = _run(con)
    assert len(_hits(data, bucket, "SR-01")) == 1
    other = "breaches" if bucket == "met" else "met"
    assert _hits(data, other, "SR-01") == []
    con.close()


def test_sr01_report_filed_without_hours_does_not_crash_and_cannot_be_called_compliant():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-0001", datetime(2026, 6, 11, 15, 30), volume=6.8)
    _reg(con, "REG-A-001", date(2026, 6, 12), "spill_report_filed", "INS-A-0001", hours=None)
    data = _run(con)                                   # used to raise TypeError on f"{None:.0f}"
    assert _hits(data, "met", "SR-01") == []
    assert len(_hits(data, "open_items", "SR-01")) == 1
    con.close()


def test_sr01_no_report_breaches_with_due_by_24h_after_discovery():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-0001", datetime(2026, 9, 24, 15, 30), volume=5.4)
    (b,) = _hits(_run(con), "breaches", "SR-01")
    assert b["due_by"] == "2026-09-25 15:30" and b["evidence_id"] == "INS-A-0001"
    con.close()


def test_sr01_deadline_still_in_the_future_is_open_not_breached():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    # discovered this morning: the 24 h deadline is tomorrow, so nothing is late yet
    _insp(con, "INS-A-0001", datetime(2026, 10, 16, 9, 0), volume=6.0)
    data = _run(con)
    assert _hits(data, "breaches", "SR-01") == []
    (o,) = _hits(data, "open_items", "SR-01")
    assert o["due_by"] == "2026-10-17 09:00"
    # discovered yesterday afternoon: deadline falls inside the as_of day => late
    con.execute("DELETE FROM inspections WHERE inspection_id = 'INS-A-0001'")
    _insp(con, "INS-A-0002", datetime(2026, 10, 15, 15, 30), volume=6.0)
    assert len(_hits(_run(con), "breaches", "SR-01")) == 1
    con.close()


def test_sr01_volume_override_makes_a_small_spill_reportable_and_correction_removes_it():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-0001", datetime(2026, 9, 24, 15, 30), volume=4.5)
    assert _hits(_run(con), "breaches", "SR-01") == []
    con.execute(
        "INSERT INTO overrides (scope_type, scope_id, field, new_value, author_user_id) "
        "VALUES ('inspection', 'INS-A-0001', 'spill_est_volume_bbl', '5.4', 'U-1')")
    assert len(_hits(_run(con), "breaches", "SR-01")) == 1          # Q13
    con.execute(
        "INSERT INTO context_notes (note_id, asset_id, created_at, note_type, inspection_id, text) "
        "VALUES ('CTX-A-1', 'WELL-A', current_timestamp, 'correction', 'INS-A-0001', 'rainwater')")
    assert _hits(_run(con), "breaches", "SR-01") == []              # formal correction wins
    con.close()


def test_sr01_events_of_another_well_do_not_leak():
    con = _iso_db(); _well(con, "WELL-A"); _well(con, "WELL-B"); _quiet_baseline(con, "WELL-A")
    _insp(con, "INS-A-0001", datetime(2026, 6, 11, 15, 30), volume=6.8)
    _insp(con, "INS-B-0001", datetime(2026, 6, 11, 15, 30), "WELL-B", volume=6.8)
    _reg(con, "REG-B-001", date(2026, 6, 12), "spill_report_filed", "INS-B-0001", hours=5, asset_id="WELL-B")
    assert len(_hits(_run(con, "WELL-A"), "breaches", "SR-01")) == 1     # B's filing doesn't cover A
    con.close()


# ---- SR-02 ------------------------------------------------------------------

@pytest.mark.parametrize(
    "hours, bucket", [(4.0, "met"), (3.9, "met"), (4.1, "breaches"), (7.1, "breaches"), (0.5, "met")]
)
def test_sr02_containment_limit_is_strictly_greater_than_4_hours(hours, bucket):
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-0001", datetime(2026, 8, 1, 10), volume=1.0)      # sub-5 bbl: SR-02 applies anyway
    _maint(con, "MNT-A-0001", datetime(2026, 8, 1, 12), trigger="INS-A-0001",
           body=f"## What happened\n\nThe release was contained about {hours} hours after the inspection.")
    data = _run(con)
    assert len(_hits(data, bucket, "SR-02")) == 1
    con.close()


def test_sr02_ignored_when_report_has_no_containment_time_or_no_report():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-0001", datetime(2026, 8, 1, 10), volume=1.0)
    _insp(con, "INS-A-0002", datetime(2026, 8, 8, 10), volume=1.0)
    _maint(con, "MNT-A-0001", datetime(2026, 8, 1, 12), trigger="INS-A-0001",
           body="## What happened\n\nCrew cleaned the pad. No timing recorded.")
    _maint(con, "MNT-A-0002", datetime(2026, 8, 8, 12), trigger="INS-A-0002", body=None)
    data = _run(con)
    assert _hits(data, "breaches", "SR-02") == [] and _hits(data, "met", "SR-02") == []
    con.close()


def test_sr02_does_not_apply_to_a_corrected_non_spill():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-0001", datetime(2026, 8, 1, 10), volume=1.0)
    _maint(con, "MNT-A-0001", datetime(2026, 8, 1, 12), trigger="INS-A-0001",
           body="The release was contained about 9 hours after the inspection.")
    con.execute(
        "INSERT INTO context_notes (note_id, asset_id, created_at, note_type, inspection_id, text) "
        "VALUES ('CTX-A-1', 'WELL-A', current_timestamp, 'correction', 'INS-A-0001', 'rainwater')")
    assert _hits(_run(con), "breaches", "SR-02") == []
    con.close()


# ---- SR-03 ------------------------------------------------------------------

@pytest.mark.parametrize(
    "days_after_discovery, bucket",
    [(59, "open_items"), (60, "open_items"), (61, "breaches")],   # due day itself is not yet late
)
def test_sr03_60_day_verification_deadline_boundary(days_after_discovery, bucket):
    con = _iso_db(); _well(con); _quiet_baseline(con)
    discovery = datetime(2026, 8, 1, 10)
    _insp(con, "INS-A-0001", discovery, volume=6.0)
    _reg(con, "REG-A-001", date(2026, 8, 2), "spill_report_filed", "INS-A-0001", hours=5)
    as_of = (discovery + timedelta(days=days_after_discovery)).date()
    data = _run(con, as_of=as_of)
    (f,) = _hits(data, bucket, "SR-03")
    assert f["due_by"] == "2026-09-30"                                    # 1 Aug + 60 d
    con.close()


def test_sr03_closed_verification_is_met_and_open_one_past_deadline_is_breached():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-0001", datetime(2026, 6, 1, 10), volume=6.0)
    _reg(con, "REG-A-001", date(2026, 6, 2), "spill_report_filed", "INS-A-0001", hours=5)
    _reg(con, "REG-A-002", date(2026, 7, 1), "site_inspection", "INS-A-0001", status="closed", protocol="SR-03")
    data = _run(con)
    assert len(_hits(data, "met", "SR-03")) == 1 and _hits(data, "breaches", "SR-03") == []
    con.execute("DELETE FROM regulatory_events WHERE event_id = 'REG-A-002'")
    _reg(con, "REG-A-003", date(2026, 7, 1), "site_inspection", "INS-A-0001", status="open", protocol="SR-03")
    assert len(_hits(_run(con), "breaches", "SR-03")) == 1
    con.close()


def test_sr03_never_applies_below_the_reportable_volume():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-0001", datetime(2026, 6, 1, 10), volume=4.99)
    data = _run(con)
    assert all(_hits(data, b, "SR-03") == [] for b in ("breaches", "open_items", "met"))
    con.close()


# ---- RC-04 ------------------------------------------------------------------

@pytest.mark.parametrize(
    "as_of, bucket",
    [(date(2026, 7, 29), "open_items"), (date(2026, 7, 30), "open_items"), (date(2026, 7, 31), "breaches")],
)
def test_rc04_nov_response_30_day_boundary(as_of, bucket):
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _reg(con, "REG-A-NOV", date(2026, 6, 30), "notice_of_violation", fine=5000)   # due 30 Jul
    data = _run(con, as_of=as_of)
    (f,) = _hits(data, bucket, "RC-04")
    assert f["due_by"] == "2026-07-30"
    con.close()


@pytest.mark.parametrize(
    "response_date, bucket",
    [
        (date(2026, 6, 30), "met"),       # same day
        (date(2026, 7, 30), "met"),       # on the last allowed day
        (date(2026, 7, 31), "breaches"),  # filed late is still a breach
        (date(2026, 6, 29), "breaches"),  # a response from BEFORE the NOV answers nothing
    ],
)
def test_rc04_response_timing(response_date, bucket):
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _reg(con, "REG-A-NOV", date(2026, 6, 30), "notice_of_violation", fine=5000)
    _reg(con, "REG-A-RESP", response_date, "nov_response_filed", protocol="RC-04")
    assert len(_hits(_run(con), bucket, "RC-04")) == 1
    con.close()


# ---- lookback window boundaries ---------------------------------------------

def test_lookback_window_start_is_inclusive_to_the_day_and_excludes_earlier():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    start = ASOF - timedelta(days=90)                                     # 2026-07-18
    _insp(con, "INS-A-IN", datetime.combine(start, datetime.min.time()), volume=6.0)             # 00:00:00
    _insp(con, "INS-A-OUT", datetime.combine(start, datetime.min.time()) - timedelta(seconds=1),
          volume=6.0)                                                                           # 1 s before
    ids = {b["evidence_id"] for b in _hits(_run(con, lookback=90), "breaches", "SR-01")}
    assert ids == {"INS-A-IN"}
    con.close()


def test_lookback_window_end_includes_the_whole_as_of_day_but_not_the_next():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-LATE", datetime(2026, 10, 16, 23, 59, 59), volume=6.0)
    _insp(con, "INS-A-NEXT", datetime(2026, 10, 17, 0, 0, 0), volume=6.0)
    data = _run(con, lookback=90)
    ids = {f["evidence_id"] for f in data["breaches"] + data["open_items"] if f["protocol_id"] == "SR-01"}
    assert ids == {"INS-A-LATE"}
    con.close()


def test_lookback_zero_days_only_sees_the_as_of_day():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-TODAY", datetime(2026, 10, 16, 8, 0), volume=6.0)
    _insp(con, "INS-A-YDAY", datetime(2026, 10, 15, 8, 0), volume=6.0)
    data = _run(con, lookback=0)
    ids = {f["evidence_id"] for f in data["breaches"] + data["open_items"] if f["protocol_id"] == "SR-01"}
    assert ids == {"INS-A-TODAY"}
    con.close()


def test_regulatory_event_window_boundary_is_inclusive_on_the_start_date():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    start = ASOF - timedelta(days=60)                                     # 2026-08-17
    _reg(con, "REG-A-IN", start, "notice_of_violation", fine=1)
    _reg(con, "REG-A-OUT", start - timedelta(days=1), "notice_of_violation", fine=1)
    ids = {f["evidence_id"] for f in _run(con, lookback=60)["breaches"] + _run(con, lookback=60)["open_items"]
           if f["protocol_id"] == "RC-04"}
    assert ids == {"REG-A-IN"}
    con.close()


def test_lookback_does_not_change_which_spills_exist_outside_it():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _insp(con, "INS-A-OLD", datetime(2025, 1, 10, 10), volume=6.0)        # > 12 months ago
    assert _hits(_run(con, lookback=365), "breaches", "SR-01") == []
    assert len(_hits(_run(con, lookback=2000), "breaches", "SR-01")) == 1
    con.close()


# ---- RC-01 (missed inspections) ---------------------------------------------

@pytest.mark.parametrize("days_ago, bucket", [(0, "met"), (7, "met"), (14, "met"), (15, "breaches"), (30, "breaches")])
def test_rc01_inspection_gap_boundary_is_strictly_greater_than_14_days(days_ago, bucket):
    con = _iso_db(); _well(con)
    _maint(con, "MNT-A-RC02", datetime(2026, 3, 7, 9), mtype="scheduled", issue="Annual flowline integrity test")
    last = datetime.combine(ASOF - timedelta(days=days_ago), datetime.min.time()) + timedelta(hours=23, minutes=59)
    _insp(con, "INS-A-LAST", last)
    data = _run(con)
    assert len(_hits(data, bucket, "RC-01")) == 1
    con.close()


def test_rc01_one_missed_weekly_inspection_is_still_compliant():
    con = _iso_db(); _well(con)
    _maint(con, "MNT-A-RC02", datetime(2026, 3, 7, 9), mtype="scheduled", issue="Annual flowline integrity test")
    _insp(con, "INS-A-1", datetime(2026, 10, 2, 9))        # 8 Oct visit missed; next would have been 15 Oct
    data = _run(con, as_of=date(2026, 10, 15))
    assert len(_hits(data, "met", "RC-01")) == 1 and _hits(data, "breaches", "RC-01") == []
    con.close()


def test_well_with_no_inspections_does_not_crash_and_reports_no_rc01_status():
    con = _iso_db(); _well(con)
    data = _run(con)
    assert all(_hits(data, b, "RC-01") == [] for b in ("breaches", "open_items", "met"))
    assert len(_hits(data, "breaches", "RC-02")) == 1              # no flowline test either
    con.close()


# ---- RC-02 ------------------------------------------------------------------

@pytest.mark.parametrize("days_ago, bucket", [(0, "met"), (365, "met"), (366, "breaches")])
def test_rc02_annual_flowline_test_window_boundary(days_ago, bucket):
    con = _iso_db(); _well(con)
    _insp(con, "INS-A-1", datetime(2026, 10, 15, 9))
    started = datetime.combine(ASOF - timedelta(days=days_ago), datetime.min.time())
    _maint(con, "MNT-A-TEST", started, mtype="scheduled", issue="Annual flowline integrity test")
    assert len(_hits(_run(con), bucket, "RC-02")) == 1
    con.close()


# ---- Manual §10: RCA --------------------------------------------------------

def _corrective(con, n, started, asset_id="WELL-A", kind="pump_jack", mtype="corrective"):
    _maint(con, f"MNT-{asset_id[-1]}-{n:03d}", started, asset_id, kind=kind, mtype=mtype)


@pytest.mark.parametrize("repairs, due", [(1, False), (2, False), (3, True), (4, True)])
def test_rca_count_threshold_is_at_least_three_by_default(repairs, due):
    con = _iso_db(); _well(con); _quiet_baseline(con)
    for i in range(repairs):
        _corrective(con, i, datetime(2026, 3 + i, 1, 9))
    assert bool(_hits(_run(con), "open_items", "§10")) is due
    con.close()


def test_rca_ignores_scheduled_work_and_does_not_pool_equipment_kinds():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _corrective(con, 1, datetime(2026, 3, 1, 9))
    _corrective(con, 2, datetime(2026, 4, 1, 9))
    _corrective(con, 3, datetime(2026, 5, 1, 9), mtype="scheduled")       # not corrective
    _corrective(con, 4, datetime(2026, 6, 1, 9), kind="rod_string")      # different equipment
    assert _hits(_run(con), "open_items", "§10") == []
    con.close()


def test_rca_twelve_month_window_boundary():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    edge = datetime.combine(ASOF - timedelta(days=365), datetime.min.time())
    _corrective(con, 1, edge)                                  # exactly 365 d ago, 00:00 => counted
    _corrective(con, 2, datetime(2026, 5, 1, 9))
    _corrective(con, 3, datetime(2026, 6, 1, 9))
    assert len(_hits(_run(con), "open_items", "§10")) == 1
    con.execute("DELETE FROM maintenance WHERE maintenance_id = 'MNT-A-001'")
    _corrective(con, 9, edge - timedelta(seconds=1))           # 1 s outside => not counted
    assert _hits(_run(con), "open_items", "§10") == []
    con.close()


def test_rca_well_type_override_changes_threshold_only_for_that_type():
    con = _iso_db(); _well(con, "WELL-A", "T200"); _well(con, "WELL-B", "T350")
    _quiet_baseline(con, "WELL-A"); _quiet_baseline(con, "WELL-B")
    for asset in ("WELL-A", "WELL-B"):
        _corrective(con, 1, datetime(2026, 4, 1, 9), asset)
        _corrective(con, 2, datetime(2026, 5, 1, 9), asset)        # two repairs each
    con.execute(
        "INSERT INTO overrides (scope_type, scope_id, field, new_value, author_user_id) "
        "VALUES ('well_type', 'T350', 'rca_corrective_count', '2', 'U-1')")
    assert len(_hits(_run(con, "WELL-B"), "open_items", "§10")) == 1   # T350 now triggers at 2
    assert _hits(_run(con, "WELL-A"), "open_items", "§10") == []       # T200 untouched
    con.close()


def test_rca_report_marking_completion_moves_it_to_met():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _corrective(con, 1, datetime(2026, 3, 1, 9))
    _corrective(con, 2, datetime(2026, 4, 1, 9))
    con.execute("INSERT INTO maintenance (maintenance_id, asset_id, started_at, completed_at, maintenance_type, "
                "equipment_kind) VALUES ('MNT-A-003', 'WELL-A', '2026-05-01 09:00', '2026-05-01 17:00', "
                "'corrective', 'pump_jack')")
    con.execute("INSERT INTO maintenance_reports (report_path, asset_id, maintenance_id, body) "
                "VALUES ('r.md', 'WELL-A', 'MNT-A-003', 'Root cause analysis completed on 10 May.')")
    data = _run(con)
    assert len(_hits(data, "met", "§10")) == 1 and _hits(data, "open_items", "§10") == []
    con.close()


# ---- planned shut-in not in the maintenance system ---------------------------

def _shutin_note(con, vfrom, vto, text="Well shut in for planned rod string replacement", note_type="operator_feedback"):
    con.execute(
        "INSERT INTO context_notes (note_id, asset_id, created_at, author_user_id, note_type, text, "
        "valid_from, valid_to) VALUES ('CTX-A-SHUT', 'WELL-A', '2026-10-07 08:00', 'U-1', ?, ?, ?, ?)",
        [note_type, text, vfrom, vto],
    )


@pytest.mark.parametrize(
    "vfrom, vto, flagged",
    [
        (date(2026, 10, 7), date(2026, 10, 20), True),
        (date(2026, 10, 16), date(2026, 10, 26), True),     # as_of == valid_from
        (date(2026, 10, 7), date(2026, 10, 16), True),      # as_of == valid_to
        (date(2026, 10, 17), date(2026, 10, 27), False),    # starts tomorrow: not active yet
        (date(2026, 10, 1), date(2026, 10, 15), False),     # ended yesterday
        (date(2026, 10, 7), None, True),                    # open-ended
    ],
)
def test_planned_shutin_note_flagged_only_while_its_window_covers_as_of(vfrom, vto, flagged):
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _shutin_note(con, vfrom, vto)
    assert bool(_hits(_run(con), "open_items", "T200§4")) is flagged
    con.close()


def test_planned_shutin_cleared_by_a_maintenance_record_from_valid_from_onwards():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _shutin_note(con, date(2026, 10, 7), date(2026, 10, 20))
    _maint(con, "MNT-A-0100", datetime(2026, 10, 6, 23, 59), mtype="scheduled", kind="rod_string")  # day before
    assert len(_hits(_run(con), "open_items", "T200§4")) == 1
    _maint(con, "MNT-A-0101", datetime(2026, 10, 7, 0, 0), mtype="scheduled", kind="rod_string")    # == valid_from
    assert _hits(_run(con), "open_items", "T200§4") == []
    con.close()


def test_planned_shutin_ignores_corrections_and_unrelated_text():
    con = _iso_db(); _well(con); _quiet_baseline(con)
    _shutin_note(con, date(2026, 10, 7), date(2026, 10, 20), note_type="correction")
    assert _hits(_run(con), "open_items", "T200§4") == []
    con.execute("DELETE FROM context_notes")
    _shutin_note(con, date(2026, 10, 7), date(2026, 10, 20), text="Gauge recalibrated, all good")
    assert _hits(_run(con), "open_items", "T200§4") == []
    con.close()
