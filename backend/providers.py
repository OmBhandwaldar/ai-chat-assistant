"""LLM provider factory: Claude (default) or Gemini primary, Ollama fallback.

Provider choice: LLM_PROVIDER=anthropic|gemini (default: anthropic when ANTHROPIC_API_KEY is
set, otherwise gemini). Ollama (gemma4) is the fallback behind either on any ModelAPIError.

See PLAN.md Phase 4.4, DECISIONS.md D4/D15.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.settings import ModelSettings

# Keys live in ai-chat-assistant/.env and/or ai-chat-assistant/backend/.env (both gitignored).
# Load them here so `uvicorn backend.app:app` works without exporting variables by hand; real
# environment variables still win (load_dotenv never overrides), then backend/.env, then root.
_BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(_BACKEND_DIR / ".env")
load_dotenv(_BACKEND_DIR.parent / ".env")

logger = logging.getLogger("backend.providers")

# Model names per role, by provider. AI Studio answers 404 "no longer available to new users"
# for gemini-2.5-flash on current keys, which silently pushed every request onto local Ollama.
_GEMINI_MODEL: dict[str, str] = {
    "primary": "gemini-3.8-flash",
    "critic": "gemma-4-26b-a4b-it",  # served via AI Studio
}
_GEMINI_MODEL_ENV = {"primary": "GEMINI_PRIMARY_MODEL", "critic": "GEMINI_CRITIC_MODEL"}
_ANTHROPIC_MODEL: dict[str, str] = {
    "primary": "claude-sonnet-5-5",
    "critic": "claude-haiku-4-5",
}
_ANTHROPIC_MODEL_ENV = {"primary": "ANTHROPIC_MODEL", "critic": "ANTHROPIC_CRITIC_MODEL"}
_OLLAMA_MODEL: dict[str, str] = {
    "primary": "gemma4:12b",
    "critic": "gemma4:12b",
}

# A hung or throttled Gemini request must fail over to Ollama instead of stalling the turn:
# the Google provider's own read timeout is 20 minutes.
GEMINI_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "25"))
# Claude answers a tool-using turn in seconds; this only guards against a hung connection.
ANTHROPIC_TIMEOUT_S = float(os.environ.get("ANTHROPIC_TIMEOUT_S", "60"))
OLLAMA_TIMEOUT_S = float(os.environ.get("OLLAMA_TIMEOUT_S", "120"))
# Gemini only (never sent to Claude). Status lookups need little reasoning; 'low' keeps tool
# choice and citations intact.
GEMINI_THINKING = os.environ.get("GEMINI_THINKING", "low")


def _ollama_model(role: Literal["primary", "critic"]) -> OllamaModel:
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    return OllamaModel(
        _OLLAMA_MODEL[role],
        provider=OllamaProvider(base_url=base_url),
        # gemma4 thinks by default; on a tool-calling turn that adds minutes and
        # trips the eval timeout. The answer has to come from tools either way.
        settings=ModelSettings(timeout=OLLAMA_TIMEOUT_S, extra_body={"think": False}),
    )


def _gemini_model(role: Literal["primary", "critic"]) -> GoogleModel:
    api_key = os.environ.get("GEMINI_API_KEY")
    name = os.environ.get(_GEMINI_MODEL_ENV[role]) or _GEMINI_MODEL[role]
    settings = GoogleModelSettings(timeout=GEMINI_TIMEOUT_S)
    if role == "primary" and GEMINI_THINKING:
        settings["thinking"] = GEMINI_THINKING  # type: ignore[typeddict-item]
    return GoogleModel(name, provider=GoogleProvider(api_key=api_key), settings=settings)


def _anthropic_model(role: Literal["primary", "critic"]) -> AnthropicModel:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    name = os.environ.get(_ANTHROPIC_MODEL_ENV[role]) or _ANTHROPIC_MODEL[role]
    return AnthropicModel(
        name,
        provider=AnthropicProvider(api_key=api_key),
        settings=ModelSettings(timeout=ANTHROPIC_TIMEOUT_S),
    )


def _log_fallback(exc: Exception) -> bool:
    if isinstance(exc, ModelAPIError):
        logger.warning("Primary LLM request failed, falling back to Ollama: %s", str(exc)[:300])
        return True
    return False


Provider = Literal["anthropic", "gemini", "ollama"]


def default_provider() -> Provider:
    """LLM_PROVIDER if set, else anthropic when ANTHROPIC_API_KEY is present, else gemini."""
    choice = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if choice in ("anthropic", "gemini", "ollama"):
        return choice  # type: ignore[return-value]
    return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "gemini"


def make_llm(
    role: Literal["primary", "critic"],
    prefer: Provider | None = None,
) -> Model:
    """Build the Pydantic AI model for *role*.

    *prefer* defaults to `default_provider()`. For 'anthropic' / 'gemini' this returns a
    `FallbackModel` that tries the cloud model first and falls back to local Ollama on a
    `ModelAPIError` (HTTP 4xx/5xx incl. 429/503/504, network errors and the per-request
    timeout). For 'ollama' it returns the Ollama model directly.
    """
    provider = prefer or default_provider()
    if provider == "ollama":
        return _ollama_model(role)
    primary = _anthropic_model(role) if provider == "anthropic" else _gemini_model(role)
    return FallbackModel(primary, _ollama_model(role), fallback_on=_log_fallback)
