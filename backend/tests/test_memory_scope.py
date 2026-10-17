"""Phase 6: multi-user flows (Q12-Q18) — scope of feedback, shared vs private
memory, conflict detection.

Every test here WRITES (overrides, shared notes, claims, conversations), so
each one runs against its own throw-away copy of backend/data.duckdb. The
real DB file is only ever read (copied once per session), never mutated.

User ids come from the sample dataset's fleet/users.json:
  U-102 Jake Morales (SW operator)    U-201 Michael Turner (NE supervisor)
  U-202 Jessica Reed (NE inspector)   U-204 Tyler Hughes (NE operator)
  U-401 Laura Simmons (HSE)           U-402 Chris Dalton (HSE)
"""

from __future__ import annotations

import shutil
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from backend.db.overrides import InvalidOverrideScope, record_override
from backend.db.resolver import resolve_inspection_notes, resolve_well_state
from backend.tools.memory import (
    recall_user_context,
    record_claim,
    record_feedback,
    record_shared_note,
)
from backend.tools.rca import rca_due
from backend.tools.sop import sop_breaches
from backend.tools.spills import spill_map, spills
from backend.tools.state import well_status_now, wells_not_producing

DB_PATH = Path(__file__).parent.parent / "data.duckdb"
VIEWS_SQL = Path(__file__).parent.parent / "db" / "views.sql"
FLEET_DIR = (
    Path(__file__).resolve().parents[3]
    / "flytbase-ahc-swe-asset-intelligence-dataset" / "sample-data" / "fleet"
)

AS_OF_DT = datetime(2026, 10, 16)
AS_OF = date(2026, 10, 16)

JAKE, MICHAEL, JESSICA, TYLER, LAURA = "U-102", "U-201", "U-202", "U-204", "U-401"


@pytest.fixture(scope="session")
def _template_db(tmp_path_factory) -> Path:
    if not DB_PATH.exists():
        pytest.skip(f"ingested DB not found at {DB_PATH} — run scripts/ingest.py first")
    template = tmp_path_factory.mktemp("memory_scope_template") / "template.duckdb"
    shutil.copyfile(DB_PATH, template)
    return template


@pytest.fixture
def con(_template_db, tmp_path):
    """A private writable copy of the ingested DB with the views (re)loaded."""
    copy = tmp_path / "scratch.duckdb"
    shutil.copyfile(_template_db, copy)
    connection = duckdb.connect(str(copy), read_only=False)
    connection.execute(VIEWS_SQL.read_text())
    yield connection
    connection.close()


def _fleet():
    return FLEET_DIR if FLEET_DIR.exists() else None


def _correct_well_0164(con):
    return record_feedback(
        con, "inspection", "INS-0164-0205", "wellhead_temp_c", 41.0, MICHAEL,
        reason="The handheld thermal gun was faulty; the field crew re-measured 41.0 C.",
    )


def _correct_well_0151(con):
    return record_feedback(
        con, "inspection", "INS-0151-0201", "spill_est_volume_bbl", 5.4, MICHAEL,
        reason="Field crew measured 5.4 bbl; the AI estimated 4.5 bbl.",
    )


# ---------------------------------------------------------------------------
# Q12 — correcting wrong raw data
# ---------------------------------------------------------------------------

def test_q12_correction_flips_state_keeps_original_and_flags_inspector_note(con):
    baseline = well_status_now(con, "WELL-0164", as_of=AS_OF_DT)
    assert baseline.data["state"] == "not_producing"

    result = _correct_well_0164(con)
    data = result.data
    assert data["recorded"] is True
    assert data["scope_type"] == "inspection" and data["scope_id"] == "INS-0164-0205"
    assert float(data["original_value"]) == 24.0          # AI-visible original kept
    assert float(data["new_value"]) == 41.0
    assert data["author"] == "Michael Turner"
    assert any(e.source_type == "override" for e in result.evidence)

    # The immediate impact: producing now, and the inspector's note disagrees.
    assert data["resulting_state"]["state"] == "producing"
    conflicts = data["resulting_state"]["conflicts"]
    assert [c["type"] for c in conflicts] == ["override_vs_inspector_note"]
    assert "Pump jack not moving." in conflicts[0]["right"]
    assert data["confirmation_needed"] is True

    # Raw inspection row is untouched; the override row carries the history.
    raw = con.execute(
        "SELECT wellhead_temp_c FROM inspections WHERE inspection_id='INS-0164-0205'"
    ).fetchone()[0]
    assert raw == 24.0
    row = con.execute(
        "SELECT original_value, new_value, author_user_id, reason FROM overrides"
    ).fetchone()
    assert float(row[0]) == 24.0 and float(row[1]) == 41.0 and row[2] == MICHAEL
    assert "thermal gun" in row[3]

    status = well_status_now(con, "WELL-0164", as_of=AS_OF_DT)
    assert status.data["state"] == "producing"
    joined = " ".join(status.data["reasons"])
    assert "21.1" in joined                                # 41.0 - 19.9
    assert "corrected by Michael Turner" in joined
    assert "24.0" in joined                                # original value mentioned


def test_q12_then_q2_lists_three_wells_and_explains_the_missing_one(con):
    before = wells_not_producing(con, area_id="NE-08", as_of=AS_OF)
    assert {w["asset_id"] for w in before.data["wells"]} == {
        "WELL-0164", "WELL-0170", "WELL-0172", "WELL-0175",
    }

    _correct_well_0164(con)

    after = wells_not_producing(con, area_id="NE-08", as_of=AS_OF)
    assert {w["asset_id"] for w in after.data["wells"]} == {
        "WELL-0170", "WELL-0172", "WELL-0175",
    }
    corrections = after.data["corrections_applied"]
    assert [c["asset_id"] for c in corrections] == ["WELL-0164"]
    assert corrections[0]["now_producing"] is True
    assert corrections[0]["author"] == "Michael Turner"
    assert float(corrections[0]["original_value"]) == 24.0
    assert float(corrections[0]["corrected_value"]) == 41.0
    assert any(e.source_type == "override" for e in after.evidence)


def test_unknown_inspection_or_field_is_rejected_not_silently_ignored(con):
    with pytest.raises(InvalidOverrideScope):
        record_override(con, "inspection", "INS-9999-0000", "wellhead_temp_c", 41.0, MICHAEL)
    with pytest.raises(InvalidOverrideScope):
        record_override(con, "inspection", "INS-0164-0205", "wellhead_temperature", 41.0, MICHAEL)
    with pytest.raises(InvalidOverrideScope):
        record_override(con, "well_type", "T999", "rca_corrective_count", 2, MICHAEL)

    # The tool wrapper turns that into an error payload instead of raising mid-run.
    result = record_feedback(
        con, "inspection", "INS-0164-0205", "wellhead_temperature", 41.0, MICHAEL
    )
    assert result.data["recorded"] is False and result.data["error"]
    assert con.execute("SELECT count(*) FROM overrides").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Q13 — correcting the AI's analysis (spill volume -> reportable)
# ---------------------------------------------------------------------------

def test_q13_volume_correction_makes_spill_reportable_with_sop_cascade(con):
    result = _correct_well_0151(con)
    data = result.data
    assert data["recorded"] is True
    assert float(data["original_value"]) == 4.5          # AI's estimate kept for reference
    assert float(data["new_value"]) == 5.4

    impact = data["impact"]
    assert impact["asset_id"] == "WELL-0151"
    assert impact["reportable"] is True
    assert impact["volume_bbl"] == 5.4 and impact["original_volume_bbl"] == 4.5
    assert impact["inspected_at"].startswith("2026-09-24 15:30")

    findings = {f["protocol_id"]: f for f in impact["sop_findings"]}
    assert findings["SR-01"]["status"] == "breach"
    assert "No written notice" in findings["SR-01"]["detail"]
    assert findings["SR-01"]["due_by"] == "2026-09-25 15:30"
    assert findings["SR-03"]["status"] == "open"
    assert findings["SR-03"]["due_by"] == "2026-11-23"
    assert findings["SR-02"]["status"] == "breach"
    assert "6.4" in findings["SR-02"]["detail"]
    assert "MNT-0151-025" in impact["related_repair_ids"]


def test_q13_sop_engine_flags_sr01_and_sr03_only_after_correction(con):
    def by_protocol(items, pid):
        return [i for i in items if i["protocol_id"] == pid]

    before = sop_breaches(con, "WELL-0151", lookback_days=365, as_of=AS_OF, fleet_dir=_fleet())
    assert by_protocol(before.data["breaches"], "SR-01") == []
    assert by_protocol(before.data["open_items"], "SR-03") == []
    # SR-02 (6.4 h containment) is independent of the volume correction.
    assert any("MNT-0151-025" in b["evidence_id"] for b in by_protocol(before.data["breaches"], "SR-02"))

    _correct_well_0151(con)
    after = sop_breaches(con, "WELL-0151", lookback_days=365, as_of=AS_OF, fleet_dir=_fleet())
    sr01 = by_protocol(after.data["breaches"], "SR-01")
    assert len(sr01) == 1 and "5.4 bbl" in sr01[0]["detail"]
    sr03 = by_protocol(after.data["open_items"], "SR-03")
    assert len(sr03) == 1 and sr03[0]["due_by"] == "2026-11-23"


# ---------------------------------------------------------------------------
# Q14 / 6.3 — a rule for a whole well type
# ---------------------------------------------------------------------------

def _rca_wells(con, **kwargs):
    result = rca_due(con, equipment_kind="pump_jack", area_id="NE-07", as_of=AS_OF, **kwargs)
    return result, {w["asset_id"]: w for w in result.data["wells"]}


def test_q14_default_rca_threshold_is_three_for_everyone(con):
    _, wells = _rca_wells(con)
    assert set(wells) == {"WELL-0108", "WELL-0121", "WELL-0138"}
    assert all(w["corrective_count"] == 3 and w["threshold"] == 3 for w in wells.values())
    assert all(w["threshold_source"] == "manual" for w in wells.values())


def test_q14_t350_rule_applies_to_t350_only_not_t200(con):
    fb = record_feedback(
        con, "well_type", "T350", "rca_corrective_count", 2, MICHAEL,
        reason="Our team now wants a pump jack RCA after 2 repairs, not 3.",
    )
    assert fb.data["recorded"] is True
    assert int(float(fb.data["original_value"])) == 3 and int(float(fb.data["new_value"])) == 2

    result, wells = _rca_wells(con)
    assert set(wells) == {"WELL-0108", "WELL-0120", "WELL-0121", "WELL-0138"}
    assert "WELL-0102" not in wells              # T200 with 2 repairs: must NOT be added

    # WELL-0120 is the newly added T350 well with exactly 2 repairs, under the rule.
    assert wells["WELL-0120"]["well_type"] == "T350"
    assert wells["WELL-0120"]["corrective_count"] == 2 and wells["WELL-0120"]["threshold"] == 2
    assert wells["WELL-0120"]["threshold_source"] == "override"
    # T200 wells still use the manual's 3.
    assert wells["WELL-0121"]["well_type"] == "T200"
    assert wells["WELL-0121"]["threshold"] == 3
    assert wells["WELL-0121"]["threshold_source"] == "manual"

    # The answer can say it follows Michael Turner's rule, not the manual.
    rules = {r["well_type"]: r for r in result.data["rules"]}
    assert rules["T350"]["source"] == "override"
    assert rules["T350"]["set_by"] == "Michael Turner"
    assert rules["T350"]["manual_threshold"] == 3 and rules["T350"]["threshold"] == 2
    assert rules["T200"]["source"] == "manual"
    assert any(e.source_type == "override" for e in result.evidence)

    # Querying T200 explicitly never sees the T350 rule.
    t200_only = rca_due(con, equipment_kind="pump_jack", well_type="T200", as_of=AS_OF)
    assert all(w["well_type"] == "T200" and w["threshold"] == 3 for w in t200_only.data["wells"])
    assert "WELL-0102" not in {w["asset_id"] for w in t200_only.data["wells"]}


def test_q14_rule_scope_id_is_case_insensitive_and_latest_rule_wins(con):
    record_feedback(con, "well_type", "t350", "rca_corrective_count", 2, MICHAEL)
    _, wells = _rca_wells(con)
    assert "WELL-0120" in wells                 # 't350' normalised to 'T350'

    record_feedback(con, "well_type", "T350", "rca_corrective_count", 3, MICHAEL,
                    reason="back to the manual value")
    _, wells = _rca_wells(con)
    assert "WELL-0120" not in wells


def test_q14_sop_engine_open_item_uses_scoped_threshold(con):
    # WELL-0120 (T350, 2 repairs) only gets an RCA open item under the T350 rule;
    # WELL-0102 (T200, 2 repairs) never does.
    def rca_items(asset):
        res = sop_breaches(con, asset, lookback_days=365, as_of=AS_OF, fleet_dir=_fleet())
        return [i for i in res.data["open_items"] if i["protocol_id"] == "§10"]

    assert rca_items("WELL-0120") == [] and rca_items("WELL-0102") == []
    record_feedback(con, "well_type", "T350", "rca_corrective_count", 2, MICHAEL)
    assert len(rca_items("WELL-0120")) == 1
    assert rca_items("WELL-0102") == []


# ---------------------------------------------------------------------------
# Q15 — a correction reaches other users
# ---------------------------------------------------------------------------

def test_q15_jessica_sees_michaels_correction_with_author_and_conflict(con):
    _correct_well_0164(con)

    # Jessica's tools/resolver run on the same DB; nothing is scoped to Michael.
    state = resolve_well_state(con, "WELL-0164", as_of=AS_OF_DT)
    assert state.state == "producing"
    assert any(c.type == "override_vs_inspector_note" for c in state.conflicts)

    result = well_status_now(con, "WELL-0164", as_of=AS_OF_DT)
    assert result.data["state"] == "producing"
    joined = " ".join(result.data["reasons"])
    assert "corrected by Michael Turner" in joined
    assert "Pump jack not moving." in joined
    assert [c["type"] for c in result.data["conflicts"]] == ["override_vs_inspector_note"]
    # Evidence cites the override itself plus the inspection.
    types = {(e.source_type, e.source_id) for e in result.evidence}
    assert ("inspection", "INS-0164-0205") in types
    override_id = con.execute("SELECT id FROM overrides").fetchone()[0]
    assert ("override", str(override_id)) in types


def test_q15_laura_ne08_reportable_spills_picks_up_q13_correction(con):
    before = spill_map(con, area_id="NE-08", days=90, as_of=AS_OF_DT)
    assert {p["asset_id"] for p in before.data["pins"]} == {
        "WELL-0151", "WELL-0165", "WELL-0175", "WELL-0186",
    }
    assert [p for p in before.data["pins"] if p["reportable"]] == []

    _correct_well_0151(con)

    after = spill_map(con, area_id="NE-08", days=90, as_of=AS_OF_DT)
    reportable = [p for p in after.data["pins"] if p["reportable"]]
    assert [p["asset_id"] for p in reportable] == ["WELL-0151"]
    pin = reportable[0]
    assert pin["bbl"] == 5.4 and pin["original_bbl"] == 4.5
    assert pin["corrected_by"] == "Michael Turner"
    assert pin["notice_on_record"] is False and pin["notice_overdue"] is True
    assert pin["notice_due_by"] == "2026-09-25 15:30"
    assert after.data["reportable_threshold_bbl"] == 5.0
    # No other spill in the period reached 5 bbl.
    assert max(p["bbl"] for p in after.data["pins"] if p["asset_id"] != "WELL-0151") < 5.0
    assert any(e.source_type == "override" for e in after.evidence)

    # And the SOP engine answers the same way for that well.
    breaches = sop_breaches(con, "WELL-0151", 365, as_of=AS_OF, fleet_dir=_fleet()).data["breaches"]
    assert "SR-01" in {b["protocol_id"] for b in breaches}


# ---------------------------------------------------------------------------
# Q16 — shared knowledge, private memory
# ---------------------------------------------------------------------------

def _seed_michael_q11_chat(con):
    con.execute(
        """
        INSERT INTO conversations (user_id, conversation_id, turn_id, role, text, ts) VALUES
          (?, 'day1', 1, 'user',
           'Which 3 wells in Northeast Block 7 had the most downtime in the last 4 months?',
           TIMESTAMP '2026-10-15 09:00:00'),
          (?, 'day1', 1, 'assistant',
           'WELL-0121 (49 days), WELL-0138 (42 days) and WELL-0107 (42 days) in Northeast Block 7.',
           TIMESTAMP '2026-10-15 09:00:05')
        """,
        [MICHAEL, MICHAEL],
    )


def test_q16_jakes_shared_note_is_visible_to_michael_via_the_resolver(con):
    note = record_shared_note(
        con, "WELL-0007",
        "FYI for everyone: WELL-0007 will be shut in from 17 Oct for a 10-day planned workover.",
        JAKE, valid_from=date(2026, 10, 17), valid_to=date(2026, 10, 27),
    )
    note_id = note.data["note_id"]
    assert note_id.startswith("CTX-RT-")
    assert note.evidence[0].source_type == "context"
    stored = con.execute(
        "SELECT author_user_id, source, valid_from, valid_to FROM context_notes WHERE note_id=?",
        [note_id],
    ).fetchone()
    assert stored[0] == JAKE and stored[1] == "runtime"

    # Michael asks "Is any downtime planned for WELL-0007?" in a new chat on 16 Oct:
    # the note is for the future (valid from 17 Oct), so it must show as upcoming.
    result = well_status_now(con, "WELL-0007", as_of=AS_OF_DT)
    joined = " ".join(result.data["reasons"])
    assert note_id in joined
    assert "Jake Morales" in joined
    assert "10-day planned workover" in joined
    assert "17 Oct 2026" in joined
    assert any(e.source_id == note_id and e.source_type == "context" for e in result.evidence)

    # Once the window opens (e.g. 20 Oct) it is simply an active note.
    later = resolve_well_state(con, "WELL-0007", as_of=datetime(2026, 10, 20))
    assert note_id in " ".join(later.reasons)
    # After the window closes it is gone.
    after = resolve_well_state(con, "WELL-0007", as_of=datetime(2026, 11, 15))
    assert note_id not in " ".join(after.reasons)


def test_q16_michaels_chat_history_is_not_visible_to_jessica(con):
    _seed_michael_q11_chat(con)
    con.execute(
        "INSERT INTO conversations (user_id, conversation_id, turn_id, role, text) "
        "VALUES (?, 'jess-1', 1, 'user', 'Is WELL-0164 producing oil right now?')",
        [JESSICA],
    )

    michael = recall_user_context(con, MICHAEL, hint="wells")
    assert michael.data["has_history"] is True
    assert set(michael.data["well_ids"]) == {"WELL-0121", "WELL-0138", "WELL-0107"}
    assert michael.data["area_names"] == ["Northeast Block 7"]

    for hint in (None, "three wells we discussed yesterday", "downtime", "WELL-0121"):
        jessica = recall_user_context(con, JESSICA, hint=hint)
        assert not ({"WELL-0121", "WELL-0138", "WELL-0107"} & set(jessica.data["well_ids"])), hint
        assert "Northeast Block 7" not in jessica.data["area_names"], hint
    assert recall_user_context(con, JESSICA).data["well_ids"] == ["WELL-0164"]  # her own chat only

    # Exactly Q16: Jessica's only turn is the question itself (app.py stores the user's
    # message before the agent runs). Nothing earlier exists for her.
    con.execute("DELETE FROM conversations WHERE user_id = ?", [JESSICA])
    con.execute(
        "INSERT INTO conversations (user_id, conversation_id, turn_id, role, text) VALUES "
        "(?, 'jess-2', 1, 'user', 'For the three wells we discussed yesterday, what have "
        "repairs cost this year?')",
        [JESSICA],
    )
    jess_hist = recall_user_context(con, JESSICA, hint="three wells we discussed yesterday")
    assert jess_hist.data["has_history"] is False      # "no such earlier conversation"
    assert jess_hist.data["well_ids"] == [] and jess_hist.data["area_names"] == []
    assert jess_hist.data["note"]

    # A user with no chats at all gets nothing either.
    assert recall_user_context(con, LAURA).data["has_history"] is False


def test_q16_agent_context_preamble_is_per_user(con):
    from backend.agent import _load_user_context

    _seed_michael_q11_chat(con)
    assert "WELL-0121" in _load_user_context(con, MICHAEL)
    assert "WELL-0121" not in _load_user_context(con, JESSICA)
    assert _load_user_context(con, JESSICA) == ""


def test_q16_shared_note_is_shared_but_conversation_is_not(con):
    """The two memory scopes side by side: the note reaches everyone, the chat does not."""
    _seed_michael_q11_chat(con)
    record_shared_note(con, "WELL-0007", "Planned workover.", JAKE,
                       valid_from=date(2026, 10, 17), valid_to=date(2026, 10, 27))
    for _user in (MICHAEL, JESSICA, LAURA):          # tools take no user: same view for all
        reasons = " ".join(well_status_now(con, "WELL-0007", as_of=AS_OF_DT).data["reasons"])
        assert "Planned workover." in reasons
    assert recall_user_context(con, JESSICA, hint="WELL-0121").data["well_ids"] == []


# ---------------------------------------------------------------------------
# Q17 — people disagree with the data
# ---------------------------------------------------------------------------

def test_q17_tyler_claims_well_0175_runs_fine_conflict_unconfirmed(con):
    # Michael's earlier statement agrees with the data.
    mine = record_claim(con, MICHAEL, "well", "WELL-0175",
                        "WELL-0175 has been shut in for planned work since 14 Oct.")
    assert mine.data["conflicts"] == []
    assert mine.data["contradicts_data"] is False

    result = record_claim(con, TYLER, "well", "WELL-0175", "WELL-0175 is running fine.")
    data = result.data
    assert data["status"] == "unconfirmed"
    assert data["contradicts_data"] is True
    assert [c["type"] for c in data["conflicts"]] == ["claim_vs_data"]
    conflict = data["conflicts"][0]
    assert "Tyler Hughes" in conflict["left"] and "running fine" in conflict["left"]
    assert "not producing" in conflict["right"] and "0.4" in conflict["right"]
    # The data side is returned too, so the agent can say what the inspection showed.
    assert data["resolved_state"]["state"] == "not_producing"
    assert any("Jessica Reed" in r and "INS-0175" in r for r in data["resolved_state"]["reasons"])
    assert any(e.source_type == "claim" for e in result.evidence)
    assert any(e.source_type == "inspection" and e.source_id.startswith("INS-0175-")
               for e in result.evidence)

    # Both statements stay on record, both unconfirmed; the data was not changed.
    rows = con.execute(
        "SELECT user_id, status FROM shared_claims WHERE scope_id='WELL-0175' ORDER BY id"
    ).fetchall()
    assert rows == [(MICHAEL, "unconfirmed"), (TYLER, "unconfirmed")]
    assert con.execute("SELECT count(*) FROM overrides").fetchone()[0] == 0

    # Everyone asking later still sees the disagreement, not a silently-picked side.
    status = well_status_now(con, "WELL-0175", as_of=AS_OF_DT)
    assert status.data["state"] == "not_producing"
    assert [c["type"] for c in status.data["conflicts"]] == ["claim_vs_data"]


def test_q17_jake_claims_well_0001_back_online_conflict_mentions_note(con):
    result = record_claim(con, JAKE, "well", "WELL-0001", "WELL-0001 is back online since 14 Oct.")
    data = result.data
    assert data["status"] == "unconfirmed" and data["contradicts_data"] is True
    assert [c["type"] for c in data["conflicts"]] == ["claim_vs_data"]
    assert "INS-0001-0203" in data["conflicts"][0]["right"]
    reasons = " ".join(data["resolved_state"]["reasons"])
    assert "CTX-0001-003" in reasons                      # planned shut-in, back ~20 Oct
    assert {e.source_id for e in result.evidence} >= {"INS-0001-0203", "CTX-0001-003"}


def test_q17_claim_conflicts_are_scoped_to_the_new_claim_and_negations_are_understood(con):
    first = record_claim(con, TYLER, "well", "WELL-0175", "WELL-0175 is running fine.")
    assert len(first.data["conflicts"]) == 1
    # A second, consistent claim is not reported as conflicting just because an
    # earlier, unrelated claim on the same well does.
    second = record_claim(con, MICHAEL, "well", "WELL-0175", "WELL-0175 is not producing, shut in.")
    assert second.data["conflicts"] == [] and second.data["contradicts_data"] is False

    # "not running" must read as a down-claim, so it conflicts with a producing well.
    _correct_well_0164(con)                                # WELL-0164 now producing
    neg = record_claim(con, TYLER, "well", "WELL-0164", "WELL-0164 is not running.")
    assert [c["type"] for c in neg.data["conflicts"]] == ["claim_vs_data"]


# ---------------------------------------------------------------------------
# Q18 — people disagree about the AI's analysis
# ---------------------------------------------------------------------------

def test_q18_two_notes_disagree_detection_stands(con):
    verdict = resolve_inspection_notes(con, "INS-0101-0197")
    assert verdict.detection_stands is True
    assert {n["note_id"] for n in verdict.notes} == {"CTX-0101-002", "CTX-0101-003"}
    assert [c.type for c in verdict.conflicts] == ["note_vs_note"]
    both_sides = verdict.conflicts[0].left + verdict.conflicts[0].right
    assert "Ryan Brooks" in both_sides and "Chris Dalton" in both_sides
    assert "produced water" in both_sides and "crude" in both_sides
    # Why it stands: no formal correction, and a spill repair exists.
    basis = " ".join(verdict.basis)
    assert "no formal correction" in basis.lower()
    assert "MNT-0101-029" in basis
    assert "MNT-0101-029" in verdict.repair_ids


def test_q18_spills_tool_returns_disagreement_for_well_0101(con):
    result = spills(con, "WELL-0101", years=4, as_of=AS_OF_DT)
    by_id = {s["inspection_id"]: s for s in result.data["spills"]}
    spill = by_id["INS-0101-0197"]                         # the 20 Aug spill, 1.0 bbl
    assert spill["bbl"] == 1.0 and spill["date"] == "2026-08-20"
    assert spill["detection_stands"] is True
    assert {n["note_id"] for n in spill["notes"]} == {"CTX-0101-002", "CTX-0101-003"}
    assert [c["type"] for c in spill["conflicts"]] == ["note_vs_note"]
    assert "MNT-0101-029" in spill["repair_ids"]
    assert [c["type"] for c in result.data["conflicts"]] == ["note_vs_note"]
    ids = {e.source_id for e in result.evidence}
    assert {"INS-0101-0197", "CTX-0101-002", "CTX-0101-003", "MNT-0101-029"} <= ids
    # Spills nobody disputes carry no conflict.
    assert all(s["conflicts"] == [] for sid, s in by_id.items() if sid != "INS-0101-0197")


def test_q18_a_formal_correction_note_would_drop_the_detection(con):
    """Contrast: a *formal* correction removes the detection entirely (Q4 rule), while the
    two informal notes for INS-0101-0197 do not."""
    con.execute(
        """
        INSERT INTO context_notes (note_id, asset_id, created_at, author_user_id, note_type,
                                   inspection_id, text, valid_from, source)
        VALUES ('CTX-TEST-FORMAL', 'WELL-0101', TIMESTAMP '2026-08-21 08:00:00', 'U-402',
                'correction', 'INS-0101-0197', 'Formal correction: produced water.',
                DATE '2026-08-21', 'dataset')
        """
    )
    ids = {s["inspection_id"] for s in spills(con, "WELL-0101", 4, as_of=AS_OF_DT).data["spills"]}
    assert "INS-0101-0197" not in ids
    assert resolve_inspection_notes(con, "INS-0101-0197").detection_stands is False
