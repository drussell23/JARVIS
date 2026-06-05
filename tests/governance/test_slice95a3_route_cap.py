"""Slice 95a-3 — Aegis route-cap authorization + honest lease-denial propagation.

TDD regression spine for the two illuminated root causes of the $0/0
Aegis-routed calibration:

  (Bug 1 — the unblock) The Aegis daemon's per-route caps fail-closed to 0.0
  when unset, so EVERY mutation lease is denied with
  ``cost_ceiling_exceeded`` ("route IMMEDIATE cap exceeded").  The operator's
  ``--budget-usd`` authorization never reached the route cap.  Fix: a single
  seam ``_MUTATION_LEASE_ROUTE`` shared by ``mutate()`` and the calibration,
  which authorizes ``JARVIS_AEGIS_ROUTE_CAP_<ROUTE>_USD`` from --budget-usd.

  (Bug 2 — honesty) ``mutate()`` correctly raises ``AegisLeaseError`` on a
  lease denial (documented ZERO-LEAK fatal), but two caller layers
  (``run_immunization_campaign`` per-seed + ``summarize_campaign`` drain)
  swallowed it into "0 LLM candidates" → call_attempts==0 → misdiagnosed as
  config starvation.  Fix: re-raise ``AegisLeaseError`` at both layers; the
  calibration catches it and reports the real cause.

All tests are hermetic — they mock the lease seam and never touch a daemon.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Sequence

import pytest

from backend.core.ouroboros.governance import self_immunization as si
from backend.core.ouroboros.governance.self_immunization import AegisLeaseError
from backend.core.ouroboros.aegis.flags import env_route_cap, route_caps_usd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _seed(name: str = "s0", source: str = "x = 1\n"):
    """A minimal duck-typed seed (name / source / category.value)."""
    return SimpleNamespace(
        name=name,
        source=source,
        category=SimpleNamespace(value="sandbox_escape"),
    )


class _RaisingProvider:
    """A MutationProvider whose mutate() raises a chosen exception."""

    def __init__(self, exc: BaseException):
        self._exc = exc
        self.call_attempts = 0
        self.generated_count = 0
        self._budget_guard = None

    async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
        self.call_attempts += 1
        raise self._exc


# ---------------------------------------------------------------------------
# Bug 1 — route constant + cap authorization wiring
# ---------------------------------------------------------------------------
def test_mutation_lease_route_constant_is_immediate():
    """The single seam exists and matches the daemon route the lease uses."""
    assert si._MUTATION_LEASE_ROUTE == "IMMEDIATE"


def test_env_route_cap_composes_the_daemon_cap_var():
    assert (
        env_route_cap(si._MUTATION_LEASE_ROUTE)
        == "JARVIS_AEGIS_ROUTE_CAP_IMMEDIATE_USD"
    )


def test_authorizing_route_cap_env_reaches_daemon_cap_map(monkeypatch):
    """Setting the per-route cap env (as the calibration does from
    --budget-usd) is reflected by route_caps_usd() — proving the operator's
    authorization reaches the daemon's cap map instead of the fail-closed 0.0
    that denied every lease."""
    cap_env = env_route_cap(si._MUTATION_LEASE_ROUTE)
    # Fail-closed baseline: unset → 0.0 (the bug).
    monkeypatch.delenv(cap_env, raising=False)
    assert route_caps_usd().get("IMMEDIATE") == 0.0
    # Authorized: the operator's budget flows to the route cap.
    monkeypatch.setenv(cap_env, repr(0.5))
    assert route_caps_usd().get("IMMEDIATE") == pytest.approx(0.5)


def test_authorize_aegis_caps_sets_all_three_fail_closed_dims(monkeypatch):
    """_authorize_aegis_caps raises the route, session AND hourly caps from
    --budget-usd — not just the route cap.  All three fail-close to 0.0, so
    authorizing only one still denies every lease (the session-cap denial that
    surfaced after the route-cap fix)."""
    import scripts.security.run_cc_parity_calibration as cal
    from backend.core.ouroboros.aegis.flags import (
        ENV_AEGIS_SESSION_CAP_USD,
        ENV_AEGIS_HOURLY_BURN_CAP_USD,
    )

    route_env = env_route_cap(si._MUTATION_LEASE_ROUTE)
    for e in (route_env, ENV_AEGIS_SESSION_CAP_USD, ENV_AEGIS_HOURLY_BURN_CAP_USD):
        monkeypatch.delenv(e, raising=False)

    out = cal._authorize_aegis_caps(0.75, si._MUTATION_LEASE_ROUTE)

    assert set(out) == {
        route_env,
        ENV_AEGIS_SESSION_CAP_USD,
        ENV_AEGIS_HOURLY_BURN_CAP_USD,
    }
    assert all(v == pytest.approx(0.75) for v in out.values())
    assert float(os.environ[ENV_AEGIS_SESSION_CAP_USD]) == pytest.approx(0.75)
    assert float(os.environ[ENV_AEGIS_HOURLY_BURN_CAP_USD]) == pytest.approx(0.75)


def test_authorize_aegis_caps_never_lowers_explicit_operator_cap(monkeypatch):
    import scripts.security.run_cc_parity_calibration as cal
    from backend.core.ouroboros.aegis.flags import ENV_AEGIS_SESSION_CAP_USD

    # Operator set a LARGER session cap explicitly — must be preserved.
    monkeypatch.setenv(ENV_AEGIS_SESSION_CAP_USD, repr(5.0))
    out = cal._authorize_aegis_caps(0.75, si._MUTATION_LEASE_ROUTE)
    assert out[ENV_AEGIS_SESSION_CAP_USD] == pytest.approx(5.0)
    assert float(os.environ[ENV_AEGIS_SESSION_CAP_USD]) == pytest.approx(5.0)


def test_authorize_aegis_caps_noop_on_zero_budget(monkeypatch):
    """dry-run / zero-budget leaves the daemon fail-closed by design."""
    import scripts.security.run_cc_parity_calibration as cal
    from backend.core.ouroboros.aegis.flags import ENV_AEGIS_SESSION_CAP_USD

    monkeypatch.delenv(ENV_AEGIS_SESSION_CAP_USD, raising=False)
    assert cal._authorize_aegis_caps(0.0, si._MUTATION_LEASE_ROUTE) == {}
    assert ENV_AEGIS_SESSION_CAP_USD not in os.environ


def test_mutate_leases_under_the_named_route(monkeypatch):
    """mutate() requests its call-lease on _MUTATION_LEASE_ROUTE.  We capture
    the route by intercepting acquire_call_lease and forcing the denial path,
    which also asserts a denial becomes AegisLeaseError (zero-leak)."""
    from backend.core.ouroboros.governance import aegis_provider_bridge as _apb
    from backend.core.ouroboros.aegis import client as _aegis_client_mod

    captured = {}

    async def _fake_acquire(*, op_id, route, estimated_cost_usd):
        captured["route"] = route
        captured["op_id"] = op_id
        raise RuntimeError("lease denied: reason=cost_ceiling_exceeded")

    monkeypatch.setattr(_aegis_client_mod, "is_enabled", lambda: True)
    monkeypatch.setattr(_apb, "acquire_call_lease", _fake_acquire)

    provider = si.LLMMutationProvider(budget_guard=si.MutationBudgetGuard(0.5))

    with pytest.raises(AegisLeaseError):
        asyncio.run(provider.mutate("x = 1\n", n=1))

    assert captured["route"] == si._MUTATION_LEASE_ROUTE == "IMMEDIATE"
    # Lease failed BEFORE the model request — call_attempts must stay 0
    # (the early-out is above the call_attempts increment).
    assert provider.call_attempts == 0


# ---------------------------------------------------------------------------
# Bug 2 — AegisLeaseError propagates (no longer swallowed)
# ---------------------------------------------------------------------------
def test_run_immunization_campaign_propagates_aegis_lease_error(monkeypatch):
    monkeypatch.setenv(si._ENV_MASTER, "true")
    provider = _RaisingProvider(AegisLeaseError("[CRITICAL] lease unobtainable"))

    async def _drain():
        async for _ in si.run_immunization_campaign(
            seeds=[_seed()], mutation_provider=provider, llm_per_seed=1
        ):
            pass

    with pytest.raises(AegisLeaseError):
        asyncio.run(_drain())
    assert provider.call_attempts == 1  # it tried, then propagated


def test_summarize_campaign_propagates_aegis_lease_error(monkeypatch):
    monkeypatch.setenv(si._ENV_MASTER, "true")
    provider = _RaisingProvider(AegisLeaseError("[CRITICAL] lease unobtainable"))

    with pytest.raises(AegisLeaseError):
        asyncio.run(
            si.summarize_campaign(
                seeds=[_seed()], mutation_provider=provider, llm_per_seed=1
            )
        )


def test_non_aegis_provider_error_is_still_swallowed(monkeypatch):
    """Backward-compat: genuine model/parse errors (not lease failures) remain
    swallowed per-seed — the campaign degrades to deterministic-only and does
    NOT raise."""
    monkeypatch.setenv(si._ENV_MASTER, "true")
    provider = _RaisingProvider(ValueError("transient model error"))

    async def _collect():
        reports = []
        async for rep in si.run_immunization_campaign(
            seeds=[_seed()], mutation_provider=provider, llm_per_seed=1
        ):
            reports.append(rep)
        return reports

    reports = asyncio.run(_collect())  # must NOT raise
    assert provider.call_attempts == 1
    assert len(reports) == 1
    # Deterministic operators still produced + evaluated candidates.
    assert reports[0].total_mutations >= 1


# ---------------------------------------------------------------------------
# Calibration surface — honest lease-denial report (not ConfigStarvationError)
# ---------------------------------------------------------------------------
def test_calibration_reports_lease_denial_distinct_from_config_starvation(
    monkeypatch, capsys
):
    """run_calibration catches AegisLeaseError and returns 1 with an [AEGIS]
    message — NOT a ConfigStarvationError (which would misattribute a route-cap
    denial to deterministic-fill starvation)."""
    import scripts.security.run_cc_parity_calibration as cal
    from backend.core.ouroboros.governance.self_immunization import (
        ConfigStarvationError,
    )

    monkeypatch.setenv(si._ENV_MASTER, "true")

    # Isolate the AegisLeaseError catch: stub the readiness gate to READY so
    # the test does not depend on a real credential or daemon.
    async def _ready():
        return cal._AegisReadiness.READY

    monkeypatch.setattr(cal, "_check_aegis_readiness", _ready)

    # A provider object so run_calibration takes the live (non-dry) path.
    class _DummyProvider:
        call_attempts = 0
        generated_count = 0
        _budget_guard = None

    # run_calibration does `from ... import self_immunization as si` locally,
    # so it resolves the SAME module object we patch here.
    monkeypatch.setattr(si, "LLMMutationProvider", lambda **kw: _DummyProvider())
    # 95b canary preflight is orthogonal — stub it so the test is hermetic.
    monkeypatch.setattr(
        si, "run_sandbox_integrity_preflight", lambda: None, raising=False
    )

    async def _raise_lease(**kwargs):
        raise AegisLeaseError(
            "lease denied: reason=cost_ceiling_exceeded "
            "detail='route IMMEDIATE cap exceeded'"
        )

    monkeypatch.setattr(si, "summarize_campaign", _raise_lease)

    rc = asyncio.run(
        cal.run_calibration(
            max_mutations=30, dry_run=False, budget_usd=0.5, bootstrap_aegis=False
        )
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "[AEGIS]" in out
    assert "cost_ceiling_exceeded" in out
    # The misdiagnosis path must NOT fire for a genuine lease denial.
    assert "ConfigStarvationError" not in out
    assert not isinstance(ConfigStarvationError, type(None))  # symbol exists
