# Asset Intelligence Copilot: results

*Page 2 of the submission write-up (PLAN.md Phase 8.3). **DRAFT.** The offline dry-run numbers are real. The live columns are from the 17 Oct re-run, which stopped when Gemini returned HTTP 429 and the Ollama follow-up timed out.*

> **Status of this page**
> - ✅ Dry-run baseline: filled in (18/18).
> - ⚠️ Live eval: **not finished.** Gemini (`gemini-3.8-flash`) answered a one-call smoke test, then a targeted re-run passed Q3, Q8 and Q10 before the API returned HTTP 429. The process was stopped so it would not keep spending quota. A follow-up on local Ollama (`gemma4:12b`) did not finish: several turns hit the per-turn timeout. There is no completed Q1–Q18 live file from this re-run.
> - ⏳ Run on the hidden test dataset: not done. The sample dataset is what the eval fixture describes (200 wells, `as_of` 2026-10-16).

## 1. What was measured

`eval/run_eval.py` runs Q1–Q18 in one process against one throw-away copy of `data.duckdb`. Runtime state (conversations, overrides, claims, runtime notes) is wiped first, so every run starts identically. Stateful questions run in order: Q11 after Q6, Q12 after Q2, Q13 after Q7, Q14 ask → rule → ask again, Q15 after Q12 and Q13, Q16 and Q17 with setup turns from other users.

`eval/scoring.py` checks each scored turn against `eval/expected.json`:

- visual type
- required text substrings (with accepted alternatives)
- numbers within 0.5 %
- required well IDs in the visual data
- required citation IDs
- minimum number of conflicts

A question passes only if every scored turn passes. Checks marked `_optional` produce warnings and never fail a question.

## 2. Dry-run baseline (offline, no LLM)

Command: `uv run python eval/run_eval.py --dry-run` (about 0.5 s).

**Result: 18/18 passed.**

What this proves and what it does not:

- The dry-run drives the real agent through a Pydantic AI `TestModel` that returns the "perfect" answer synthesised from `expected.json`. It exercises the fixture, the stateful run order, the post-processing, and the scorer.
- It does **not** show that the model picks the right tools or reasons correctly. The tool trace in a dry-run is not meaningful (every row shows only `well_status_now`). Only the live run measures answer quality.

| Q | Pass | Turns | Visual | Q | Pass | Turns | Visual |
|---|---|---|---|---|---|---|---|
| Q1 | ✅ | 1/1 | status_card | Q10 | ✅ | 1/1 | table |
| Q2 | ✅ | 1/1 | table | Q11 | ✅ | 1/1 | bar |
| Q3 | ✅ | 1/1 | line | Q12 | ✅ | 2/2 | table |
| Q4 | ✅ | 1/1 | table | Q13 | ✅ | 1/1 | none |
| Q5 | ✅ | 1/1 | none | Q14 | ✅ | 2/2 | table |
| Q6 | ✅ | 1/1 | bar | Q15 | ✅ | 2/2 | table |
| Q7 | ✅ | 1/1 | map | Q16 | ✅ | 2/2 | none |
| Q8 | ✅ | 1/1 | heatmap | Q17 | ✅ | 2/2 | none |
| Q9 | ✅ | 1/1 | table | Q18 | ✅ | 1/1 | none |

Supporting checks run the same day:

- `uv run pytest backend`: **234 passed** (11.5 s).
- Corrupted-dataset ingest rehearsal (`scripts/make_fake_dataset.py`, 161 wells, 31,671 inspections, 19 warning categories expected): ingest finished in **28.5 s**, and the `--check-db` comparison against the generator's expectations reported "all expectations met".
- Sample dataset ingest: 200 wells, 39,567 inspections, 3,886 report chunks in **34.9 s** with 0 warnings.

## 3. Live eval

**Run metadata** (fill from the `meta` block of the JSON that `--json` writes):

| Field | Value |
|---|---|
| Command | `uv run python eval/run_eval.py --prefer gemini --only Q3,Q8,Q10,Q12,Q14,Q15,Q16,Q18` (stopped after HTTP 429). Ollama follow-up: same command with `--prefer ollama` (turns timed out; not a finished file). |
| Provider and model | Gemini `gemini-3.8-flash` until 429, then local Ollama `gemma4:12b`. Anthropic was not called. |
| Dataset | Sample, `as_of` 2026-10-16, throw-away copy of `backend/data.duckdb` |
| `--inject-today` | Off. The agent still injects today's date from the newest inspection. |
| Result file | No finished JSON. Partial logs: `eval/results/gemini-targeted.txt`, `eval/results/ollama-targeted.txt`. Earlier Claude run (not this re-run): `eval/results/claude-run1.json` was 10/18. |
| **Passed** | Not a full score. Before the 429: Q3, Q8, Q10 passed on Gemini. Target in PLAN.md is ≥ 15/18. |
| Total wall time | Not a complete run. Gemini turns that finished: Q3 13.9s, Q8 9.1s, Q10 6.9s. |

## 4. Question-by-question results

Columns: the question as asked, who asked, what the fixture requires, the dry-run baseline, the live result, and a short note. The "Requires" column comes from `eval/expected.json`. Fill the last two columns from the live output. For every ❌, say what failed, whether the cause was model behaviour, a tool or resolver gap, or a fixture strictness issue, and what we would change.

| Q | Asker | Question (short) | Requires | Dry-run | Live | Time | What worked / what didn't / why |
|---|---|---|---|---|---|---|---|
| Q1 | U-203 | Is WELL-0001 producing now? Last inspection and by whom? Status card | `status_card`; not producing; INS-0001-0203, Ryan Brooks, 15 Oct, CTX-0001-003; delta 3.2 °C | ✅ | — | — | Not in the targeted re-run. No finished Q1–Q18 live file. |
| Q2 | U-203 | NE Block 8 wells not producing and why, as a table | `table`; WELL-0164, 0170, 0172, 0175; MNT-0170-031, MNT-0172-028; says "no recorded reason" for the two without one | ✅ | — | — | Not in the targeted re-run. No finished Q1–Q18 live file. |
| Q3 | U-203 | Line chart, WELL-0001 wellhead vs air temp, 13 weeks, mark not-producing | `line`; 8 Oct to 15 Oct; INS-0001-0203 cited | ✅ | ✅ | 13.9s | Gemini, before the 429. Line points are copied from `temp_timeseries` when the model leaves `visual.data` empty. |
| Q4 | U-203 | WELL-0001 spills in 4 years and total volume, table | `table`; **6 spills, 15.1 bbl** (INS-0001-0070 excluded as rainwater, CTX-0001-001); six spill inspections cited | ✅ | — | — | Not in the targeted re-run. No finished Q1–Q18 live file. |
| Q5 | U-203 | Days WELL-0001 was down in 2025 | no visual; **21 days**; INS-0001-0140, -0152, -0153 cited | ✅ | — | — | Not in the targeted re-run. No finished Q1–Q18 live file. |
| Q6 | U-201 | Top 3 NE Block 7 wells by downtime, last 4 months, bar | `bar`; WELL-0121 (49 d), 0138 (42 d), 0107 (42 d); WELL-0119 absent | ✅ | ✅ | 125s | Passed as a prerequisite after Gemini timed out and the runner fell back to Ollama. The process was stopped after the 429, so this is not in a finished JSON file. |
| Q7 | U-203 | Map of NE Block 8 wells with a real spill in 90 days | `map`; WELL-0151, 0165, 0175, 0186 pinned | ✅ | — | — | Not in the targeted re-run. No finished Q1–Q18 live file. |
| Q8 | U-203 | Heatmap of 2026 spills by area and month | `heatmap`; the four named areas present; corrected spills removed | ✅ | ✅ | 9.1s | Gemini, before the 429. Area names come from `spill_heatmap`, not from the model retyping the matrix. |
| Q9 | U-203 | Rules WELL-0001 broke in 12 months, what is open | `table` or none; SR-01, SR-02, SR-03, RC-04; REG-0001-001, -002, -003 | ✅ | — | — | Not in the targeted re-run. No finished Q1–Q18 live file. |
| Q10 | U-203 | SW Block 24 spills in 90 days not contained within SOP limit | `table`; WELL-0082 (7.1 h), WELL-0100 (8.1 h) vs 4 h limit; MNT-0082-028, MNT-0100-034 cited | ✅ | ✅ | 6.9s | Gemini, before the 429. `containment_breaches` builds the table (7.1 h / 8.1 h) instead of a spill map. |
| Q11 | U-201 | Repair cost this year for "the three wells we discussed yesterday" | WELL-0121 USD 25,794, WELL-0138 USD 46,942, WELL-0107 USD 5,176; depends on Q6 history | ✅ | ✅ | 28.1s | Passed after the Gemini 429, on the Ollama fallback. Run stopped before a finished JSON was written. |
| Q12 | U-201 | Correction: 15 Oct reading for WELL-0164 was wrong, re-measured 41.0 °C; then re-ask Q2 | INS-0164-0205; 24.0 → 41.0 °C; well now producing; inspector note "Pump jack not moving" shown and user asked to confirm; Q2 re-ask drops WELL-0164 | ✅ | ✅ | 46s | Q12 35.6s and Q12b 10.6s both passed after the 429 fallback. Run stopped at Q13, so this is a log line, not a saved JSON result. |
| Q13 | U-201 | Correction: WELL-0151 spill 4.5 → 5.4 bbl | INS-0151-0201; now reportable, SR-01 notice missing | ✅ | — | — | Targeted run was stopped here, just after the Gemini 429. No score. |
| Q14 | U-201 | RCA on pump jack in NE Block 7, then T350 rule "RCA after 2 repairs", then re-ask in a new chat | Step 1: WELL-0108, 0121, 0138. Step 3: WELL-0108, 0120, 0121, 0138; WELL-0102 (T200) absent; says it follows his rule | ✅ | — | — | In the targeted set, but the Gemini run was stopped before it, and the Ollama re-run timed out. |
| Q15 | U-202 | Is WELL-0164 producing now? (another user, after Q12/Q13); then U-401 asks about NE-8 reportable spills | WELL-0164 producing per Michael Turner's correction, with the dissenting note; then WELL-0151 only, flagged overdue | ✅ | — | — | In the targeted set, but the Gemini run was stopped before it, and the Ollama re-run timed out. |
| Q16 | U-201 | Is downtime planned for WELL-0007? (after a shared note from U-102); then U-202 repeats Q11 | Jake Morales, from 17 Oct for 10 days; U-202 must **not** see Michael's wells or costs | ✅ | — | — | In the targeted set, but the Gemini run was stopped before it, and the Ollama re-run timed out. |
| Q17 | U-204 | Claim: "WELL-0175 is running fine" (after Michael says it is shut in); then U-102 claims WELL-0001 is back online | Data shows not producing (Jessica Reed's 15 Oct inspection); claim kept as unconfirmed, user asked to confirm; second claim recorded unconfirmed | ✅ | — | — | Not in the targeted re-run. No finished Q1–Q18 live file. |
| Q18 | U-203 | Was the 20 Aug spill at WELL-0101 real oil? | INS-0101-0197; Ryan Brooks vs Chris Dalton (CTX-0101-002, -003); answer yes, with both views shown | ✅ | — | — | In the targeted set, but the Gemini run was stopped before it, and the Ollama re-run timed out. |

## 5. Summary of live results

The re-run did not produce a finished Q1–Q18 score.

- Gemini smoke test returned a one-word reply (not HTTP 429). `LLM_PROVIDER=gemini`. The Anthropic path is still in `providers.py` and was not called.
- Targeted command covered Q3, Q8, Q10, Q12, Q14, Q15, Q16, Q18 plus prerequisites Q6, Q11, Q13. On Gemini, Q3 (13.9s), Q8 (9.1s) and Q10 (6.9s) passed. The next cloud call returned HTTP 429 (quota). The runner logged a fallback to Ollama. The process was stopped there so it would not keep calling Gemini.
- A clean `--prefer ollama` run (`gemma4:12b`) did not replace that. One earlier Q3 passed in 110s; later turns, including another Q3, sat until the per-turn timeout (220–300s) and were killed. Those are timeouts, not wrong answers. No Ollama fallback count is meaningful for that run because Ollama was the only model.
- Questions not listed above were not scored in a finished re-run. An older Claude log, `eval/results/claude-run1.json`, scored 10/18 and is not repeated here as the live result.
- With more quota, the first thing to re-run is the same targeted set on Gemini, then one full Q1–Q18. The code changes below are already in place for the crashes and the empty-visual failures: DuckDB tools run one at a time, unknown or placeholder `record_*` calls return an error instead of raising, and the visual for a line, heatmap, RCA table, containment table, or regulator map is copied from the tool.

## 6. Known limits that affect these results

These come from the code and are true whatever the live numbers turn out to be:

- The fixture checks substrings and numbers, not whether the reasoning is right. A correct answer worded in an unlisted way can fail, and a thin answer that contains the right tokens can pass.
- Every live run goes through whichever provider is configured. Results from Claude, Gemini and the Ollama fallback are not interchangeable, so record which one produced each result file.
- The eval runs the agent in-process and skips HTTP, SSE streaming and the critic event. The streaming path and the critic ordering are covered by `backend/tests/test_app.py`, not by the eval.
- The eval does not measure the UI. Visual types are checked as data, not as rendered components. A manual walk through the UI (the `/dev/visuals` page and the live chat) is the check for that.

## 7. How to reproduce

```bash
uv sync && cp .env.example .env            # set one provider key; do not commit it
uv run python scripts/ingest.py "$DATASET_ROOT"
uv run python eval/run_eval.py --dry-run   # offline baseline, 18/18
uv run python eval/run_eval.py --json eval/results/live.json   # live run, ~26 agent turns
```
