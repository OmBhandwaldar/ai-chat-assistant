"""Tests for backend/providers.py (Phase 4.4).

No network calls here — just verifies make_llm wires the right model types and
falls back to Ollama on construction. Live reachability is scripts/smoke_llm.py.
"""

import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.ollama import OllamaModel

from backend.providers import make_llm


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("LLM_PROVIDER", "ANTHROPIC_MODEL", "ANTHROPIC_CRITIC_MODEL", "GEMINI_PRIMARY_MODEL"):
        monkeypatch.delenv(var, raising=False)


def test_default_is_anthropic_when_key_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-construction-only")
    model = make_llm("primary")
    assert isinstance(model, FallbackModel)
    assert isinstance(model.models[0], AnthropicModel)
    assert model.models[0].model_name == "claude-sonnet-5-5"
    assert isinstance(model.models[1], OllamaModel)
    assert "thinking" not in (model.models[0].settings or {})


def test_anthropic_model_env_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-construction-only")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    assert make_llm("primary").models[0].model_name == "claude-sonnet-4-6"


def test_llm_provider_env_selects_gemini(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-construction-only")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-construction-only")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert "gemini" in make_llm("primary").models[0].model_name


def test_gemini_default_without_anthropic_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-construction-only")
    model = make_llm("primary")
    assert isinstance(model, FallbackModel)
    assert len(model.models) == 2
    assert isinstance(model.models[1], OllamaModel)


def test_prefer_ollama_returns_ollama_directly():
    model = make_llm("primary", prefer="ollama")
    assert isinstance(model, OllamaModel)


def test_primary_gemini_is_38_flash_with_low_thinking(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-construction-only")
    model = make_llm("primary", prefer="gemini")
    gemini = model.models[0]
    assert gemini.model_name == "gemini-3.8-flash"
    assert gemini.settings is not None
    assert gemini.settings["timeout"] == 25.0
    assert gemini.settings["thinking"] == "low"


def test_critic_role_uses_gemma_model_name():
    model = make_llm("primary", prefer="ollama")
    assert "gemma4" in model.model_name

    critic_model = make_llm("critic", prefer="ollama")
    assert "gemma4" in critic_model.model_name
