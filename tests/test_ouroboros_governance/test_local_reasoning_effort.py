"""Thinking budget for reasoning-capable local models.

Measured on this host, qwen3.8:27b over /v1/chat/completions with the
json_schema response_format attached:

    thinking on           -> valid JSON, 629 chars reasoning, 6.8s
    reasoning_effort=none -> valid JSON,   0 chars reasoning, 1.5s
    reasoning_effort=low  -> valid JSON, 531 chars reasoning, 2.3s

So JSON validity is NOT at risk on this path -- ollama returns reasoning
in a separate field and the constrained content stays schema-valid. What
is at risk is throughput: a 4.5x wall-clock tax on a wall-capped soak.

Two spellings measured as silently IGNORED on this endpoint and pinned
here so nobody reaches for them again: ollama's native ``think`` field
and ``chat_template_kwargs {enable_thinking: false}``.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance import local_inference_director as lid

_ENV = "JARVIS_LOCAL_REASONING_EFFORT"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    lid._REASONING_UNSUPPORTED.clear()


@pytest.fixture()
def cfg(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_NAME", "qwen3.8:27b")
    monkeypatch.setenv(
        "JARVIS_LOCAL_MODEL_BASE_URL", "http://127.0.0.1:11434"
    )
    return lid.LocalConfig.from_env()


def test_default_is_none(cfg: Any) -> None:
    """Cheapest setting that keeps the answer, by measurement."""
    body: Dict[str, Any] = {}
    assert lid._apply_reasoning_effort(body, cfg) == "none"
    assert body["reasoning_effort"] == "none"


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh"])
def test_valid_efforts_pass_through(
    cfg: Any, monkeypatch: pytest.MonkeyPatch, effort: str,
) -> None:
    monkeypatch.setenv(_ENV, effort)
    body: Dict[str, Any] = {}
    assert lid._apply_reasoning_effort(body, cfg) == effort
    assert body["reasoning_effort"] == effort


def test_empty_omits_the_field_entirely(
    cfg: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The byte-identical escape hatch."""
    monkeypatch.setenv(_ENV, "")
    body: Dict[str, Any] = {}
    assert lid._apply_reasoning_effort(body, cfg) == ""
    assert "reasoning_effort" not in body


def test_unknown_value_is_not_sent(
    cfg: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An engine should never be handed a budget nobody defined."""
    monkeypatch.setenv(_ENV, "turbo")
    body: Dict[str, Any] = {}
    assert lid._apply_reasoning_effort(body, cfg) == ""
    assert "reasoning_effort" not in body


def test_case_and_whitespace_tolerant(
    cfg: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV, "  LOW  ")
    body: Dict[str, Any] = {}
    assert lid._apply_reasoning_effort(body, cfg) == "low"


# ---------------------------------------------------------------------------
# Degrade by observation
# ---------------------------------------------------------------------------


def test_degrade_removes_the_field_and_is_remembered(cfg: Any) -> None:
    body: Dict[str, Any] = {}
    lid._apply_reasoning_effort(body, cfg)
    assert "reasoning_effort" in body

    assert lid._degrade_reasoning_effort(body, cfg) is True
    assert "reasoning_effort" not in body

    # Never re-attached for this engine.
    body2: Dict[str, Any] = {}
    assert lid._apply_reasoning_effort(body2, cfg) == ""
    assert "reasoning_effort" not in body2


def test_degrade_is_idempotent(cfg: Any) -> None:
    """A persistently-400ing endpoint must not become a retry loop."""
    body: Dict[str, Any] = {}
    lid._apply_reasoning_effort(body, cfg)
    assert lid._degrade_reasoning_effort(body, cfg) is True
    assert lid._degrade_reasoning_effort(body, cfg) is False


def test_reasoning_rejection_does_not_disable_the_schema(cfg: Any) -> None:
    """The misattribution hazard: constrained decoding is what made
    invalid JSON unrepresentable, and must not be switched off because a
    DIFFERENT field was refused."""
    lid._SCHEMA_UNSUPPORTED.discard(lid._schema_key(cfg))
    body: Dict[str, Any] = {}
    lid._apply_reasoning_effort(body, cfg)
    lid._degrade_reasoning_effort(body, cfg)

    assert lid._schema_key(cfg) not in lid._SCHEMA_UNSUPPORTED
    fresh: Dict[str, Any] = {}
    assert lid._apply_response_format(fresh, cfg) in (
        "json_schema", "json_object",
    )
    assert "response_format" in fresh


def test_apply_never_raises_on_a_broken_config() -> None:
    class _Boom:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("config exploded")

    assert lid._apply_reasoning_effort({}, _Boom()) == ""


def test_degrade_never_raises_on_a_broken_config() -> None:
    class _Boom:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("config exploded")

    assert lid._degrade_reasoning_effort({}, _Boom()) is False


def test_flag_is_registered() -> None:
    """The registry advertises the same default the code reads."""
    seen: list = []

    class _Reg:
        def bulk_register(self, specs: Any, override: bool = False) -> None:
            seen.extend(specs)

    assert lid.register_flags(_Reg()) > 0
    match = [s for s in seen if s.name == _ENV]
    assert match, "JARVIS_LOCAL_REASONING_EFFORT not registered"
    assert match[0].default == "none"
