# Decisions log — Asset Intelligence Copilot

Written during planning, 17 Oct 2026. Each entry: **what we chose, what we considered, why**.

## D1. Build the copilot in Python, not TypeScript

**Chose:** Python 3.11 backend, TypeScript only in the Next.js frontend.
**Considered:** TypeScript end-to-end with Vercel AI SDK.
**Why:** Python has the fastest path for everything on the data side: `google-genai`, `pydantic-ai`, `duckdb`, `chromadb`, `sentence-transformers`. The event has heavy numeric and SQL work; Python wins. The frontend is a thin AG-UI client.

## D2. DuckDB over SQLite and Postgres

**Chose:** DuckDB as the single data store (structured + FTS).
**Considered:** SQLite (+FTS5), Postgres, Parquet files + Pandas.
**Why:** The eval queries (heatmap, top-k downtime, cost rollups) are aggregations over ~40k inspections. DuckDB is 10–100× faster than SQLite on those, reads JSONL in one line, and ships as one file. Postgres is overkill ops. Pandas lacks durability and SQL ergonomics.

## D3. Pydantic AI over hand-rolled loop and over LangChain

**Chose:** Pydantic AI as the agent framework.
**Considered:** Hand-rolled tool-calling loop; LangChain / LangGraph; Vercel AI SDK (TS).
**Why:** Typed tool arguments and typed outputs catch most agent bugs for free. First-class AG-UI integration (shipped July 2025). Provider-agnostic — switch between Gemini and Ollama by changing one line. Smaller surface area than LangChain, less fragile than hand-rolled.

## D4. Gemini 2.5 Flash as primary, Gemma 4 as critic

**Chose:** Gemini 2.5 Flash on AI Studio for orchestration and final answers; Gemma 4 26b-a4b-it for critic; Ollama `gemma4:12b` as local fallback.
**Considered:** Gemma 4 as primary (as mentioned in prerequisites); Claude or OpenAI (paid).
**Why:** Gemma 4 is weaker at structured tool-calling and JSON mode. For a tool-heavy agent with strict output schemas, Gemini 2.5 Flash is the better free choice. Gemma remains perfect for the cheap, read-only critic pass. Local Ollama is the resilience plan if AI Studio throttles.

## D5. AG-UI as protocol, A2UI as component schema

**Chose:** AG-UI (CopilotKit's open protocol) for the agent↔UI channel. A2UI-shaped payloads carried inside AG-UI events for component descriptions.
**Considered:** A2UI alone; plain SSE with ad-hoc JSON; Vercel AI SDK streaming.
**Why:** AG-UI has a shipped Pydantic AI integration today; A2UI's listed integrations do not include Pydantic AI. A2UI is a declarative component schema, which is useful; AG-UI is a runtime channel, which is what we need to stream events. Using both costs nothing extra and tells the right story to judges.

## D6. Tremor + react-leaflet + Nivo, not OpenUI

**Chose:** Tremor for cards/charts/tables, react-leaflet for the map, Nivo for the heatmap.
**Considered:** OpenUI (mentioned in sources.md); hand-rolled Recharts; Mapbox.
**Why:** OpenUI is research-stage. Tremor is production-grade and shipped by Databricks' Mosaic team; it covers 5 of 6 required visuals out of the box. react-leaflet needs no API token. Nivo fills the heatmap gap. Judges rate the visual; polished defaults matter.

## D7. Three memory tables, not Mem0/Zep/Supermemory

**Chose:** DuckDB tables for conversations, overrides, shared notes, and claims. Pure SQL. State resolver as a Python function.
**Considered:** Mem0 (simple, cloud), Zep (temporal graph), Supermemory (bundled RAG + memory).
**Why:** The challenge requires **explicit scope control** (per-user private, per-well shared, per-well-type rules). Managed memory tools extract memories automatically and hide scope. We'd fight them to get Q13, Q14, Q16 right. Hand-rolled tables are smaller, inspectable, and the judge-facing story is stronger.

## D8. No graph database

**Chose:** Relational schema + SQL views + Python resolver.
**Considered:** Neo4j, Kuzu, networkx.
**Why:** Every eval query is a 1–2 hop traversal that SQL joins handle in <10 ms. A graph DB adds a second query language and a dependency without earning accuracy. For Q14 (type-wide rule) and Q9 (compliance across many sources), SQL CTEs are cleaner than Cypher.

## D9. State resolver as the correctness contract

**Chose:** One pure function returns `{state, reasons[], conflicts[]}` for any (entity, moment). The LLM narrates what the resolver returns; it does not decide state.
**Considered:** Let the LLM reason over raw records with heavy prompting.
**Why:** `recorded_status` lies, notes override inspections, feedback overrides notes, type-wide rules override single-well rules. The LLM cannot keep all this straight across 18+ queries. Centralising the logic makes it testable, reviewable, and consistent. It is our single biggest differentiator against teams who put everything in the prompt.

## D10. Deterministic tools, LLM picks only which to call

**Chose:** 15+ typed tools; the LLM selects and parameterises them; numbers come from tool results.
**Considered:** Let the LLM compute from raw data in-prompt.
**Why:** The LLM is bad at counting 40k rows and bad at date arithmetic. SQL is excellent at both. Determinism is the only way to make Q4–Q10 reliable.

## D11. Every answer must carry citations; "no evidence" is a valid answer

**Chose:** Output schema requires `citations[]`. Empty citations means the system prompt forces "no evidence in the data" and surfaces the gap.
**Considered:** Letting the LLM free-form.
**Why:** Judging rewards grounded evidence. An honest "I don't know" beats a confident wrong answer. The critic pass enforces this.

## D12. Critic pass before answers ship

**Chose:** A second Gemma call verifies the primary answer against tool results: (a) every number appears in a result, (b) every citation resolves, (c) no override was ignored. One retry on failure.
**Considered:** Trust the primary; or run self-consistency with multiple samples.
**Why:** Self-consistency is expensive on a free tier. A single cheap critic catches the common failures (fabricated IDs, numbers invented, missed corrections) at ~5% of the primary's cost.

## D13. Loading the test dataset is a config swap

**Chose:** `DATASET_ROOT` env var; one `python scripts/ingest.py $DATASET_ROOT` rebuilds everything. Idempotent. Rehearsed once before the window.
**Considered:** Separate code paths for sample vs test.
**Why:** The final 2-hour window is the real test. Code paths for "sample vs test" invite subtle bugs. One switch, same code, rehearsed.

## D14. Eval harness over Q1–Q18 blocks regressions

**Chose:** `eval/run_eval.py` runs the 18 example questions end-to-end and diffs against `eval/expected.json`. Required to be green before any merge.
**Considered:** Manual testing.
**Why:** We will refactor often. Without a harness, we won't catch when a change to the resolver silently breaks Q5 or Q9. The 18 are a free regression suite.

## D15. Model split for coding (this planning session)

**Chose:**
- **Opus (sparingly):** architecture, hard logic (resolver, overrides, SOP breach engine, memory scoping), prompt design, review.
- **Sonnet (default):** tool implementations, FastAPI routes, data loaders, React components.
- **Ollama Gemma 4 12b (local):** tightly specified small tasks — Pydantic schemas from a spec, repetitive React component variants, test fixtures. One retry; then Sonnet picks it up.

**Why:** Opus is expensive but worth it on design, where a wrong choice costs hours. Sonnet is the workhorse. The local Gemma handles boilerplate without burning the Claude quota.

## D16. Scope and non-goals

**Chose:** No auth beyond user picker. No cloud hosting. No image/photo generation. No fine-tuning. No real-time features beyond SSE streaming.
**Why:** Judged on a laptop, 1-day event. Every non-goal removes risk.
