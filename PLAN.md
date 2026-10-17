# Build plan — Asset Intelligence Copilot

**Event:** FlytBase AHC SWE Hackathon, 17 Oct 2026.
**Prereqs:** read [TECH_STACK.md](TECH_STACK.md), [DECISIONS.md](DECISIONS.md), [REQUIREMENTS_COVERAGE.md](REQUIREMENTS_COVERAGE.md) before starting any step.

## Model split (strict)

- **[Opus]** — reserved for design + hard logic + prompt design. ≤ 6 calls, each at **low or medium effort**. Nothing else.
- **[Sonnet]** — default coder. All non-trivial implementation, debugging, review.
- **[Ollama gemma4:12b]** — tightly specified mechanical tasks only. One retry on failure, then Sonnet picks up. Each Ollama task below includes a complete spec (inputs, outputs, acceptance tests) so the model can execute without context.

Ollama tasks run locally via its OpenAI-compatible endpoint. Keep each Ollama prompt under 2k tokens of context; paste only the single file or spec it needs.

## Working rules

1. Finish each phase's "Acceptance" before starting the next.
2. Every tool returns `(data, evidence_ids[])`. Enforce in Pydantic schemas.
3. Every answer carries `citations[]`. Empty citations ⇒ "no evidence in the data."
4. Every change ends with `pytest` green + `python eval/run_eval.py` on the subset covered so far.

---

## Phase 0 — Scaffold (30 min)

### 0.1 Repo skeleton **[Ollama]**
**Spec:** create the directory tree exactly as in `TECH_STACK.md` §10. Create empty files with a one-line docstring in each Python file naming its purpose. Create `pyproject.toml` with `uv` as the manager and dependencies: `pydantic`, `pydantic-ai[ag-ui]`, `google-genai`, `duckdb`, `chromadb`, `sentence-transformers`, `fastapi`, `uvicorn`, `python-dotenv`, `httpx`, `pytest`, `ruff`, `mypy`. Create `.env.example` listing `GEMINI_API_KEY`, `DATASET_ROOT`, `OLLAMA_BASE_URL`.
**Acceptance:** `uv sync` completes; `uv run python -c "import pydantic_ai, duckdb, chromadb; print('ok')"` prints ok.

### 0.2 Frontend skeleton **[Sonnet]**
Create Next.js 15 + TS + Tailwind + shadcn/ui app under `frontend/`. Install `@copilotkit/react-core`, `@copilotkit/react-ui`, `@tremor/react`, `@nivo/heatmap`, `react-leaflet`, `leaflet`, `zustand`. Set up the base layout with:
- Left sidebar (dark text on light, collapsible), with sections: logo, Search / Chat / Voice / Imagine icons (keep only Chat active; others are stubs for the hackathon), **Projects**, **History** of chats, user picker at the bottom. Inspired by the attached mock.
- Main area: chat scroll + input bar fixed at the bottom (attach icon, "Ask anything…" placeholder, mic stub, send button, model selector showing "Expert").
Use the **ui-ux-pro-max** skill for component composition and spacing choices.
**Acceptance:** `pnpm dev` shows the empty chat shell matching the mock's layout.

### 0.3 FastAPI health endpoint **[Ollama]**
**Spec:** create `backend/app.py` with FastAPI app, a `GET /health` returning `{"ok": true, "version": "0.1"}`, and CORS enabled for `http://localhost:3000`. Start with `uvicorn backend.app:app --reload`.
**Acceptance:** `curl localhost:8000/health` returns the JSON.

---

## Phase 1 — Ingest + DuckDB schema + base views (2 hrs)

### 1.1 DuckDB schema **[Sonnet]**
Create `backend/db/schema.sql` with tables:
- `areas(area_id PK, name, team_id, lat, lon)`
- `teams(team_id PK, name, lead_user_id)`
- `users(user_id PK, name, role, team_id)`
- `wells(asset_id PK, name, well_type, area_id, team_id, in_charge_user_id, lat, lon, installed_on, recorded_status)`
- `well_equipment(asset_id, equipment_id, kind, model)`
- `inspections(inspection_id PK, asset_id, inspected_at TIMESTAMP, inspector_user_id, method, ambient_temp_c, wellhead_temp_c, spill_detected, spill_area_pct, spill_est_volume_bbl, notes)`
- `maintenance(maintenance_id PK, asset_id, started_at, completed_at, maintenance_type, equipment_kind, issue, action_taken, technician_user_id, contractor, downtime_hours, cost_labor, cost_parts, cost_total, triggered_by_inspection_id, report_file)`
- `regulatory_events(event_id PK, asset_id, date, agency, event_type, protocol_id, related_inspection_id, hours_to_report, detail, fine_usd, status)`
- `context_notes(note_id PK, asset_id, created_at, author_user_id, note_type, inspection_id, text, valid_from, valid_to, source TEXT DEFAULT 'dataset')`
- `maintenance_reports(report_path PK, asset_id, maintenance_id, body TEXT)`
- `well_type_rules(well_type PK, producing_threshold_c, max_wellhead_c)` — populated from the manuals
- `overrides(id BIGINT PK, scope_type, scope_id, field, original_value JSON, new_value JSON, author_user_id, reason, created_at)`
- `shared_claims(id BIGINT PK, user_id, scope_type, scope_id, text, status, created_at)`
- `conversations(user_id, turn_id, role, text, ts)`
- `turn_summaries(user_id PK, summary, updated_at)`

Add indexes on `asset_id`, `inspected_at`, `date`.
Add FTS index on `maintenance_reports.body` using DuckDB's `fts` extension.

### 1.2 Ingest script **[Sonnet]**
`scripts/ingest.py $DATASET_ROOT`:
- Load `fleet/areas.json`, `fleet/users.json` into `areas`, `teams`, `users`.
- Walk `assets/WELL-*/` and load `metadata.json`, `inspections.jsonl`, `maintenance.jsonl`, `regulatory-events.jsonl`, `context.jsonl` with DuckDB's `read_json_auto` where possible, otherwise Python glob.
- Walk `assets/WELL-*/maintenance-reports/*.md` into `maintenance_reports` (join `maintenance_id` by filename or `report_file` from the maintenance row).
- Parse manual files to seed `well_type_rules`: T200 → (15, 85), T350 → (20, 95). Hardcode for now; a later step reads them from markdown.
- Rebuild the FTS index at the end.
- Idempotent: drop and recreate tables each run.
**Acceptance:** `python scripts/ingest.py sample-data` loads without errors; `SELECT COUNT(*) FROM inspections` returns ~39,600; `SELECT COUNT(*) FROM wells` returns 200.

### 1.3 Base views **[Sonnet]**
`backend/db/views.sql`:
- `v_effective_inspections` = `inspections` LEFT JOIN `overrides` (scope=inspection) to apply field-level overrides.
- `v_corrected_spills` = inspections where `spill_detected = TRUE` AND no `correction` note on that inspection AND no override setting spill_detected=false.
- `v_well_latest_inspection` = latest row per `asset_id` from `v_effective_inspections`.
- `v_is_producing` = per inspection, computed flag using `well_type_rules`.
Load views on DB open.
**Acceptance:** `SELECT asset_id, is_producing FROM v_is_producing WHERE asset_id='WELL-0001' ORDER BY inspected_at DESC LIMIT 3` returns the three latest with correct booleans (last two should be False for WELL-0001).

### 1.4 Pydantic schemas **[Ollama]**
**Spec:** write `backend/models/schemas.py` with Pydantic models matching the SQL tables (one class per table) plus:
- `Evidence(source_id: str, source_type: Literal['inspection','maintenance','report','context','regulatory','sop','manual','override','claim'])`
- `ToolResult[T](data: T, evidence: list[Evidence])`
- `WellState(state: Literal['producing','not_producing','unknown'], reasons: list[str], conflicts: list[dict])`
- `UIVisual(type: Literal['status_card','table','line','bar','map','heatmap','none'], data: dict)`
- `AgentAnswer(text: str, visual: UIVisual, citations: list[Evidence], conflicts: list[dict])`
Each model has `model_config = ConfigDict(extra='forbid')`.
**Acceptance:** `pytest backend/tests/test_schemas.py` passes with a round-trip test for each model.

---

## Phase 2 — State resolver + SOP engine (CORE differentiator)

### 2.1 State resolver — design + prompt **[Opus, low effort]**
**One call.** Opus writes `backend/db/resolver.py` with `resolve_well_state(conn, asset_id, as_of=None) -> WellState`. Priority order (highest first):
1. `v_is_producing` on the latest inspection ≤ as_of (truth after overrides).
2. Formal `correction` notes on that inspection: can flip spill detection; cannot flip producing (unless feedback override on temp).
3. Feedback overrides on temp (Q12 flow — temp changed, producing flag recomputes).
4. Runtime shared notes with `valid_from ≤ as_of ≤ valid_to` ("planned shut-in" → state unchanged but reason added).
5. `recorded_status` — only as a comparison, flagged "out of date" if it disagrees.
Returns reasons as plain English strings and a `conflicts` list for every disagreement found. Includes doctring with each rule and the Q it covers.
**Acceptance:** `pytest backend/tests/test_resolver.py` passes a fixture-based test for WELL-0001 (should be `not_producing`, reasons mention CTX-0001-003 and recorded_status out of date).

### 2.2 Overrides engine **[Sonnet]**
`backend/db/overrides.py`:
- `record_override(scope_type, scope_id, field, new_value, author_user_id, reason) -> override_id`
- `apply_overrides_to_inspection(row)` and `apply_overrides_to_rule(well_type, rule_name, default)`
- Register listener: after insert into `overrides`, bump a `cache_version` for the resolver.
**Acceptance:** test: record override on INS-0164-0205 wellhead_temp_c=41.0; resolver on WELL-0164 flips to `producing`.

### 2.3 SOP breach engine — design **[Opus, medium effort]**
**One call.** Opus writes `backend/tools/sop.py`:
- `sop_breaches(asset_id, lookback_days) -> {breaches: [...], open_items: [...], met: [...], evidence}`
- Reads SOP thresholds from the SOP markdown by protocol ID. Covers: SR-01 (24h written notice for ≥5 bbl; use `v_corrected_spills` + volume override), SR-02 (4h containment, parsed from the top repair-report chunk), SR-03 (60d verification), RC-01, RC-02, RC-03, RC-04, RC-05, type-manual §10 RCA threshold (default 3 corrective per equipment_kind in 12 months, overridable per well_type via `overrides`).
- Each breach line includes `protocol_id`, `rule_text_quote`, `evidence_id`, `due_by`, `status`.
**Acceptance:** `sop_breaches('WELL-0001', 365)` returns the 4 breaches from Q9 (SR-01, SR-02, SR-03, RC-04) and the two pending items. Diff against `eval/expected.json["Q9"]`.

### 2.4 SOP markdown parser **[Ollama]**
**Spec:** write `backend/tools/sop_rules.py` with `load_sop_rules(sops_dir) -> dict[str, dict]`. For each `.md` in `fleet/sops/` and each `fleet/well-types/*.md`, extract protocol IDs (`SR-01`, `WS-05`, `RC-02`, section numbers like `9.3`, `10`) and the following paragraph as `text`. Return `{protocol_id: {text, source_path}}`. Use a regex `^\*\*(?P<id>[A-Z]{2,3}-\d{2}|\d+(?:\.\d+)*)\*\*` for IDs in tables and `##? (?P<id>\d+(?:\.\d+)*)` for sections. Also expose `extract_threshold(text, pattern) -> number|None` for pulling things like `at least 15 °C` or `within 24 hours`.
**Acceptance:** `pytest backend/tests/test_sop_rules.py` — loads returns ≥ 15 entries; `rules["SR-01"]["text"]` contains "24 hours"; `rules["SR-02"]["text"]` contains "4 hours".

---

## Phase 3 — Tool catalogue

Write each tool as a Pydantic AI `@agent.tool` function. Every tool: typed args, returns `ToolResult[T]`, pulls evidence IDs from the SQL it ran.

### 3.1 Structured tools **[Sonnet]**
Implement in `backend/tools/`:
- `state.py` — `well_status_now`, `wells_not_producing`
- `trends.py` — `temp_timeseries`, `top_downtime`
- `spills.py` — `spills`, `spill_heatmap`
- `downtime.py` — `downtime_days`
- `costs.py` — `repair_costs`
- `rca.py` — `rca_due`
Each is 20–40 lines of SQL + a small Pydantic wrapper.
**Acceptance:** `pytest backend/tests/test_tools_structured.py` passes 1 round-trip test per tool using known-answer fixtures from Q1–Q11.

### 3.2 Report retrieval tool **[Sonnet]**
`backend/tools/search.py`:
- Build chunker: split each `maintenance_report.body` into ~400-token windows, 50-token overlap. Store in a `report_chunks` table with `chunk_id, asset_id, maintenance_id, text, embedding BLOB`.
- Embed with `bge-small-en-v1.5` via sentence-transformers.
- Store vectors in ChromaDB collection `reports` with metadata `{asset_id, area_id, started_at, maintenance_id}`.
- `search_reports(query, well_id?, area_id?, start?, end?, k=5)`:
  - Dense: Chroma top-20 with metadata filter.
  - Sparse: DuckDB FTS top-20 same filter.
  - RRF fuse → top-k. Return chunk text + `maintenance_id` as evidence.
**Acceptance:** `search_reports('contained after', area_id='SW-24', start='2026-07-18')` returns MNT-0082-028 and MNT-0100-034 in top-3.

### 3.3 Memory + feedback tools **[Sonnet]**
`backend/tools/memory.py`:
- `record_feedback(scope_type, scope_id, field, new_value, reason, user_id)` → writes `overrides`.
- `record_shared_note(well_id, text, valid_from?, valid_to?, user_id)` → writes `context_notes` with `source='runtime'`.
- `record_claim(user_id, scope_type, scope_id, text)` → writes `shared_claims`, immediately runs resolver to flag conflicts; returns conflicts in result.
- `recall_user_context(user_id, hint)` → scans recent turns for the user, extracts well_ids and area_ids mentioned, returns top matches by recency.
**Acceptance:** test the Q12 → Q2 flow end-to-end (record feedback, re-run `wells_not_producing`, assert WELL-0164 is gone).

### 3.4 SOP + manual lookup tools **[Ollama]**
**Spec:** add to `backend/tools/sop.py`:
- `get_sop(protocol_id: str) -> ToolResult[str]` — returns the protocol text from `load_sop_rules()`.
- `get_manual_section(well_type: Literal['T200','T350'], section: str) -> ToolResult[str]` — returns the section text.
Both include `Evidence(source_type='sop'|'manual', source_id=f'{file}#{section}')`.
**Acceptance:** `get_sop('SR-01').data` contains "24 hours".

---

## Phase 4 — Agent + critic + AG-UI

### 4.1 Agent system prompt + output contract **[Opus, medium effort]**
**One call.** Opus writes `backend/agent.py` creating a Pydantic AI `Agent[Deps, AgentAnswer]`:
- System prompt sections: role; always-on rule cards (T200/T350 thresholds, SR-01/02/03 deadlines, RC rules); tool-selection guidance ("prefer typed tools for numbers; search reports only for free-text facts"); conflict policy ("never silently pick a side"); citation policy ("every number must trace to an evidence_id"); visual policy (user request wins; otherwise pick from the Q→visual map); scope policy for feedback.
- Register all tools from Phase 3.
- Output type = `AgentAnswer` (Pydantic enforces).
- Pre-call hook: inject `turn_summaries[user_id]` and the 5 most recent turns.
- Post-call hook: run critic (step 4.2).
**Acceptance:** manual: ask Q1 against WELL-0001, resolver + tools fire, output validates as `AgentAnswer` with non-empty citations.

### 4.2 Critic pass **[Opus, low effort]**
**One call.** Opus writes `backend/critic.py`:
- Input: user question + `AgentAnswer` JSON + list of tool results that fired.
- Model: Gemma 4 (26b-a4b-it) via AI Studio.
- Prompt returns JSON `{ok: bool, issues: [str], must_retry: bool}`.
- Checks: (a) every number in `text` appears in a tool result; (b) every `citations[].source_id` exists in the DB; (c) if `conflicts` is empty, verify no override would flip the state; (d) `visual.type` is consistent with the Q→visual rules.
- On `must_retry=true`, agent retries once with the critic's `issues` appended; on second failure, UI shows "unable to verify" with partial results.
**Acceptance:** inject a hallucinated inspection ID into an answer in a unit test; critic flags it.

### 4.3 Wire Pydantic AI to AG-UI **[Sonnet]**
In `backend/app.py` add `POST /ag-ui` using Pydantic AI's `.to_ag_ui()` adapter, streaming SSE. Thread user_id from a header.
**Acceptance:** CopilotKit React client in the frontend can connect and stream a response.

### 4.4 Gemini + Ollama providers **[Ollama]**
**Spec:** write `backend/providers.py` with `make_llm(role: Literal['primary','critic'], prefer: Literal['gemini','ollama']='gemini')`. Reads `GEMINI_API_KEY` for Gemini 2.5 Flash (primary) and Gemma 4 26b-a4b-it (critic). On a 429 or network error, falls back to Ollama (`gemma4:12b` via `OLLAMA_BASE_URL`). Returns a Pydantic AI `Model`. Add a 1-line smoke-test script `scripts/smoke_llm.py` that calls both.
**Acceptance:** `python scripts/smoke_llm.py` prints two "ok" lines.

---

## Phase 5 — Frontend

Use the **ui-ux-pro-max** skill while building. Layout matches the attached mock (sidebar + main + bottom input).

### 5.1 Chat shell + user picker **[Sonnet]**
- Zustand store: `user_id`, `conversation_id`, `messages[]`.
- Sidebar `UserPicker` dropdown lists users from `/users` endpoint (write that endpoint too — simple `SELECT * FROM users`).
- `History` lists the user's past conversations (from `/conversations?user_id=…`).
- Bottom input bar with attach icon (stub), textarea, mic icon (stub), send button, "Expert" selector (stub).
- On send: POST message to CopilotKit `/ag-ui` with `X-User-Id` header.

### 5.2 Component registry (A2UI-shaped) **[Sonnet]**
`frontend/components/visuals/registry.tsx`:
- Dispatcher takes `{type, data}` and renders:
  - `status_card` → `StatusCard` (shadcn `Card` + Tremor `Metric` + a reason list + coloured badge for state).
  - `table` → Tremor `Table` with sortable columns (passed as `data.columns`).
  - `line` → Tremor `LineChart`.
  - `bar` → Tremor `BarChart`.
  - `heatmap` → Nivo `ResponsiveHeatMap`.
  - `map` → `react-leaflet` with pins from `data.pins[]`.
  - `none` → nothing.
- Below the visual, always render a `CitationsRow` (chips) and, if present, a `ConflictsPanel`.

### 5.3 Visual components **[Ollama]** (one task per component)
For each of `StatusCard`, `WellTable`, `LineChart`, `BarChart`, `MapView`, `Heatmap`:
**Spec (template):** write `frontend/components/visuals/<Name>.tsx`. Receives `props: { data: <JSON shape per TECH_STACK schema> }`. Uses the libraries listed. Export default. Must handle empty data (`data == null` or `data.rows.length === 0`) by showing a muted "No data" row. Styling: `rounded-xl border border-neutral-200 p-4 bg-white`. Dark text, no accent colours other than Tremor defaults.
**Acceptance:** render each with a fixture in `frontend/__fixtures__/`; Storybook stub or a `/dev/visuals` route.

### 5.4 Chat message rendering **[Sonnet]**
- User bubble (right, dark text, small avatar from users table).
- Agent response block: optional "Thought for Xs" line (from AG-UI step events), then the visual, then the text (small, below), then citations chips, then conflicts panel.
- Streaming: show visual first (if emitted), then text appends.

### 5.5 Polish **[Sonnet]**
- Dark text on light, no accent colours except Tremor's. Match the mock's airy spacing.
- Collapse sidebar button in the top-left (matches mock).
- Loading placeholder: the dotted pattern from the mock while the visual is being generated.

---

## Phase 6 — Multi-user flows (Part B)

### 6.1 Conflict detection on claims **[Sonnet]**
Already partly in resolver. In `record_claim`, call resolver immediately; store `status='unconfirmed'` on contradiction; return the conflict so the agent surfaces it (Q17).

### 6.2 Private vs shared memory tests **[Sonnet]**
`pytest backend/tests/test_memory_scope.py`:
- Jake posts shared note on WELL-0007; Michael's agent reads it; Jessica's agent does not see Michael's chat history (Q16).
- Michael records feedback on WELL-0164 temp; Jessica's resolver uses it (Q15).

### 6.3 Rule-at-scope **[Sonnet]**
Verify Q14: feedback with scope `well_type='T350'` for `rca_corrective_count=2` is applied only to T350 wells in `rca_due`. Add test.

---

## Phase 7 — Eval harness

### 7.1 Expected answers fixture **[Ollama]**
**Spec:** create `eval/expected.json` with one entry per question Q1–Q18. For each: `{id, question_text, expected: {text_contains: [...], visual_type, well_ids_in_data?, numbers?: {name: value}, citations_must_include: [...]}}`. Transcribe directly from `03-evaluation-queries.md` (read it and copy expected values). Keep it machine-checkable.
**Acceptance:** JSON validates; Q4 has `numbers.total_bbl=15.1` and `numbers.spill_count=6`; Q6 includes `WELL-0121` with 49 days.

### 7.2 Eval runner **[Sonnet]**
`eval/run_eval.py`:
- Spins up DB + agent in-process (no HTTP).
- For each question in `expected.json`: run the agent (with the specified user_id when given), capture `AgentAnswer` and the tool trace.
- Assert: `visual.type` matches; every `text_contains` substring is in `text`; every `numbers.*` appears in `text` within 0.5% tolerance; every citation in `citations_must_include` is present.
- For questions with prior state (Q11 follows Q6; Q13→Q15; Q14→restated), run them in order within one process.
- Output: table with ✅/❌ per question, failing diffs, summary count.
**Acceptance:** targets ≥ 12 / 18 green by end of Phase 7; ≥ 15 / 18 before submission.

### 7.3 Resolver + SOP unit tests **[Sonnet]**
Already seeded in earlier phases. Expand coverage for scope edge cases, missed inspections, boundary windows.

---

## Phase 8 — Test-dataset rehearsal + submission

### 8.1 Ingest rehearsal **[Sonnet]**
Make a corrupted/shuffled copy of `sample-data` in `/tmp/fake-test-data`; add some wells with extra JSON fields and empty files. Run `python scripts/ingest.py /tmp/fake-test-data`. Fix any crashes. Rebuild indices. Measure ingest time; target < 5 min.

### 8.2 Write-up (page 1 — system design) **[Sonnet]**
Derived from `TECH_STACK.md` + `DECISIONS.md`. Keep to one page: architecture diagram, storage, retrieval, memory + feedback, conflicts, models + cost, next steps.

### 8.3 Write-up (page 2 — results) **[Sonnet]**
Table of the final questions with our answers, from the eval runner's output on the test dataset. For each: what worked, what didn't, why.

### 8.4 Video script + record **[you]**
Opening: the business problem, 20 s. The system diagram, 60 s. Live demo of Q13 end-to-end (correction cascade), 2 min. Close with results, 60 s. Total ≤ 5 min.

### 8.5 OSS license + push **[Ollama]**
**Spec:** add `LICENSE` (MIT, author: Om). Push to a public GitHub repo named `flytbase-ahc-asset-intelligence-copilot`. Add `README.md` with 10-line project overview + install + run commands.
**Acceptance:** repo visible publicly, link ready for submission.

---

## Dependencies / parallelism

- Phase 1 blocks everything.
- Phase 2 blocks Phase 3's structured tools.
- Phase 4 blocks Phase 7.
- Phase 5 can start after Phase 0 and run in parallel with Phases 1–4 (use fixture data).
- Phase 6 starts after 4 is wired.

## Time budget (if hackathon is a single day)

| Phase | Target hours |
|---|---|
| 0 | 0.5 |
| 1 | 2.0 |
| 2 | 2.5 |
| 3 | 2.5 |
| 4 | 2.0 |
| 5 | 2.5 |
| 6 | 1.0 |
| 7 | 1.5 |
| 8 | 1.5 (last 2 hrs are test window) |

Trim Phase 5.5 polish and Phase 6 stretch items first if we fall behind. The resolver + overrides + eval harness must ship.

## Opus call budget

Used by this plan: 3 calls (D2.1 resolver, D2.3 SOP engine, D4.1 agent prompt) at low/medium effort. Reserve 3 more for mid-build rescue: hard bug diagnosis, a late prompt rewrite if the critic fails too often, final answer-schema tightening.
