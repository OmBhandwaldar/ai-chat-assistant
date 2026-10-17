"""Tests for backend/critic.py (Phase 4.2).

Acceptance: inject a hallucinated inspection ID into an answer; critic flags it.
"""

from pathlib import Path

import duckdb
import pytest

from backend.critic import (
    check_numbers_grounded,
    check_visual_type,
    run_critic,
)
from backend.models.schemas import AgentAnswer, Evidence, UIVisual

DB_PATH = Path(__file__).parent.parent.parent / "backend" / "data.duckdb"


def _conn():
    if not DB_PATH.exists():
        pytest.skip("ingested DB not found")
    return duckdb.connect(str(DB_PATH), read_only=True)


def test_hallucinated_citation_flagged():
    """Core acceptance: a fabricated inspection ID is caught."""
    con = _conn()
    answer = AgentAnswer(
        text="WELL-0001 had a temperature of 45.2 C.",
        visual=UIVisual(type="status_card", data={}),
        citations=[
            Evidence(source_type="inspection", source_id="INS-FAKE-9999"),
            Evidence(source_type="inspection", source_id="INS-0001-0203"),
        ],
        conflicts=[],
    )
    tool_results = [{"data": {"wellhead_temp_c": 45.2}}]

    result = run_critic("Is WELL-0001 producing?", answer, tool_results, con)
    assert not result.ok
    assert any("INS-FAKE-9999" in i for i in result.issues)
    assert result.must_retry
    con.close()


def test_valid_answer_passes():
    """A correctly cited answer should pass all checks."""
    con = _conn()
    answer = AgentAnswer(
        text="WELL-0001 is not producing. Delta was 0.4 C.",
        visual=UIVisual(type="status_card", data={}),
        citations=[
            Evidence(source_type="inspection", source_id="INS-0001-0203"),
        ],
        conflicts=[],
    )
    tool_results = [{"data": {"delta_c": 0.4, "state": "not_producing"}}]

    result = run_critic("What is the status of WELL-0001?", answer, tool_results, con)
    assert result.ok, f"Unexpected issues: {result.issues}"
    con.close()


def test_ungrounded_number_flagged():
    """A number in the text that doesn't appear in tool results is flagged."""
    answer = AgentAnswer(
        text="The well had 999 spills last year.",
        visual=UIVisual(type="none", data={}),
        citations=[],
        conflicts=[],
    )
    issues = check_numbers_grounded(answer, [{"data": {"count": 3}}])
    assert any("999" in i for i in issues)


def test_visual_type_mismatch_flagged():
    """If user asks about temperature and we return a table, flag it."""
    answer = AgentAnswer(
        text="Temperature trend for WELL-0001.",
        visual=UIVisual(type="table", data={}),
        citations=[],
        conflicts=[],
    )
    issues = check_visual_type(answer, "Show me the temperature trend for WELL-0001")
    assert len(issues) >= 1
    assert "line" in issues[0]


def test_not_producing_list_may_be_a_table():
    """'not producing' contains 'producing'; the longer phrase asks for a table."""
    answer = AgentAnswer(
        text="Four wells are shut in.",
        visual=UIVisual(type="table", data={}),
        citations=[],
        conflicts=[],
    )
    assert check_visual_type(answer, "Which wells in area NE-08 are not producing?") == []


def test_list_markers_dates_floats_and_rule_card_are_not_hallucinations():
    """Q2 false positives: '1.', ISO dates, 4.100000000000001 vs 4.1, the 15 C rule."""
    answer = AgentAnswer(
        text=(
            "As of October 16, 2026 there are 4 wells. "
            "1. Delta was 4.1 C on 2026-10-03, below the 15 C T200 line. "
            "2. Another was 2.9 C. 3. A third was 5.7 C."
        ),
        visual=UIVisual(type="table", data={}),
        citations=[],
        conflicts=[],
    )
    tool_results = [{
        "wells": [
            {"delta_c": 4.100000000000001},
            {"delta_c": 2.9000000000000004},
            {"delta_c": 5.699999999999999},
        ]
    }]
    assert check_numbers_grounded(answer, tool_results) == []


def test_invented_number_still_must_retry():
    """A number that is not in the tools or the rule card is a real hallucination."""
    con = _conn()
    answer = AgentAnswer(
        text="The well had 999 spills.",
        visual=UIVisual(type="none", data={}),
        citations=[Evidence(source_type="inspection", source_id="INS-0001-0203")],
        conflicts=[],
    )
    result = run_critic("How many spills were there?", answer, [{"data": {"count": 3}}], con)
    assert result.must_retry
    assert any("999" in issue for issue in result.issues)
    con.close()
