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
