"""Spine — P1 battle-test provider-readiness pre-flight gate.

Closes the v18 wasted-spend hole: refuse the SWE inject (→ $0) when
providers are already known-bad, BEFORE maybe_inject_swe_bench_at_boot.

Pinned invariants (operator-authorized):
  * default-FALSE byte-identity (flag off → PROCEED_DISABLED, no
    CB read / no probe).
  * source-order: assess_provider_readiness called STRICTLY before
    maybe_inject_swe_bench_at_boot; REFUSE short-circuits the inject.
  * NEVER-raises (raising CB / raising probe → a verdict, not an exc).
  * closed taxonomy (exact PreflightVerdict membership + is_refusal).
  * CB OPEN → REFUSE_CLAUDE_CB_OPEN (the v18 5xx/529 condition).
  * indeterminate → Option A PROCEED+WARN default; env-knob → B
    strict REFUSE.
  * composes-only (AST: imports get_claude_circuit_breaker; no new
    circuit-breaker class / no new http client).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import provider_preflight as pp


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in (
        "JARVIS_BATTLE_PREFLIGHT_PROVIDER_READINESS_ENABLED",
        "JARVIS_BATTLE_PREFLIGHT_STRICT_INDETERMINATE",
        "JARVIS_BATTLE_PREFLIGHT_PROBE_TIMEOUT_S",
    ):
        monkeypatch.delenv(v, raising=False)


class _CB:
    def __init__(self, allow):
        self._allow = allow

    def should_allow_request(self):
        return self._allow


def _patch_cb(monkeypatch, allow):
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.claude_circuit_breaker."
        "get_claude_circuit_breaker",
        lambda: _CB(allow),
    )


# ---------------------------------------------------------------------------
# default-FALSE byte-identity
# ---------------------------------------------------------------------------


def test_disabled_by_default_no_side_effects(monkeypatch):
    assert pp.preflight_enabled() is False
    called = {"cb": False}
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.claude_circuit_breaker."
        "get_claude_circuit_breaker",
        lambda: called.__setitem__("cb", True) or _CB(True),
    )
    import asyncio
    v = asyncio.run(pp.assess_provider_readiness())
    assert v is pp.PreflightVerdict.PROCEED_DISABLED
    assert called["cb"] is False, "flag off → no CB read (byte-identical)"


# ---------------------------------------------------------------------------
# CB OPEN → refuse (the v18 condition)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cb_open_refuses(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_BATTLE_PREFLIGHT_PROVIDER_READINESS_ENABLED", "true")
    _patch_cb(monkeypatch, allow=False)
    v = await pp.assess_provider_readiness()
    assert v is pp.PreflightVerdict.REFUSE_CLAUDE_CB_OPEN
    assert v.is_refusal is True


@pytest.mark.asyncio
async def test_cb_closed_no_probe_proceeds(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_BATTLE_PREFLIGHT_PROVIDER_READINESS_ENABLED", "true")
    _patch_cb(monkeypatch, allow=True)
    v = await pp.assess_provider_readiness()  # no probe_handle
    assert v is pp.PreflightVerdict.PROCEED
    assert v.is_refusal is False


# ---------------------------------------------------------------------------
# active probe
# ---------------------------------------------------------------------------


class _Healthy:
    async def health_probe(self) -> bool:
        return True


class _Unhealthy:
    async def health_probe(self) -> bool:
        return False


class _Raises:
    async def health_probe(self) -> bool:
        raise RuntimeError("provider down")


@pytest.mark.asyncio
async def test_probe_healthy_proceeds(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_BATTLE_PREFLIGHT_PROVIDER_READINESS_ENABLED", "true")
    _patch_cb(monkeypatch, allow=True)
    v = await pp.assess_provider_readiness(probe_handle=_Healthy())
    assert v is pp.PreflightVerdict.PROCEED


@pytest.mark.asyncio
async def test_probe_unhealthy_refuses(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_BATTLE_PREFLIGHT_PROVIDER_READINESS_ENABLED", "true")
    _patch_cb(monkeypatch, allow=True)
    v = await pp.assess_provider_readiness(probe_handle=_Unhealthy())
    assert v is pp.PreflightVerdict.REFUSE_PROVIDER_UNREACHABLE


@pytest.mark.asyncio
async def test_probe_raises_is_indeterminate_not_fatal(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_BATTLE_PREFLIGHT_PROVIDER_READINESS_ENABLED", "true")
    _patch_cb(monkeypatch, allow=True)
    # CB closed + probe raised (indeterminate) → CB authoritative →
    # PROCEED (Option A); never raises.
    v = await pp.assess_provider_readiness(probe_handle=_Raises())
    assert v in (
        pp.PreflightVerdict.PROCEED,
        pp.PreflightVerdict.PROCEED_INDETERMINATE_WARN,
    )
    assert not v.is_refusal


# ---------------------------------------------------------------------------
# indeterminate policy: Option A default vs B strict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_indeterminate_option_a_default(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_BATTLE_PREFLIGHT_PROVIDER_READINESS_ENABLED", "true")
    # CB unreachable (raises) + no probe → purely indeterminate
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.claude_circuit_breaker."
        "get_claude_circuit_breaker",
        lambda: (_ for _ in ()).throw(RuntimeError("cb gone")),
    )
    v = await pp.assess_provider_readiness()
    assert v is pp.PreflightVerdict.PROCEED_INDETERMINATE_WARN
    assert not v.is_refusal


@pytest.mark.asyncio
async def test_indeterminate_option_b_strict_refuses(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_BATTLE_PREFLIGHT_PROVIDER_READINESS_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_BATTLE_PREFLIGHT_STRICT_INDETERMINATE", "true")
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.claude_circuit_breaker."
        "get_claude_circuit_breaker",
        lambda: (_ for _ in ()).throw(RuntimeError("cb gone")),
    )
    v = await pp.assess_provider_readiness()
    assert v is pp.PreflightVerdict.REFUSE_PROVIDER_UNREACHABLE
    assert v.is_refusal is True


# ---------------------------------------------------------------------------
# closed taxonomy
# ---------------------------------------------------------------------------


def test_closed_taxonomy():
    assert {v.value for v in pp.PreflightVerdict} == {
        "proceed", "proceed_disabled", "proceed_indeterminate_warn",
        "refuse_claude_cb_open", "refuse_provider_unreachable",
    }
    refusals = {v for v in pp.PreflightVerdict if v.is_refusal}
    assert refusals == {
        pp.PreflightVerdict.REFUSE_CLAUDE_CB_OPEN,
        pp.PreflightVerdict.REFUSE_PROVIDER_UNREACHABLE,
    }


# ---------------------------------------------------------------------------
# AST — composes-only + harness source-order/short-circuit
# ---------------------------------------------------------------------------


def test_ast_composes_canonical_cb_no_new_stack():
    src = Path(pp.__file__).read_text(encoding="utf-8")
    assert "get_claude_circuit_breaker" in src, (
        "must compose the canonical Claude breaker singleton"
    )
    tree = ast.parse(src)
    # No new circuit-breaker class, no new http client/session.
    classes = {
        n.name for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef)
    }
    assert classes == {"PreflightVerdict"}, (
        f"only the verdict enum may be defined here; got {classes}"
    )
    assert "aiohttp" not in src and "httpx" not in src, (
        "no new HTTP client — composition only"
    )


def test_harness_preflight_precedes_inject_and_shortcircuits():
    h = Path(
        __import__(
            "backend.core.ouroboros.battle_test.harness",
            fromlist=["__file__"],
        ).__file__
    ).read_text(encoding="utf-8")
    assess = h.index("assess_provider_readiness()")
    inject = h.index("maybe_inject_swe_bench_at_boot(\n", assess)
    assert assess < inject, (
        "pre-flight must be assessed BEFORE the spend-causing "
        "maybe_inject_swe_bench_at_boot"
    )
    # REFUSE short-circuits the inject (skip, no spend).
    win = h[assess:inject + 60]
    assert "_preflight_refused" in win
    assert "if _preflight_refused:" in h
    # the inject is gated under the not-refused branch
    refused_blk = h.index("if _preflight_refused:")
    else_inject = h.index("maybe_inject_swe_bench_at_boot(", refused_blk)
    assert refused_blk < else_inject
