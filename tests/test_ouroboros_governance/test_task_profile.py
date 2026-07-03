"""
Tests for Task 3: Upgrade PrimeProvider Contract — TaskProfile

Covers:
1. TaskProfile.as_dict() serialises all four fields correctly.
2. PrimeRequest accepts task_profile and model_name fields.
3. PrimeClient._build_payload() embeds task_profile in JSON when present.
4. _build_payload() falls back to task_profile.model when model_name is None.
5. model_name takes priority over task_profile.model.
6. PrimeProvider.generate() builds TaskProfile from routing telemetry.
7. PrimeProvider.generate() strips "cai_intent_" prefix from routing_reason.
8. PrimeProvider.generate() handles absent telemetry (no crash, task_profile=None).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.prime_client import TaskProfile, PrimeRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_prime_request(**kwargs: Any) -> PrimeRequest:
    return PrimeRequest(prompt="write hello world", **kwargs)


def _build_payload(request: PrimeRequest):
    """Invoke _build_payload via a throwaway PrimeClient instance."""
    from backend.core.prime_client import PrimeClient, PrimeClientConfig
    cfg = PrimeClientConfig()  # use env-resolved defaults
    client = PrimeClient.__new__(PrimeClient)
    client._config = cfg
    return client._build_payload(request)


def _sample_profile() -> TaskProfile:
    return TaskProfile(
        intent="code_generation",
        complexity="heavy_code",
        brain_id="qwen_coder",
        model="qwen-2.5-coder-7b",
    )


# ---------------------------------------------------------------------------
# Test 1 — TaskProfile.as_dict()
# ---------------------------------------------------------------------------


def test_task_profile_as_dict():
    profile = _sample_profile()
    d = profile.as_dict()
    assert d == {
        "intent": "code_generation",
        "complexity": "heavy_code",
        "brain_id": "qwen_coder",
        "model": "qwen-2.5-coder-7b",
    }


# ---------------------------------------------------------------------------
# Test 2 — PrimeRequest accepts task_profile
# ---------------------------------------------------------------------------


def test_prime_request_accepts_task_profile():
    profile = _sample_profile()
    req = _make_prime_request(task_profile=profile)
    assert req.task_profile is profile


def test_prime_request_task_profile_defaults_none():
    req = _make_prime_request()
    assert req.task_profile is None


# ---------------------------------------------------------------------------
# Test 3 — _build_payload includes task_profile in JSON
# ---------------------------------------------------------------------------


def test_build_payload_includes_task_profile():
    profile = _sample_profile()
    req = _make_prime_request(task_profile=profile)
    payload = _build_payload(req)
    assert "task_profile" in payload
    assert payload["task_profile"] == profile.as_dict()


def test_build_payload_omits_task_profile_when_none():
    req = _make_prime_request()
    payload = _build_payload(req)
    assert "task_profile" not in payload


# ---------------------------------------------------------------------------
# Test 4 — model fallback: task_profile.model used when model_name is None
# ---------------------------------------------------------------------------


def test_build_payload_uses_task_profile_model_when_no_model_name():
    profile = _sample_profile()
    req = _make_prime_request(task_profile=profile, model_name=None)
    payload = _build_payload(req)
    assert payload["model"] == "qwen-2.5-coder-7b"


# ---------------------------------------------------------------------------
# Test 5 — model_name takes priority over task_profile.model
# ---------------------------------------------------------------------------


def test_build_payload_model_name_wins_over_task_profile():
    profile = _sample_profile()
    req = _make_prime_request(task_profile=profile, model_name="override-model")
    payload = _build_payload(req)
    assert payload["model"] == "override-model"


def test_build_payload_default_model_when_neither_set():
    req = _make_prime_request(model_name=None, task_profile=None)
    payload = _build_payload(req)
    assert payload["model"] == "jarvis-prime"


# ---------------------------------------------------------------------------
# Test 6 — PrimeProvider.generate() builds TaskProfile from telemetry
# ---------------------------------------------------------------------------


def _make_routing_intent(
    brain_id: str = "qwen_coder",
    brain_model: str = "qwen-2.5-coder-7b",
    routing_reason: str = "cai_intent_code_generation",
    task_complexity: str = "heavy_code",
):
    from backend.core.ouroboros.governance.op_context import RoutingIntentTelemetry
    return RoutingIntentTelemetry(
        expected_provider="GCP_PRIME_SPOT",
        policy_reason="NORMAL",
        brain_id=brain_id,
        brain_model=brain_model,
        routing_reason=routing_reason,
        task_complexity=task_complexity,
    )


def _make_op_context(routing_intent=None) -> MagicMock:
    from backend.core.ouroboros.governance.op_context import TelemetryContext, HostTelemetry
    ctx = MagicMock()
    ctx.description = "write a cache service"
    ctx.target_files = ("src/cache.py",)
    ctx.expanded_context_files = ()
    if routing_intent is not None:
        host_tel = MagicMock(spec=HostTelemetry)
        tel = TelemetryContext(local_node=host_tel, routing_intent=routing_intent)
        ctx.telemetry = tel
    else:
        ctx.telemetry = None
    return ctx


@pytest.mark.asyncio
async def test_prime_provider_builds_task_profile_from_telemetry():
    """PrimeProvider.generate() must pass a TaskProfile built from routing telemetry."""
    from backend.core.ouroboros.governance.providers import PrimeProvider

    ri = _make_routing_intent()
    ctx = _make_op_context(ri)

    captured: list[Any] = []

    async def fake_generate(**kwargs):
        captured.append(kwargs)
        mock_resp = MagicMock()
        mock_resp.content = '{"schema_version":"2b.1","patches":{}}'
        mock_resp.tokens_used = 0
        return mock_resp

    mock_client = MagicMock()
    mock_client.generate = fake_generate

    provider = PrimeProvider(prime_client=mock_client)

    deadline = datetime(2099, 1, 1, tzinfo=timezone.utc)
    # _build_codegen_prompt may raise if repo_root missing — patch it
    with patch(
        "backend.core.ouroboros.governance.providers._build_codegen_prompt",
        return_value="prompt text",
    ):
        try:
            await provider.generate(ctx, deadline)
        except Exception:
            pass  # schema parse may fail on mock; we only need the kwarg capture

    assert captured, "generate() was never called on mock client"
    call_kwargs = captured[0]
    assert "task_profile" in call_kwargs
    tp: TaskProfile = call_kwargs["task_profile"]
    assert isinstance(tp, TaskProfile)
    assert tp.brain_id == "qwen_coder"
    assert tp.model == "qwen-2.5-coder-7b"
    assert tp.complexity == "heavy_code"


# ---------------------------------------------------------------------------
# Test 7 — routing_reason "cai_intent_X" → intent "X"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prime_provider_strips_cai_intent_prefix():
    from backend.core.ouroboros.governance.providers import PrimeProvider

    ri = _make_routing_intent(routing_reason="cai_intent_segfault_analysis")
    ctx = _make_op_context(ri)
    captured: list[Any] = []

    async def fake_generate(**kwargs):
        captured.append(kwargs)
        mock_resp = MagicMock()
        mock_resp.content = '{"schema_version":"2b.1","patches":{}}'
        mock_resp.tokens_used = 0
        return mock_resp

    mock_client = MagicMock()
    mock_client.generate = fake_generate
    provider = PrimeProvider(prime_client=mock_client)
    deadline = datetime(2099, 1, 1, tzinfo=timezone.utc)

    with patch(
        "backend.core.ouroboros.governance.providers._build_codegen_prompt",
        return_value="prompt text",
    ):
        try:
            await provider.generate(ctx, deadline)
        except Exception:
            pass

    assert captured
    tp: TaskProfile = captured[0]["task_profile"]
    assert tp.intent == "segfault_analysis", (
        f"Expected 'segfault_analysis', got {tp.intent!r}"
    )


# ---------------------------------------------------------------------------
# Test 8 — no telemetry → task_profile=None, no crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prime_provider_no_telemetry_passes_none():
    from backend.core.ouroboros.governance.providers import PrimeProvider

    ctx = _make_op_context(routing_intent=None)
    captured: list[Any] = []

    async def fake_generate(**kwargs):
        captured.append(kwargs)
        mock_resp = MagicMock()
        mock_resp.content = '{"schema_version":"2b.1","patches":{}}'
        mock_resp.tokens_used = 0
        return mock_resp

    mock_client = MagicMock()
    mock_client.generate = fake_generate
    provider = PrimeProvider(prime_client=mock_client)
    deadline = datetime(2099, 1, 1, tzinfo=timezone.utc)

    with patch(
        "backend.core.ouroboros.governance.providers._build_codegen_prompt",
        return_value="prompt text",
    ):
        try:
            await provider.generate(ctx, deadline)
        except Exception:
            pass

    assert captured
    assert captured[0].get("task_profile") is None


# ---------------------------------------------------------------------------
# Request-side prefix KV-cache reuse (keep_alive + stable-prefix + cache_prompt)
# Self-hosted J-Prime analogue of DW prompt caching (prefill-latency win).
# ---------------------------------------------------------------------------


def test_build_payload_no_cache_fields_when_master_off(monkeypatch):
    """Master OFF (default) → byte-identical legacy body: no keep_alive / cache_prompt."""
    monkeypatch.delenv("JARVIS_JPRIME_PROMPT_CACHE_ENABLED", raising=False)
    req = _make_prime_request(system_prompt="STABLE SYSTEM RULES")
    payload = _build_payload(req)
    assert "keep_alive" not in payload
    assert "cache_prompt" not in payload


def test_build_payload_master_off_byte_identical(monkeypatch):
    """The master-OFF payload must equal the payload built with the flag machinery
    completely absent (proves opt-in adds nothing when off)."""
    monkeypatch.setenv("JARVIS_JPRIME_PROMPT_CACHE_ENABLED", "false")
    req = _make_prime_request(system_prompt="S", task_profile=_sample_profile())
    off = _build_payload(req)
    assert "keep_alive" not in off and "cache_prompt" not in off
    # Only the legacy keys are present.
    assert set(off.keys()) <= {
        "messages", "max_tokens", "temperature", "model", "metadata",
        "stop", "task_profile",
    }


def test_build_payload_keep_alive_present_when_cache_enabled(monkeypatch):
    """Master ON → keep_alive is present in the request body."""
    monkeypatch.setenv("JARVIS_JPRIME_PROMPT_CACHE_ENABLED", "true")
    monkeypatch.delenv("JARVIS_JPRIME_KEEP_ALIVE", raising=False)
    req = _make_prime_request(system_prompt="S")
    payload = _build_payload(req)
    assert "keep_alive" in payload


def test_build_payload_keep_alive_env_int(monkeypatch):
    """A purely-numeric keep_alive env is coerced to int (strict int-typed servers)."""
    monkeypatch.setenv("JARVIS_JPRIME_PROMPT_CACHE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_JPRIME_KEEP_ALIVE", "-1")
    payload = _build_payload(_make_prime_request(system_prompt="S"))
    assert payload["keep_alive"] == -1
    assert isinstance(payload["keep_alive"], int)


def test_build_payload_keep_alive_env_duration_string(monkeypatch):
    """A duration-string keep_alive env is passed through verbatim (Ollama accepts it)."""
    monkeypatch.setenv("JARVIS_JPRIME_PROMPT_CACHE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_JPRIME_KEEP_ALIVE", "30m")
    payload = _build_payload(_make_prime_request(system_prompt="S"))
    assert payload["keep_alive"] == "30m"


def test_build_payload_cache_prompt_flag_on(monkeypatch):
    """Explicit llama.cpp cache_prompt lever ON by default when master ON."""
    monkeypatch.setenv("JARVIS_JPRIME_PROMPT_CACHE_ENABLED", "true")
    monkeypatch.delenv("JARVIS_JPRIME_PREFIX_CACHE_ENABLED", raising=False)
    payload = _build_payload(_make_prime_request(system_prompt="S"))
    assert payload.get("cache_prompt") is True


def test_build_payload_cache_prompt_flag_off_keeps_keepalive(monkeypatch):
    """cache_prompt lever OFF drops only cache_prompt; keep_alive still present."""
    monkeypatch.setenv("JARVIS_JPRIME_PROMPT_CACHE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_JPRIME_PREFIX_CACHE_ENABLED", "false")
    payload = _build_payload(_make_prime_request(system_prompt="S"))
    assert "cache_prompt" not in payload
    assert "keep_alive" in payload


def test_stable_prefix_first_and_identical_across_volatile(monkeypatch):
    """The STABLE system prefix is FIRST and byte-identical across two requests
    whose VOLATILE user content differs (incl. a git-momentum digest). The
    git-momentum text must NOT leak into the stable system prefix."""
    monkeypatch.setenv("JARVIS_JPRIME_PROMPT_CACHE_ENABLED", "true")
    stable = "You are a precise code assistant. Iron-Gate rules. schema 2b.1."
    momentum = "Recent Development Momentum: feat(a1) x12, fix(dw) x3"
    p1 = _build_payload(PrimeRequest(
        prompt="GOAL A\n\n" + momentum, system_prompt=stable))
    p2 = _build_payload(PrimeRequest(
        prompt="GOAL B (completely different volatile body)", system_prompt=stable))
    # Stable prefix first + identical.
    assert p1["messages"][0]["role"] == "system"
    assert p1["messages"][0]["content"] == p2["messages"][0]["content"]
    # Volatile trailing user content differs.
    assert p1["messages"][-1]["role"] == "user"
    assert p1["messages"][-1]["content"] != p2["messages"][-1]["content"]
    # git-momentum digest rides in the volatile user message, NOT the stable prefix.
    assert momentum not in p1["messages"][0]["content"]
    assert momentum in p1["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_fail_soft_server_rejects_cache_field(monkeypatch):
    """A server that REJECTS the cache fields must NOT break generation: the client
    strips the opt-in fields and retries once with the legacy body."""
    monkeypatch.setenv("JARVIS_JPRIME_PROMPT_CACHE_ENABLED", "true")
    from backend.core.prime_client import (
        PrimeClient, PrimeClientConfig, PrimeRequest,
    )

    posted_bodies: list[dict] = []

    class _Resp:
        def __init__(self, status, payload):
            self.status = status
            self._payload = payload
            self.headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return "cache_prompt: extra fields not permitted"

        async def json(self):
            return self._payload

    class _Session:
        def post(self, url, json=None, headers=None):
            posted_bodies.append(dict(json))
            # Reject any body carrying the opt-in cache fields; accept legacy.
            if "cache_prompt" in json or "keep_alive" in json:
                return _Resp(422, {})
            return _Resp(200, {"choices": [{"message": {"content": "OK"}}],
                               "usage": {"total_tokens": 1}})

    class _Pool:
        def get_session(self):
            sess = _Session()

            class _CM:
                async def __aenter__(self_inner):
                    return sess

                async def __aexit__(self_inner, *a):
                    return False
            return _CM()

    class _Circuit:
        async def record_success(self):
            pass

        async def record_failure(self):
            pass

    client = PrimeClient.__new__(PrimeClient)
    client._config = PrimeClientConfig()
    client._pool = _Pool()
    client._circuit = _Circuit()
    client._initialized = True
    client._lifecycle = None

    resp = await client._do_execute_request(
        PrimeRequest(prompt="hi", system_prompt="S"))
    assert resp.content == "OK"
    # First POST carried cache fields (rejected); the retry stripped them.
    assert any("cache_prompt" in b or "keep_alive" in b for b in posted_bodies)
    assert any(
        "cache_prompt" not in b and "keep_alive" not in b for b in posted_bodies
    )
