# Tech Stack — Asset Intelligence Copilot

Target: FlytBase AHC SWE Hackathon, 17 Oct 2026. Deliverable runs locally on a 24 GB MacBook Air, judged on a laptop.

## 1. Backend

| Piece | Choice | Role |
|---|---|---|
| Language | **Python 3.11+** | SDK coverage (google-genai, pydantic-ai, duckdb, chromadb), fast enough for a hackathon. |
| Web framework | **FastAPI** | Async, typed, OpenAPI for free, Server-Sent Events (SSE) support needed for AG-UI streaming. |
| Agent framework | **Pydantic AI** (with `[ag-ui]` extra) | Typed tools, provider-agnostic (Gemini + Ollama), built-in AG-UI adapter, validation on every tool call and output. |
| Agent↔UI protocol | **AG-UI** (CopilotKit's open protocol) | First-class Pydantic AI integration. SSE stream of agent events: tool_calls, tool_results, state deltas, UI components. |
| Component schema format | **A2UI-shaped payloads carried inside AG-UI events** | Declarative component descriptions (`{type, props, data}`) rendered by the frontend. Separates "what to show" from "how to render." |
| Data access | **DuckDB** (single file `data.duckdb`) | Columnar, 10–100× faster than SQLite on aggregations (heatmaps, top-k, group-bys). Reads JSONL natively (`read_json_auto`). Supports FTS extension. |
| Full-text index | **DuckDB `fts` extension** on `maintenance_reports` chunks | BM25 over report text. One DB file for everything. |
| Vector store | **ChromaDB** (local, persistent) | Dense retrieval over report chunks and SOP sections. Simple API, zero ops. |
| Embeddings | **`BAAI/bge-small-en-v1.5`** (384 dim) | Runs on CPU in ~30 ms, 60 MB model, strong quality for English technical text. |
| Reranker (optional, high-stakes queries) | **`BAAI/bge-reranker-v2-m3`** | Cross-encoder, runs on CPU, improves retrieval quality for Q9/Q10/Q13-style questions. |

## 2. Models

| Role | Model | Why |
|---|---|---|
| Primary agent (orchestration + final answers) | **Gemini 2.5 Flash** via Google AI Studio | Free tier, strong tool-calling, strict JSON mode, lower hallucination than Gemma on structured tasks. |
| Critic / self-check | **Gemma 4 26b-a4b-it** via Google AI Studio | Free, cheap, fast. Verifies citations and numbers before the answer ships. |
| Local fallback (if AI Studio rate-limits) | **`gemma4:12b` via Ollama** | Already installed, OpenAI-compatible endpoint at `localhost:11434/v1/`. |

Keys live in `ai-chat-assistant/.env` (gitignored).

## 3. Frontend

| Piece | Choice | Role |
|---|---|---|
| Framework | **Next.js 15 (App Router) + React 19 + TypeScript** | Fast to build, SSR off, excellent AG-UI client support. |
| Styling | **Tailwind CSS + shadcn/ui** | Shadcn for cards, dialogs, tabs; Tailwind for everything else. |
| Dashboard components | **Tremor** | Polished `<Card>`, `<Metric>`, `<BarChart>`, `<LineChart>`, `<DonutChart>`, `<AreaChart>`, `<Table>`. Judge-ready visuals in minutes. |
| Map | **react-leaflet + OpenStreetMap tiles** | Free, no token, SSR-friendly, dead simple for pin-on-map (Q7). |
| Heatmap | **@nivo/heatmap** | Tremor doesn't have a heatmap; Nivo's is clean. Fallback: Tremor table with coloured cells. |
| Agent runtime (frontend) | **CopilotKit React runtime (AG-UI client)** | Consumes AG-UI events, dispatches to a component registry (`StatusCard`, `WellTable`, `LineChart`, `BarChart`, `Map`, `Heatmap`, `TextAnswer`). |
| State | **Zustand** (small, fast) | Private user picker, open conversation, per-user chat history. |

## 4. Memory, overrides, resolver

| Store | Where | Purpose |
|---|---|---|
| `conversations(user_id, turn_id, role, text, ts)` | DuckDB | Raw private chat per user. Read with `WHERE user_id = ?`. |
| `turn_summaries(user_id, summary, updated_at)` | DuckDB | Rolling 1–2 KB summary per user. Loaded on every turn, keeps the context tight. |
| `shared_well_notes(well_id, author_user_id, text, scope, valid_from, valid_to, source)` | DuckDB | Runtime "FYI for everyone" notes (Q16). Treated identically to dataset notes. |
| `overrides(scope_type, scope_id, field, original_value, new_value, author_user_id, reason, created_at)` | DuckDB | All feedback and corrections. Scope ∈ {`inspection`, `well`, `well_type`, `global`}. Q12, Q13, Q14. |
| `shared_claims(user_id, scope_type, scope_id, text, status, created_at)` | DuckDB | User statements that disagree with data (Q17, Q18). Stay as `unconfirmed` until formally corrected. |

**State resolver** (pure Python function, SQL-backed): for any (well, moment), returns `{state, reasons[], conflicts[]}`. Priority: inspection → correction note → feedback override → runtime shared note → recorded_status (comparison only). The LLM never decides state; it narrates what the resolver returns.

## 5. Retrieval paths

Three paths, picked by tool, not by model:

1. **SQL over DuckDB views** for all structured questions (Q1–Q8, Q11, parts of Q9/Q10/Q13/Q14). Views apply overrides automatically: `v_effective_inspections`, `v_spills` (corrections removed), `v_well_state_now`.
2. **Hybrid retrieval over free-text repair reports** (Q9, Q10, Q13). BM25 (DuckDB FTS) + dense (ChromaDB) → RRF fusion → optional cross-encoder rerank. Always scoped by well_id or area + date range.
3. **Protocol-ID retrieval over SOPs and manuals**. Indexed by `SR-01`, `WS-05`, `9.3`, etc. The LLM asks for the ID, gets the section text verbatim.

## 6. Tool catalogue (Pydantic AI tools exposed to the agent)

Typed arguments and typed returns. Each returns `(data, evidence_ids[])`.

- `well_status_now(well_id)` → state card
- `wells_not_producing(area_id|team_id|all, as_of)` → table
- `temp_timeseries(well_id, weeks)` → line chart
- `spills(well_id|area_id, start, end, apply_corrections=True)` → list + totals
- `downtime_days(well_id, start, end)` → number + evidence
- `top_downtime(area_id, start, end, k)` → bar chart
- `spill_heatmap(year, scope=areas_by_month)` → heatmap
- `sop_breaches(well_id, lookback_days)` → table + open items
- `repair_costs(well_id|list, start, end)` → table
- `rca_due(area_id, equipment_kind, lookback_days)` → list
- `search_reports(query, well_id?, area_id?, start?, end?, k=5)` → chunks + citations
- `get_sop(protocol_id)` / `get_manual_section(well_type, section)` → text
- `record_feedback(scope, field, new_value, reason, user_id)` → ack
- `record_shared_note(well_id, text, valid_from, valid_to, user_id)` → ack
- `record_claim(user_id, scope, text)` → ack + conflict check
- `recall_user_context(user_id, hint)` → recent entities for "the three wells we discussed yesterday"

## 7. Output contract

Every agent reply is a single JSON object:

```json
{
  "text": "Short natural-language answer.",
  "visual": { "type": "status_card|table|line|bar|map|heatmap|none", "data": {...} },
  "citations": [{"source_id": "INS-0001-0203", "source_type": "inspection"}, ...],
  "conflicts": [{"type": "claim_vs_data", "left": "...", "right": "..."}]
}
```

Validated by Pydantic AI's output schema. Streamed to the frontend as AG-UI events; the component registry renders `visual.data` with Tremor / react-leaflet / Nivo.

## 8. QA and evaluation

- **Critic pass**: after the primary agent emits its JSON, a Gemma call verifies (a) every numeric claim appears in a tool result, (b) every citation ID resolves, (c) no override was ignored. One retry on failure; otherwise the UI shows "unable to verify" with partial findings.
- **Eval harness**: `eval/run_eval.py` runs Q1–Q18 and diffs against expected answers in `eval/expected.json`. Green bar before any change ships.
- **Test-dataset rehearsal**: `python scripts/ingest.py $DATASET_ROOT` is the single switch. Rehearsed on a shuffled copy before the final 2 hours.

## 9. Dev tooling

- **uv** for Python package management (fast, lockfile).
- **ruff + mypy** for lint/type (lightweight, fast).
- **pytest** for tool-level tests of the resolver, overrides, SOP breach engine.
- **Docker Compose** optional for a single-command demo (`docker compose up` → UI on `:3000`, API on `:8000`).

## 10. Repository layout

```
ai-chat-assistant/
  .env                      # keys, gitignored
  TECH_STACK.md
  DECISIONS.md
  REQUIREMENTS_COVERAGE.md
  PLAN.md                   # written next
  backend/
    app.py                  # FastAPI + AG-UI endpoint
    agent.py                # Pydantic AI agent, tool registration, critic
    tools/                  # one file per tool domain
      state.py              # resolver, state_now
      spills.py             # spill counts, heatmap, scope filters
      downtime.py
      sop.py                # breach engine
      search.py             # hybrid retrieval over reports + SOPs
      memory.py             # feedback, claims, shared notes, recall
    db/
      ingest.py             # JSONL → DuckDB
      schema.sql            # tables, views, FTS index
      resolver.py           # pure-function state resolver
    models/
      schemas.py            # Pydantic models for tool I/O and UI payloads
  frontend/
    app/                    # Next.js App Router pages
    components/
      visuals/              # StatusCard, Table, LineChart, BarChart, Map, Heatmap
      chat/                 # AG-UI client, user picker, message list
  eval/
    expected.json           # Q1–Q18 expected answers
    run_eval.py
  scripts/
    ingest.py               # one-shot: load DATASET_ROOT into DuckDB + indices
```

## 11. Non-goals

- No real-time features beyond streaming.
- No authentication beyond the user-picker dropdown.
- No external hosting; everything runs on the laptop.
- No training or fine-tuning; models are called via APIs or Ollama only.
