"""Tests for backend/app.py AG-UI endpoint (Phase 4.3).

Uses Pydantic AI's TestModel so no real LLM provider is needed.
Acceptance: a client can POST to /ag-ui and stream back a completed run.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel

import backend.app as app_module
from backend.agent import build_agent

DB_PATH = Path(__file__).parent.parent.parent / "backend" / "data.duckdb"


@pytest.fixture
def client(monkeypatch):
    if not DB_PATH.exists():
        pytest.skip("ingested DB not found")
    test_agent = build_agent(model=TestModel(call_tools=[]))
    monkeypatch.setattr(app_module, "get_agent", lambda: test_agent)
    return TestClient(app_module.app)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_ag_ui_streams_response(client):
    body = {
        "threadId": "test-thread",
        "runId": "test-run",
        "messages": [{"id": "m1", "role": "user", "content": "Is WELL-0001 producing?"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    resp = client.post(
        "/ag-ui",
        json=body,
        headers={"X-User-Id": "jake", "Accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert "RUN_STARTED" in resp.text
    assert "RUN_FINISHED" in resp.text


def test_ag_ui_persists_conversation(client):
    import duckdb

    body = {
        "threadId": "test-thread-persist",
        "runId": "test-run-2",
        "messages": [{"id": "m1", "role": "user", "content": "What is the status of WELL-0001?"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    client.post(
        "/ag-ui",
        json=body,
        headers={"X-User-Id": "jake", "Accept": "text/event-stream"},
    )

    con = duckdb.connect(str(DB_PATH), read_only=True)
    rows = con.execute(
        "SELECT role, text FROM conversations WHERE conversation_id = ? ORDER BY role",
        ["test-thread-persist"],
    ).fetchall()
    con.close()
    roles = {r[0] for r in rows}
    assert "user" in roles
    assert "assistant" in roles


def test_ag_ui_emits_agent_answer_even_when_critic_fails(client, monkeypatch):
    """The answer event is yielded before the critic, so a critic crash cannot swallow it."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("critic exploded")

    monkeypatch.setattr(app_module, "run_critic", boom)
    resp = client.post(
        "/ag-ui",
        json={
            "threadId": "test-thread-critic-fail",
            "runId": "test-run-critic-fail",
            "messages": [{"id": "m1", "role": "user", "content": "Is WELL-0001 producing?"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
        headers={"X-User-Id": "jake", "Accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    answer_at = resp.text.find('"name":"agent_answer"')
    critic_at = resp.text.find('"name":"critic_result"')
    assert answer_at != -1
    assert critic_at != -1
    assert answer_at < critic_at
    assert "critic exploded" in resp.text


def test_ag_ui_emits_custom_agent_answer_event(client):
    body = {
        "threadId": "test-thread-custom",
        "runId": "test-run-3",
        "messages": [{"id": "m1", "role": "user", "content": "Is WELL-0001 producing?"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    resp = client.post(
        "/ag-ui",
        json=body,
        headers={"X-User-Id": "jake", "Accept": "text/event-stream"},
    )
    assert '"type":"CUSTOM"' in resp.text
    assert '"name":"agent_answer"' in resp.text


def test_ag_ui_returns_503_with_cors_when_agent_cannot_be_built(client, monkeypatch):
    """A misconfigured LLM provider must yield a readable 503 the browser can see.

    A bare 500 skips CORSMiddleware, which the browser reports as an opaque network error.
    """

    def boom():
        raise RuntimeError("missing GEMINI_API_KEY")

    monkeypatch.setattr(app_module, "get_agent", boom)
    resp = client.post(
        "/ag-ui",
        json={
            "threadId": "test-thread-503",
            "runId": "r",
            "messages": [{"id": "m1", "role": "user", "content": "hi"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
        headers={"X-User-Id": "jake", "Origin": "http://localhost:3000"},
    )
    assert resp.status_code == 503
    assert "missing GEMINI_API_KEY" in resp.json()["detail"]
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"

    # Nothing was persisted for a request that never reached the agent.
    history = client.get("/conversations/test-thread-503/messages", params={"user_id": "jake"})
    assert history.json() == []


def test_list_users(client):
    resp = client.get("/users")
    assert resp.status_code == 200
    users = resp.json()
    assert len(users) > 0
    assert {"user_id", "name", "role", "team_id"} <= users[0].keys()


def test_list_conversations_for_user(client):
    resp = client.get("/conversations", params={"user_id": "jake"})
    assert resp.status_code == 200
    conversations = resp.json()
    assert len(conversations) > 0
    convo_ids = {c["conversation_id"] for c in conversations}
    assert "test-thread-persist" in convo_ids
    assert {"conversation_id", "user_id", "title", "updated_at"} <= conversations[0].keys()


def test_get_conversation_messages_returns_turns_in_order(client):
    thread_id = "test-thread-history"
    client.post(
        "/ag-ui",
        json={
            "threadId": thread_id,
            "runId": "test-run-history",
            "messages": [{"id": "m1", "role": "user", "content": "History please?"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
        headers={"X-User-Id": "jake", "Accept": "text/event-stream"},
    )

    resp = client.get(f"/conversations/{thread_id}/messages", params={"user_id": "jake"})
    assert resp.status_code == 200
    messages = resp.json()
    assert len(messages) >= 2
    assert {"id", "role", "text", "ts"} <= messages[0].keys()
    # Each turn is user-then-assistant, never reordered.
    assert [m["role"] for m in messages[:2]] == ["user", "assistant"]
    assert messages[0]["text"] == "History please?"
    # Ids must be unique so React keys / store updates can't collide.
    assert len({m["id"] for m in messages}) == len(messages)


def test_get_conversation_messages_is_private_per_user(client):
    """Another user's conversation id must not leak their chat history (Q16)."""
    resp = client.get(
        "/conversations/test-thread-persist/messages", params={"user_id": "someone-else"}
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_conversations_empty_for_unknown_user(client):
    resp = client.get("/conversations", params={"user_id": "no-such-user"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_ag_ui_answer_is_not_held_for_a_slow_critic(client, monkeypatch):
    """A slow critic still produces agent_answer, and that event is emitted first.

    TestClient reads the finished body, so this checks yield order rather than
    socket timing. The handler yields agent_answer before calling run_critic.
    """
    import time

    from backend.critic import CriticResult

    def slow(*_args, **_kwargs):
        time.sleep(0.05)
        return CriticResult(ok=False, issues=["late check"], must_retry=False)

    monkeypatch.setattr(app_module, "run_critic", slow)
    resp = client.post(
        "/ag-ui",
        json={
            "threadId": "test-thread-critic-slow",
            "runId": "test-run-critic-slow",
            "messages": [{"id": "m1", "role": "user", "content": "Is WELL-0001 producing?"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
        headers={"X-User-Id": "jake", "Accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    answer_at = resp.text.find('"name":"agent_answer"')
    critic_at = resp.text.find('"name":"critic_result"')
    assert answer_at != -1 and critic_at != -1
    assert answer_at < critic_at
    assert "late check" in resp.text


def test_today_comes_from_the_latest_inspection_and_fills_deps():
    import asyncio
    from datetime import datetime, timedelta

    import duckdb
    from pydantic_ai.models.test import TestModel

    from backend.agent import Deps, build_agent, dataset_as_of

    if not DB_PATH.exists():
        pytest.skip("ingested DB not found")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    derived = dataset_as_of(con)
    latest = con.execute("SELECT max(inspected_at) FROM inspections").fetchone()[0]
    assert derived == datetime.combine(latest.date() + timedelta(days=1), datetime.min.time())

    agent = build_agent(model=TestModel(call_tools=[]))
    deps = Deps(conn=con, user_id="jake")
    asyncio.run(agent.run("Is WELL-0001 producing?", deps=deps))
    assert deps.as_of == derived

    pinned = Deps(conn=con, user_id="jake", as_of=datetime(2020, 1, 1))
    asyncio.run(agent.run("Is WELL-0001 producing?", deps=pinned))
    assert pinned.as_of == datetime(2020, 1, 1)
    con.close()


def test_merge_citations_keeps_existing_and_adds_text_ids_from_tools():
    from backend.agent import merge_citations
    from backend.models.schemas import AgentAnswer, Evidence, UIVisual

    answer = AgentAnswer(
        text="Shut in per CTX-0001-003 after INS-0001-0203. Also INS-FAKE-9999.",
        visual=UIVisual(type="status_card", data={}),
        citations=[Evidence(source_type="inspection", source_id="INS-0001-0203")],
        conflicts=[],
    )
    tools = [{
        "evidence": [
            {"source_type": "inspection", "source_id": "INS-0001-0203"},
            {"source_type": "context", "source_id": "CTX-0001-003"},
        ]
    }]
    merged = merge_citations(answer, tools)
    assert [c.source_id for c in merged.citations] == ["INS-0001-0203", "CTX-0001-003"]
    assert merge_citations(
        AgentAnswer(text="no evidence in the data", visual=UIVisual(type="none", data={}),
                    citations=[], conflicts=[]),
        [],
    ).citations == []


def test_merge_citations_adds_tool_evidence_ids_absent_from_text():
    """Q2 names wells, not inspection ids; those ids still merge in from the tool."""
    from backend.agent import ensure_wells_not_producing_table, merge_citations
    from backend.models.schemas import AgentAnswer, Evidence, UIVisual

    answer = AgentAnswer(
        text=(
            "WELL-0164, WELL-0170, WELL-0172 and WELL-0175 are shut in. "
            "WELL-0175: No recorded reason. Also INS-FAKE-9999."
        ),
        visual=UIVisual(type="none", data={}),
        citations=[Evidence(source_type="maintenance", source_id="MNT-0170-031")],
        conflicts=[],
    )
    wells = [
        {"asset_id": "WELL-0164", "reason": "No recorded reason."},
        {"asset_id": "WELL-0170", "reason": "Repair MNT-0170-031, still open"},
        {"asset_id": "WELL-0172", "reason": "Repair MNT-0172-028, still open"},
        {"asset_id": "WELL-0175", "reason": "No recorded reason."},
    ]
    tools = [{
        "data": {"wells": wells},
        "evidence": [
            {"source_type": "inspection", "source_id": "INS-0164-0205"},
            {"source_type": "inspection", "source_id": "INS-0170-0206"},
            {"source_type": "inspection", "source_id": "INS-0172-0206"},
            {"source_type": "inspection", "source_id": "INS-0175-0204"},
            {"source_type": "maintenance", "source_id": "MNT-0170-031"},
            {"source_type": "maintenance", "source_id": "MNT-0172-028"},
        ],
    }]
    merged = merge_citations(answer, tools)
    ids = [c.source_id for c in merged.citations]
    assert ids[0] == "MNT-0170-031"
    for cid in (
        "INS-0164-0205", "INS-0170-0206", "INS-0172-0206", "INS-0175-0204", "MNT-0172-028",
    ):
        assert cid in ids
    assert "INS-FAKE-9999" not in ids
    assert merged.text == answer.text

    table = ensure_wells_not_producing_table(
        answer,
        [("wells_not_producing", tools[0])],
        "Which wells in Northeast Block 8 are not producing oil right now? Show them in a table.",
    )
    assert table.visual.type == "table"
    assert [row["asset_id"] for row in table.visual.data["rows"]] == [
        "WELL-0164", "WELL-0170", "WELL-0172", "WELL-0175",
    ]
    left_alone = ensure_wells_not_producing_table(
        answer,
        [("wells_not_producing", tools[0])],
        "Show a line chart of temperature and mark when it was not producing.",
    )
    assert left_alone.visual.type == "none"


def test_post_process_visual_table_from_wells_not_producing_when_rows_null():
    from backend.agent import post_process_visual
    from backend.models.schemas import AgentAnswer, UIVisual

    wells = [{"asset_id": "WELL-0164"}, {"asset_id": "WELL-0170"}]
    tool = {"data": {"wells": wells}, "evidence": []}
    answer = AgentAnswer(
        text="two wells",
        visual=UIVisual(type="table", data={"rows": None, "columns": None}),
        citations=[],
        conflicts=[],
    )
    out = post_process_visual(
        answer,
        [("wells_not_producing", tool)],
        "Show them in a table.",
    )
    assert out.visual.type == "table"
    assert [r["asset_id"] for r in out.visual.data["rows"]] == ["WELL-0164", "WELL-0170"]


def test_post_process_visual_fills_empty_status_card_from_well_status():
    from backend.agent import post_process_visual
    from backend.models.schemas import AgentAnswer, UIVisual

    tool = {
        "data": {
            "asset_id": "WELL-0001",
            "state": "not_producing",
            "reasons": ["Pump jack not moving."],
            "conflicts": [],
        },
        "evidence": [],
    }
    answer = AgentAnswer(
        text="not producing",
        visual=UIVisual(type="status_card", data={}),
        citations=[],
        conflicts=[],
    )
    out = post_process_visual(
        answer,
        [("well_status_now", tool)],
        "Is WELL-0001 producing oil right now? Show me its status card.",
    )
    assert out.visual.type == "status_card"
    assert out.visual.data["asset_id"] == "WELL-0001"
    assert out.visual.data["state"] == "not_producing"
    assert out.visual.data["reasons"] == ["Pump jack not moving."]

    filled = AgentAnswer(
        text="not producing",
        visual=UIVisual(type="status_card", data={"asset_id": "WELL-0001", "state": "not_producing", "reasons": ["kept"]}),
        citations=[],
        conflicts=[],
    )
    left = post_process_visual(filled, [("well_status_now", tool)], "status card")
    assert left.visual.data["reasons"] == ["kept"]


def test_post_process_visual_downtime_days_forces_none_without_chart():
    from backend.agent import post_process_visual
    from backend.models.schemas import AgentAnswer, UIVisual

    answer = AgentAnswer(
        text="21 days down",
        visual=UIVisual(type="bar", data={"days": 21}),
        citations=[],
        conflicts=[],
    )
    tool = {"data": {"asset_id": "WELL-0001", "days": 21}, "evidence": []}
    out = post_process_visual(
        answer,
        [("downtime_days", tool)],
        "How many days was WELL-0001 down in 2025?",
    )
    assert out.visual.type == "none"
    assert out.visual.data == {}


def test_post_process_visual_copies_top_downtime_wells_into_bar():
    from backend.agent import post_process_visual
    from backend.models.schemas import AgentAnswer, UIVisual

    wells = [
        {"asset_id": "WELL-0121", "downtime_days": 49},
        {"asset_id": "WELL-0138", "downtime_days": 42},
    ]
    tool = {"data": {"start": "2026-06-16", "end": "2026-10-16", "wells": wells}, "evidence": []}
    answer = AgentAnswer(
        text="summary",
        visual=UIVisual(type="bar", data={"wells_list": None}),
        citations=[],
        conflicts=[],
    )
    out = post_process_visual(
        answer,
        [("top_downtime", tool)],
        "Show a bar chart of the top 3 wells.",
    )
    assert out.visual.type == "bar"
    assert [w["asset_id"] for w in out.visual.data["wells"]] == ["WELL-0121", "WELL-0138"]


def test_post_process_visual_copies_spill_map_pins():
    from backend.agent import post_process_visual
    from backend.models.schemas import AgentAnswer, UIVisual

    pins = [
        {"asset_id": "WELL-0151", "lat": 1.0, "lon": 2.0, "bbl": 4.5},
        {"asset_id": "WELL-0165", "lat": 3.0, "lon": 4.0, "bbl": 1.2},
    ]
    tool = {
        "data": {
            "start": "2026-07-18",
            "end": "2026-10-16",
            "pins": pins,
            "reportable_threshold_bbl": 5,
        },
        "evidence": [],
    }
    answer = AgentAnswer(
        text="four spills",
        visual=UIVisual(type="map", data={"pins_list": None, "pins_data": None}),
        citations=[],
        conflicts=[],
    )
    out = post_process_visual(
        answer,
        [("spill_map", tool)],
        "Show on a map every well with a spill.",
    )
    assert out.visual.type == "map"
    assert [p["asset_id"] for p in out.visual.data["pins"]] == ["WELL-0151", "WELL-0165"]


def test_post_process_well_ids_rewrites_informal_mentions():
    from backend.agent import post_process_well_ids
    from backend.models.schemas import AgentAnswer, UIVisual

    answer = AgentAnswer(
        text="Well 121 had 49 days",
        visual=UIVisual(type="none", data={}),
        citations=[],
        conflicts=[],
    )
    tools = [{"data": {"wells": [{"asset_id": "WELL-0121", "downtime_days": 49}]}}]
    out = post_process_well_ids(answer, tools)
    assert "WELL-0121" in out.text

    already = AgentAnswer(
        text="WELL-0001 is not producing",
        visual=UIVisual(type="none", data={}),
        citations=[],
        conflicts=[],
    )
    kept = post_process_well_ids(already, [{"data": {"asset_id": "WELL-0001"}}])
    assert "WELL-0001" in kept.text
    assert kept.text == already.text


def test_resolve_inspection_notes_is_a_tool():
    import duckdb

    from backend.agent import ALL_TOOLS, _resolve_inspection_notes

    names = {t.name for t in ALL_TOOLS}
    assert "resolve_inspection_notes" in names
    if not DB_PATH.exists():
        pytest.skip("ingested DB not found")
    con = duckdb.connect(str(DB_PATH), read_only=True)

    class _Ctx:
        class deps:
            conn = con

    payload = _resolve_inspection_notes(_Ctx(), "INS-0101-0197")
    assert payload["data"]["detection_stands"] is True
    assert payload["data"]["conflicts"]
    ids = {e["source_id"] for e in payload["evidence"]}
    assert "CTX-0101-002" in ids and "CTX-0101-003" in ids
    con.close()


def test_post_process_fills_empty_line_heatmap_and_rca_and_filters_reportable_pins():
    from backend.agent import post_process_facts, post_process_visual
    from backend.models.schemas import AgentAnswer, UIVisual

    empty = AgentAnswer(text="chart", visual=UIVisual(type="line", data={}), citations=[], conflicts=[])
    line = post_process_visual(
        empty,
        [("temp_timeseries", {"data": {"asset_id": "WELL-0001", "points": [
            {"inspected_at": "2026-10-15T00:00:00", "wellhead_temp_c": 25.6},
        ]}})],
        "Show a line chart of wellhead temperature.",
    )
    assert line.visual.type == "line"
    assert "2026-10-15" in str(line.visual.data)

    heat = post_process_visual(
        AgentAnswer(text="heat", visual=UIVisual(type="heatmap", data={}), citations=[], conflicts=[]),
        [("spill_heatmap", {"data": {"areas": ["Southwest Block 24"], "months": [1], "matrix": {}}} )],
        "Show a heatmap of 2026 spills.",
    )
    assert "Southwest Block 24" in str(heat.visual.data)

    rca = post_process_visual(
        AgentAnswer(text="three wells", visual=UIVisual(type="table", data={}), citations=[], conflicts=[]),
        [("rca_due", {"data": {"wells": [
            {"asset_id": "WELL-0108"}, {"asset_id": "WELL-0121"}, {"asset_id": "WELL-0138"},
        ]}})],
        "Which wells in Northeast Block 7 need a root-cause analysis on their pump jack?",
    )
    assert [r["asset_id"] for r in rca.visual.data["rows"]] == ["WELL-0108", "WELL-0121", "WELL-0138"]

    pins = [
        {"asset_id": "WELL-0151", "reportable": True, "bbl": 5.4},
        {"asset_id": "WELL-0165", "reportable": False, "bbl": 1.2},
    ]
    mapped = post_process_visual(
        AgentAnswer(text="spills", visual=UIVisual(type="map", data={"pins": pins}), citations=[], conflicts=[]),
        [("spill_map", {"data": {"pins": pins}})],
        "Which spills needed a report to the regulator?",
    )
    assert [p["asset_id"] for p in mapped.visual.data["pins"]] == ["WELL-0151"]

    contained = post_process_visual(
        AgentAnswer(text="two spills", visual=UIVisual(type="map", data={"pins": pins}), citations=[], conflicts=[]),
        [("containment_breaches", {"data": {"rows": [
            {"asset_id": "WELL-0082", "hours": 7.1}, {"asset_id": "WELL-0100", "hours": 8.1},
        ]}})],
        "Which spills were not contained within the SOP time limit?",
    )
    assert contained.visual.type == "table"
    assert "WELL-0165" not in str(contained.visual.data)

    narrated = post_process_facts(
        AgentAnswer(text="I recorded the correction.", visual=UIVisual(type="none", data={}), citations=[], conflicts=[]),
        [("record_feedback", {"data": {
            "recorded": True, "field": "wellhead_temp_c", "asset_id": "WELL-0164",
            "scope_id": "INS-0164-0205", "original_value": 24.0, "new_value": 41.0,
            "author": "Michael Turner", "confirmation_needed": True,
            "resulting_state": {
                "state": "producing",
                "reasons": ['Inspector note: "Pump jack not moving."'],
                "conflicts": [{"type": "override_vs_inspector_note", "left": "producing", "right": "not moving"}],
            },
        }})],
        "The 15 Oct reading for WELL-0164 is wrong.",
    )
    assert "is now producing" in narrated.text
    assert "41.0" in narrated.text and "Michael Turner" in narrated.text
    assert "not moving" in narrated.text and "confirm" in narrated.text.casefold()
    assert narrated.conflicts

    refused = post_process_facts(
        AgentAnswer(text="WELL-0121 cost 25794.", visual=UIVisual(type="none", data={}), citations=[], conflicts=[]),
        [("recall_user_context", {"data": {"has_history": False, "well_ids": []}})],
        "For the three wells we discussed yesterday, what have repairs cost this year?",
    )
    assert "no earlier conversation" in refused.text
    assert "WELL-0121" not in refused.text and "25794" not in refused.text
