"""Critic pass: verifies agent answers against tool evidence before they ship.

Four deterministic checks run locally (no LLM needed):
  (a) Every number in `text` appears in at least one tool result.
  (b) Every `citations[].source_id` resolves to a real record in the DB.
  (c) If `conflicts` is empty, verify no active override would flip the state.
  (d) `visual.type` is consistent with the Q→visual heuristic.

An optional LLM-based pass (Gemma 4 via AI Studio) can run on top for
deeper semantic checks; the deterministic checks alone are sufficient for
the unit-test acceptance criterion.

See PLAN.md Phase 4.2, DECISIONS.md D12.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import duckdb

from backend.models.schemas import AgentAnswer

# Maps Evidence.source_type → (table_name, id_column)
_SOURCE_TABLE: dict[str, tuple[str, str]] = {
    "inspection": ("inspections", "inspection_id"),
    "maintenance": ("maintenance", "maintenance_id"),
    "report": ("maintenance", "maintenance_id"),
    "context": ("context_notes", "note_id"),
    "regulatory": ("regulatory_events", "event_id"),
    "sop": (None, None),      # file-based, not in DB
    "manual": (None, None),   # file-based, not in DB
    "override": ("overrides", "id"),
    "claim": ("shared_claims", "id"),
}

_NUMBER_RE = re.compile(r"[+-]?\d[\d,]*\.?\d*")
_STRUCTURED_ID_RE = re.compile(r"\b[A-Z]{2,5}-[\w-]+\b")
# Dates are not measurements. Pull them out before the number regex, which
# otherwise splits 2026-10-03 into 2026 / -10 / -03.
_ISO_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_PROSE_DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)"
    r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|"
    r"July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|"
    r"Aug|Sept|Sep|Oct|Nov|Dec)\.?\s+\d{4}\b",
    re.IGNORECASE,
)


@dataclass
class CriticResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    must_retry: bool = False


def _scrub_dates(text: str) -> str:
    return _PROSE_DATE_RE.sub(" ", _ISO_DATE_RE.sub(" ", text))


def _normalize_number(raw: str) -> str | None:
    """Drop list-marker periods, signs, and leading zeros. 4.10 and 4.1 match."""
    s = raw.replace(",", "").rstrip(".")
    if s[:1] in "+-":
        s = s[1:].rstrip(".")
    if not s or not any(ch.isdigit() for ch in s):
        return None
    if "." in s:
        whole, frac = s.split(".", 1)
        whole = whole.lstrip("0") or "0"
        frac = frac.rstrip("0")
        if not frac:
            return whole
        return f"{whole}.{frac}"
    return s.lstrip("0") or "0"


def _extract_numbers(text: str) -> set[str]:
    """Pull every number-like token from text, normalised (no commas).

    Strips dates and structured IDs (WELL-0001, INS-0001-0203, etc.) first so
    their numeric segments aren't treated as data claims.
    """
    cleaned = _STRUCTURED_ID_RE.sub(" ", _scrub_dates(text))
    results: set[str] = set()
    for raw in _NUMBER_RE.findall(cleaned):
        norm = _normalize_number(raw)
        if norm:
            results.add(norm)
    return results


def _with_rounded(numbers: set[str]) -> set[str]:
    """Add 1- and 2-decimal forms so 4.100000000000001 grounds a written 4.1."""
    extra: set[str] = set()
    for n in numbers:
        if "." not in n:
            continue
        try:
            value = float(n)
        except ValueError:
            continue
        for places in (1, 2):
            rounded = _normalize_number(f"{value:.{places}f}")
            if rounded:
                extra.add(rounded)
    return numbers | extra


@lru_cache(maxsize=1)
def _rule_card_numbers() -> frozenset[str]:
    """Thresholds printed in the always-on rule card are grounded by definition."""
    from backend.agent import SYSTEM_PROMPT

    start = SYSTEM_PROMPT.find("# Rule cards")
    end = SYSTEM_PROMPT.find("\n# Tools")
    section = SYSTEM_PROMPT[start:end] if start != -1 and end != -1 else ""
    return frozenset(_extract_numbers(section))


def check_numbers_grounded(
    answer: AgentAnswer,
    tool_results: list[dict],
) -> list[str]:
    """(a) Every number in answer.text must appear in a tool result or the rule card."""
    answer_nums = _extract_numbers(answer.text)
    if not answer_nums:
        return []

    grounded: set[str] = set(_rule_card_numbers())
    for tr in tool_results:
        grounded |= _with_rounded(_extract_numbers(str(tr)))

    ungrounded = answer_nums - grounded
    if ungrounded:
        return [f"Numbers in text not found in any tool result: {sorted(ungrounded)}"]
    return []


def check_citations_exist(
    answer: AgentAnswer,
    conn: duckdb.DuckDBPyConnection,
) -> list[str]:
    """(b) Every citation source_id must resolve to a real DB record."""
    issues: list[str] = []
    for cit in answer.citations:
        mapping = _SOURCE_TABLE.get(cit.source_type)
        if mapping is None:
            issues.append(f"Unknown source_type '{cit.source_type}' for {cit.source_id}")
            continue
        table, col = mapping
        if table is None:
            continue
        if col == "id":
            try:
                sid = int(cit.source_id)
            except (ValueError, TypeError):
                issues.append(
                    f"Citation {cit.source_id} ({cit.source_type}): "
                    f"non-integer id for {table}"
                )
                continue
            row = conn.execute(f"SELECT 1 FROM {table} WHERE {col} = ?", [sid]).fetchone()
        else:
            row = conn.execute(f"SELECT 1 FROM {table} WHERE {col} = ?", [cit.source_id]).fetchone()
        if row is None:
            issues.append(f"Citation {cit.source_id} ({cit.source_type}) not found in {table}")
    return issues


def check_missing_conflicts(
    answer: AgentAnswer,
    conn: duckdb.DuckDBPyConnection,
) -> list[str]:
    """(c) If conflicts is empty, check that no override could flip things."""
    if answer.conflicts:
        return []

    well_ids: list[str] = []
    for cit in answer.citations:
        if cit.source_type == "inspection":
            row = conn.execute(
                "SELECT asset_id FROM inspections WHERE inspection_id = ?",
                [cit.source_id],
            ).fetchone()
            if row:
                well_ids.append(row[0])

    for well_id in set(well_ids):
        override_count = conn.execute(
            """
            SELECT count(*) FROM overrides
            WHERE scope_type = 'inspection'
              AND scope_id IN (
                  SELECT inspection_id FROM inspections WHERE asset_id = ?
              )
            """,
            [well_id],
        ).fetchone()[0]
        if override_count > 0:
            return [
                (
                    f"Answer has no conflicts but {well_id} has {override_count} "
                    f"inspection override(s) — verify the state accounts for them."
                )
            ]
    return []


def check_visual_type(
    answer: AgentAnswer,
    user_question: str,
) -> list[str]:
    """(d) Visual type should be consistent with the question intent."""
    from backend.agent import Q_VISUAL_MAP

    vtype = answer.visual.type
    if vtype == "none":
        return []
    q_lower = user_question.lower()

    # Longest keyword wins: "not producing" contains "producing", and the
    # shorter key would otherwise demand a status card for a well list.
    matches = [(kw, exp) for kw, exp in Q_VISUAL_MAP.items() if kw in q_lower]
    if not matches:
        return []
    keyword, expected = max(matches, key=lambda pair: len(pair[0]))
    if vtype != expected:
        return [
            (
                f"Question mentions '{keyword}' suggesting visual='{expected}', "
                f"but got visual='{vtype}'."
            )
        ]
    return []


def run_critic(
    user_question: str,
    answer: AgentAnswer,
    tool_results: list[dict],
    conn: duckdb.DuckDBPyConnection,
) -> CriticResult:
    """Run all four deterministic critic checks.

    Returns CriticResult with ok=True only if all checks pass.
    """
    issues: list[str] = []

    issues.extend(check_numbers_grounded(answer, tool_results))
    issues.extend(check_citations_exist(answer, conn))
    issues.extend(check_missing_conflicts(answer, conn))
    issues.extend(check_visual_type(answer, user_question))

    must_retry = any(
        "not found" in i or "not found in" in i
        for i in issues
    )

    return CriticResult(
        ok=len(issues) == 0,
        issues=issues,
        must_retry=must_retry,
    )
