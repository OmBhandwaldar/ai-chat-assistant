"""FastAPI app entrypoint: AG-UI endpoint, health check, user/conversation REST routes."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Asset Intelligence Copilot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": "0.1"}


# TODO(Phase 4.3, Sonnet): POST /ag-ui — Pydantic AI .to_ag_ui() adapter, SSE stream.
# TODO(Phase 5.1, Sonnet): GET /users, GET /conversations?user_id=...
