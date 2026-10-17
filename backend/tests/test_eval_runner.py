"""Tests for the eval harness (eval/): fixture schema, pure scoring, run planning, DB handling,
and an offline dry-run through the real Agent with pydantic-ai's TestModel. No LLM calls."""

from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path

import duckdb
import pytest

from eval import run_eval
from eval.scoring import (
    QUESTION_IDS,
    entry_turns,
    extract_numbers,
    load_expected,
    normalize,
    number_in_text,
    number_matches,
    score_answer,
    select_questions,
    synthesize_answer,
    tally,
    validate_expected,
)

REPO = Path(__file__).resolve().parents[2]
DB_PATH = REPO / "backend" / "data.duckdb"
SCHEMA_SQL = REPO / "backend" / "db" / "schema.sql"
USERS_JSON = (
    REPO.parent / "flytbase-ahc-swe-asset-intelligence-dataset" / "sample-data" / "fleet" / "users.json"
)


def _answer(text="", vtype="none", data=None, citations=(), conflicts=()):
    return {
        "text": text,
        "visual": {"type": vtype, "data": data or {}},
        "citations": [{"source_id": c, "source_type": "inspection"} for c in citations],
        "conflicts": [{"type": "t", "left": "l", "right": "r"} for _ in conflicts],
    }


# ---------------------------------------------------------------------------
# expected.json schema (7.1)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def expected():
    return load_expected()


def test_expected_json_is_valid_json_with_all_18_questions(expected):
    raw = json.loads((REPO / "eval" / "expected.json").read_text())
    assert [k for k in raw if not k.startswith("_")] == QUESTION_IDS  # also pins run order
    assert set(expected) == set(QUESTION_IDS)


def test_expected_json_passes_schema_validation(expected):
    known_users = None
    if USERS_JSON.exists():
        known_users = {u["user_id"] for u in json.loads(USERS_JSON.read_text())["users"]}
    assert validate_expected(expected, known_user_ids=known_users) == []


def test_every_entry_has_required_keys(expected):
    for qid, entry in expected.items():
        assert entry["id"] == qid
        assert entry["question_text"].strip()
        assert re.fullmatch(r"U-\d{3}", entry["user_id"])
        assert isinstance(entry["expected"], dict)
        assert "visual_type" in entry["expected"]
        assert isinstance(entry["depends_on"], list) and isinstance(entry["runs_after"], list)


def test_acceptance_values_from_the_plan(expected):
    q4 = expected["Q4"]["expected"]["numbers"]
    assert q4["total_bbl"] == 15.1 and q4["spill_count"] == 6
    q6 = expected["Q6"]["expected"]
    assert "WELL-0121" in q6["well_ids_in_data"] and q6["numbers"]["WELL-0121_days"] == 49


def test_stateful_dependencies_are_recorded(expected):
    assert "Q6" in expected["Q11"]["depends_on"]
    assert {"Q12", "Q13"} <= set(expected["Q15"]["depends_on"])
    assert "Q11" in expected["Q16"]["depends_on"]
    assert "Q2" in expected["Q12"]["runs_after"]
    assert "Q7" in expected["Q13"]["runs_after"]
    # Q14: ask -> state rule -> new chat ask, with different conversation labels
    convs = [t.conversation for t in entry_turns(expected["Q14"])]
    assert len(convs) == 3 and len(set(convs)) == 3
    assert [t.scored for t in entry_turns(expected["Q14"])] == [True, False, True]
    # Q11 asked as the same user as Q6 so the history exists
    assert expected["Q11"]["user_id"] == expected["Q6"]["user_id"]
    # Q16: Jessica must not be Michael
    post = entry_turns(expected["Q16"])[-1]
    assert post.user_id != expected["Q11"]["user_id"]
    assert post.expected["text_not_contains"]


@pytest.fixture(scope="module")
def db_copy(tmp_path_factory):
    if not DB_PATH.exists():
        pytest.skip("ingested DB not found - run scripts/ingest.py first")
    dst = tmp_path_factory.mktemp("evaldb") / "data.duckdb"
    shutil.copy2(DB_PATH, dst)
    con = duckdb.connect(str(dst))
    yield con
    con.close()


def test_expected_ids_exist_in_the_ingested_dataset(expected, db_copy):
    """Catches transcription typos: every cited record / well id must exist in the data."""
    tables = {
        "INS": ("inspections", "inspection_id"),
        "MNT": ("maintenance", "maintenance_id"),
        "CTX": ("context_notes", "note_id"),
        "REG": ("regulatory_events", "event_id"),
    }
    missing = []
    for qid, entry in expected.items():
        for turn in entry_turns(entry):
            exp = turn.expected or {}
            for cid in exp.get("citations_must_include", []):
                table, col = tables[cid.split("-")[0]]
                if not db_copy.execute(f"SELECT 1 FROM {table} WHERE {col} = ?", [cid]).fetchone():
                    missing.append((qid, cid))
            for key in ("well_ids_in_data", "well_ids_not_in_data", "well_ids_in_data_optional"):
                for wid in exp.get(key, []):
                    if not db_copy.execute("SELECT 1 FROM wells WHERE asset_id = ?", [wid]).fetchone():
                        missing.append((qid, wid))
    assert missing == []


def test_validate_expected_catches_problems(expected):
    bad = copy.deepcopy(expected)
    del bad["Q7"]
    bad["Q1"]["expected"]["bogus_key"] = 1
    bad["Q2"]["expected"]["numbers"] = {"x": "not-a-number"}
    bad["Q3"]["expected"]["visual_type"] = "pie"
    bad["Q4"]["depends_on"] = ["Q9"]  # forward reference
    bad["Q5"]["user_id"] = ""
    errors = " | ".join(validate_expected(bad))
    for needle in ("Q7: missing entry", "bogus_key", "not a number", "pie", "must appear earlier",
                   "user_id must be"):
        assert needle in errors


def test_validate_expected_flags_unknown_user(expected):
    errors = validate_expected(expected, known_user_ids={"U-201"})
    assert any("unknown user_id" in e for e in errors)


# ---------------------------------------------------------------------------
# Number extraction + matching
# ---------------------------------------------------------------------------

def test_extract_numbers_ignores_ids_dates_and_handles_commas():
    nums = extract_numbers(
        "WELL-0121 had 49 days; cost $25,794 on 2026-10-15T09:45:00 at 09:45 (INS-0001-0203, SR-01)"
    )
    assert nums == [49.0, 25794.0]
    assert extract_numbers("-102.18445 and 3.2 C and 15.1.") == [-102.18445, 3.2, 15.1]


def test_number_matching_tolerance_is_half_a_percent():
    assert number_matches(15.1, 15.1)
    assert number_matches(15.1, 15.17)         # +0.46 %
    assert not number_matches(15.1, 15.2)      # +0.66 %
    assert number_matches(25794, 25794.0)
    assert not number_matches(49, 50)
    assert number_matches(0, 0) and not number_matches(0, 0.1)
    assert number_in_text(21, "It was down 21 days in 2025") == 21.0
    assert number_in_text(21, "It was down 12 days") is None


# ---------------------------------------------------------------------------
# score_answer
# ---------------------------------------------------------------------------

def test_perfect_answer_passes():
    exp = {
        "visual_type": "bar",
        "text_contains": ["WELL-0121", ["alpha", "49 days"]],
        "numbers": {"d": 49},
        "well_ids_in_data": ["WELL-0121"],
        "citations_must_include": ["INS-1"],
        "min_citations": 1,
        "conflicts_min": 1,
    }
    ans = _answer("WELL-0121 was down 49 days", "bar", {"rows": [{"well": "WELL-0121"}]},
                  ["INS-1"], conflicts=[1])
    s = score_answer(exp, ans)
    assert s.passed and s.failures == [] and s.checks >= 7


def test_visual_type_mismatch_fails_and_lists_are_any_of():
    assert not score_answer({"visual_type": "table"}, _answer(vtype="bar")).passed
    assert score_answer({"visual_type": ["table", "bar"]}, _answer(vtype="bar")).passed
    assert score_answer({"visual_type": None}, _answer(vtype="heatmap")).passed
    msg = score_answer({"visual_type": "table"}, _answer(vtype="bar")).failures[0]
    assert "table" in msg and "bar" in msg


def test_text_contains_is_case_insensitive_and_dash_tolerant():
    ans = _answer("well\u20110001 is NOT PRODUCING")  # U+2011 non-breaking hyphen
    assert score_answer({"text_contains": ["WELL-0001", "not producing"]}, ans).passed
    s = score_answer({"text_contains": ["WELL-0002", ["foo", "bar"]]}, ans)
    assert not s.passed and len(s.failures) == 2


def test_text_not_contains():
    assert score_answer({"text_not_contains": ["WELL-0121"]}, _answer("nothing here")).passed
    s = score_answer({"text_not_contains": ["WELL-0121"]}, _answer("see well-0121"))
    assert not s.passed and "WELL-0121" in s.failures[0]


def test_numbers_within_tolerance_and_comma_formats():
    exp = {"numbers": {"total_bbl": 15.1, "spill_count": 6, "usd": 25794}}
    assert score_answer(exp, _answer("6 spills, 15.1 barrels, $25,794")).passed
    assert score_answer(exp, _answer("6 spills, 15.12 bbl, 25794 USD")).passed
    s = score_answer(exp, _answer("5 spills, 15.1 barrels, $25,794"))
    assert not s.passed and "spill_count" in s.failures[0]
    # ids must not be mistaken for values: WELL-0006 is not "6"
    assert not score_answer({"numbers": {"n": 6}}, _answer("WELL-0006 only")).passed


def test_visual_numbers_and_data_checks():
    ans = _answer(vtype="map", data={"pins": [{"asset_id": "WELL-0151", "bbl": 4.5, "area": "NE-08"}]})
    exp = {
        "visual_numbers": {"bbl": 4.5},
        "well_ids_in_data": ["WELL-0151"],
        "well_ids_not_in_data": ["WELL-0165"],
        "visual_data_contains": ["NE-08"],
    }
    assert score_answer(exp, ans).passed
    bad = score_answer({"well_ids_not_in_data": ["WELL-0151"], "well_ids_in_data": ["WELL-0099"]}, ans)
    assert len(bad.failures) == 2


def test_citations_and_conflicts():
    ans = _answer(citations=["INS-1", "MNT-2"])
    assert score_answer({"citations_must_include": ["INS-1", "MNT-2"], "min_citations": 2}, ans).passed
    s = score_answer({"citations_must_include": ["REG-9"], "min_citations": 3, "conflicts_min": 1}, ans)
    assert not s.passed and len(s.failures) == 3
    assert score_answer({"min_citations": 1}, _answer()).failures  # empty citations


def test_optional_keys_only_warn():
    exp = {"text_contains": ["a"], "text_contains_optional": ["zzz"], "numbers_optional": {"n": 99}}
    s = score_answer(exp, _answer("a"))
    assert s.passed and s.failures == [] and len(s.warnings) == 2


def test_accepts_agent_answer_model_and_never_raises_on_sparse_dicts():
    from backend.models.schemas import AgentAnswer, UIVisual

    model = AgentAnswer(text="WELL-0001", visual=UIVisual(type="table", data={"k": 1}))
    assert score_answer({"visual_type": "table", "text_contains": ["WELL-0001"]}, model).passed
    assert not score_answer({"text_contains": ["x"]}, {}).passed


def test_synthesized_answer_satisfies_every_scored_turn(expected):
    """Round trip: the 'perfect' answer built from each expected block must score a pass."""
    count = 0
    for entry in expected.values():
        for turn in entry_turns(entry):
            if turn.expected is None:
                continue
            s = score_answer(turn.expected, synthesize_answer(turn.expected, turn.question_text))
            assert s.passed, (turn.label, s.failures)
            assert s.warnings == [], (turn.label, s.warnings)
            count += 1
    assert count >= 18


def test_each_scored_turn_is_discriminating(expected):
    """An empty answer must fail every scored turn (no turn is vacuously satisfiable)."""
    for entry in expected.values():
        for turn in entry_turns(entry):
            if turn.expected is not None:
                assert not score_answer(turn.expected, _answer()).passed, turn.label


def test_normalize():
    assert normalize("  Foo\u00a0BAR\u2013baz ") == "foo bar-baz"


# ---------------------------------------------------------------------------
# Run planning
# ---------------------------------------------------------------------------

def test_select_all_keeps_file_order(expected):
    sel = select_questions(expected)
    assert [q for q, _ in sel] == QUESTION_IDS and not any(dep for _, dep in sel)


def test_select_only_pulls_transitive_dependencies(expected):
    sel = select_questions(expected, ["Q16"])
    assert sel == [("Q6", True), ("Q11", True), ("Q16", False)]
    assert [q for q, _ in select_questions(expected, ["Q15"])] == ["Q12", "Q13", "Q15"]
    assert select_questions(expected, ["Q4"]) == [("Q4", False)]
    assert select_questions(expected, ["Q16"], include_deps=False) == [("Q16", False)]


def test_select_unknown_id_raises(expected):
    with pytest.raises(KeyError):
        select_questions(expected, ["Q99"])


def test_tally_ignores_dependencies():
    from eval.scoring import QuestionResult, TurnResult

    def qr(qid, ok, dep):
        t = TurnResult(qid, qid, "primary", "U-1", "q", True, ok)
        return QuestionResult(qid, dep, [t])

    assert tally([qr("Q6", False, True), qr("Q11", True, False), qr("Q2", False, False)]) == (1, 2)


# ---------------------------------------------------------------------------
# DB handling: the real DB must be safe
# ---------------------------------------------------------------------------

def _tiny_db(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute(SCHEMA_SQL.read_text())
    con.execute("INSERT INTO teams VALUES ('T-1', 'Team', NULL)")
    con.execute("INSERT INTO well_type_rules (well_type, producing_threshold_c, max_wellhead_c) "
                "VALUES ('T200', 15, 85)")
    con.execute("INSERT INTO wells (asset_id, name, well_type) VALUES ('WELL-1', 'W1', 'T200')")
    con.execute("INSERT INTO conversations (user_id, conversation_id, turn_id, role, text) "
                "VALUES ('U-1', 'c', 1, 'user', 'hi')")
    con.execute("INSERT INTO shared_claims (user_id, scope_type, scope_id, text) "
                "VALUES ('U-1', 'well', 'WELL-1', 'x')")
    con.execute("INSERT INTO turn_summaries (user_id, summary) VALUES ('U-1', 's')")
    for note_id, src in (("CTX-D", "dataset"), ("CTX-RT-1", "runtime")):
        con.execute("INSERT INTO context_notes (note_id, asset_id, created_at, note_type, text, source) "
                    "VALUES (?, 'WELL-1', current_timestamp, 'operator_note', 't', ?)", [note_id, src])
    con.close()


def test_prepare_db_copies_and_leaves_the_source_untouched(tmp_path):
    src = tmp_path / "data.duckdb"
    _tiny_db(src)
    before = src.read_bytes()

    copy_path, tmpdir = run_eval.prepare_db(src)
    try:
        assert copy_path != src and tmpdir is not None and copy_path.exists()
        con = duckdb.connect(str(copy_path))
        con.execute("DELETE FROM conversations")
        con.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    assert src.read_bytes() == before
    assert run_eval.prepare_db(src, in_place=True) == (src, None)
    with pytest.raises(FileNotFoundError):
        run_eval.prepare_db(tmp_path / "missing.duckdb")


def test_reset_runtime_state_keeps_dataset_rows(tmp_path):
    path = tmp_path / "t.duckdb"
    _tiny_db(path)
    con = duckdb.connect(str(path))
    run_eval.reset_runtime_state(con)
    assert con.execute("SELECT count(*) FROM conversations").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM shared_claims").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM turn_summaries").fetchone()[0] == 0
    assert con.execute("SELECT note_id FROM context_notes").fetchall() == [("CTX-D",)]
    assert con.execute("SELECT count(*) FROM wells").fetchone()[0] == 1
    con.close()


def test_resolve_fleet_dir_prefers_explicit_root(tmp_path):
    (tmp_path / "fleet").mkdir()
    assert run_eval.resolve_fleet_dir(tmp_path) == tmp_path / "fleet"
    (tmp_path / "other" / "sample-data" / "fleet").mkdir(parents=True)
    assert run_eval.resolve_fleet_dir(tmp_path / "other") == tmp_path / "other" / "sample-data" / "fleet"


# ---------------------------------------------------------------------------
# Offline dry run through the real Agent (TestModel)
# ---------------------------------------------------------------------------

def test_dry_run_all_18_pass_offline(capsys, tmp_path):
    if not DB_PATH.exists():
        pytest.skip("ingested DB not found - run scripts/ingest.py first")
    out = tmp_path / "res.json"
    code = run_eval.main(["--dry-run", "--json", str(out)])
    stdout = capsys.readouterr().out
    assert code == 0, stdout
    assert "SUMMARY: 18/18 passed" in stdout
    data = json.loads(out.read_text())
    assert data["summary"] == {"passed": 18, "total": 18} and data["meta"]["mode"] == "dry-run"
    q12 = next(q for q in data["questions"] if q["id"] == "Q12")
    assert len(q12["turns"]) == 2
    # first turn of a chat calls the tool; the 2nd reuses the thread history (TestModel sees the
    # tool already ran), proving per-conversation message history is threaded through
    assert q12["turns"][0]["tools"][0]["name"] == "well_status_now"


def test_dry_run_only_runs_prerequisites_and_min_pass_gates_exit(capsys):
    if not DB_PATH.exists():
        pytest.skip("ingested DB not found - run scripts/ingest.py first")
    assert run_eval.main(["--dry-run", "--only", "Q11"]) == 0
    out = capsys.readouterr().out
    assert "Q6*" in out and "SUMMARY: 1/1" in out
    assert run_eval.main(["--dry-run", "--only", "Q11", "--no-deps", "--min-pass", "2"]) == 1
    assert run_eval.main(["--dry-run", "--only", "Q99"]) == 2


def test_list_flag_prints_all_questions(capsys):
    assert run_eval.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert all(f"Q{i} " in out for i in range(1, 19))
