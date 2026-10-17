"""Human + JSON reporting for eval results. Pure formatting over eval.scoring dataclasses."""

from __future__ import annotations

import dataclasses
import textwrap
from datetime import UTC, datetime

from eval.scoring import QuestionResult, TurnResult, tally

PASS, FAIL = "✅", "❌"


def _mark(result: QuestionResult) -> str:
    return PASS if result.passed else FAIL


def _visual_of(turn: TurnResult) -> str:
    if turn.answer is None:
        return "-"
    return str((turn.answer.get("visual") or {}).get("type", "-"))


def _tools_of(result: QuestionResult) -> str:
    names: list[str] = []
    for t in result.turns:
        for call in t.tools:
            if call["name"] not in names:
                names.append(call["name"])
    return ", ".join(names) or "-"


def format_table(results: list[QuestionResult]) -> str:
    """One row per question: mark, scored-turn tally, final visual, time, tools used."""
    header = f"{'Q':<5} {'':<3} {'turns':<6} {'visual':<12} {'time':>6}  tools"
    lines = [header, "-" * len(header)]
    for r in results:
        scored = [t for t in r.turns if t.scored]
        ok = sum(1 for t in scored if t.passed and not t.error)
        final = scored[-1] if scored else (r.turns[-1] if r.turns else None)
        visual = _visual_of(final) if final else "-"
        qid = r.qid + ("*" if r.is_dependency else "")
        lines.append(
            f"{qid:<5} {_mark(r):<3} {ok}/{len(scored):<4} {visual:<12} "
            f"{r.elapsed_s:>5.0f}s  {_tools_of(r)}"
        )
    if any(r.is_dependency for r in results):
        lines.append("(* = run only as a prerequisite; not counted in the summary)")
    return "\n".join(lines)


def format_failures(results: list[QuestionResult], width: int = 100) -> str:
    """Diff-style detail for every failing/errored scored turn, plus warnings for passing ones."""
    out: list[str] = []
    for r in results:
        for t in r.turns:
            if t.error:
                out.append(f"{FAIL} {t.label} [{t.user_id}] ERROR: {t.error}")
                continue
            if not t.scored:
                continue
            if not t.passed:
                out.append(f"{FAIL} {t.label} [{t.user_id}] {t.question_text[:80]}")
                out += [f"    - {f}" for f in t.failures]
                if t.answer is not None:
                    snippet = textwrap.shorten(str(t.answer.get("text", "")), width=width * 2)
                    out.append(f"    answer: {snippet}")
            if t.warnings:
                out.append(f"{'⚠️ ' if t.passed else '   '} {t.label} optional misses:")
                out += [f"    ~ {w}" for w in t.warnings]
    return "\n".join(out)


def format_summary(results: list[QuestionResult]) -> str:
    passed, total = tally(results)
    pct = (100 * passed / total) if total else 0.0
    failed = [r.qid for r in results if not r.is_dependency and not r.passed]
    tail = f"  failed: {', '.join(failed)}" if failed else ""
    return f"SUMMARY: {passed}/{total} passed ({pct:.0f}%){tail}"


def to_json(results: list[QuestionResult], meta: dict) -> dict:
    passed, total = tally(results)
    return {
        "meta": {**meta, "generated_at": datetime.now(UTC).isoformat()},
        "summary": {"passed": passed, "total": total},
        "questions": [
            {
                "id": r.qid,
                "passed": r.passed,
                "is_dependency": r.is_dependency,
                "elapsed_s": round(r.elapsed_s, 2),
                "turns": [dataclasses.asdict(t) for t in r.turns],
            }
            for r in results
        ],
    }
