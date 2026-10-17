"""Smoke-tests Anthropic, Gemini and Ollama providers are reachable. See PLAN.md Phase 4.4.

Run: python scripts/smoke_llm.py
Prints "ok (gemini): ..." and "ok (ollama): ..." on success.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic_ai import Agent

from backend.providers import make_llm


async def _check(label: str, prefer: str) -> None:
    agent = Agent(model=make_llm("primary", prefer=prefer))
    result = await agent.run("Reply with exactly one word: pong")
    print(f"ok ({label}): {result.output.strip()!r}")


async def main() -> None:
    for label in ("anthropic", "gemini"):
        try:
            await _check(label, label)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"FAIL ({label}): {type(exc).__name__}: {str(exc)[:200]}")
    await _check("ollama", "ollama")


if __name__ == "__main__":
    asyncio.run(main())
