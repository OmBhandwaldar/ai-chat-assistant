# Requirements coverage — Asset Intelligence Copilot

Every requirement from the problem statement, the dataset guide, the example queries, and the kickoff material, with the mechanism that satisfies it. This is our scorecard against the judging criteria.

## A. What the challenge asks for (from `01-problem-statement.md`)

### A1. A copilot chat UI with generative UI
**What they want:** chat interface; inline visuals (status card, table, line chart, bar chart, map, heatmap); the copilot picks the visual, or honors the user's choice.
**How we ensure it:**
- Output contract: every answer has `visual = {type, data}`.
- Component registry on the frontend maps each `visual.type` to a React component (Tremor / react-leaflet / Nivo).
- A `pick_visual()` heuristic in the agent prompt and tool metadata, plus explicit override when the user says "show as a table."
- AG-UI stream delivers visuals progressively; the user sees the card appear before the text finishes.

### A2. Answers grounded in the data, every answer cites records and documents
**How we ensure it:**
- Every tool returns `(data, evidence_ids[])`.
- Pydantic output schema requires non-empty `citations` or an explicit `"no evidence"` text.
- Critic pass rejects answers whose citations don't resolve to real records.
- UI renders citations as clickable pills showing the source record.

### A3. Memory at two levels (shared about a well + private per user) and learning from feedback
**How we ensure it:**
- **Private memory per user**: `conversations` and `turn_summaries` tables, every read filtered by `user_id`. Tool `recall_user_context(user_id, hint)` powers "the three wells we discussed yesterday" (Q11).
- **Shared knowledge about a well**: `shared_well_notes` table, written by any user via `record_shared_note`, read by every tool that reports on that well (Q16).
- **Learning from feedback at correct scope**:
  - `overrides(scope_type, scope_id, field, original_value, new_value, author, reason)` records every correction with explicit scope (`inspection`, `well`, `well_type`, `global`).
  - Views apply overrides automatically (`v_effective_inspections`, `v_spills`).
  - A type-wide rule like Q14 (RCA threshold 2 for T350) is a row with `scope_type='well_type', scope_id='T350'` applied by the SOP engine, which filters by the queried well's type.
  - The original value is preserved for traceability.

### A4. Honesty about conflicts
**How we ensure it:**
- The state resolver returns `conflicts[]` whenever two sources disagree (data vs claim, note vs inspection, two users disagree).
- Output schema has a required `conflicts` field; UI shows a "disagreements" section.
- The agent system prompt forbids silent tie-breaking and requires naming both sides.

### A5. A system that holds up on new data
**How we ensure it:**
- All paths relative to `DATASET_ROOT`. One ingest script rebuilds everything.
- No tool hardcodes well IDs, area names, or thresholds — everything is parameterised and read from the data or SOPs.
- Rehearsal on a shuffled copy of the sample before the real test window.
- Eval harness pattern-matches on shape, not exact strings, where appropriate.

## B. What the dataset guide calls out (from `02-dataset-guide.md`)

| Trap | How we handle it |
|---|---|
| **`recorded_status` can be stale** | Resolver uses inspections as truth; `recorded_status` only appears in the answer as a comparison, flagged "out of date" when it disagrees. |
| **"Producing oil" is derived, not stored** | `is_producing(well, inspection)` reads `wellhead_temp_c - ambient_temp_c` against the manual's threshold for the well's type (T200: 15°C, T350: 20°C). Absolute temperature is never used alone. |
| **Downtime = count of inspections × 7 days** | `downtime_days()` counts inspections in the window where `is_producing == False`, multiplies by 7. Never uses `maintenance.downtime_hours`. |
| **A spill may be corrected to "not a spill"** | `v_spills` view LEFT JOINs `context.jsonl` corrections and `overrides`; corrected detections drop out. Q4, Q7, Q8 all use this view. |
| **Thresholds live in manuals and SOPs, not in data files** | Manuals and SOPs are indexed and queried by protocol ID. The SOP breach engine reads deadlines (SR-01: 24h, SR-02: 4h, SR-03: 60d) from the SOP text, not from constants in code. |
| **Latest record can disagree with `recorded_status` or notes** | Resolver returns both; UI shows the disagreement. |
| **Some facts (containment time) are only in free-text reports** | Hybrid retrieval over reports with a scope filter. `search_reports(query, well_id, start, end)` returns chunks and citations. SOP breach engine parses "contained after X hours" from the top chunk. |
| **People disagree in notes about the same inspection** | Resolver returns all notes as `conflicts`; neither is treated as a correction unless formal (type `correction`). |

## C. Example queries, mapped to tools

| Q | Mechanism |
|---|---|
| Q1 | `well_status_now(WELL-0001)` → resolver returns state + reasons + note CTX-0001-003; visual=status_card |
| Q2 | `wells_not_producing(area='NE-08')` → view-based query; table with reasons |
| Q3 | `temp_timeseries(WELL-0001, weeks=13)` → line chart with producing-flag per point |
| Q4 | `spills(WELL-0001, 2022-10-16, 2026-10-16)` → `v_spills` applies corrections (CTX-0001-001 drops INS-0001-0070) |
| Q5 | `downtime_days(WELL-0001, 2025)` → 3 inspections × 7 = 21 days; evidence inspection IDs + MNT references |
| Q6 | `top_downtime(area='NE-07', last_days=120, k=3)` → bar chart |
| Q7 | `spills(area='NE-08', last_days=90)` with corrections → map |
| Q8 | `spill_heatmap(year=2026)` → areas × months, corrections removed |
| Q9 | `sop_breaches(WELL-0001, lookback_days=365)` → joins regulator events + report-parsed SR-02 + overdue checks |
| Q10 | `search_reports('containment hours', area='SW-24', last_days=90)` + SR-02 breach parser |
| Q11 | `recall_user_context('Michael Turner', hint='three wells discussed')` then `repair_costs([...], 2026)` |
| Q12 | `record_feedback(scope='inspection', id='INS-0164-0205', field='wellhead_temp_c', value=41.0)` → resolver re-evaluates WELL-0164; Q2 re-answer excludes it; conflict with "Pump jack not moving" surfaced |
| Q13 | `record_feedback(scope='inspection', id='INS-0151-0201', field='spill_est_volume_bbl', value=5.4)` → `v_spills` reclassifies as reportable → SOP engine flags SR-01 overdue + SR-03 due 2026-11-23 |
| Q14 | `record_feedback(scope='well_type', id='T350', field='rca_corrective_count', value=2)` → `rca_due()` filters by type |
| Q15 | Jessica queries WELL-0164 → resolver uses override → answer cites "corrected by Michael Turner"; Laura's query over NE-08 picks up Q13 correction |
| Q16 | Jake posts `shared_well_notes(WELL-0007, ...)` → Michael reads it in his next session; Jessica's "three wells" returns none (private memory not shared) |
| Q17 | `record_claim(Tyler, WELL-0175, "running fine")` → resolver returns `conflicts=[claim_vs_data]`; status=unconfirmed, both kept |
| Q18 | Two notes disagree about INS-0101-0197 → resolver returns both as conflicts; detection stands because no formal correction and MNT-0101-029 exists |

## D. Hackathon kickoff emphases

| Emphasis | How we ensure it |
|---|---|
| **Context engineering** | Compact rule cards (SOP thresholds, well-type rules) always in the system prompt. Long docs retrieved as needed. Tool results carry only the smallest answer. Per-turn summary keeps chat context tight. |
| **Where and how data is stored** | DuckDB (one file) for all structured data; FTS inside DuckDB for reports; ChromaDB for dense embeddings; manuals/SOPs indexed by protocol ID. Justified in `TECH_STACK.md`. |
| **Efficient and correct retrieval** | Deterministic SQL for aggregations; hybrid BM25+dense+rerank for free text; scope filters on every search; protocol-ID lookup for SOP sections. |
| **Reliable, correct outputs** | Typed tool I/O, output JSON schema, resolver as single source of truth, critic pass, "no evidence" is a valid answer. |
| **QA of outputs** | Eval harness over Q1–Q18 with expected diffs; critic pass live on every production answer; manual review before test window. |
| **Complex query handling** | Composed tools (e.g., `sop_breaches` calls four sub-queries), reactive re-evaluation after feedback, resolver centralises priority rules. |
| **Test dataset performance** | One ingest command; same code paths; parameters-only configuration; rehearsed. |

## E. Submission items (from `01-problem-statement.md`)

| Item | Our plan |
|---|---|
| System design write-up (page 1) | Architecture diagram + prose covering: ingest, storage, retrieval, resolver, memory layers, conflict handling, models, cost, next steps. Derived from `TECH_STACK.md` and `DECISIONS.md`. |
| Results write-up (page 2) | Table of final questions with our answers, what worked, what didn't, why. Derived from the eval harness output. |
| 5-minute video | Narration + screen capture: system diagram, then one real question end-to-end (Q12 or Q13 — correction cascade shows the differentiator). |
| Public GitHub repo with OSS license | MIT license, pushed before deadline. |

## F. What could still go wrong, and the mitigation

| Risk | Mitigation |
|---|---|
| AI Studio rate-limits mid-demo | Fall back to Ollama `gemma4:12b`; one env var swap. |
| Report retrieval misses a key chunk | Second query with reranker enabled; log misses for post-hoc inspection. |
| LLM invents a well ID | Critic pass + output validator reject IDs not in the DB. |
| Feedback applied at wrong scope | Scope is explicit in the API call (`scope_type` enum); UI shows the scope back to the user before applying; tests cover each scope. |
| Test dataset has shape we didn't see | Ingest script tolerates extra fields, missing optional fields, and empty files; eval harness has shape-not-value assertions for structural checks. |
| Running out of time | Phased plan prioritises Q1–Q8 (A1) before memory/feedback (A2) before multi-user (Part B). Each phase has a usable demo. |
