"""Hosted resilience lane (LongCat stub Phase 1) — regression spine.

Proves the four mandates structurally:
- policy-shape-driven arming (no vendor strings in the FSM)
- instantiation-boundary endpoint injection via the ClaudeProvider seam
- Slice 4 T2 ``is_budget_refusal`` taxonomy composition (zero duplication)
- fail-soft everywhere: dark lane == byte-identical legacy behavior
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.hosted_resilience_lane import (
    HostedResilienceLane,
    LaneConfig,
    load_lane_configs,
    preflight,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MASTER_ENV = "JARVIS_HOSTED_RESILIENCE_LANE_ENABLED"


def _cfg(**over) -> LaneConfig:
    base = dict(
        name="testvendor",
        dialect="anthropic",
        endpoint="https://api.example-vendor.test/anthropic",
        api_key_env="JARVIS_TESTVENDOR_API_KEY",
        lane_model="Test-Model-Chat",
        routes=("background", "speculative"),
        policy_enabled=True,
        master_env=_MASTER_ENV,
        verdict_artifact="phase0-report.json",
        required_verdict="PATH_A_VERIFIED",
    )
    base.update(over)
    return LaneConfig(**base)


@pytest.fixture()
def clean_env(monkeypatch):
    for var in (_MASTER_ENV, "JARVIS_TESTVENDOR_API_KEY",
                "JARVIS_AEGIS_URL", "JARVIS_AEGIS_BOOTSTRAP_PSK"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _write_verdict(tmp_path: Path, verdict: str) -> None:
    (tmp_path / "phase0-report.json").write_text(
        json.dumps({"verdict": verdict}), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Policy parsing — generic shape, real policy file
# ---------------------------------------------------------------------------

def test_real_policy_parses_and_ships_dark():
    """The checked-in policy declares at least one resilience-lane candidate
    and EVERY one ships policy_enabled=False (double-dark invariant)."""
    configs = load_lane_configs()
    assert configs, "no resilience_lane candidates parsed from real policy"
    for cfg in configs:
        assert cfg.policy_enabled is False, (
            f"candidate {cfg.name!r} ships enabled=true — Phase 1 must be dark"
        )
        assert cfg.routes, "lane declares no routes"
        assert cfg.master_env, "lane declares no master env"
        assert cfg.required_verdict, "lane declares no verdict gate"


def test_unreadable_policy_returns_empty(tmp_path):
    bad = tmp_path / "nope.yaml"
    assert load_lane_configs(bad) == []  # missing file → dark, never raises
    bad.write_text("{{{not yaml", encoding="utf-8")
    assert load_lane_configs(bad) == []


# ---------------------------------------------------------------------------
# Preflight gate ladder (Mandate 4 — each reason a distinct class)
# ---------------------------------------------------------------------------

def test_master_env_off_disarms(clean_env):
    armed, reason = preflight(_cfg())
    assert (armed, reason) == (False, "master_env_off")


def test_policy_disabled_disarms(clean_env):
    clean_env.setenv(_MASTER_ENV, "true")
    armed, reason = preflight(_cfg(policy_enabled=False))
    assert (armed, reason) == (False, "policy_disabled")


def test_openai_dialect_disarms_with_path_b_pointer(clean_env):
    clean_env.setenv(_MASTER_ENV, "true")
    armed, reason = preflight(_cfg(dialect="openai"))
    assert armed is False and reason.startswith("dialect_unsupported:openai")


def test_aegis_active_disarms(clean_env, tmp_path):
    """Factory would swap api_key for the daemon placeholder — the lane must
    refuse to arm (mirrors probe_longcat_phase0's BLOCKED_AEGIS gate)."""
    clean_env.setenv(_MASTER_ENV, "true")
    clean_env.setenv("JARVIS_TESTVENDOR_API_KEY", "sk-test")
    clean_env.setenv("JARVIS_AEGIS_URL", "http://127.0.0.1:9999")
    clean_env.setenv("JARVIS_AEGIS_BOOTSTRAP_PSK", "psk-test")
    _write_verdict(tmp_path, "PATH_A_VERIFIED")
    armed, reason = preflight(_cfg(), repo_root=tmp_path)
    assert (armed, reason) == (False, "aegis_active_would_placeholder_key")


def test_missing_credentials_disarm(clean_env, tmp_path):
    clean_env.setenv(_MASTER_ENV, "true")
    _write_verdict(tmp_path, "PATH_A_VERIFIED")
    armed, reason = preflight(_cfg(), repo_root=tmp_path)
    assert armed is False and reason.startswith("no_credentials:")


def test_phase0_artifact_missing_disarms(clean_env, tmp_path):
    clean_env.setenv(_MASTER_ENV, "true")
    clean_env.setenv("JARVIS_TESTVENDOR_API_KEY", "sk-test")
    armed, reason = preflight(_cfg(), repo_root=tmp_path)
    assert (armed, reason) == (False, "phase0_artifact_missing")


def test_phase0_wrong_verdict_disarms(clean_env, tmp_path):
    clean_env.setenv(_MASTER_ENV, "true")
    clean_env.setenv("JARVIS_TESTVENDOR_API_KEY", "sk-test")
    _write_verdict(tmp_path, "PATH_A_REJECTED_PIVOT_PATH_B")
    armed, reason = preflight(_cfg(), repo_root=tmp_path)
    assert (armed, reason) == (
        False, "phase0_verdict:PATH_A_REJECTED_PIVOT_PATH_B",
    )


def test_full_gate_ladder_arms(clean_env, tmp_path):
    clean_env.setenv(_MASTER_ENV, "true")
    clean_env.setenv("JARVIS_TESTVENDOR_API_KEY", "sk-test")
    _write_verdict(tmp_path, "PATH_A_VERIFIED")
    armed, reason = preflight(_cfg(), repo_root=tmp_path)
    assert (armed, reason) == (True, "armed")


# ---------------------------------------------------------------------------
# Provider construction — the instantiation-boundary seam (Mandates 1+3)
# ---------------------------------------------------------------------------

_LANE_POLICY_YAML = """
hosted_provider_candidates:
  testvendor:
    dialect: "anthropic"
    endpoint: "https://api.example-vendor.test/anthropic"
    api_key_env: "JARVIS_TESTVENDOR_API_KEY"
    models:
      - model_name: "Test-Model-Chat"
        context_window: 131072
        pricing_per_mtok: { input_usd: 0.20, output_usd: 0.80 }
    resilience_lane:
      enabled: true
      routes: ["background", "speculative"]
      master_env: "JARVIS_HOSTED_RESILIENCE_LANE_ENABLED"
      phase0_verdict_artifact: "phase0-report.json"
      required_verdict: "PATH_A_VERIFIED"
      lane_model: "Test-Model-Chat"
"""


def _armed_lane(tmp_path, monkeypatch) -> HostedResilienceLane:
    monkeypatch.setenv(_MASTER_ENV, "true")
    monkeypatch.setenv("JARVIS_TESTVENDOR_API_KEY", "sk-test-lane")
    for var in ("JARVIS_AEGIS_URL", "JARVIS_AEGIS_BOOTSTRAP_PSK"):
        monkeypatch.delenv(var, raising=False)
    policy = tmp_path / "policy.yaml"
    policy.write_text(_LANE_POLICY_YAML, encoding="utf-8")
    _write_verdict(tmp_path, "PATH_A_VERIFIED")
    return HostedResilienceLane(policy_path=policy, repo_root=tmp_path)


def test_lane_builds_claudeprovider_with_policy_endpoint(tmp_path, monkeypatch):
    """The lane provider is a REAL ClaudeProvider whose base_url landed at
    the instantiation boundary from policy — no URL surgery anywhere."""
    from backend.core.ouroboros.governance.providers import ClaudeProvider

    lane = _armed_lane(tmp_path, monkeypatch)
    provider, reason = lane.provider_for_route("background")
    assert reason == "armed:testvendor"
    assert isinstance(provider, ClaudeProvider)
    assert provider._base_url == "https://api.example-vendor.test/anthropic"
    assert provider._model == "Test-Model-Chat"
    # Same object cached on the second consult
    provider2, _ = lane.provider_for_route("speculative")
    assert provider2 is provider


def test_undeclared_route_stays_dark(tmp_path, monkeypatch):
    lane = _armed_lane(tmp_path, monkeypatch)
    provider, reason = lane.provider_for_route("immediate")
    assert provider is None and reason == "no_armed_lane"


def test_claudeprovider_base_url_default_is_none():
    """base_url=None must be byte-identical legacy behavior (the empty-dict
    kwargs branch at both factory sites)."""
    from backend.core.ouroboros.governance.providers import ClaudeProvider
    p = ClaudeProvider(api_key="sk-legacy-shape")
    assert p._base_url is None


def test_real_policy_lane_is_dark_end_to_end(clean_env):
    """Against the CHECKED-IN policy + clean env: no route arms. This is the
    'dark lane == byte-identical production' invariant."""
    lane = HostedResilienceLane()
    for route in ("background", "speculative"):
        provider, reason = lane.provider_for_route(route)
        assert provider is None, f"lane armed unexpectedly on {route}: {reason}"


# ---------------------------------------------------------------------------
# Generator consult — T2 taxonomy composition (Mandate 3) + fail-soft
# ---------------------------------------------------------------------------

def _generator_with_fake_lane(fake_provider):
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    gen = CandidateGenerator.__new__(CandidateGenerator)
    gen._hosted_resilience_lane = SimpleNamespace(
        provider_for_route=lambda route: (fake_provider, "armed:fake"),
    )
    return gen


def _ctx():
    return SimpleNamespace(op_id="op-lane-test")


@pytest.mark.asyncio
async def test_budget_refusal_propagates_on_t2_axis():
    """A SessionBudgetPreflightRefused raised inside the lane provider must
    PROPAGATE (local wallet gate — Slice 4 T2 axis), never be swallowed as
    a lane fault. is_budget_refusal must classify it, including through the
    BACKGROUND error-shaping's `raise ... from exc` chain."""
    from datetime import datetime, timedelta, timezone

    from backend.core.ouroboros.governance.session_budget_authority import (
        SessionBudgetPreflightRefused,
        is_budget_refusal,
    )

    refusal = SessionBudgetPreflightRefused(
        provider="testvendor", estimated_cost_usd=0.05,
        session_remaining_usd=0.0,
    )

    class _RefusingProvider:
        async def generate(self, context, deadline):
            raise refusal

    gen = _generator_with_fake_lane(_RefusingProvider())
    deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
    with pytest.raises(SessionBudgetPreflightRefused) as excinfo:
        await gen._try_hosted_resilience_lane(
            _ctx(), deadline, route="background", tier0_error="dw_down",
        )
    assert is_budget_refusal(excinfo.value) is True
    # And through a transport wrapper (the BG shaping preserves __cause__):
    try:
        raise RuntimeError("background_fallback_failed:wrapper") from excinfo.value
    except RuntimeError as wrapped:
        assert is_budget_refusal(wrapped) is True


@pytest.mark.asyncio
async def test_lane_transport_failure_falls_through_to_none():
    """Any non-budget lane failure returns None — the caller's legacy
    cascade/raise stays byte-identical (Bulletproof)."""
    from datetime import datetime, timedelta, timezone

    class _BrokenProvider:
        async def generate(self, context, deadline):
            raise TimeoutError("vendor stalled")

    gen = _generator_with_fake_lane(_BrokenProvider())
    deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
    result = await gen._try_hosted_resilience_lane(
        _ctx(), deadline, route="background", tier0_error="dw_down",
    )
    assert result is None


@pytest.mark.asyncio
async def test_lane_success_returns_result():
    from datetime import datetime, timedelta, timezone

    fake_result = SimpleNamespace(
        candidates=[object()], generation_duration_s=0.5, cost_usd=0.003,
    )

    class _GoodProvider:
        async def generate(self, context, deadline):
            return fake_result

    gen = _generator_with_fake_lane(_GoodProvider())
    deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
    result = await gen._try_hosted_resilience_lane(
        _ctx(), deadline, route="background", tier0_error="dw_down",
    )
    assert result is fake_result


@pytest.mark.asyncio
async def test_broken_lane_module_never_bricks_dispatch(monkeypatch):
    """Import failure marks the lane broken and returns None forever after
    — generator dispatch survives a broken lane module (Mandate 4)."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    gen = CandidateGenerator.__new__(CandidateGenerator)
    gen._hosted_resilience_lane_broken = True  # simulate prior import failure
    provider, reason = gen._get_resilience_provider("background")
    assert provider is None and reason == "lane_module_broken"


# ---------------------------------------------------------------------------
# Source pins — the two exhaustion-site hooks (idiomatic, cf. Slice 4 T2)
# ---------------------------------------------------------------------------

def _generator_src() -> str:
    import inspect

    from backend.core.ouroboros.governance import candidate_generator as cg
    return (
        inspect.getsource(cg.CandidateGenerator._generate_background)
        + inspect.getsource(cg.CandidateGenerator._generate_speculative)
    )


def test_background_consults_lane_before_claude_cascade():
    src = _generator_src()
    lane = src.index("_try_hosted_resilience_lane(")
    cascade = src.index("Either cascade to Claude or raise.")
    assert lane < cascade, (
        "BACKGROUND must consult the resilience lane at DW exhaustion "
        "BEFORE the Claude-cascade/raise decision"
    )


def test_speculative_dispatches_lane_when_tier0_down():
    src = _generator_src()
    assert "_get_resilience_provider(" in src
    assert src.index("_get_resilience_provider(") < src.index(
        'raise RuntimeError("speculative_deferred")'
    )
