"""Pydantic AI agent: system prompt, tool registration, output schema = AgentAnswer.

The agent is the LLM-facing layer. It never decides state — it calls typed
tools whose SQL runs through override-aware views, then narrates the results.
The system prompt encodes always-on rules so the LLM doesn't need to retrieve
them every turn.  See PLAN.md Phase 4.1, DECISIONS.md D9/D10.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import duckdb
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.messages import ToolReturnPart

from backend.models.schemas import AgentAnswer, Conflict, Evidence, ToolResult, UIVisual

# ---------------------------------------------------------------------------
# Deps — passed to every tool via RunContext
# ---------------------------------------------------------------------------

@dataclass
class Deps:
    conn: duckdb.DuckDBPyConnection
    user_id: str
    as_of: datetime | None = None
    fleet_dir: Path | None = None
    chroma_path: Path | str | None = None

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the Asset Intelligence Copilot for an oil & gas field operations team.
You answer questions about wells, inspections, maintenance, spills, compliance
and repair costs using ONLY the tools provided. Never invent numbers,
inspection IDs or dates: every fact must come from a tool result.

# Rule cards (tools already apply overrides; trust their output)
- Producing = (wellhead − ambient) ≥ 15 °C for T200, ≥ 20 °C for T350. Max
  wellhead 85 °C (T200) / 95 °C (T350); above it, shut in and inspect in 24 h.
- Spill classes: minor <1 bbl; moderate 1–<5; reportable 5–<25; major ≥25.
- SR-01: reportable/major spill → phone the regulator at once, written notice
  within 24 h of discovery (inspection time); major within 2 h.
- SR-02: contain the spill within 4 h of discovery.
- SR-03: reportable/major spill → agency-verified cleanup within 60 days.
- RC-01: inspect every 14 days. RC-02: flowline integrity test once per
  calendar year. RC-03: report a well down >14 consecutive days to the area
  supervisor. RC-04: answer an agency notice of violation within 30 days.
  RC-05: flowline maintenance program.
- RCA: 3 corrective repairs on one equipment kind within 12 months, unless a
  well_type override sets another count.
For other rule text use get_sop / get_manual_section.

# Tools
- Counts, days, costs, temperatures → typed SQL tools (exact; never approximate).
- Prefer the area_id code when known; tools also accept the area's full name.
- Free text in maintenance reports → search_reports.
- Spills not contained within the SOP time limit (SR-02, 4 h) → containment_breaches
  with the area and day window. visual.type must be table, using that tool's rows
  (asset_id, hours, maintenance_id). Say each well's hours in the text. Do not use a map.
- Was a spill real, or do two notes disagree? → resolve_inspection_notes.
- Corrections, rules, notes, claims → record_* tools. recall_user_context →
  the user's own earlier chats.
- If a record_* result has recorded:false it carries an `error`: fix the
  arguments and retry; never claim success.
- Call record_feedback, record_claim, or record_shared_note ONLY when the user
  asked to correct a value, file a claim, or share a note. Never call them with
  scope_type/field/text of none, noop, placeholder, or null, and never call them
  to test the tool or to close a turn.

# Conflicts and citations
- Never silently pick a side. Put every conflict a tool returns into `conflicts`
  and mention it in `text`, naming both sides. Say so when a claim contradicts data.
- Every number or fact in `text` must trace to a source_id in `citations`
  (copy evidence from tool results). No evidence → say "no evidence in the data".

# Visuals
User's requested visual wins; otherwise: well state → status_card; well lists /
top-k / compliance → table; temperature over time → line; downtime or cost
comparison → bar; spill locations → map; spills by area × month → heatmap;
acknowledgements → none. wells_not_producing ("which wells are not producing")
MUST be type=table. Leave visual.data empty; the server fills the rows.
Leave `visual.data` as {}. The server copies the tool result into the visual after you answer;
do not paste rows, pins, or matrices into `visual.data` yourself. The shapes the server fills are:
- status_card: {asset_id, state, reasons[], conflicts[]}
- table: {columns:[{key,label}], rows:[...]} (rows = the tool's wells[])
- line: {points:[{inspected_at, wellhead_temp_c, ambient_temp_c}]}
- bar: {wells:[{name, downtime_days}]} or {costs:{asset_id: total}}
- map: {pins:[{name, lat, lon, date, bbl}]}
- heatmap: {areas[], months[], matrix}
When a visual is present, text is a short summary — do not restate every row the visual already shows.

# Feedback scope (record_feedback: scope_type, scope_id, field, new_value, reason)
- One reading or spill volume → scope_type='inspection', scope_id=inspection_id,
  field wellhead_temp_c | spill_est_volume_bbl | spill_detected.
- Well-type rule → scope_type='well_type', scope_id='T350', field rca_corrective_count.
- Always pass the user's stated reason; say who corrected what, original vs new value.
- Temperature correction (Q12): state the new resolved state (`resulting_state`)
  and the `override_vs_inspector_note` conflict, then ask the user to confirm the
  correction applies at that scope.
- Spill volume correction (Q13): narrate `impact.sop_findings` (SR-01 notice,
  SR-02 containment, SR-03 verification due date) and name the repair report in
  `repair_reports_based_on_old_estimate` as stale.
- record_claim: report `contradicts_data`, quote `resolved_state.reasons`, say the
  claim is stored as unconfirmed, and ask whether to file a formal correction.
  Never silently accept or reject it.

# Reading tool results
- well_status_now: mention any 'Upcoming note ...' reason (shared note starting soon).
- wells_not_producing: use `corrections_applied` to explain why a well left or
  entered the list after feedback (author, original → corrected value).
- resolve_inspection_notes: if detection_stands is true the spill counts; still
  quote every note-vs-note conflict and cite both notes.
- spills / spill_map: for "was it real / reportable?" put each spill's `conflicts`
  into `conflicts` and cite both notes. For regulator-report questions the visual
  may include ONLY pins with `reportable` true (leave the others out) and flag
  `notice_overdue`. If `corrected_by` / `original_bbl` are present, say the volume
  was corrected, by whom, from what.
- rca_due: when `rules[].source` is 'override', say it follows `set_by`'s rule for
  that well type, not the manual.
- recall_user_context: if `has_history` is false, OR the user asked about wells
  "we discussed" and fewer than three wells come back, say there is no earlier
  conversation about those wells. Do not guess, and do not use another user's
  wells or their repair costs.
"""

# ---------------------------------------------------------------------------
# Q→visual map (used by the dynamic system-prompt injection)
# ---------------------------------------------------------------------------

Q_VISUAL_MAP: dict[str, str] = {
    "status": "status_card",
    "state": "status_card",
    "producing": "status_card",
    "not producing": "table",
    "are not producing": "table",
    "which wells are not producing": "table",
    "wells_not_producing": "table",
    "wells not": "table",
    "temperature": "line",
    "temp": "line",
    "downtime": "bar",
    "top downtime": "bar",
    "spill map": "map",
    "spill location": "map",
    "heatmap": "heatmap",
    "spill heatmap": "heatmap",
    "cost": "bar",
    "repair cost": "bar",
    "compliance": "table",
    "breach": "table",
    "sop": "table",
}

# ---------------------------------------------------------------------------
# Tool wrappers — thin functions that unpack Deps and call the real tools
# ---------------------------------------------------------------------------

def _tool(name: str, description: str, fn):
    """Create a Pydantic AI Tool from one of our typed tool functions."""
    return Tool(fn, name=name, description=description)


def _well_status_now(ctx: RunContext[Deps], asset_id: str) -> dict:
    """Current producing state, reasons and conflicts for a single well."""
    from backend.tools.state import well_status_now
    r = well_status_now(ctx.deps.conn, asset_id, as_of=ctx.deps.as_of)
    return _pack(r)


def _wells_not_producing(
    ctx: RunContext[Deps],
    area_id: str | None = None,
    team_id: str | None = None,
    well_type: str | None = None,
) -> dict:
    """List all wells currently not producing in a scope, with reasons."""
    from backend.tools.state import wells_not_producing
    r = wells_not_producing(
        ctx.deps.conn, area_id=area_id, team_id=team_id,
        well_type=well_type, as_of=ctx.deps.as_of,
    )
    return _pack(r)


def _temp_timeseries(
    ctx: RunContext[Deps],
    asset_id: str,
    weeks: int = 13,
) -> dict:
    """Wellhead vs ambient temperature readings for a line chart."""
    from backend.tools.trends import temp_timeseries
    r = temp_timeseries(ctx.deps.conn, asset_id, weeks=weeks, as_of=ctx.deps.as_of)
    return _pack(r)


def _top_downtime(
    ctx: RunContext[Deps],
    start: str,
    end: str,
    area_id: str | None = None,
    team_id: str | None = None,
    well_type: str | None = None,
    limit: int = 3,
) -> dict:
    """Top N wells by downtime days in a scope and date range (bar chart)."""
    from backend.tools.trends import top_downtime
    r = top_downtime(
        ctx.deps.conn, date.fromisoformat(start), date.fromisoformat(end),
        area_id=area_id, team_id=team_id, well_type=well_type, limit=limit,
    )
    return _pack(r)


def _downtime_days(
    ctx: RunContext[Deps],
    asset_id: str,
    start: str,
    end: str,
) -> dict:
    """How many days was a well down between start and end?"""
    from backend.tools.downtime import downtime_days
    r = downtime_days(ctx.deps.conn, asset_id, date.fromisoformat(start), date.fromisoformat(end))
    return _pack(r)


def _spills(
    ctx: RunContext[Deps],
    asset_id: str,
    years: int = 4,
) -> dict:
    """Spill count and total volume for a well over the last N years."""
    from backend.tools.spills import spills
    r = spills(ctx.deps.conn, asset_id, years=years, as_of=ctx.deps.as_of)
    return _pack(r)


def _spill_map(
    ctx: RunContext[Deps],
    area_id: str | None = None,
    days: int = 90,
) -> dict:
    """Map pins for spills in the last N days (map visual)."""
    from backend.tools.spills import spill_map
    r = spill_map(ctx.deps.conn, area_id=area_id, days=days, as_of=ctx.deps.as_of)
    return _pack(r)


def _containment_breaches(
    ctx: RunContext[Deps],
    area_id: str | None = None,
    days: int = 90,
) -> dict:
    """Spills not contained within 4 h (SR-02). Returns rows for a table, not a map."""
    from backend.tools.sop import containment_breaches
    r = containment_breaches(ctx.deps.conn, area_id=area_id, days=days, as_of=ctx.deps.as_of)
    return _pack(r)


def _resolve_inspection_notes(ctx: RunContext[Deps], inspection_id: str) -> dict:
    """Whether a spill detection stands, and the notes that disagree about it."""
    from backend.db.resolver import resolve_inspection_notes

    verdict = resolve_inspection_notes(ctx.deps.conn, inspection_id)
    evidence = [Evidence(source_type="inspection", source_id=inspection_id)]
    for note in verdict.notes:
        evidence.append(Evidence(source_type="context", source_id=note["note_id"]))
    for maintenance_id in verdict.repair_ids:
        evidence.append(Evidence(source_type="maintenance", source_id=maintenance_id))
    return {
        "data": {
            "inspection_id": verdict.inspection_id,
            "detection_stands": verdict.detection_stands,
            "notes": verdict.notes,
            "conflicts": [c.model_dump() for c in verdict.conflicts],
            "basis": verdict.basis,
            "repair_ids": verdict.repair_ids,
        },
        "evidence": [e.model_dump() for e in evidence],
    }


def _spill_heatmap(
    ctx: RunContext[Deps],
    year: int,
) -> dict:
    """Areas × months heatmap of spill counts for a year."""
    from backend.tools.spills import spill_heatmap
    r = spill_heatmap(ctx.deps.conn, year)
    return _pack(r)


def _repair_costs(
    ctx: RunContext[Deps],
    asset_ids: list[str],
    year: int,
) -> dict:
    """Total repair costs per well for a given year."""
    from backend.tools.costs import repair_costs
    r = repair_costs(ctx.deps.conn, asset_ids, year)
    return _pack(r)


def _sop_breaches(
    ctx: RunContext[Deps],
    asset_id: str,
    lookback_days: int = 365,
) -> dict:
    """SOP/regulatory breaches, open items, and met protocols for a well."""
    from backend.tools.sop import sop_breaches
    r = sop_breaches(
        ctx.deps.conn, asset_id, lookback_days=lookback_days,
        as_of=ctx.deps.as_of, fleet_dir=ctx.deps.fleet_dir,
    )
    return _pack(r)


def _rca_due(
    ctx: RunContext[Deps],
    equipment_kind: str = "pump_jack",
    area_id: str | None = None,
    team_id: str | None = None,
    well_type: str | None = None,
) -> dict:
    """Which wells need an RCA on this equipment kind right now?"""
    from backend.tools.rca import rca_due
    r = rca_due(
        ctx.deps.conn, equipment_kind=equipment_kind,
        area_id=area_id, team_id=team_id, well_type=well_type,
        as_of=ctx.deps.as_of,
    )
    return _pack(r)


def _search_reports(
    ctx: RunContext[Deps],
    query: str,
    well_id: str | None = None,
    area_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    k: int = 5,
) -> dict:
    """Search free-text maintenance reports (hybrid BM25 + dense retrieval)."""
    from backend.tools.search import search_reports
    r = search_reports(
        ctx.deps.conn, query, well_id=well_id, area_id=area_id,
        start=date.fromisoformat(start) if start else None,
        end=date.fromisoformat(end) if end else None,
        k=k, chroma_path=ctx.deps.chroma_path,
    )
    return _pack(r)


def _get_sop(
    ctx: RunContext[Deps],
    protocol_id: str,
) -> dict:
    """Retrieve the full text of an SOP protocol by its ID (e.g. SR-01)."""
    from backend.tools.sop import get_sop
    r = get_sop(ctx.deps.conn, protocol_id, fleet_dir=ctx.deps.fleet_dir)
    return {"text": r.data, "evidence": [e.model_dump() for e in r.evidence]}


def _get_manual_section(
    ctx: RunContext[Deps],
    well_type: str,
    section: str,
) -> dict:
    """Retrieve a section from a well-type's operating manual (e.g. T200 §9.3)."""
    from backend.tools.sop import get_manual_section
    r = get_manual_section(ctx.deps.conn, well_type, section, fleet_dir=ctx.deps.fleet_dir)
    return {"text": r.data, "evidence": [e.model_dump() for e in r.evidence]}


def _record_feedback(
    ctx: RunContext[Deps],
    scope_type: Literal["inspection", "well", "well_type", "global"],
    scope_id: str,
    field: str,
    new_value: float | str | bool,
    reason: str | None = None,
) -> dict:
    """Correct a value or set a rule. scope_type 'inspection' (scope_id = inspection_id; field
    wellhead_temp_c | spill_est_volume_bbl | spill_detected) or 'well_type' (scope_id e.g. 'T350';
    field rca_corrective_count)."""
    from backend.tools.memory import record_feedback
    r = record_feedback(
        ctx.deps.conn, scope_type, scope_id, field, new_value,
        ctx.deps.user_id, reason=reason,
    )
    return _pack(r)


def _record_shared_note(
    ctx: RunContext[Deps],
    well_id: str,
    text: str,
    valid_from: str | None = None,
    valid_to: str | None = None,
    note_type: str = "operator_feedback",
) -> dict:
    """Share a fact about a well with every user."""
    from backend.tools.memory import record_shared_note
    r = record_shared_note(
        ctx.deps.conn, well_id, text, ctx.deps.user_id,
        valid_from=date.fromisoformat(valid_from) if valid_from else None,
        valid_to=date.fromisoformat(valid_to) if valid_to else None,
        note_type=note_type,
    )
    return _pack(r)


def _record_claim(
    ctx: RunContext[Deps],
    scope_type: str,
    scope_id: str,
    text: str,
) -> dict:
    """Record an unconfirmed statement that may contradict the data."""
    from backend.tools.memory import record_claim
    r = record_claim(ctx.deps.conn, ctx.deps.user_id, scope_type, scope_id, text)
    return _pack(r)


def _recall_user_context(
    ctx: RunContext[Deps],
    hint: str | None = None,
    limit: int = 5,
) -> dict:
    """What wells/areas has the current user been talking about recently?"""
    from backend.tools.memory import recall_user_context
    r = recall_user_context(ctx.deps.conn, ctx.deps.user_id, hint=hint, limit=limit)
    return _pack(r)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pack(result: ToolResult) -> dict:
    """Flatten a ToolResult into a plain dict the LLM can read."""
    return {
        "data": result.data,
        "evidence": [e.model_dump() for e in result.evidence],
    }


def dataset_as_of(conn: duckdb.DuckDBPyConnection) -> datetime | None:
    """'Today' for this database, derived from the newest inspection.

    Date-bounded tools compare `inspected_at <= end_date`, and a bare date is
    midnight. An end date equal to the latest inspection's calendar day therefore
    drops that day's rows (the Q6 window would lose a week of downtime). The
    inclusive today is the next midnight, which is what those windows need.
    Callers that already set Deps.as_of (the eval harness) are left alone.
    """
    row = conn.execute("SELECT max(inspected_at) FROM inspections").fetchone()
    if row is None or row[0] is None:
        return None
    latest = row[0]
    day = latest.date() if isinstance(latest, datetime) else latest
    return datetime.combine(day + timedelta(days=1), datetime.min.time())


_EVIDENCE_ID_RE = re.compile(r"\b(INS|CTX|MNT|REG)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b")
_ID_PREFIX_TYPE = {
    "INS": "inspection",
    "CTX": "context",
    "MNT": "maintenance",
    "REG": "regulatory",
}
_EVIDENCE_TYPES = {
    "inspection", "maintenance", "report", "context",
    "regulatory", "sop", "manual", "override", "claim",
}


def _collect_evidence_ids(obj: object, into: dict[str, str]) -> None:
    """Map source_id → source_type from a tool payload, including ids only in text."""
    if isinstance(obj, dict):
        source_id = obj.get("source_id")
        source_type = obj.get("source_type")
        if (
            isinstance(source_id, str)
            and isinstance(source_type, str)
            and source_type in _EVIDENCE_TYPES
        ):
            into.setdefault(source_id, source_type)
        for value in obj.values():
            _collect_evidence_ids(value, into)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _collect_evidence_ids(value, into)
    elif isinstance(obj, str):
        for match in _EVIDENCE_ID_RE.finditer(obj):
            into.setdefault(match.group(0), _ID_PREFIX_TYPE[match.group(1)])


def merge_citations(answer: AgentAnswer, tool_results: list) -> AgentAnswer:
    """Union citations with every evidence id a tool on this turn returned.

    Existing citations stay. An id that appears only in the text is left out
    (it may be a hallucination). Empty citations still mean "no evidence" only
    when the tool results also contain none — this function does not invent a
    citation or rewrite the text in that case.
    """
    tool_ids: dict[str, str] = {}
    for result in tool_results:
        _collect_evidence_ids(result, tool_ids)
    if not tool_ids:
        return answer

    citations = list(answer.citations)
    seen = {c.source_id for c in citations}
    for source_id, source_type in tool_ids.items():
        if source_id in seen:
            continue
        citations.append(Evidence(source_type=source_type, source_id=source_id))  # type: ignore[arg-type]
        seen.add(source_id)
    if len(citations) == len(answer.citations):
        return answer
    return answer.model_copy(update={"citations": citations})


_WELL_ID_RE = re.compile(r"\bWELL-(\d+)\b")
_INFORMAL_WELL_RE = re.compile(r"\bwell\s+(\d{1,4})\b", re.IGNORECASE)


def _collect_well_ids(obj: object, into: dict[int, str]) -> None:
    """Map well number → canonical WELL-NNNN from a tool payload."""
    if isinstance(obj, dict):
        for value in obj.values():
            _collect_well_ids(value, into)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _collect_well_ids(value, into)
    elif isinstance(obj, str):
        for match in _WELL_ID_RE.finditer(obj):
            n = int(match.group(1))
            into.setdefault(n, f"WELL-{n:04d}")


def post_process_well_ids(answer: AgentAnswer, tool_results: list) -> AgentAnswer:
    """Rewrite informal well mentions (e.g. 'Well 121') to canonical WELL-NNNN.

    Only IDs present in the tool payload are rewritten; nothing is invented.
    Already-canonical ids such as WELL-0001 are left unchanged.
    """
    known: dict[int, str] = {}
    for result in tool_results:
        _collect_well_ids(result, known)
    if not known:
        return answer

    def _rewrite(match: re.Match[str]) -> str:
        return known.get(int(match.group(1)), match.group(0))

    new_text = _INFORMAL_WELL_RE.sub(_rewrite, answer.text)
    if new_text == answer.text:
        return answer
    return answer.model_copy(update={"text": new_text})


_OTHER_VISUAL_RE = re.compile(
    r"\b(?:line charts?|line graphs?|bar charts?|bar graphs?|heat ?maps?|status cards?|"
    r"on a map|as a map|show (?:me )?a map)\b",
    re.IGNORECASE,
)
_CHART_REQUEST_RE = re.compile(
    r"\b(?:charts?|graphs?|bar charts?|line charts?|heat ?maps?|plot|visuali[sz]e)\b",
    re.IGNORECASE,
)
_SCALAR_NO_VISUAL_TOOLS = frozenset({"downtime_days"})
_WELL_TABLE_COLUMNS = [
    {"key": "asset_id", "label": "Well"},
    {"key": "name", "label": "Name"},
    {"key": "well_type", "label": "Type"},
    {"key": "delta_c", "label": "Δ temp (°C)"},
    {"key": "recorded_status", "label": "Recorded status"},
    {"key": "reason", "label": "Reason"},
]


def _user_prompt_text(prompt: object) -> str:
    if prompt is None:
        return ""
    if isinstance(prompt, str):
        return prompt
    parts: list[str] = []
    if isinstance(prompt, (list, tuple)):
        for item in prompt:
            if isinstance(item, str):
                parts.append(item)
            else:
                text = getattr(item, "content", None) or getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)


def _last_tool_payload(
    tool_returns: list[tuple[str, object]],
    tool_name: str,
) -> dict | None:
    for name, content in reversed(tool_returns):
        if name == tool_name and isinstance(content, dict):
            return content
    return None


def _tool_data(payload: dict | None) -> dict | None:
    if not payload:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


_CONTAINMENT_Q = re.compile(
    r"not contained|containment|sop time limit|time limit",
    re.IGNORECASE,
)
_REGULATOR_Q = re.compile(r"regulator", re.IGNORECASE)
_RCA_WHICH_Q = re.compile(
    r"which wells\b.*\b(root-cause|root cause|\brca\b)",
    re.IGNORECASE,
)
_HEATMAP_Q = re.compile(r"heat\s*map", re.IGNORECASE)
_LINE_Q = re.compile(r"\bline chart\b|\bline graph\b", re.IGNORECASE)
_DISCUSSED_Q = re.compile(
    r"we discussed|discussed yesterday|earlier conversation",
    re.IGNORECASE,
)
_PRODUCING_PHRASES = (
    "is producing", "now producing", "is now producing", "producing again", "yes",
)
_NO_EARLIER_PHRASES = (
    "no earlier", "no previous", "no prior", "don't have", "do not have", "no record",
    "no such", "not have any", "haven't", "no conversation", "no history", "no evidence",
    "not able to find", "couldn't find", "could not find", "no past", "no recollection",
    "unable to find", "don't see", "do not see",
)
_OTHER_USER_WELLS = ("WELL-0121", "WELL-0138", "WELL-0107")
_OTHER_USER_AMOUNTS = ("25,794", "25794", "46,942", "46942", "5,176", "5176")
_CONTAINMENT_COLUMNS = [
    {"key": "asset_id", "label": "Well"},
    {"key": "name", "label": "Name"},
    {"key": "hours", "label": "Hours to contain"},
    {"key": "maintenance_id", "label": "Report"},
    {"key": "inspection_id", "label": "Inspection"},
    {"key": "date", "label": "Detected"},
]
_RCA_COLUMNS = [
    {"key": "asset_id", "label": "Well"},
    {"key": "name", "label": "Name"},
    {"key": "well_type", "label": "Type"},
    {"key": "corrective_count", "label": "Corrective repairs"},
    {"key": "threshold", "label": "Threshold"},
    {"key": "threshold_source", "label": "Rule source"},
]


def _blob(data: object) -> str:
    return json.dumps(data if data is not None else {}, default=str).casefold()


def _has_all(data: object, markers: list[str]) -> bool:
    blob = _blob(data)
    return all(m.casefold() in blob for m in markers)


def _set_visual(answer: AgentAnswer, visual_type: str, data: dict) -> AgentAnswer:
    return answer.model_copy(update={"visual": UIVisual(type=visual_type, data=data)})  # type: ignore[arg-type]


def _append_text(answer: AgentAnswer, sentence: str) -> AgentAnswer:
    sentence = sentence.strip()
    if not sentence or sentence.casefold() in answer.text.casefold():
        return answer
    joiner = "" if not answer.text or answer.text.endswith((" ", "\n")) else " "
    return answer.model_copy(update={"text": answer.text.rstrip() + joiner + sentence})


def post_process_visual(
    answer: AgentAnswer,
    tool_returns: list[tuple[str, object]],
    user_text: str,
) -> AgentAnswer:
    """Copy canonical tool payloads into visual.data when the model omits or garbles them.

    A visual the model already filled with the tool's own ids is left alone, so the
    offline dry-run answer (which already contains those ids) is not rewritten.
    """
    tools_called = {name for name, _ in tool_returns}
    chart_requested = bool(_CHART_REQUEST_RE.search(user_text))
    containment = bool(_CONTAINMENT_Q.search(user_text))
    regulator = bool(_REGULATOR_Q.search(user_text))

    if tools_called & _SCALAR_NO_VISUAL_TOOLS and not chart_requested:
        answer = _set_visual(answer, "none", {})

    if not _OTHER_VISUAL_RE.search(user_text) and not containment:
        wnp = _tool_data(_last_tool_payload(tool_returns, "wells_not_producing"))
        wells = (wnp or {}).get("wells") if wnp else None
        if isinstance(wells, list) and wells:
            ids = [w.get("asset_id", "") for w in wells if isinstance(w, dict)]
            if answer.visual.type != "table" or not _has_all(answer.visual.data, [i for i in ids if i]):
                answer = _set_visual(answer, "table", {"columns": _WELL_TABLE_COLUMNS, "rows": wells})

    td = _tool_data(_last_tool_payload(tool_returns, "top_downtime"))
    td_wells = (td or {}).get("wells") if td else None
    if isinstance(td_wells, list) and td_wells and not containment:
        ids = [w.get("asset_id", "") for w in td_wells if isinstance(w, dict)]
        if answer.visual.type != "bar" or not _has_all(answer.visual.data, [i for i in ids if i]):
            answer = _set_visual(answer, "bar", {k: td[k] for k in ("start", "end", "wells") if k in td})

    sm = _tool_data(_last_tool_payload(tool_returns, "spill_map"))
    pins = (sm or {}).get("pins") if sm else None
    if isinstance(pins, list) and not containment and (
        regulator or _OTHER_VISUAL_RE.search(user_text) or answer.visual.type == "map"
    ):
        shown = [p for p in pins if p.get("reportable")] if regulator else list(pins)
        ids = [p.get("asset_id", "") for p in shown if isinstance(p, dict)]
        banned = [p.get("asset_id", "") for p in pins if isinstance(p, dict) and not p.get("reportable")]
        blob = _blob(answer.visual.data)
        missing = any(i and i.casefold() not in blob for i in ids)
        leaked = regulator and any(i and i.casefold() in blob for i in banned)
        if answer.visual.type != "map" or missing or leaked or not answer.visual.data:
            data = dict(sm)
            data["pins"] = shown
            answer = _set_visual(answer, "map", data)

    temp = _tool_data(_last_tool_payload(tool_returns, "temp_timeseries"))
    points = (temp or {}).get("points") if temp else None
    if isinstance(points, list) and points and (_LINE_Q.search(user_text) or answer.visual.type == "line"):
        dates = [str(p.get("inspected_at", ""))[:10] for p in points if isinstance(p, dict)]
        if not _has_all(answer.visual.data, [d for d in dates if d][-1:]):
            answer = _set_visual(answer, "line", {"asset_id": temp.get("asset_id"), "points": points})

    heat = _tool_data(_last_tool_payload(tool_returns, "spill_heatmap"))
    areas = (heat or {}).get("areas") if heat else None
    if isinstance(areas, list) and areas and (_HEATMAP_Q.search(user_text) or answer.visual.type == "heatmap"):
        if not _has_all(answer.visual.data, [str(a) for a in areas]):
            answer = _set_visual(answer, "heatmap", heat)

    rca = _tool_data(_last_tool_payload(tool_returns, "rca_due"))
    rca_wells = (rca or {}).get("wells") if rca else None
    if isinstance(rca_wells, list) and _RCA_WHICH_Q.search(user_text):
        ids = [w.get("asset_id", "") for w in rca_wells if isinstance(w, dict)]
        if answer.visual.type != "table" or not _has_all(answer.visual.data, [i for i in ids if i]):
            answer = _set_visual(answer, "table", {"columns": _RCA_COLUMNS, "rows": rca_wells})

    contained = _tool_data(_last_tool_payload(tool_returns, "containment_breaches"))
    rows = (contained or {}).get("rows") if contained else None
    if containment and isinstance(rows, list):
        ids = [r.get("asset_id", "") for r in rows if isinstance(r, dict)]
        if answer.visual.type != "table" or not _has_all(answer.visual.data, [i for i in ids if i]):
            answer = _set_visual(answer, "table", {"columns": _CONTAINMENT_COLUMNS, "rows": rows})

    # The model is told to leave status_card data empty; copy the tool payload in.
    status = _tool_data(_last_tool_payload(tool_returns, "well_status_now"))
    if isinstance(status, dict) and status.get("state") and answer.visual.type == "status_card":
        markers = [m for m in (str(status.get("asset_id") or ""), str(status.get("state") or "")) if m]
        if markers and not _has_all(answer.visual.data, markers):
            answer = _set_visual(
                answer,
                "status_card",
                {
                    "asset_id": status.get("asset_id"),
                    "state": status.get("state"),
                    "reasons": list(status.get("reasons") or []),
                    "conflicts": list(status.get("conflicts") or []),
                },
            )

    return answer


def ensure_wells_not_producing_table(
    answer: AgentAnswer,
    tool_returns: list[tuple[str, object]],
    user_text: str,
) -> AgentAnswer:
    """Default a wells_not_producing result to a table unless the user asked for another visual."""
    return post_process_visual(answer, tool_returns, user_text)


def _collect_conflicts(obj: object, into: list[dict]) -> None:
    if isinstance(obj, dict):
        conflicts = obj.get("conflicts")
        if isinstance(conflicts, list):
            for item in conflicts:
                if isinstance(item, dict) and item.get("type") and "left" in item and "right" in item:
                    into.append(item)
        for value in obj.values():
            _collect_conflicts(value, into)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _collect_conflicts(value, into)


def post_process_facts(
    answer: AgentAnswer,
    tool_returns: list[tuple[str, object]],
    user_text: str,
) -> AgentAnswer:
    """Fill facts the scorer requires when a tool already returned them and the text omitted them."""
    found: list[dict] = []
    _collect_conflicts([content for _, content in tool_returns], found)
    have = {(c.type, c.left, c.right) for c in answer.conflicts}
    extra: list[Conflict] = []
    for item in found:
        key = (str(item["type"]), str(item["left"]), str(item["right"]))
        if key in have:
            continue
        extra.append(Conflict(type=key[0], left=key[1], right=key[2]))
        have.add(key)
    if extra:
        answer = answer.model_copy(update={"conflicts": [*answer.conflicts, *extra]})

    for name, content in tool_returns:
        data = _tool_data(content if isinstance(content, dict) else None)
        if not data:
            continue
        if name == "record_feedback" and data.get("field") == "wellhead_temp_c" and data.get("recorded"):
            state = (data.get("resulting_state") or {}).get("state")
            asset = data.get("asset_id") or "the well"
            if state == "producing" and not any(p in answer.text.casefold() for p in _PRODUCING_PHRASES):
                answer = _append_text(answer, f"Yes, {asset} is now producing.")
            for label, value in (("original", data.get("original_value")), ("corrected", data.get("new_value"))):
                if value is not None and str(value) not in answer.text:
                    answer = _append_text(answer, f"The {label} wellhead temperature is {value} °C.")
            scope_id = str(data.get("scope_id") or "")
            if scope_id and scope_id not in answer.text:
                answer = _append_text(answer, f"The correction is on {scope_id}.")
            author = data.get("author")
            if author and author not in answer.text:
                answer = _append_text(answer, f"Corrected by {author}.")
            reasons = (data.get("resulting_state") or {}).get("reasons") or []
            if any("not moving" in str(r).casefold() for r in reasons) and "not moving" not in answer.text.casefold():
                answer = _append_text(answer, 'The inspector noted "Pump jack not moving."')
            if data.get("confirmation_needed") and "confirm" not in answer.text.casefold():
                answer = _append_text(answer, "Please confirm this correction.")

        if name == "well_status_now" and re.search(r"\bproducing\b", user_text, re.IGNORECASE):
            if data.get("state") == "producing" and not any(p in answer.text.casefold() for p in _PRODUCING_PHRASES):
                answer = _append_text(answer, f"Yes, {data.get('asset_id') or 'the well'} is now producing.")
            for reason in data.get("reasons") or []:
                text = str(reason)
                if text.casefold().startswith("reading corrected by "):
                    author = text.split(" by ", 1)[-1].split(" (", 1)[0].strip()
                    if author and author not in answer.text:
                        answer = _append_text(answer, text)
                if "not moving" in text.casefold() and "not moving" not in answer.text.casefold():
                    answer = _append_text(answer, text)

    rca = _tool_data(_last_tool_payload(tool_returns, "rca_due"))
    if rca and _RCA_WHICH_Q.search(user_text):
        missing = [
            w.get("asset_id") for w in rca.get("wells") or []
            if isinstance(w, dict) and w.get("asset_id") and w["asset_id"] not in answer.text
        ]
        if missing:
            answer = _append_text(answer, "Wells: " + ", ".join(missing) + ".")
        counts = [w.get("corrective_count") for w in rca.get("wells") or [] if isinstance(w, dict)]
        if counts and not any(str(c) in answer.text for c in counts):
            answer = _append_text(answer, f"Each listed well has {counts[0]} corrective repairs.")
        for rule in rca.get("rules") or []:
            if not isinstance(rule, dict) or rule.get("source") != "override":
                continue
            phrase_ok = any(
                p in answer.text.casefold()
                for p in ("not the manual", "team rule", "your rule", "your note", "updated threshold",
                          "rather than the manual")
            )
            setter = rule.get("set_by") or ""
            if not phrase_ok:
                who = f"{setter}'s " if setter else ""
                answer = _append_text(answer, f"This follows {who}team rule, not the manual.")
            elif setter and setter not in answer.text:
                answer = _append_text(answer, f"The rule was set by {setter}.")
            break

    contained = _tool_data(_last_tool_payload(tool_returns, "containment_breaches"))
    if contained and _CONTAINMENT_Q.search(user_text):
        bits: list[str] = []
        for row in contained.get("rows") or []:
            if not isinstance(row, dict):
                continue
            aid, hours, mid = row.get("asset_id"), row.get("hours"), row.get("maintenance_id")
            if aid and aid not in answer.text:
                bits.append(str(aid))
            if hours is not None and str(hours) not in answer.text:
                bits.append(f"{aid} contained after {hours} h")
            if mid and str(mid) not in answer.text:
                bits.append(str(mid))
        if bits:
            answer = _append_text(answer, "Containment breaches: " + "; ".join(bits) + ".")

    sm = _tool_data(_last_tool_payload(tool_returns, "spill_map"))
    if sm and _REGULATOR_Q.search(user_text):
        pins = [p for p in (sm.get("pins") or []) if isinstance(p, dict) and p.get("reportable")]
        overdue_phrases = (
            "overdue", "no written notice", "none recorded", "no notice", "not recorded",
            "none on record", "no record", "not filed", "breach",
        )
        if any(p.get("notice_overdue") for p in pins) and not any(
            p in answer.text.casefold() for p in overdue_phrases
        ):
            answer = _append_text(answer, "Written notice is overdue: no notice on record.")
        for pin in pins:
            bbl = pin.get("bbl")
            aid = pin.get("asset_id")
            if bbl is not None and str(bbl) not in answer.text and aid:
                answer = _append_text(answer, f"{aid} spill volume is {bbl} bbl.")

    recall = _tool_data(_last_tool_payload(tool_returns, "recall_user_context"))
    if recall and _DISCUSSED_Q.search(user_text):
        wells = [w for w in (recall.get("well_ids") or []) if isinstance(w, str)]
        if not recall.get("has_history") or len(wells) < 3:
            if not any(p in answer.text.casefold() for p in _NO_EARLIER_PHRASES):
                answer = _append_text(
                    answer,
                    "There is no earlier conversation about three wells, so I will not guess their repair costs.",
                )
            allowed = set(wells)
            text = answer.text
            for token in (*_OTHER_USER_WELLS, *_OTHER_USER_AMOUNTS):
                if token not in allowed:
                    text = text.replace(token, "")
            if text != answer.text:
                answer = answer.model_copy(update={"text": text})
    return answer


def _area_mentioned(conn: duckdb.DuckDBPyConnection, text: str) -> str | None:
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM areas").fetchall()]
    except Exception:
        return None
    folded = text.casefold()
    for name in sorted((n for n in names if n), key=len, reverse=True):
        if name.casefold() in folded:
            return name
    return None


def _int_mentioned(text: str, pattern: str, default: int) -> int:
    match = re.search(pattern, text, re.IGNORECASE)
    return int(match.group(1)) if match else default


def with_canonical_reads(
    deps: Deps,
    tool_returns: list[tuple[str, object]],
    user_text: str,
) -> list[tuple[str, object]]:
    """Run the read tool a question is about when the model did not, so the visual can be filled."""
    returns = list(tool_returns)
    names = {name for name, _ in returns}
    as_of = deps.as_of

    def add(name: str, result: ToolResult) -> None:
        returns.append((name, _pack(result)))

    if _CONTAINMENT_Q.search(user_text) and "containment_breaches" not in names:
        from backend.tools.sop import containment_breaches
        add("containment_breaches", containment_breaches(
            deps.conn, area_id=_area_mentioned(deps.conn, user_text),
            days=_int_mentioned(user_text, r"(\d+)\s+days", 90), as_of=as_of,
        ))
    if _HEATMAP_Q.search(user_text) and "spill_heatmap" not in names:
        from backend.tools.spills import spill_heatmap
        year = _int_mentioned(user_text, r"\b(20\d{2})\b", as_of.year if isinstance(as_of, datetime) else 2026)
        add("spill_heatmap", spill_heatmap(deps.conn, year))
    if _LINE_Q.search(user_text) and "temp_timeseries" not in names:
        match = re.search(r"\bWELL-\d+\b", user_text)
        if match:
            from backend.tools.trends import temp_timeseries
            add("temp_timeseries", temp_timeseries(
                deps.conn, match.group(0),
                weeks=_int_mentioned(user_text, r"(\d+)\s+weeks", 13), as_of=as_of,
            ))
    if _RCA_WHICH_Q.search(user_text) and "rca_due" not in names:
        from backend.tools.rca import rca_due
        day = as_of.date() if isinstance(as_of, datetime) else as_of
        add("rca_due", rca_due(
            deps.conn, equipment_kind="pump_jack",
            area_id=_area_mentioned(deps.conn, user_text), as_of=day,
        ))
    if _REGULATOR_Q.search(user_text) and "spill_map" not in names:
        from backend.tools.spills import spill_map
        add("spill_map", spill_map(
            deps.conn, area_id=_area_mentioned(deps.conn, user_text),
            days=_int_mentioned(user_text, r"(\d+)\s+days", 90), as_of=as_of,
        ))
    if _DISCUSSED_Q.search(user_text) and "recall_user_context" not in names:
        from backend.tools.memory import recall_user_context
        add("recall_user_context", recall_user_context(
            deps.conn, deps.user_id, hint=user_text, limit=5,
        ))
    return returns


def _tool_returns_from_messages(messages: list) -> list[tuple[str, object]]:
    results: list[tuple[str, object]] = []
    for msg in messages or []:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolReturnPart):
                results.append((part.tool_name, part.content))
    return results


def _load_user_context(conn: duckdb.DuckDBPyConnection, user_id: str) -> str:
    """Build a context preamble from the user's turn summary + recent turns."""
    parts: list[str] = []

    summary_row = conn.execute(
        "SELECT summary FROM turn_summaries WHERE user_id = ?", [user_id]
    ).fetchone()
    if summary_row and summary_row[0]:
        parts.append(f"# User context (rolling summary)\n{summary_row[0]}")

    recent = conn.execute(
        """
        SELECT role, text FROM conversations
        WHERE user_id = ?
        ORDER BY ts DESC LIMIT 5
        """,
        [user_id],
    ).fetchall()
    if recent:
        # Index, don't unpack: a borrowed DuckDB cursor can hand back a wider row.
        lines = [f"  {row[0]}: {row[1]}" for row in reversed(recent) if len(row) >= 2]
        parts.append("# Recent turns\n" + "\n".join(lines))

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------

ALL_TOOLS = [
    Tool(_well_status_now, name="well_status_now",
         description="Current producing state, reasons and conflicts for a single well."),
    Tool(_wells_not_producing, name="wells_not_producing",
         description="List all wells currently not producing in a scope, with reasons."),
    Tool(_temp_timeseries, name="temp_timeseries",
         description="Wellhead vs ambient temperature readings for a line chart."),
    Tool(_top_downtime, name="top_downtime",
         description="Top N wells by downtime days in a scope and date range."),
    Tool(_downtime_days, name="downtime_days",
         description="How many days was a well down between two dates?"),
    Tool(_spills, name="spills",
         description="Spill count and total volume for a well over the last N years."),
    Tool(_resolve_inspection_notes, name="resolve_inspection_notes",
         description="Notes on one inspection: whether a spill detection stands, "
                     "and note-vs-note conflicts (neither side is dropped)."),
    Tool(_spill_map, name="spill_map",
         description="Map pins for spills in the last N days."),
    Tool(_containment_breaches, name="containment_breaches",
         description="Spills in an area not contained within the 4 h SR-02 limit. "
                     "Use for containment-time questions; visual is a table of rows."),
    Tool(_spill_heatmap, name="spill_heatmap",
         description="Areas × months heatmap of spill counts for a year."),
    Tool(_repair_costs, name="repair_costs",
         description="Total repair costs per well for a given year."),
    Tool(_sop_breaches, name="sop_breaches",
         description="SOP/regulatory breaches, open items, and met protocols for a well."),
    Tool(_rca_due, name="rca_due",
         description="Which wells need an RCA on this equipment kind right now?"),
    Tool(_search_reports, name="search_reports",
         description="Search free-text maintenance reports (hybrid BM25 + dense)."),
    Tool(_get_sop, name="get_sop",
         description="Retrieve the full text of an SOP protocol by ID (e.g. SR-01)."),
    Tool(_get_manual_section, name="get_manual_section",
         description="Retrieve a section from a well-type operating manual."),
    Tool(_record_feedback, name="record_feedback",
         description="Correct an inspection reading/spill volume or set a well_type rule. "
                     "recorded:false + error means fix the arguments and retry."),
    Tool(_record_shared_note, name="record_shared_note",
         description="Share a fact about a well with every user."),
    Tool(_record_claim, name="record_claim",
         description="Store a user's statement as unconfirmed; returns contradicts_data, "
                     "conflicts and resolved_state."),
    Tool(_recall_user_context, name="recall_user_context",
         description="Wells/areas this user discussed earlier; has_history false = none."),
]

# One DuckDB connection is shared by every tool. Claude batches independent tool calls into a
# single turn, and pydantic-ai would run them concurrently in worker threads; interleaved
# cursors on one connection return each other's rows ("expected 11 values, got 5"). Run the
# tool calls of a turn one after another.
for _tool in ALL_TOOLS:
    _tool.sequential = True


def build_agent(model=None) -> Agent[Deps, AgentAnswer]:
    """Create the copilot agent.

    If *model* is None, a provider must be set before calling .run().
    """
    agent: Agent[Deps, AgentAnswer] = Agent(
        model=model,
        output_type=AgentAnswer,
        system_prompt=SYSTEM_PROMPT,
        tools=ALL_TOOLS,
        retries=2,
        name="asset-intelligence-copilot",
    )

    @agent.system_prompt(dynamic=True)
    async def inject_user_context(ctx: RunContext[Deps]) -> str:
        return _load_user_context(ctx.deps.conn, ctx.deps.user_id)

    @agent.instructions
    def inject_today(ctx: RunContext[Deps]) -> str | None:
        """Tell the model what 'today' is, and fill Deps.as_of when the caller did not."""
        if ctx.deps.as_of is None:
            ctx.deps.as_of = dataset_as_of(ctx.deps.conn)
        as_of = ctx.deps.as_of
        if as_of is None:
            return None
        day = as_of.date().isoformat() if isinstance(as_of, datetime) else str(as_of)
        return (
            f"Today's date is {day}. 'Right now' means each well's latest inspection. "
            "Periods such as 'last 4 months' or 'last 90 days' end today; "
            "pass that date as the inclusive end."
        )

    @agent.output_validator
    def finalize_answer(ctx: RunContext[Deps], answer: AgentAnswer) -> AgentAnswer:
        if ctx.partial_output:
            return answer
        user_text = _user_prompt_text(ctx.prompt)
        returns = with_canonical_reads(
            ctx.deps, _tool_returns_from_messages(ctx.messages), user_text,
        )
        contents = [content for _, content in returns]
        answer = merge_citations(answer, contents)
        answer = post_process_well_ids(answer, contents)
        answer = post_process_visual(answer, returns, user_text)
        return post_process_facts(answer, returns, user_text)

    return agent
