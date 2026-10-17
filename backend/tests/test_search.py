"""Tests for backend/tools/search.py (Phase 3.2).

Builds the report index once (slow: chunks + embeds ~1500 reports) and
validates the acceptance query from PLAN.md Phase 3.2 / eval Q10:
search_reports('contained after', area_id='SW-24', start='2026-07-18')
should surface MNT-0082-028 and MNT-0100-034 in the top-3.
"""

from datetime import date
from pathlib import Path

import duckdb
import pytest

from backend.tools.search import search_reports

DB_PATH = Path(__file__).parent.parent.parent / "backend" / "data.duckdb"
CHROMA_PATH = Path(__file__).parent.parent / "chroma_db_test"


def _conn():
    if not DB_PATH.exists():
        pytest.skip("ingested DB not found — run scripts/ingest.py first")
    con = duckdb.connect(str(DB_PATH), read_only=False)
    con.execute(open(DB_PATH.parent / "db" / "views.sql").read())
    return con


@pytest.fixture(scope="module")
def conn():
    con = _conn()
    yield con
    con.close()


def test_search_reports_q10_containment(conn):
    result = search_reports(
        conn,
        "contained after",
        area_id="SW-24",
        start=date(2026, 7, 18),
        chroma_path=CHROMA_PATH,
    )
    maintenance_ids = {r["maintenance_id"] for r in result.data["results"]}
    assert "MNT-0082-028" in maintenance_ids
    assert "MNT-0100-034" in maintenance_ids

    evidence_ids = {e.source_id for e in result.evidence}
    assert "MNT-0082-028" in evidence_ids
    assert "MNT-0100-034" in evidence_ids


def test_search_reports_well_scoped(conn):
    result = search_reports(conn, "walking beam bearing wear", well_id="WELL-0170", k=3, chroma_path=CHROMA_PATH)
    assert len(result.data["results"]) > 0
    assert all(r["asset_id"] == "WELL-0170" for r in result.data["results"])
