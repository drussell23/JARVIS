"""Sovereign State Isolation (2026-06-19) — per-provider terminal_quota
isolation. A provider's economic death (Claude 402) must be contained to
that provider's lane breaker and must NOT trip the provider-neutral per-op
breaker (which would poison the op for DW autarky)."""
from __future__ import annotations

from backend.core.ouroboros.governance.candidate_generator import (
    quota_isolation_skips_op_breaker,
    _provider_quota_isolation_enabled,
)


def test_isolation_default_on(monkeypatch):
    monkeypatch.delenv("JARVIS_PROVIDER_QUOTA_ISOLATION_ENABLED", raising=False)
    assert _provider_quota_isolation_enabled() is True


def test_isolation_force_off(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_QUOTA_ISOLATION_ENABLED", "0")
    assert _provider_quota_isolation_enabled() is False


def test_predicate_truth_table():
    # economic block + isolation on -> skip the op-breaker trip (contain it)
    assert quota_isolation_skips_op_breaker(
        is_provider_economic_block=True, isolation_enabled=True) is True
    # economic block but isolation off -> legacy: op-breaker trips (no skip)
    assert quota_isolation_skips_op_breaker(
        is_provider_economic_block=True, isolation_enabled=False) is False
    # NON-economic failure (e.g. real structural/timeout) -> never skip,
    # the op-breaker must still trip on genuine op-fatal failures
    assert quota_isolation_skips_op_breaker(
        is_provider_economic_block=False, isolation_enabled=True) is False
    assert quota_isolation_skips_op_breaker(
        is_provider_economic_block=False, isolation_enabled=False) is False


def test_real_failure_still_trips_op_breaker():
    """The isolation must NOT mask genuine op-fatal failures: a structural/
    config death (not an economic block) must still allow the op breaker to
    trip, regardless of isolation flag."""
    for iso in (True, False):
        assert quota_isolation_skips_op_breaker(
            is_provider_economic_block=False, isolation_enabled=iso) is False
