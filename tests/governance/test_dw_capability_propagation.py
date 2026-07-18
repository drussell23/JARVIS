"""Dynamic Capability Propagation — the DreamEngine 403-storm root-cause fix.

Live run bt-2026-07-18-081903 hit a 59× DW 403 storm: the RT 403 handler did a
coarse ``EntitlementCatalogCache.reset()``, the next ``get_entitled_ids``
re-fetched ``/v1/models`` (which still LISTS the model — the catalog lists it as
available while actual USE 403s), and blind election re-picked it → 403 → repeat.

The fix records DW's authoritative 403 as durable session ground truth
(``mark_blocked``), subtracts it from every entitled read (even after a
re-fetch), and filters it out of model election (``_entitlement_filter``). These
tests pin: the blocked model survives a cache reset + a catalog re-fetch that
re-lists it, and election seamlessly fails over to the next entitled model.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.dw_entitlement_fallback import (
    EntitlementCatalogCache,
    get_process_entitlement_cache,
    select_fallback_model,
)
from backend.core.ouroboros.governance.doubleword_provider import DoublewordProvider


# ---------------------------------------------------------------------------
# Fakes — a catalog client whose /v1/models STILL LISTS the blocked model
# (exactly the RBAC-vs-catalog mismatch that caused the storm).
# ---------------------------------------------------------------------------


class _Snapshot:
    def __init__(self, ids):
        self._ids = tuple(ids)

    def model_ids(self):
        return self._ids


class _CatalogClient:
    """Always returns the full catalog — including the 403'd model — mimicking
    DW's /v1/models, which lists catalog availability, not per-account use."""

    def __init__(self, ids):
        self._ids = tuple(ids)
        self.fetch_count = 0

    async def fetch(self):
        self.fetch_count += 1
        return _Snapshot(self._ids)


CATALOG = ("Qwen/Qwen3.5-35B-A3B-FP8", "Qwen3.5-397B", "Qwen3-Embedding-8B")
BLOCKED = "Qwen/Qwen3.5-35B-A3B-FP8"


@pytest.fixture(autouse=True)
def _reset_process_cache():
    c = get_process_entitlement_cache()
    c.reset()
    c.reset_blocked()
    yield
    c.reset()
    c.reset_blocked()


# ===========================================================================
# A. EntitlementCatalogCache — blocked-set mechanics
# ===========================================================================


def test_mark_blocked_records_and_subtracts():
    c = EntitlementCatalogCache()
    assert not c.is_blocked(BLOCKED)
    c.mark_blocked(BLOCKED)
    assert c.is_blocked(BLOCKED)
    assert BLOCKED in c.blocked_ids()


def test_mark_blocked_ignores_empty():
    c = EntitlementCatalogCache()
    c.mark_blocked("")
    c.mark_blocked("   ")
    assert c.blocked_ids() == frozenset()


async def test_get_entitled_ids_subtracts_blocked_even_after_refetch():
    """THE storm-avoidance invariant: a re-fetched catalog that STILL LISTS the
    403'd model must NOT re-surface it once it is marked blocked."""
    c = EntitlementCatalogCache()
    client = _CatalogClient(CATALOG)

    first = await c.get_entitled_ids(client)
    assert BLOCKED in first                       # catalog lists it initially

    c.mark_blocked(BLOCKED)                        # DW 403'd it

    refetched = await c.get_entitled_ids(client, force_refresh=True)
    assert client.fetch_count == 2                 # catalog WAS re-fetched
    assert BLOCKED not in refetched                # ...but the block holds
    assert "Qwen3.5-397B" in refetched             # other models still entitled


def test_cached_entitled_ids_is_sync_and_excludes_blocked():
    c = EntitlementCatalogCache()
    c._entitled_ids = frozenset(CATALOG)           # simulate a prior fetch
    c.mark_blocked(BLOCKED)
    peek = c.cached_entitled_ids()                 # no await, no I/O
    assert BLOCKED not in peek and "Qwen3.5-397B" in peek


def test_reset_keeps_blocked_but_clears_entitled():
    c = EntitlementCatalogCache()
    c._entitled_ids = frozenset(CATALOG)
    c.mark_blocked(BLOCKED)
    c.reset()
    assert c.cached_entitled_ids() == frozenset()  # entitled cleared
    assert c.is_blocked(BLOCKED)                    # ...but the 403 is remembered


def test_select_fallback_skips_blocked_model():
    entitled = frozenset(CATALOG) - {BLOCKED}
    alt = select_fallback_model(
        blocked_model_id=BLOCKED,
        preference_order=CATALOG,                  # policy ranks the blocked one first
        entitled_ids=entitled,
    )
    assert alt == "Qwen3.5-397B"                    # first entitled ≠ blocked


# ===========================================================================
# B. Provider election filter — _entitlement_filter (self-state-free)
# ===========================================================================


def _filter(model):
    # The method uses no instance state (logger is module-global), so a dummy
    # self exercises it faithfully without constructing a full provider.
    return DoublewordProvider._entitlement_filter(object(), model)


def _prune(model):
    return DoublewordProvider._prune_unentitled_model(object(), model)


def test_filter_passes_unblocked_model_through():
    assert _filter("Qwen3.5-397B") == "Qwen3.5-397B"


def test_filter_substitutes_blocked_model_from_entitled(monkeypatch):
    c = get_process_entitlement_cache()
    c._entitled_ids = frozenset(CATALOG)
    c.mark_blocked(BLOCKED)
    # Policy preference ranks the blocked model first; filter must skip to next entitled.
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.dw_entitlement_fallback.default_preference_order",
        lambda: CATALOG,
    )
    assert _filter(BLOCKED) == "Qwen3.5-397B"


def test_filter_uses_preference_when_entitled_cold(monkeypatch):
    # Entitled set never fetched (cold) — filter still avoids the blocked model
    # by walking the policy preference order.
    c = get_process_entitlement_cache()
    c.mark_blocked(BLOCKED)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.dw_entitlement_fallback.default_preference_order",
        lambda: CATALOG,
    )
    out = _filter(BLOCKED)
    assert out != BLOCKED and out in CATALOG


def test_filter_fail_open_when_nothing_resolvable(monkeypatch):
    c = get_process_entitlement_cache()
    c.mark_blocked(BLOCKED)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.dw_entitlement_fallback.default_preference_order",
        lambda: (),                                # no alternatives
    )
    assert _filter(BLOCKED) == BLOCKED             # fail-open, never crash


def test_filter_respects_master_switch_off(monkeypatch):
    c = get_process_entitlement_cache()
    c.mark_blocked(BLOCKED)
    monkeypatch.setenv("JARVIS_DW_ENTITLEMENT_FALLBACK_ENABLED", "false")
    assert _filter(BLOCKED) == BLOCKED             # disabled → unchanged


def test_filter_empty_model_passthrough():
    assert _filter("") == ""


# ===========================================================================
# C. 403 handler + end-to-end storm scenario
# ===========================================================================


def test_prune_marks_blocked_not_coarse_reset():
    c = get_process_entitlement_cache()
    c._entitled_ids = frozenset(CATALOG)
    _prune(BLOCKED)
    assert c.is_blocked(BLOCKED)
    # entitled set is NOT wholesale-reset (the other models survive)
    assert "Qwen3.5-397B" in c.cached_entitled_ids()


def test_storm_scenario_403_then_election_fails_over(monkeypatch):
    """END-TO-END: model 403s → prune records it → next election avoids it and
    picks the next entitled model, even though the catalog still lists it. This
    is the loop that previously stormed 59×; it now converges after one 403."""
    c = get_process_entitlement_cache()
    c._entitled_ids = frozenset(CATALOG)           # catalog still lists BLOCKED
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.dw_entitlement_fallback.default_preference_order",
        lambda: CATALOG,
    )
    # Cycle 1: DW returns 403 for the elected model.
    _prune(BLOCKED)
    # Cycle 2: election re-runs — must NOT re-pick the blocked model.
    elected = _filter(BLOCKED)
    assert elected != BLOCKED
    assert elected in ("Qwen3.5-397B", "Qwen3-Embedding-8B")
    # And it stays converged on every subsequent cycle (no storm).
    assert _filter(BLOCKED) == elected
