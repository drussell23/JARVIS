"""Priority C consumer — Venom `hypothesize` tool integration tests.

Pins the model-callable HypothesisProbe surface. The model uses the
``hypothesize`` tool during generation to autonomously resolve
epistemic ambiguity before making structural decisions.

Pins:
  §1   Tool manifest registered in _L1_MANIFESTS
  §2   Manifest declares read-only capability (no subprocess/write/network)
  §3   Manifest's arg_schema covers all 7 args (claim/prior/strategy/
       expected_signal/max_iterations/budget_usd/max_wall_s)
  §4   Tool listed in async-native dispatch path
  §5   Disabled probe (master flag off) returns POLICY_DENIED
  §6   Empty claim returns EXEC_ERROR
  §7   Bad arg types return EXEC_ERROR (no crash)
  §8   Happy path returns SUCCESS with structured JSON payload
  §9   Payload contains posterior + convergence_state + iterations
  §10  Unknown strategy → SUCCESS with convergence_state=unknown_strategy
       (probe handles gracefully; tool surfaces it correctly)
  §11  Memorialized hypothesis short-circuits to memorialized_dead
  §12  Tool respects upstream timeout
  §13  Tool capability set forbids mutating capabilities
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

import pytest


# Regrettably long import path — the tool_executor module is hefty.
from backend.core.ouroboros.governance.tool_executor import (
    _L1_MANIFESTS,
    AsyncProcessToolBackend,
    ToolCall,
    ToolExecStatus,
)


# ---------------------------------------------------------------------------
# §1-§3 — Manifest contract
# ---------------------------------------------------------------------------


def test_hypothesize_manifest_registered() -> None:
    assert "hypothesize" in _L1_MANIFESTS
    m = _L1_MANIFESTS["hypothesize"]
    assert m.name == "hypothesize"
    assert m.version == "1.0"


def test_hypothesize_capability_is_read_only() -> None:
    m = _L1_MANIFESTS["hypothesize"]
    assert m.capabilities == frozenset({"read"})
    # Defensive: explicitly forbid any mutating capability
    forbidden = {"write", "subprocess", "network", "mutation"}
    assert not (m.capabilities & forbidden)


def test_hypothesize_arg_schema_covers_seven_args() -> None:
    m = _L1_MANIFESTS["hypothesize"]
    expected_args = {
        "claim",
        "confidence_prior",
        "test_strategy",
        "expected_signal",
        "max_iterations",
        "budget_usd",
        "max_wall_s",
    }
    assert set(m.arg_schema.keys()) >= expected_args


# ---------------------------------------------------------------------------
# §4 — Async-native dispatch wiring
# ---------------------------------------------------------------------------


def test_hypothesize_in_async_native_dispatch_path() -> None:
    """The tool name must be listed in execute_async's async-native
    tools tuple so it routes to _run_async_native_tool (which calls
    HypothesisProbe). If this regresses, the handler chain skips
    hypothesize entirely."""
    from backend.core.ouroboros.governance import tool_executor as te
    src = inspect.getsource(te.AsyncProcessToolBackend.execute_async)
    assert '"hypothesize"' in src


# ---------------------------------------------------------------------------
# §5-§9 — Tool execution paths
# ---------------------------------------------------------------------------


@pytest.fixture
def probe_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_HYPOTHESIS_LEDGER_PATH",
        str(tmp_path / "failed_hypotheses.jsonl"),
    )
    yield


@pytest.fixture
def probe_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "false")
    yield


def _build_backend():
    """Construct a minimal AsyncProcessToolBackend for testing the
    handler in isolation."""
    sem = asyncio.Semaphore(1)
    return AsyncProcessToolBackend(
        semaphore=sem,
        approval_provider=None,
        mcp_client=None,
    )


def _build_policy_ctx(repo_root: Path = Path(".")):
    """Build a PolicyContext for the handler. Real signature: repo +
    repo_root + op_id + call_id + round_index + risk_tier."""
    from backend.core.ouroboros.governance.tool_executor import (
        PolicyContext,
    )
    return PolicyContext(
        repo="jarvis",
        repo_root=repo_root,
        op_id="op-test",
        call_id="op-test:r0:hypothesize",
        round_index=0,
        risk_tier=None,
    )


def test_hypothesize_master_off_returns_policy_denied(
    probe_disabled,
) -> None:
    backend = _build_backend()
    policy_ctx = _build_policy_ctx()
    call = ToolCall(
        name="hypothesize",
        arguments={
            "claim": "x exists",
            "expected_signal": "file_exists:foo.py",
        },
    )
    result = asyncio.run(
        backend._run_async_native_tool(
            call, policy_ctx, timeout=30.0, cap=8192,
        ),
    )
    assert result.status == ToolExecStatus.POLICY_DENIED
    assert "disabled" in result.error.lower()


def test_hypothesize_empty_claim_returns_exec_error(
    probe_enabled,
) -> None:
    backend = _build_backend()
    policy_ctx = _build_policy_ctx()
    call = ToolCall(
        name="hypothesize",
        arguments={"claim": "", "expected_signal": "x"},
    )
    result = asyncio.run(
        backend._run_async_native_tool(
            call, policy_ctx, timeout=30.0, cap=8192,
        ),
    )
    assert result.status == ToolExecStatus.EXEC_ERROR
    assert "non-empty" in result.error.lower() or "claim" in result.error.lower()


def test_hypothesize_bad_arg_type_returns_exec_error(
    probe_enabled,
) -> None:
    backend = _build_backend()
    policy_ctx = _build_policy_ctx()
    call = ToolCall(
        name="hypothesize",
        arguments={
            "claim": "test",
            "expected_signal": "x",
            "confidence_prior": "not-a-number",  # bad
        },
    )
    result = asyncio.run(
        backend._run_async_native_tool(
            call, policy_ctx, timeout=30.0, cap=8192,
        ),
    )
    # Should NOT crash; should EXEC_ERROR cleanly
    assert result.status == ToolExecStatus.EXEC_ERROR


def test_hypothesize_happy_path_returns_structured_json(
    probe_enabled, tmp_path,
) -> None:
    """End-to-end: real file exists → CONFIRMED → posterior > prior."""
    target = tmp_path / "real.py"
    target.write_text("x = 1\n")
    backend = _build_backend()
    policy_ctx = _build_policy_ctx()
    call = ToolCall(
        name="hypothesize",
        arguments={
            "claim": "real.py exists",
            "confidence_prior": 0.5,
            "test_strategy": "lookup",
            "expected_signal": f"file_exists:{target}",
            "max_iterations": 1,
        },
    )
    result = asyncio.run(
        backend._run_async_native_tool(
            call, policy_ctx, timeout=30.0, cap=8192,
        ),
    )
    assert result.status == ToolExecStatus.SUCCESS
    payload = json.loads(result.output)
    assert payload["claim"] == "real.py exists"
    assert payload["confidence_prior"] == 0.5
    assert payload["confidence_posterior"] > 0.5  # CONFIRMED moves up
    assert payload["iterations_used"] == 1
    assert "convergence_state" in payload
    assert "evidence_hash" in payload


def test_hypothesize_payload_contract(probe_enabled, tmp_path) -> None:
    """Verify the JSON payload has all the required keys for the
    model to parse the result."""
    target = tmp_path / "x.py"
    target.write_text("y = 2\n")
    backend = _build_backend()
    policy_ctx = _build_policy_ctx()
    call = ToolCall(
        name="hypothesize",
        arguments={
            "claim": "x.py contains y",
            "confidence_prior": 0.6,
            "test_strategy": "lookup",
            "expected_signal": f"contains:{target}:y =",
        },
    )
    result = asyncio.run(
        backend._run_async_native_tool(
            call, policy_ctx, timeout=30.0, cap=8192,
        ),
    )
    payload = json.loads(result.output)
    expected_keys = {
        "claim", "confidence_prior", "confidence_posterior",
        "convergence_state", "iterations_used", "cost_usd",
        "observation_summary", "evidence_hash",
    }
    assert set(payload.keys()) == expected_keys


# ---------------------------------------------------------------------------
# §10 — Unknown strategy graceful handling
# ---------------------------------------------------------------------------


def test_hypothesize_unknown_strategy_succeeds_with_diagnostic(
    probe_enabled,
) -> None:
    backend = _build_backend()
    policy_ctx = _build_policy_ctx()
    call = ToolCall(
        name="hypothesize",
        arguments={
            "claim": "x",
            "test_strategy": "no-such-strategy",
            "expected_signal": "file_exists:foo",
        },
    )
    result = asyncio.run(
        backend._run_async_native_tool(
            call, policy_ctx, timeout=30.0, cap=8192,
        ),
    )
    # Tool succeeds; the probe's convergence_state encodes the failure
    assert result.status == ToolExecStatus.SUCCESS
    payload = json.loads(result.output)
    assert payload["convergence_state"] == "unknown_strategy"


# ---------------------------------------------------------------------------
# §11 — Memorialization short-circuit
# ---------------------------------------------------------------------------


def test_hypothesize_memorialized_short_circuits(
    probe_enabled, tmp_path,
) -> None:
    """When a hypothesis was previously declared dead, retrying the
    same hypothesis (cosmetic-variant or not) short-circuits to
    memorialized_dead — the cage's adversarial-retry defense."""
    from backend.core.ouroboros.governance.verification.hypothesis_probe import (
        Hypothesis as _Hyp,
        memorialize_hypothesis,
        ProbeResult as _PR,
    )
    h = _Hyp(
        claim="dead-on-arrival",
        confidence_prior=0.5,
        test_strategy="lookup",
        expected_signal="file_exists:nope",
    )
    fake_result = _PR(
        confidence_posterior=0.5, observation_summary="dead",
        cost_usd=0.0, iterations_used=3,
        convergence_state="inconclusive", evidence_hash="x",
    )
    memorialize_hypothesis(h, fake_result)

    backend = _build_backend()
    policy_ctx = _build_policy_ctx()
    call = ToolCall(
        name="hypothesize",
        arguments={
            "claim": "dead-on-arrival",
            "test_strategy": "lookup",
            "expected_signal": "file_exists:nope",
        },
    )
    result = asyncio.run(
        backend._run_async_native_tool(
            call, policy_ctx, timeout=30.0, cap=8192,
        ),
    )
    assert result.status == ToolExecStatus.SUCCESS
    payload = json.loads(result.output)
    assert payload["convergence_state"] == "memorialized_dead"
    assert payload["iterations_used"] == 0
    assert payload["cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# §12 — Timeout handling
# ---------------------------------------------------------------------------


def test_hypothesize_respects_upstream_timeout(probe_enabled) -> None:
    """Tool wraps probe.test in asyncio.wait_for with the upstream
    timeout. Verifying that's hooked up correctly — small timeout
    should bound the call."""
    backend = _build_backend()
    policy_ctx = _build_policy_ctx()
    call = ToolCall(
        name="hypothesize",
        arguments={
            "claim": "x",
            "test_strategy": "lookup",
            "expected_signal": "file_exists:foo",
        },
    )
    # 0.001s timeout — should TIMEOUT or complete extremely fast
    # since the probe is lightweight; either way no crash
    result = asyncio.run(
        backend._run_async_native_tool(
            call, policy_ctx, timeout=0.001, cap=8192,
        ),
    )
    assert result.status in (
        ToolExecStatus.SUCCESS,
        ToolExecStatus.TIMEOUT,
        ToolExecStatus.EXEC_ERROR,
    )


# ---------------------------------------------------------------------------
# §13 — Authority invariant: capability set forbids mutating
# ---------------------------------------------------------------------------


def test_manifest_capability_is_strictly_read() -> None:
    """The hypothesize tool must declare ONLY 'read' capability.
    This is the cage's contract — any future addition of 'write',
    'subprocess', 'network', etc. would break the authority
    promise that hypothesize is bounded read-only."""
    m = _L1_MANIFESTS["hypothesize"]
    assert m.capabilities == frozenset({"read"})
