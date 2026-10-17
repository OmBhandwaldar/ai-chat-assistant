"""Tests for backend/tools/sop_rules.py against the real sample dataset."""

from pathlib import Path

import pytest

from backend.tools.sop_rules import extract_threshold, load_sop_rules

FLEET_DIR = Path(__file__).parent.parent.parent.parent / "flytbase-ahc-swe-asset-intelligence-dataset" / "sample-data" / "fleet"


@pytest.fixture(scope="module")
def rules():
    if not FLEET_DIR.exists():
        pytest.skip(f"dataset not found at {FLEET_DIR}")
    return load_sop_rules(FLEET_DIR)


def test_loads_at_least_fifteen_entries(rules):
    assert len(rules) >= 15


def test_sr01_contains_24_hours(rules):
    assert "24 hours" in rules["SR-01"]["text"]


def test_sr02_contains_4_hours(rules):
    assert "4 hours" in rules["SR-02"]["text"]


def test_sr03_contains_60_days(rules):
    assert "60 days" in rules["SR-03"]["text"]


def test_ws05_pressure_test_present(rules):
    assert "pressure-test" in rules["WS-05"]["text"]


def test_rc_protocols_present(rules):
    for pid in ["RC-01", "RC-02", "RC-03", "RC-04", "RC-05"]:
        assert pid in rules


def test_well_type_sections_present(rules):
    assert "T200#9.3" in rules
    assert "T350#3" in rules
    assert "15" in rules["T200#9.3"]["text"]
    assert "20" in rules["T350#3"]["text"]


def test_extract_threshold_sr01_report_deadline():
    text = "file the written notice within 24 hours of discovery"
    assert extract_threshold(text, r"within\s+(\d+)\s+hours") == 24.0


def test_extract_threshold_returns_none_when_absent():
    assert extract_threshold("no numbers here", r"within\s+(\d+)\s+hours") is None
