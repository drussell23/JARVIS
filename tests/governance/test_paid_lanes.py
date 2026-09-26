"""The paid-lane authority, and every seam that now asks it.

2026-09-26: with $0 behind both keys, the organism still reached for Claude
and DoubleWord — a key in .env switched off the local-first pre-route, failed
local generations cascaded into a Claude fallback that could not answer, and
~15 paths built their own paid clients. Each test below pins one seam: with
paid lanes declared off, the paid lane is never CALLED, and the work lands on
the local lane instead of failing.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import paid_lanes as pl


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("JARVIS_PAID_LANES_ENABLED", "JARVIS_PROVIDER_CLAUDE_DISABLED",
                 "JARVIS_PROVIDER_DOUBLEWORD_DISABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-unfunded")
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "dw-test-unfunded")
    monkeypatch.setattr(pl, "_aegis_holds_credentials", lambda: False)
    monkeypatch.setattr(pl, "_economically_dead", lambda lane: None)
    pl.reset_for_tests()
    yield
    pl.reset_for_tests()


@pytest.fixture()
def paid_off(monkeypatch):
    monkeypatch.setenv("JARVIS_PAID_LANES_ENABLED", "false")


# --------------------------------------------------------------------------
# The authority
# --------------------------------------------------------------------------

def test_keys_alone_make_both_lanes_configured():
    """Unset master keeps the historical behaviour for funded hosts."""
    assert pl.paid_lane_configured("claude") and pl.paid_lane_configured("doubleword")


def test_the_operator_declaration_turns_every_paid_lane_off(paid_off):
    for name in ("claude", "doubleword", "claude-api", "dw", "doubleword-397b"):
        v = pl.allowed_verdict(name)
        assert not v.allowed and "JARVIS_PAID_LANES_ENABLED=false" in v.reason
    assert pl.posture()["mode"] == "local-only"


def test_the_per_provider_switch_is_derived_not_hardcoded(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_DOUBLEWORD_DISABLED", "true")
    assert not pl.paid_lane_configured("doubleword")
    assert pl.paid_lane_configured("claude")
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")   # Slice 19a
    assert not pl.paid_lane_configured("claude")


def test_no_credential_means_no_lane_on_this_host(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert pl.configured_verdict("claude").reason == "no ANTHROPIC_API_KEY"


def test_the_three_questions_stay_separate(monkeypatch):
    """Found by the before/after: folding the credential into every verdict
    refused explicitly-keyed providers and changed what "structurally
    disabled" meant to eight readers in a keyless environment."""
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("DOUBLEWORD_API_KEY")
    assert pl.paid_lane_switched_on("claude")        # nobody switched it off
    assert pl.paid_lane_allowed("claude")            # a keyed caller may call
    assert not pl.paid_lane_configured("claude")     # but no lane on this host
    from backend.core.ouroboros.governance import candidate_generator as cg
    assert cg._claude_config_disabled() is False     # pre-existing meaning kept
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    assert DoublewordProvider(api_key="explicit-key").is_available is True


def test_free_lanes_are_never_refused(paid_off):
    for name in ("local", "gcp-jprime", "prime", ""):
        assert pl.paid_lane_allowed(name)


def test_model_ids_resolve_to_their_lane():
    assert pl.lane_for("claude-sonnet-4-6").name == "claude"
    assert pl.lane_for("doubleword-397b").name == "doubleword"
    assert pl.lane_for("qwen3-coder-ov:30b") is None


def test_an_observed_unfunded_lane_is_refused_while_the_observation_stands(monkeypatch):
    """The ADAPTIVE half: configured, but the ledger saw it refuse for money."""
    monkeypatch.setattr(pl, "_economically_dead",
                        lambda lane: "credit balance too low" if lane.name == "claude" else None)
    assert pl.paid_lane_configured("claude")            # boot would still build it
    v = pl.allowed_verdict("claude")
    assert not v.allowed and "unfunded (observed)" in v.reason
    assert pl.paid_lane_allowed("doubleword")


def test_require_paid_lane_raises_the_typed_refusal(paid_off):
    with pytest.raises(pl.PaidLaneDisabled) as exc:
        pl.require_paid_lane("claude")
    assert exc.value.provider == "claude"


# --------------------------------------------------------------------------
# Boot: neither paid provider is constructed
# --------------------------------------------------------------------------

def test_the_boot_gate_builds_neither_paid_provider(paid_off):
    from backend.core.ouroboros.governance.governed_loop_service import (
        _provider_construction_gate,
    )
    assert not _provider_construction_gate(local_api_key="k", provider_name="claude")
    assert not _provider_construction_gate(local_api_key="k", provider_name="doubleword")


def test_the_boot_gate_is_unchanged_for_a_funded_host():
    from backend.core.ouroboros.governance.governed_loop_service import (
        _provider_construction_gate,
    )
    assert _provider_construction_gate(local_api_key="k", provider_name="claude")


# --------------------------------------------------------------------------
# Routing: the local lane becomes first for EVERY route
# --------------------------------------------------------------------------

def test_keys_in_env_no_longer_switch_off_the_local_first_pre_route(paid_off, monkeypatch):
    """THE root cause: `_free_lane_active()` said "not free" because a key
    existed, so `_try_local_primary` never ran."""
    from backend.core.ouroboros.governance import candidate_generator as cg
    from backend.core.ouroboros.governance import local_inference_director as lid
    monkeypatch.setattr(lid, "local_prime_enabled", lambda: True)
    monkeypatch.setattr(cg, "_refresh_paid_lane_credentials", lambda: None)
    assert cg._free_lane_active() is True
    assert cg._claude_config_disabled() is True


def test_immediate_ops_are_demoted_instead_of_failing(paid_off):
    from backend.core.ouroboros.governance import urgency_router as ur
    assert ur._claude_tier_structurally_absent() is True


def test_the_dw_sentinel_does_not_activate_with_no_paid_lane(paid_off, monkeypatch):
    from backend.core.ouroboros.governance import candidate_generator as cg
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    assert cg._slice23_should_activate_sentinel("standard") == (False, "no_paid_lanes")


# --------------------------------------------------------------------------
# The shared call seams
# --------------------------------------------------------------------------

def test_the_gate_answers_locally_and_never_calls_a_paid_tier(paid_off, monkeypatch):
    from backend.core.ouroboros.governance import rt_gate
    called = []

    async def _paid(*a, **k):
        called.append("paid")
        return "paid answer"

    async def _local(*a, **k):
        return "local answer"

    monkeypatch.setattr(rt_gate, "_try_claude", _paid)
    monkeypatch.setattr(rt_gate, "_try_dw_rt", _paid)
    monkeypatch.setattr(rt_gate, "_try_local", _local)
    monkeypatch.setattr(rt_gate, "local_tier_enabled", lambda: True)
    text, tier = asyncio.run(rt_gate.gate_completion_detailed("q", caller_id="t"))
    assert (text, tier) == ("local answer", "local") and called == []


def test_the_gate_with_no_tier_at_all_says_why(paid_off, monkeypatch):
    from backend.core.ouroboros.governance import rt_gate
    monkeypatch.setattr(rt_gate, "local_tier_enabled", lambda: False)
    with pytest.raises(rt_gate.GateProviderExhaustedError) as exc:
        asyncio.run(rt_gate.gate_completion("q", caller_id="t"))
    assert "paid lanes refused: claude,dw" in str(exc.value) \
        or "paid lanes refused: dw,claude" in str(exc.value)


def test_the_one_anthropic_factory_refuses(paid_off):
    from backend.core.ouroboros.governance.aegis_provider_bridge import (
        make_async_anthropic_client,
    )
    with pytest.raises(pl.PaidLaneDisabled):
        make_async_anthropic_client(api_key="sk-test-unfunded")


def test_both_dw_auth_builders_refuse(paid_off):
    from backend.core.ouroboros.governance import aegis_provider_bridge as b
    with pytest.raises(pl.PaidLaneDisabled):
        b.dw_authorization_header()
    with pytest.raises(pl.PaidLaneDisabled):
        asyncio.run(b.dw_session_auth_header())


def test_claude_inference_returns_its_no_answer_contract(paid_off):
    from backend.core.ouroboros.claude_fallback import claude_inference
    assert asyncio.run(claude_inference("q")) is None


def test_the_rt_lane_is_answered_by_the_local_gate(paid_off, monkeypatch):
    from backend.core.ouroboros.governance import cognition_lanes, rt_gate
    seen = {}

    async def _gate(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return "local text"

    monkeypatch.setattr(rt_gate, "gate_completion", _gate)
    out = asyncio.run(cognition_lanes.rt_prompt("p", model="m", caller_id="c"))
    assert out == "local text" and seen["caller_id"] == "c"


def test_dw_reports_unavailable(paid_off):
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    assert DoublewordProvider(api_key="dw-test-unfunded").is_available is False


def test_the_dw_recovery_supervisor_does_not_arm(paid_off):
    from backend.core.ouroboros.governance.autonomous_supervisor import (
        AutonomousSupervisor,
    )
    sup = AutonomousSupervisor.__new__(AutonomousSupervisor)
    sup.state = "DORMANT"
    sup._open_db = lambda: pytest.fail("must not even read the queue")
    assert asyncio.run(sup.evaluate()) is False


# --------------------------------------------------------------------------
# The cockpit
# --------------------------------------------------------------------------

def test_cockpit_questions_are_answered_by_the_local_gate(paid_off):
    from backend.core.ouroboros.governance import fast_path_qa as fq
    assert fq._default_provider_callable() is fq._default_gate_callable


def test_cockpit_questions_still_use_claude_when_funded():
    from backend.core.ouroboros.governance import fast_path_qa as fq
    assert fq._default_provider_callable() is fq._default_claude_callable


def test_the_status_chip_reads_local_when_paid_lanes_are_declared_off(paid_off, monkeypatch):
    from backend.core.ouroboros.battle_test.status_line import StatusLineBuilder
    from backend.core.ouroboros.governance import capability_state, economic_state
    monkeypatch.setattr(economic_state, "display_liquidity",
                        lambda **_: {"readable": True, "economic_dry": []})
    monkeypatch.setattr(capability_state.CapabilityEvaluator, "_read_remote",
                        staticmethod(lambda: ("serving", "localhost:11434", True)))
    assert StatusLineBuilder()._sample_funding() == ("local", "localhost:11434")
