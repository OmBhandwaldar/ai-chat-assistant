"""FastAPI app entrypoint: AG-UI endpoint, health check, user/conversation REST routes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
from ag_ui.core import CustomEvent, EventType
from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.ui.ag_ui import AGUIAdapter

from backend.agent import Agent, AgentAnswer, Deps, build_agent
from backend.critic import run_critic

logger = logging.getLogger("backend.app")

DB_PATH = Path(__file__).parent / "data.duckdb"
FLEET_DIR = (
    Path(__file__).resolve().parents[1]
    / "flytbase-ahc-swe-asset-intelligence-dataset"
    / "sample-data"
    / "fleet"
)
# Ingest writes backend/chroma_db. backend/chroma does not exist, so the old
# default made the first search_reports rebuild an empty collection (~41s).
CHROMA_PATH = Path(__file__).parent / "chroma_db"


def _warm_search_index() -> None:
    """Open Chroma and load the embedder. Does not re-embed report chunks."""
    try:
        from backend.tools.search import _get_chroma_collection, _get_embedder

        if CHROMA_PATH.exists():
            _get_chroma_collection(CHROMA_PATH)
        _get_embedder()
    except Exception:
        logger.warning("search warm-up failed", exc_info=True)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    task: asyncio.Task | None = None
    # Pytest builds a TestClient per test; loading the embedder there is not cheap.
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        task = asyncio.create_task(asyncio.to_thread(_warm_search_index))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


app = FastAPI(title="Asset Intelligence Copilot API", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_agent: Agent | None = None


def get_agent() -> Agent:
    """Build (once) the shared copilot agent, lazily picking up the LLM provider."""
    global _agent
    if _agent is None:
        from backend.providers import make_llm

        _agent = build_agent(model=make_llm("primary"))
    return _agent


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": "0.1"}


def _extract_tool_results(result) -> list[dict]:
    """Pull every tool-return payload from a completed run, for the critic pass."""
    results: list[dict] = []
    for msg in result.new_messages():
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolReturnPart):
                content = part.content
                results.append(content if isinstance(content, dict) else {"content": content})
    return results


@app.post("/ag-ui")
async def ag_ui_endpoint(request: Request, x_user_id: str = Header(alias="X-User-Id")):
    """Stream an agent run over the AG-UI protocol. Threads user_id from a header."""
    body = await request.body()
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {}

    # Build the agent before touching the DB: if the LLM provider is misconfigured (e.g. missing
    # API key) we want a clean 503 — not a leaked write connection, and not a bare 500 that
    # bypasses CORS and shows up in the browser as an opaque "Failed to fetch".
    try:
        agent = get_agent()
    except Exception as exc:
        logger.exception("could not build the agent")
        return JSONResponse(status_code=503, content={"detail": f"Agent unavailable: {exc}"})

    conversation_id = payload.get("threadId") or "default"
    user_messages = [m for m in payload.get("messages", []) if m.get("role") == "user"]
    user_text = user_messages[-1].get("content", "") if user_messages else ""

    conn = duckdb.connect(str(DB_PATH), read_only=False)
    fleet_dir = FLEET_DIR if FLEET_DIR.exists() else None
    chroma_path = CHROMA_PATH if CHROMA_PATH.exists() else None
    deps = Deps(conn=conn, user_id=x_user_id, fleet_dir=fleet_dir, chroma_path=chroma_path)

    next_turn = conn.execute(
        "SELECT coalesce(max(turn_id), 0) + 1 FROM conversations WHERE conversation_id = ?",
        [conversation_id],
    ).fetchone()[0]

    if user_text:
        conn.execute(
            """
            INSERT INTO conversations (user_id, conversation_id, turn_id, role, text)
            VALUES (?, ?, ?, 'user', ?)
            """,
            [x_user_id, conversation_id, next_turn, user_text],
        )

    async def on_complete(result):
        answer: AgentAnswer = result.output
        try:
            conn.execute(
                """
                INSERT INTO conversations (user_id, conversation_id, turn_id, role, text)
                VALUES (?, ?, ?, 'assistant', ?)
                """,
                [x_user_id, conversation_id, next_turn, answer.text],
            )
            # AgentAnswer is a structured (tool-based) output, not plain assistant text, so the
            # frontend can't recover visual/citations/conflicts from TEXT_MESSAGE_* events alone.
            # Yield it before the critic: a slow critic (Gemma / Ollama) must not hold the UI.
            # PLAN 4.2's retry-once is not done on this path. Retrying would keep the stream
            # open for a second model call after every flagged answer; the common path stays
            # answer-then-critic. critic_result is a second CUSTOM event the current UI ignores.
            yield CustomEvent(
                type=EventType.CUSTOM, name="agent_answer", value=answer.model_dump()
            )
            try:
                critique = run_critic(user_text, answer, _extract_tool_results(result), conn)
                payload = {
                    "ok": critique.ok,
                    "issues": critique.issues,
                    "must_retry": critique.must_retry,
                }
                if not critique.ok:
                    logger.warning(
                        "critic flagged issues for %s: %s", conversation_id, critique.issues
                    )
            except Exception as exc:
                logger.exception("critic failed for %s", conversation_id)
                payload = {
                    "ok": False,
                    "issues": [f"critic failed: {type(exc).__name__}: {exc}"],
                    "must_retry": False,
                }
            yield CustomEvent(type=EventType.CUSTOM, name="critic_result", value=payload)
        finally:
            conn.close()

    async def on_cancel(_cancelled) -> None:
        conn.close()

    return await AGUIAdapter.dispatch_request(
        request,
        agent=agent,
        deps=deps,
        on_complete=on_complete,
        on_cancel=on_cancel,
    )


@app.get("/users")
def list_users() -> list[dict]:
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    rows = conn.execute("SELECT user_id, name, role, team_id FROM users ORDER BY name").fetchall()
    conn.close()
    return [
        {"user_id": r[0], "name": r[1], "role": r[2], "team_id": r[3]}
        for r in rows
    ]


@app.get("/conversations")
def list_conversations(user_id: str) -> list[dict]:
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    rows = conn.execute(
        """
        SELECT
            c.conversation_id,
            c.user_id,
            (
                SELECT text FROM conversations c2
                WHERE c2.conversation_id = c.conversation_id AND c2.role = 'user'
                ORDER BY c2.turn_id LIMIT 1
            ) AS title,
            max(c.ts) AS updated_at
        FROM conversations c
        WHERE c.user_id = ?
        GROUP BY c.conversation_id, c.user_id
        ORDER BY updated_at DESC
        """,
        [user_id],
    ).fetchall()
    conn.close()
    return [
        {
            "conversation_id": r[0],
            "user_id": r[1],
            "title": (r[2] or "New conversation")[:80],
            "updated_at": r[3].isoformat(),
        }
        for r in rows
    ]


@app.get("/conversations/{conversation_id}/messages")
def get_conversation_messages(conversation_id: str, user_id: str) -> list[dict]:
    """Return one conversation's turns, oldest first.

    Always filtered by ``user_id`` as well as ``conversation_id`` — chat history is private
    per user, so another user's conversation id yields an empty list, not their messages.
    """
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    rows = conn.execute(
        """
        SELECT id, role, text, ts
        FROM conversations
        WHERE conversation_id = ? AND user_id = ?
        ORDER BY turn_id, CASE role WHEN 'user' THEN 0 ELSE 1 END, id
        """,
        [conversation_id, user_id],
    ).fetchall()
    conn.close()
    return [
        {"id": f"{conversation_id}-{r[0]}", "role": r[1], "text": r[2], "ts": r[3].isoformat()}
        for r in rows
    ]
