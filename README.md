# Asset Intelligence Copilot

Chat assistant for an oilfield fleet. It answers questions about wells, inspections,
maintenance, and SOP breaches from the FlytBase asset dataset, and it cites the rows
and reports it used.

Structured facts live in DuckDB (SQL views decide producing state, downtime, and
corrections). Repair-report text is chunked into a local Chroma index. A Gemini model
calls those tools; an optional local Ollama model is the fallback. The Next.js app
talks to the FastAPI backend over AG-UI.

The loader is not tied to the sample. Point it at any dataset with the same `fleet/`
and `assets/` layout. Extra JSON fields are ignored, missing optional fields become
NULL, and broken files are skipped with a counted warning instead of aborting the run.

```bash
uv sync
cp .env.example .env          # then set GEMINI_API_KEY
uv run python scripts/ingest.py "$DATASET_ROOT"
uv run uvicorn backend.app:app --reload --port 8000
cd frontend && pnpm install && pnpm dev
uv run pytest
uv run python eval/run_eval.py --list
uv run python eval/run_eval.py --dry-run
uv run python eval/run_eval.py --only Q1
```
