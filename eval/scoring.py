"""Pure scoring + fixture logic for the eval harness. No LLM, no DB, no backend imports.

Everything here is a plain function over plain data so it can be unit-tested offline
(backend/tests/test_eval_runner.py). The runner (eval/run_eval.py) supplies the answers.

An *answer* is an AgentAnswer or any dict with the same shape:
    {"text": str, "visual": {"type": str, "data": dict}, "citations": [{"source_id": str, ...}],
     "conflicts": [..]}

An *expected* block (one per scored turn) supports these keys (see eval/expected.json `_meta`):
    visual_type, text_contains, text_not_contains, numbers, visual_numbers, well_ids_in_data,
    well_ids_not_in_data, visual_data_contains, citations_must_include, min_citations,
    conflicts_min — and any of them with an `_optional` suffix, which only produce warnings.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXPECTED_PATH = Path(__file__).with_name("expected.json")
QUESTION_IDS = [f"Q{i}" for i in range(1, 19)]
NUMBER_TOLERANCE = 0.005  # 0.5 %

VISUAL_TYPES = {"status_card", "table", "line", "bar", "map", "heatmap", "none"}

# expected-block keys -> kind of value they carry
_LIST_KEYS = {
    "text_contains", "text_not_contains", "well_ids_in_data", "well_ids_not_in_data",
    "visual_data_contains", "citations_must_include",
}
_NUMBER_KEYS = {"numbers", "visual_numbers"}
_SCALAR_KEYS = {"visual_type", "min_citations", "conflicts_min"}
_BASE_KEYS = _LIST_KEYS | _NUMBER_KEYS | _SCALAR_KEYS
# Only keys where "optional" is meaningful.
_OPTIONAL_BASES = _LIST_KEYS | _NUMBER_KEYS | {"min_citations", "conflicts_min"}
EXPECTED_KEYS = _BASE_KEYS | {f"{k}_optional" for k in _OPTIONAL_BASES}

_ENTRY_KEYS = {
    "id", "question_text", "user_id", "conversation", "depends_on", "runs_after", "notes",
    "pre_turns", "expected", "post_turns",
}
_TURN_KEYS = {"label", "question_text", "user_id", "conversation", "expected"}

# ---------------------------------------------------------------------------
# Text normalisation + number extraction
# ---------------------------------------------------------------------------

_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
_SPACES = dict.fromkeys(map(ord, "\u00a0\u2009\u202f\u200b"), " ")
_ID_RE = re.compile(r"\b[A-Z]{2,5}-[\w-]+\b")  # WELL-0121, INS-0001-0203, SR-01, ...
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?)?\b")
_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_NUM_RE = re.compile(r"(?<![\w.])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def normalize(text: str) -> str:
    """Casefold, unify exotic dashes/spaces (LLMs emit U+2011 inside IDs), collapse whitespace."""
    text = str(text).translate(_DASHES).translate(_SPACES)
    return re.sub(r"\s+", " ", text).strip().casefold()


def extract_numbers(text: str) -> list[float]:
    """Every numeric value in *text*, ignoring record IDs (WELL-0121), ISO dates and clock times.

    "25,794" -> 25794.0; "$5,000" -> 5000.0; "2026-10-15" contributes nothing.
    """
    cleaned = _DATE_RE.sub(" ", str(text).translate(_DASHES).translate(_SPACES))
    cleaned = _ID_RE.sub(" ", cleaned)
    cleaned = _CLOCK_RE.sub(" ", cleaned)
    out: list[float] = []
    for token in _NUM_RE.findall(cleaned):
        try:
            out.append(float(token.replace(",", "")))
        except ValueError:  # pragma: no cover - regex guarantees parseable
            continue
    return out


def number_matches(expected: float, found: float, rel_tol: float = NUMBER_TOLERANCE) -> bool:
    """True when *found* is within *rel_tol* (relative) of *expected*; exact for 0."""
    if expected == 0:
        return found == 0
    return abs(found - expected) <= rel_tol * abs(expected) + 1e-12


def number_in_text(expected: float, text: str, rel_tol: float = NUMBER_TOLERANCE) -> float | None:
    """The first number in *text* matching *expected* within tolerance, else None."""
    for found in extract_numbers(text):
        if number_matches(expected, found, rel_tol):
            return found
    return None


# ---------------------------------------------------------------------------
# Answer normalisation
# ---------------------------------------------------------------------------

@dataclass
class NormAnswer:
    text: str
    visual_type: str
    visual_json: str  # JSON of visual.data, ready for substring search
    citation_ids: list[str]
    conflicts: int


def normalize_answer(answer: Any) -> NormAnswer:
    """Accept an AgentAnswer (pydantic) or a dict and flatten it for scoring."""
    if hasattr(answer, "model_dump"):
        answer = answer.model_dump()
    visual = answer.get("visual") or {}
    data = visual.get("data") if isinstance(visual, dict) else None
    citations = answer.get("citations") or []
    return NormAnswer(
        text=str(answer.get("text") or ""),
        visual_type=str((visual or {}).get("type") or "none"),
        visual_json=json.dumps(data if data is not None else {}, ensure_ascii=False, default=str),
        citation_ids=[str(c.get("source_id", "")).strip() for c in citations],
        conflicts=len(answer.get("conflicts") or []),
    )


# ---------------------------------------------------------------------------
# Scoring one turn
# ---------------------------------------------------------------------------

@dataclass
class TurnScore:
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: int = 0


def _alternatives(item: Any) -> list[str]:
    return [item] if isinstance(item, str) else list(item)


def _check_visual_type(expected: Any, actual: str) -> list[str]:
    if expected is None:
        return []
    allowed = [expected] if isinstance(expected, str) else list(expected)
    if actual in allowed:
        return []
    return [f"visual.type: expected {' | '.join(allowed)}, got {actual!r}"]


def _check_text_contains(items: list[Any], haystack: str) -> list[str]:
    norm = normalize(haystack)
    out = []
    for item in items:
        alts = _alternatives(item)
        if not any(normalize(a) in norm for a in alts):
            out.append(
                f"text missing {alts[0]!r}" if len(alts) == 1
                else f"text missing any of {alts!r}"
            )
    return out


def _check_text_not_contains(items: list[str], haystack: str) -> list[str]:
    norm = normalize(haystack)
    return [f"text must not contain {s!r}" for s in items if normalize(s) in norm]


def _check_numbers(numbers: dict[str, float], haystack: str, where: str) -> list[str]:
    out = []
    for name, value in numbers.items():
        if number_in_text(float(value), haystack) is None:
            out.append(f"{where}: number {name}={value} not found (±{NUMBER_TOLERANCE:.1%})")
    return out


def _check_in_data(items: list[str], data_json: str) -> list[str]:
    norm = normalize(data_json)
    return [f"visual.data missing {s!r}" for s in items if normalize(s) not in norm]


def _check_not_in_data(items: list[str], data_json: str) -> list[str]:
    norm = normalize(data_json)
    return [f"visual.data must not contain {s!r}" for s in items if normalize(s) in norm]


def _check_citations(required: list[str], have: list[str]) -> list[str]:
    present = {c.casefold() for c in have}
    return [f"citation {cid} missing" for cid in required if cid.casefold() not in present]


def _run_checks(exp: dict, ans: NormAnswer, *, optional: bool) -> list[str]:
    """Run every check present in *exp*. With optional=True, read the `*_optional` keys."""
    sfx = "_optional" if optional else ""

    def get(key: str) -> Any:
        return exp.get(key + sfx)

    out: list[str] = []
    if not optional and "visual_type" in exp:
        out += _check_visual_type(exp["visual_type"], ans.visual_type)
    if (v := get("text_contains")) is not None:
        out += _check_text_contains(v, ans.text)
    if (v := get("text_not_contains")) is not None:
        out += _check_text_not_contains(v, ans.text)
    if (v := get("numbers")) is not None:
        out += _check_numbers(v, ans.text, "text")
    if (v := get("visual_numbers")) is not None:
        out += _check_numbers(v, ans.visual_json, "visual.data")
    if (v := get("well_ids_in_data")) is not None:
        out += _check_in_data(v, ans.visual_json)
    if (v := get("well_ids_not_in_data")) is not None:
        out += _check_not_in_data(v, ans.visual_json)
    if (v := get("visual_data_contains")) is not None:
        out += _check_in_data(v, ans.visual_json)
    if (v := get("citations_must_include")) is not None:
        out += _check_citations(v, ans.citation_ids)
    if (v := get("min_citations")) is not None and len(ans.citation_ids) < v:
        out.append(f"citations: expected at least {v}, got {len(ans.citation_ids)}")
    if (v := get("conflicts_min")) is not None and ans.conflicts < v:
        out.append(f"conflicts: expected at least {v}, got {ans.conflicts}")
    return out


def _count_checks(exp: dict) -> int:
    n = 0
    for key, value in exp.items():
        if key.endswith("_optional"):
            continue
        n += len(value) if isinstance(value, (list, dict)) else 1
    return n


def score_answer(expected: dict | None, answer: Any) -> TurnScore:
    """Score one AgentAnswer against one expected block. Pure; never raises on bad answers."""
    expected = expected or {}
    ans = normalize_answer(answer)
    failures = _run_checks(expected, ans, optional=False)
    warnings = _run_checks(expected, ans, optional=True)
    return TurnScore(
        passed=not failures, failures=failures, warnings=warnings, checks=_count_checks(expected)
    )


# ---------------------------------------------------------------------------
# Fixture: load, validate, order
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    qid: str
    label: str
    kind: str  # "pre" | "primary" | "post"
    question_text: str
    user_id: str
    conversation: str
    expected: dict | None

    @property
    def scored(self) -> bool:
        return self.expected is not None


def load_expected(path: Path | str = EXPECTED_PATH) -> dict[str, dict]:
    """Load expected.json, dropping `_`-prefixed metadata keys. Order = run order."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def entry_turns(entry: dict) -> list[Turn]:
    """Expand a fixture entry into its turns in execution order: pre_turns, primary, post_turns."""
    qid = entry["id"]
    default_conv = entry.get("conversation") or qid

    def make(spec: dict, kind: str, label: str) -> Turn:
        return Turn(
            qid=qid,
            label=spec.get("label") or label,
            kind=kind,
            question_text=spec["question_text"],
            user_id=spec["user_id"],
            conversation=spec.get("conversation") or default_conv,
            expected=spec.get("expected"),
        )

    turns = [make(t, "pre", f"{qid}-pre{i}") for i, t in enumerate(entry.get("pre_turns") or [])]
    turns.append(make(entry, "primary", qid))
    turns += [make(t, "post", f"{qid}-post{i}") for i, t in enumerate(entry.get("post_turns") or [])]
    return turns


def _validate_expected_block(where: str, exp: Any, errors: list[str]) -> None:
    if not isinstance(exp, dict):
        errors.append(f"{where}: expected must be an object")
        return
    for key in exp:
        if key not in EXPECTED_KEYS:
            errors.append(f"{where}: unknown expected key {key!r}")
    for key, value in exp.items():
        base = key.removesuffix("_optional")
        if base == "visual_type":
            vals = [value] if isinstance(value, str) or value is None else value
            if isinstance(vals, list) and vals != [None]:
                for v in vals:
                    if v not in VISUAL_TYPES:
                        errors.append(f"{where}: bad visual_type {v!r}")
        elif base in _NUMBER_KEYS:
            if not isinstance(value, dict) or not value:
                errors.append(f"{where}.{key}: must be a non-empty object of name -> number")
            else:
                for name, num in value.items():
                    if isinstance(num, bool) or not isinstance(num, (int, float)):
                        errors.append(f"{where}.{key}.{name}: not a number")
        elif base in _LIST_KEYS:
            if not isinstance(value, list) or not value:
                errors.append(f"{where}.{key}: must be a non-empty list")
                continue
            for item in value:
                ok = isinstance(item, str) or (
                    isinstance(item, list) and item and all(isinstance(a, str) for a in item)
                )
                if base != "text_contains" and not isinstance(item, str):
                    ok = False
                if not ok or item == "" or item == []:
                    errors.append(f"{where}.{key}: bad item {item!r}")
        elif base in {"min_citations", "conflicts_min"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append(f"{where}.{key}: must be a non-negative integer")


def validate_expected(data: dict[str, dict], known_user_ids: set[str] | None = None) -> list[str]:
    """Return a list of schema problems (empty = valid). Checks Q1..Q18 all present, required
    keys/types, known keys only, dependency ids exist and appear earlier, user ids known."""
    errors: list[str] = []
    for qid in QUESTION_IDS:
        if qid not in data:
            errors.append(f"{qid}: missing entry")
    order = list(data)
    for qid, entry in data.items():
        if not isinstance(entry, dict):
            errors.append(f"{qid}: entry must be an object")
            continue
        errors_before = len(errors)
        for key in entry:
            if key not in _ENTRY_KEYS:
                errors.append(f"{qid}: unknown key {key!r}")
        if entry.get("id") != qid:
            errors.append(f"{qid}: id field {entry.get('id')!r} != key")
        for key in ("question_text", "user_id"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                errors.append(f"{qid}: {key} must be a non-empty string")
        if "expected" not in entry or entry["expected"] is None:
            errors.append(f"{qid}: primary turn needs an expected block")
        else:
            _validate_expected_block(f"{qid}.expected", entry["expected"], errors)
        for rel in ("depends_on", "runs_after"):
            deps = entry.get(rel)
            if not isinstance(deps, list):
                errors.append(f"{qid}: {rel} must be a list")
                continue
            for dep in deps:
                if dep not in data:
                    errors.append(f"{qid}: {rel} references unknown {dep!r}")
                elif dep in order and order.index(dep) >= order.index(qid):
                    errors.append(f"{qid}: {rel} {dep!r} must appear earlier in the file")
        for kind in ("pre_turns", "post_turns"):
            turns = entry.get(kind) or []
            if not isinstance(turns, list):
                errors.append(f"{qid}.{kind}: must be a list")
                continue
            for i, t in enumerate(turns):
                where = f"{qid}.{kind}[{i}]"
                for key in t:
                    if key not in _TURN_KEYS:
                        errors.append(f"{where}: unknown key {key!r}")
                for key in ("question_text", "user_id", "conversation"):
                    if not isinstance(t.get(key), str) or not t[key].strip():
                        errors.append(f"{where}: {key} must be a non-empty string")
                if t.get("expected") is not None:
                    _validate_expected_block(f"{where}.expected", t["expected"], errors)
        if known_user_ids is not None and len(errors) == errors_before:
            for t in entry_turns(entry):
                if t.user_id not in known_user_ids:
                    errors.append(f"{qid}: unknown user_id {t.user_id!r}")
    return errors


def select_questions(
    data: dict[str, dict], only: list[str] | None = None, include_deps: bool = True
) -> list[tuple[str, bool]]:
    """Ordered [(qid, is_dependency)] to run. File order = run order.

    With *only*, run just those plus (when include_deps) their transitive `depends_on` closure;
    the extras come back with is_dependency=True so reports can exclude them from the tally.
    """
    if not only:
        return [(q, False) for q in data]
    unknown = [q for q in only if q not in data]
    if unknown:
        raise KeyError(f"unknown question id(s): {', '.join(unknown)}")
    wanted = set(only)
    needed = set(only)
    if include_deps:
        stack = list(only)
        while stack:
            for dep in data[stack.pop()].get("depends_on", []):
                if dep not in needed:
                    needed.add(dep)
                    stack.append(dep)
    return [(q, q not in wanted) for q in data if q in needed]


# ---------------------------------------------------------------------------
# Dry-run helper: build the "perfect" answer for an expected block
# ---------------------------------------------------------------------------

_PREFIX_TO_SOURCE = {"INS": "inspection", "MNT": "maintenance", "CTX": "context", "REG": "regulatory"}


def _fmt_num(value: float) -> str:
    return f"{value:.10g}"


def synthesize_answer(expected: dict | None, question_text: str = "") -> dict:
    """An AgentAnswer-shaped dict that satisfies *expected* (all required AND optional keys).

    Used by --dry-run to drive the real Agent through pydantic-ai's TestModel offline.
    """
    exp = expected or {}

    def merged(key: str) -> Any:
        a, b = exp.get(key), exp.get(key + "_optional")
        if key in ("numbers", "visual_numbers"):
            return {**(a or {}), **(b or {})}
        return list(a or []) + list(b or [])

    parts = [f"Dry-run answer for: {question_text}".strip()]
    parts += [_alternatives(item)[0] for item in merged("text_contains")]
    parts += [f"{name}: {_fmt_num(v)}" for name, v in merged("numbers").items()]
    text = ". ".join(parts)

    vtype = exp.get("visual_type")
    vtype = vtype if isinstance(vtype, str) else (vtype[0] if vtype else "none")
    vdata = {
        "well_ids": merged("well_ids_in_data"),
        "contains": merged("visual_data_contains"),
        "values": merged("visual_numbers"),
    }

    ids = list(merged("citations_must_include"))
    while len(ids) < (exp.get("min_citations") or 0):
        ids.append(f"INS-DRY-{len(ids):04d}")
    citations = [
        {"source_id": i, "source_type": _PREFIX_TO_SOURCE.get(i.split("-")[0], "inspection")}
        for i in ids
    ]
    conflicts = [
        {"type": "dry_run", "left": f"left-{n}", "right": f"right-{n}"}
        for n in range(exp.get("conflicts_min") or 0)
    ]
    return {"text": text, "visual": {"type": vtype, "data": vdata},
            "citations": citations, "conflicts": conflicts}


# ---------------------------------------------------------------------------
# Result containers (shared by runner + report)
# ---------------------------------------------------------------------------

@dataclass
class TurnResult:
    qid: str
    label: str
    kind: str
    user_id: str
    question_text: str
    scored: bool
    passed: bool | None  # None for unscored setup turns that did not error
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    answer: dict | None = None
    tools: list[dict] = field(default_factory=list)
    critic_issues: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_s: float = 0.0
    usage: dict = field(default_factory=dict)  # input/output tokens, requests, cost (USD) if known


@dataclass
class QuestionResult:
    qid: str
    is_dependency: bool
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        scored = [t for t in self.turns if t.scored]
        errored = any(t.error for t in self.turns)
        return bool(scored) and all(t.passed for t in scored) and not errored

    @property
    def elapsed_s(self) -> float:
        return sum(t.elapsed_s for t in self.turns)


def tally(results: list[QuestionResult]) -> tuple[int, int]:
    """(passed, total) over questions that were explicitly selected (dependencies excluded)."""
    counted = [r for r in results if not r.is_dependency]
    return sum(1 for r in counted if r.passed), len(counted)
