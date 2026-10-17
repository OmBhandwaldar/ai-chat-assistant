"""Runs Q1-Q18 against the agent, diffs against eval/expected.json. See PLAN.md Phase 7.2.

Everything runs in ONE process against ONE database (no HTTP), in file order, so stateful
questions work (Q11 after Q6, Q15 after Q12+Q13, Q14 ask -> rule -> ask again, ...).

By default the DB is a throw-away COPY of backend/data.duckdb with runtime state (conversations,
overrides, claims, runtime notes) wiped first, so every run starts identically and your real DB
is never touched.

Examples (from ai-chat-assistant/):
    uv run python eval/run_eval.py --list
    uv run python eval/run_eval.py --dry-run                       # offline plumbing check
    uv run python eval/run_eval.py --only Q1                       # live LLM, one question
    uv run python eval/run_eval.py --only Q15 --no-deps            # skip prerequisite Q12/Q13
    uv run python eval/run_eval.py --json out.json --min-pass 12   # full live run (slow!)

Exit code: 0 when every selected question passes (or, with --min-pass N, at least N pass);
1 otherwise; 2 on a fixture/setup error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb

from eval import report
from eval.scoring import (
    EXPECTED_PATH,
    QuestionResult,
    Turn,
    TurnResult,
    entry_turns,
    load_expected,
    score_answer,
    select_questions,
    synthesize_answer,
    tally,
    validate_expected,
)

DEFAULT_DB = ROOT / "backend" / "data.duckdb"
VIEWS_SQL = ROOT / "backend" / "db" / "views.sql"
DEFAULT_FLEET = (
    ROOT.parent / "flytbase-ahc-swe-asset-intelligence-dataset" / "sample-data" / "fleet"
)
DEFAULT_CHROMA = ROOT / "backend" / "chroma"
AS_OF_DEFAULT = "2026-10-16"
TODAY_HINT = (
    "Today's date is {d}. 'Right now' means each well's latest inspection. "
    "Periods such as 'last 4 months' or 'last 90 days' end today."
)

# Tables wiped on the working copy so each run starts from the pristine dataset.
_RUNTIME_TABLES = ("conversations", "turn_summaries", "overrides", "shared_claims")


# ---------------------------------------------------------------------------
# Database + paths
# ---------------------------------------------------------------------------

def resolve_fleet_dir(dataset_root: str | Path | None) -> Path | None:
    """fleet/ directory for the SOP/manual lookups: --dataset-root, $DATASET_ROOT, or the sample."""
    candidates: list[Path] = []
    root = dataset_root or os.environ.get("DATASET_ROOT")
    if root:
        root = Path(root)
        candidates += [root / "fleet", root / "sample-data" / "fleet"]
    candidates.append(DEFAULT_FLEET)
    return next((c for c in candidates if c.exists()), None)


def reset_runtime_state(conn: duckdb.DuckDBPyConnection) -> None:
    """Remove everything users created at runtime, keeping the ingested dataset."""
    for table in _RUNTIME_TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM context_notes WHERE source = 'runtime'")


def prepare_db(src: Path, *, in_place: bool = False) -> tuple[Path, Path | None]:
    """Return (db_path_to_use, tmpdir_to_delete). Copies *src* (and its .wal) unless in_place."""
    if not src.exists():
        raise FileNotFoundError(f"database not found: {src} (run scripts/ingest.py first)")
    if in_place:
        return src, None
    tmpdir = Path(tempfile.mkdtemp(prefix="eval-db-"))
    dst = tmpdir / src.name
    shutil.copy2(src, dst)
    wal = src.with_name(src.name + ".wal")
    if wal.exists():
        shutil.copy2(wal, dst.with_name(dst.name + ".wal"))
    return dst, tmpdir


def open_db(path: Path, *, keep_state: bool = False) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(path), read_only=False)
    conn.execute(VIEWS_SQL.read_text())  # idempotent CREATE OR REPLACE VIEW
    if not keep_state:
        reset_runtime_state(conn)
    return conn


# ---------------------------------------------------------------------------
# Running one turn
# ---------------------------------------------------------------------------

def extract_trace(result, tool_names: set[str]) -> tuple[list[dict], list[dict]]:
    """(trace, tool_return_payloads) from a finished run, restricted to registered tools."""
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    calls: list[dict] = []
    payloads: list[dict] = []
    for msg in result.new_messages():
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolCallPart) and part.tool_name in tool_names:
                args = part.args if isinstance(part.args, (dict, str)) else str(part.args)
                calls.append({"name": part.tool_name, "args": args})
            elif isinstance(part, ToolReturnPart) and part.tool_name in tool_names:
                content = part.content
                payloads.append(content if isinstance(content, dict) else {"content": content})
    return calls, payloads


class Harness:
    """Holds the shared DB connection, agent and per-conversation message histories."""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        agent,
        *,
        fleet_dir: Path | None,
        chroma_path: Path | None,
        as_of: datetime,
        dry_run: bool,
        timeout_s: float,
        inject_today: bool,
    ):
        self.conn = conn
        self.agent = agent
        self.fleet_dir = fleet_dir
        self.chroma_path = chroma_path
        self.as_of = as_of
        self.dry_run = dry_run
        self.timeout_s = timeout_s
        self.inject_today = inject_today
        self.histories: dict[str, list] = {}

        from backend.agent import ALL_TOOLS

        self.tool_names = {t.name for t in ALL_TOOLS}

    # -- persistence, mirroring backend/app.py ---------------------------------
    def _persist(self, user_id: str, conv_id: str, role: str, text: str, turn_id: int) -> None:
        self.conn.execute(
            "INSERT INTO conversations (user_id, conversation_id, turn_id, role, text) "
            "VALUES (?, ?, ?, ?, ?)",
            [user_id, conv_id, turn_id, role, text],
        )

    def _next_turn(self, conv_id: str) -> int:
        return self.conn.execute(
            "SELECT coalesce(max(turn_id), 0) + 1 FROM conversations WHERE conversation_id = ?",
            [conv_id],
        ).fetchone()[0]

    async def run_turn(self, turn: Turn) -> TurnResult:
        from backend.agent import Deps
        from backend.critic import run_critic

        res = TurnResult(
            qid=turn.qid, label=turn.label, kind=turn.kind, user_id=turn.user_id,
            question_text=turn.question_text, scored=turn.scored, passed=None,
        )
        conv_id = f"eval-{turn.conversation}"
        turn_id = self._next_turn(conv_id)
        self._persist(turn.user_id, conv_id, "user", turn.question_text, turn_id)

        deps = Deps(
            conn=self.conn, user_id=turn.user_id, as_of=self.as_of,
            fleet_dir=self.fleet_dir, chroma_path=self.chroma_path,
        )
        kwargs: dict = {"deps": deps}
        if self.histories.get(conv_id):
            kwargs["message_history"] = self.histories[conv_id]
        if self.inject_today:
            kwargs["instructions"] = TODAY_HINT.format(d=self.as_of.date().isoformat())
        if self.dry_run:
            from pydantic_ai.models.test import TestModel

            kwargs["model"] = TestModel(
                call_tools=["well_status_now"],
                custom_output_args=synthesize_answer(turn.expected, turn.question_text),
            )

        t0 = time.monotonic()
        try:
            run = await asyncio.wait_for(
                self.agent.run(turn.question_text, **kwargs), timeout=self.timeout_s
            )
        except Exception as exc:  # noqa: BLE001 - any LLM/tool/validation failure is a result
            res.elapsed_s = time.monotonic() - t0
            res.error = f"{type(exc).__name__}: {str(exc)[:500]}"
            if os.environ.get("EVAL_TRACE"):
                import traceback

                traceback.print_exc()
            res.passed = False if turn.scored else None
            return res
        res.elapsed_s = time.monotonic() - t0

        u = run.usage
        res.usage = {
            "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_tokens, "cache_write_tokens": u.cache_write_tokens,
            "requests": u.requests, "tool_calls": u.tool_calls,
        }
        try:
            res.usage["cost_usd"] = float(run.response.cost().total_price)
        except Exception:  # noqa: BLE001 - price data may be unknown for some models
            pass
        answer = run.output
        res.answer = answer.model_dump()
        res.tools, payloads = extract_trace(run, self.tool_names)
        self.histories[conv_id] = run.all_messages()
        self._persist(turn.user_id, conv_id, "assistant", answer.text, turn_id)
        try:
            res.critic_issues = run_critic(turn.question_text, answer, payloads, self.conn).issues
        except Exception as exc:  # noqa: BLE001 - critic is informational only
            res.critic_issues = [f"critic crashed: {type(exc).__name__}: {exc}"]

        if turn.scored:
            score = score_answer(turn.expected, answer)
            res.passed, res.failures, res.warnings = score.passed, score.failures, score.warnings
        return res

    async def run_question(self, qid: str, entry: dict, is_dependency: bool) -> QuestionResult:
        qr = QuestionResult(qid=qid, is_dependency=is_dependency)
        for turn in entry_turns(entry):
            tr = await self.run_turn(turn)
            qr.turns.append(tr)
            _progress(tr)
        return qr


def _progress(tr: TurnResult) -> None:
    status = "ERR " if tr.error else ("PASS" if tr.passed else ("FAIL" if tr.scored else "done"))
    print(f"  [{status}] {tr.label:<10} {tr.user_id} {tr.elapsed_s:5.1f}s  "
          f"{tr.question_text[:70]}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the Q1-Q18 eval against the agent.")
    p.add_argument("--only", help="comma-separated question ids, e.g. Q1,Q4")
    p.add_argument("--list", action="store_true", help="list questions (user, deps) and exit")
    p.add_argument("--expected", type=Path, default=EXPECTED_PATH, help="fixture path")
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help="DuckDB file to copy and run against (default backend/data.duckdb)")
    p.add_argument("--dataset-root", type=Path,
                   help="dataset root holding fleet/ (SOPs, manuals); default $DATASET_ROOT or "
                        "the sample dataset next to this repo")
    p.add_argument("--in-place", action="store_true",
                   help="DANGEROUS: run against --db directly instead of a temp copy")
    p.add_argument("--keep-state", action="store_true",
                   help="do not wipe conversations/overrides/claims/runtime notes on the copy")
    p.add_argument("--keep-db", action="store_true", help="keep the temp DB copy and print its path")
    p.add_argument("--no-deps", action="store_true",
                   help="with --only, do not auto-run prerequisite questions (depends_on)")
    p.add_argument("--dry-run", action="store_true",
                   help="no LLM: drive the real agent with pydantic-ai TestModel that returns "
                        "the 'perfect' answer built from expected.json (verifies the plumbing)")
    p.add_argument("--prefer", choices=["anthropic", "gemini", "ollama"], default=None,
                   help="LLM provider for live runs (default: $LLM_PROVIDER, else anthropic if "
                        "ANTHROPIC_API_KEY is set, else gemini; all fall back to ollama)")
    p.add_argument("--as-of", default=AS_OF_DEFAULT, help="'today' for tools (default 2026-10-16)")
    p.add_argument("--inject-today", action="store_true",
                   help="also tell the LLM today's date via per-run instructions (agent.py's "
                        "system prompt does not; off by default to measure the product as-is)")
    p.add_argument("--timeout", type=float, default=300.0, help="per-turn timeout in seconds")
    p.add_argument("--json", dest="json_out", type=Path, help="write full results to this file")
    p.add_argument("--min-pass", type=int,
                   help="exit non-zero if fewer than N selected questions pass "
                        "(default: all selected must pass)")
    return p


def _print_list(data: dict[str, dict]) -> None:
    print(f"{'Q':<5} {'user':<6} {'turns':<6} {'depends_on':<12} {'runs_after':<10} question")
    for qid, entry in data.items():
        turns = entry_turns(entry)
        scored = sum(1 for t in turns if t.scored)
        print(f"{qid:<5} {entry['user_id']:<6} {scored}/{len(turns):<4} "
              f"{','.join(entry['depends_on']) or '-':<12} "
              f"{','.join(entry['runs_after']) or '-':<10} {entry['question_text'][:70]}")


def _user_ids(conn: duckdb.DuckDBPyConnection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT user_id FROM users").fetchall()}


async def run_selected(
    harness: Harness, data: dict[str, dict], selection: list[tuple[str, bool]]
) -> list[QuestionResult]:
    results: list[QuestionResult] = []
    for qid, is_dep in selection:
        label = " (prerequisite)" if is_dep else ""
        print(f"{qid}{label}", file=sys.stderr, flush=True)
        results.append(await harness.run_question(qid, data[qid], is_dep))
    return results


def exit_code(results: list[QuestionResult], min_pass: int | None) -> int:
    passed, total = tally(results)
    return 0 if passed >= (total if min_pass is None else min_pass) else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data = load_expected(args.expected)

    if args.list:
        _print_list(data)
        return 0

    only = [q.strip().upper() for q in args.only.split(",") if q.strip()] if args.only else None
    try:
        selection = select_questions(data, only, include_deps=not args.no_deps)
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2

    try:
        db_path, tmpdir = prepare_db(args.db, in_place=args.in_place)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    conn = None
    try:
        conn = open_db(db_path, keep_state=args.keep_state or args.in_place)
        problems = validate_expected(data, known_user_ids=_user_ids(conn))
        if problems:
            print("expected.json is invalid:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 2

        from backend.agent import build_agent

        if args.dry_run:
            agent = build_agent(model=None)
        else:
            from backend.providers import default_provider, make_llm

            args.prefer = args.prefer or default_provider()
            agent = build_agent(model=make_llm("primary", prefer=args.prefer))
            n_turns = sum(len(entry_turns(data[q])) for q, _ in selection)
            print(f"LIVE run: {len(selection)} question(s), {n_turns} agent turn(s) "
                  f"(~70 s each on the free tier).", file=sys.stderr)

        harness = Harness(
            conn, agent,
            fleet_dir=resolve_fleet_dir(args.dataset_root),
            chroma_path=DEFAULT_CHROMA if DEFAULT_CHROMA.exists() else None,
            as_of=datetime.fromisoformat(args.as_of),
            dry_run=args.dry_run, timeout_s=args.timeout, inject_today=args.inject_today,
        )
        print(f"DB: {db_path}{'  (temp copy)' if tmpdir else '  (IN PLACE)'}"
              f"  mode: {'dry-run' if args.dry_run else 'live/' + args.prefer}", file=sys.stderr)

        results = asyncio.run(run_selected(harness, data, selection))
    finally:
        if conn is not None:
            conn.close()
        if tmpdir is not None:
            if args.keep_db:
                print(f"kept temp DB: {db_path}", file=sys.stderr)
            else:
                shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    print(report.format_table(results))
    failures = report.format_failures(results)
    if failures:
        print("\nDetails:\n" + failures)
    print("\n" + report.format_summary(results))

    if args.json_out:
        meta = {"mode": "dry-run" if args.dry_run else f"live/{args.prefer}",
                "as_of": args.as_of, "only": only, "inject_today": args.inject_today,
                "db": str(args.db), "in_place": args.in_place}
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report.to_json(results, meta), indent=2, ensure_ascii=False, default=str)
        )
        print(f"wrote {args.json_out}", file=sys.stderr)

    return exit_code(results, args.min_pass)


if __name__ == "__main__":
    sys.exit(main())
