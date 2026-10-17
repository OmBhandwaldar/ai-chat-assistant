"""Area and team arguments accept an id or a human name.

Uses backend/data.duckdb read-only (views are already ingested). Never writes.
"""

from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from backend.db.scope import resolve_scope
from backend.tools.search import search_reports
from backend.tools.spills import spill_map
from backend.tools.state import wells_not_producing

DB_PATH = Path(__file__).parent.parent / "data.duckdb"
AS_OF = date(2026, 10, 16)
AS_OF_DT = datetime(2026, 10, 16)
Q2_WELLS = {"WELL-0164", "WELL-0170", "WELL-0172", "WELL-0175"}


@pytest.fixture(scope="module")
def con():
    if not DB_PATH.exists():
        pytest.skip(f"ingested DB not found at {DB_PATH} — run scripts/ingest.py first")
    connection = duckdb.connect(str(DB_PATH), read_only=True)
    yield connection
    connection.close()


def _well_ids(result) -> set[str]:
    return {well["asset_id"] for well in result.data["wells"]}


def test_wells_not_producing_name_matches_area_id(con):
    by_id = wells_not_producing(con, area_id="NE-08", as_of=AS_OF)
    by_name = wells_not_producing(con, area_id="Northeast Block 8", as_of=AS_OF)
    assert _well_ids(by_id) == _well_ids(by_name) == Q2_WELLS
    assert "WELL-0164" in _well_ids(by_name)
    assert by_name.evidence  # a real scope cites inspections, not an empty miss


def test_area_name_variants_resolve_to_the_same_wells(con):
    baseline = _well_ids(wells_not_producing(con, area_id="NE-08", as_of=AS_OF))
    for variant in ("ne-08", "NE-8", "northeast block 8", "Northeast Block 08", "NE Block 8"):
        assert _well_ids(wells_not_producing(con, area_id=variant, as_of=AS_OF)) == baseline


def test_unknown_area_is_unresolved_not_an_empty_producing_list(con):
    result = wells_not_producing(con, area_id="Atlantis Block 99", as_of=AS_OF)
    assert result.data["wells"] == []
    assert result.data["corrections_applied"] == []
    assert result.evidence == []
    assert result.data["unresolved_scope"][0]["kind"] == "area"
    assert "Atlantis Block 99" in result.data["message"]
    assert "not an empty result" in result.data["message"]


def test_team_name_matches_team_id(con):
    by_id = wells_not_producing(con, team_id="TEAM-NE", as_of=AS_OF)
    by_name = wells_not_producing(con, team_id="Northeast Production", as_of=AS_OF)
    assert _well_ids(by_id) == _well_ids(by_name)
    assert "WELL-0164" in _well_ids(by_name)

    missing = wells_not_producing(con, team_id="The Avengers", as_of=AS_OF)
    assert missing.data["wells"] == []
    assert missing.evidence == []
    assert missing.data["unresolved_scope"][0]["kind"] == "team"
    assert "not an empty result" in missing.data["message"]


def test_spill_map_accepts_area_display_name(con):
    by_id = spill_map(con, area_id="NE-08", days=90, as_of=AS_OF_DT)
    by_name = spill_map(con, area_id="Northeast Block 8", days=90, as_of=AS_OF_DT)
    pins = {pin["asset_id"] for pin in by_name.data["pins"]}
    assert pins == {pin["asset_id"] for pin in by_id.data["pins"]}
    assert pins  # NE-08 has spills; a failed name lookup would be empty
    assert by_name.evidence

    missing = spill_map(con, area_id="Atlantis Block 99", days=90, as_of=AS_OF_DT)
    assert missing.data["pins"] == []
    assert missing.evidence == []
    assert "unresolved_scope" in missing.data


def test_resolve_scope_leaves_omitted_filters_alone(con):
    scope = resolve_scope(con)
    assert scope.ok and scope.area_id is None and scope.team_id is None


def test_search_reports_unknown_area_returns_before_any_hits(con):
    result = search_reports(con, "contained after", area_id="Atlantis Block 99")
    assert result.data["results"] == []
    assert result.evidence == []
    assert result.data["unresolved_scope"][0]["kind"] == "area"
    assert "not an empty result" in result.data["message"]
