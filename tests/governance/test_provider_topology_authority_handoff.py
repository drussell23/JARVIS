"""Phase 12 Slice D — provider_topology authority handoff regression spine.

The flip: when JARVIS_DW_CATALOG_AUTHORITATIVE=true AND the dynamic
catalog holder is fresh AND the route has a non-empty assignment, the
holder is consulted FIRST. YAML remains the fallback.

Pins:
  §1 catalog_authoritative_enabled flag — default off, truthy/falsy parsing
  §2 Authoritative OFF → YAML still authoritative even when holder populated
                         (Slice C shadow-mode invariant preserved)
  §3 Authoritative ON, no holder → YAML (cold-start fallback)
  §4 Authoritative ON, holder fresh, route present → catalog wins
  §5 Authoritative ON, holder fresh, route MISSING → YAML
  §6 Authoritative ON, holder fresh, route empty → YAML
  §7 Authoritative ON, holder STALE → YAML
  §8 fallback_tolerance / block_mode / dw_allowed / reason
                          stay YAML-authored regardless of authoritative
                          (only dw_models swaps source)
  §9 Hot-revert — flipping authoritative=false mid-session immediately
                  returns dispatcher to YAML
  §10 Topology disabled (yaml absent) — return () regardless
  §11 IMMEDIATE route — empty in YAML AND empty in catalog → ()
  §12 Source-level pin — fallback_tolerance NEVER reads dynamic holder
"""
from __future__ import annotations

import time
from typing import Any  # noqa: F401

import pytest

from backend.core.ouroboros.governance.provider_topology import (
    catalog_authoritative_enabled,
    clear_dynamic_catalog,
    get_topology,
    set_dynamic_catalog,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_holder():
    clear_dynamic_catalog()
    yield
    clear_dynamic_catalog()


def _yaml_has_complex_models() -> bool:
    """Test environments where YAML is loaded with complex dw_models."""
    t = get_topology()
    return t.enabled and bool(t.dw_models_for_route("complex"))


# ---------------------------------------------------------------------------
# §1 — Master flag
# ---------------------------------------------------------------------------


def test_authoritative_default_on_post_graduation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice E graduation flip: unset/empty env returns True."""
    monkeypatch.delenv("JARVIS_DW_CATALOG_AUTHORITATIVE", raising=False)
    assert catalog_authoritative_enabled() is True


def test_authoritative_truthy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", val)
        assert catalog_authoritative_enabled() is True


def test_authoritative_falsy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-graduation: empty string is the unset-marker for default
    True. Hot-revert requires an explicit ``false``-class string."""
    for val in ("0", "false", "no", "off", "garbage"):
        monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", val)
        assert catalog_authoritative_enabled() is False


# ---------------------------------------------------------------------------
# §2 — Authoritative OFF preserves Slice C shadow-mode invariant
# ---------------------------------------------------------------------------


def test_off_preserves_yaml_when_holder_populated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice C contract pinned: holder populated, authoritative off
    → dispatcher reads YAML."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "false")
    if not _yaml_has_complex_models():
        pytest.skip("yaml topology lacks complex models in this env")
    set_dynamic_catalog(
        {"complex": ("dynamic-only/should-not-win",)},
        fetched_at_unix=time.time(),
    )
    yaml_models = get_topology().dw_models_for_route("complex")
    assert "dynamic-only/should-not-win" not in yaml_models


# ---------------------------------------------------------------------------
# §3 — Authoritative ON, no holder → YAML
# ---------------------------------------------------------------------------


def test_on_no_holder_falls_back_to_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold-start safety: discovery hasn't run yet, dispatcher must
    NOT see an empty list — that would crash the sentinel cascade."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    if not _yaml_has_complex_models():
        pytest.skip("yaml topology lacks complex models")
    yaml_models = get_topology().dw_models_for_route("complex")
    assert yaml_models  # non-empty, from YAML


# ---------------------------------------------------------------------------
# §4 — Authoritative ON, holder fresh, route present → catalog wins
# ---------------------------------------------------------------------------


def test_on_fresh_holder_with_assignment_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handoff: with both flags on + fresh holder + non-empty
    route assignment → catalog wins."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    set_dynamic_catalog(
        {"complex": ("dynamic-vendor/Kimi-99B",
                     "dynamic-vendor/GLM-50B")},
        fetched_at_unix=time.time(),
    )
    result = get_topology().dw_models_for_route("complex")
    assert result == ("dynamic-vendor/Kimi-99B", "dynamic-vendor/GLM-50B")


def test_on_fresh_holder_returns_full_catalog_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order preserved end-to-end — what the classifier ranked is
    what the dispatcher iterates."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    ranked = ("a/m-50B", "b/m-30B", "c/m-14B")
    set_dynamic_catalog(
        {"complex": ranked},
        fetched_at_unix=time.time(),
    )
    assert get_topology().dw_models_for_route("complex") == ranked


# ---------------------------------------------------------------------------
# §5 — Authoritative ON, route missing from holder → YAML
# ---------------------------------------------------------------------------


def test_on_holder_route_missing_falls_back_to_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holder has 'complex' but not 'background' → background reads YAML."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    if not _yaml_has_complex_models():
        pytest.skip("yaml topology lacks models")
    set_dynamic_catalog(
        {"complex": ("dynamic-only/m-99B",)},
        fetched_at_unix=time.time(),
    )
    yaml_bg = get_topology().dw_models_for_route("background")
    # background was NOT in the holder → falls through to YAML
    assert "dynamic-only/m-99B" not in yaml_bg


# ---------------------------------------------------------------------------
# §6 — Authoritative ON, route present but empty → YAML
# ---------------------------------------------------------------------------


def test_on_holder_route_empty_falls_back_to_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty list signals 'classifier found nothing' → caller still
    needs SOMETHING to try, so YAML wins for that route. Prevents a
    misclassified empty list from disabling DW for a route entirely."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    if not _yaml_has_complex_models():
        pytest.skip("yaml topology lacks complex models")
    yaml_models = get_topology().dw_models_for_route("complex")
    set_dynamic_catalog(
        {"complex": ()},  # explicitly empty
        fetched_at_unix=time.time(),
    )
    after = get_topology().dw_models_for_route("complex")
    # Falls back to YAML, NOT the empty catalog list
    assert after == yaml_models


# ---------------------------------------------------------------------------
# §7 — Authoritative ON, holder STALE → YAML
# ---------------------------------------------------------------------------


def test_on_stale_holder_falls_back_to_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holder older than max_age_s → caller must NOT trust it. YAML
    wins until the next discovery cycle refreshes."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    monkeypatch.setenv("JARVIS_DW_CATALOG_MAX_AGE_S", "1")  # 1-sec freshness
    if not _yaml_has_complex_models():
        pytest.skip("yaml topology lacks complex models")
    yaml_models = get_topology().dw_models_for_route("complex")
    # Populate holder, then "wait" by setting old timestamp
    set_dynamic_catalog(
        {"complex": ("dynamic-only/stale-99B",)},
        fetched_at_unix=time.time() - 10.0,  # 10 sec old, > 1-sec threshold
    )
    after = get_topology().dw_models_for_route("complex")
    assert "dynamic-only/stale-99B" not in after
    assert after == yaml_models


def test_on_holder_within_freshness_window_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holder just under max_age_s → still used."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    monkeypatch.setenv("JARVIS_DW_CATALOG_MAX_AGE_S", "60")
    set_dynamic_catalog(
        {"complex": ("dynamic-only/m-99B",)},
        fetched_at_unix=time.time() - 30.0,  # 30 sec old, well under 60
    )
    after = get_topology().dw_models_for_route("complex")
    assert after == ("dynamic-only/m-99B",)


# ---------------------------------------------------------------------------
# §8 — Policy fields stay YAML-authored
# ---------------------------------------------------------------------------


def test_fallback_tolerance_unaffected_by_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cost contract: fallback_tolerance is policy, not catalog.
    Flipping authoritative MUST NOT change BG/SPEC's queue contract."""
    topo = get_topology()
    if not topo.enabled:
        pytest.skip("yaml topology disabled")
    # Capture YAML truth
    before_bg = topo.fallback_tolerance_for_route("background")
    before_spec = topo.fallback_tolerance_for_route("speculative")
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    set_dynamic_catalog(
        {"background": ("malicious-injection/m-99B",),
         "speculative": ("another/m-1B",)},
        fetched_at_unix=time.time(),
    )
    # Same answers — fallback_tolerance never touched the holder
    assert topo.fallback_tolerance_for_route("background") == before_bg
    assert topo.fallback_tolerance_for_route("speculative") == before_spec


def test_dw_allowed_unaffected_by_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dw_allowed is YAML-only — operator-controlled cost gate."""
    topo = get_topology()
    if not topo.enabled:
        pytest.skip("yaml topology disabled")
    before = topo.dw_allowed_for_route("background")
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    set_dynamic_catalog(
        {"background": ("any/m-7B",)},
        fetched_at_unix=time.time(),
    )
    assert topo.dw_allowed_for_route("background") == before


def test_block_mode_unaffected_by_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topo = get_topology()
    if not topo.enabled:
        pytest.skip("yaml topology disabled")
    before = topo.block_mode_for_route("background")
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    set_dynamic_catalog(
        {"background": ("any/m-7B",)},
        fetched_at_unix=time.time(),
    )
    assert topo.block_mode_for_route("background") == before


def test_reason_unaffected_by_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topo = get_topology()
    if not topo.enabled:
        pytest.skip("yaml topology disabled")
    before = topo.reason_for_route("background")
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    set_dynamic_catalog(
        {"background": ("any/m-7B",)},
        fetched_at_unix=time.time(),
    )
    assert topo.reason_for_route("background") == before


# ---------------------------------------------------------------------------
# §9 — Hot-revert
# ---------------------------------------------------------------------------


def test_flag_flip_takes_effect_mid_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator flips authoritative=false at runtime — next call goes
    immediately to YAML, no re-init needed."""
    if not _yaml_has_complex_models():
        pytest.skip("yaml topology lacks complex models")
    yaml_models = get_topology().dw_models_for_route("complex")
    set_dynamic_catalog(
        {"complex": ("dynamic-only/winning",)},
        fetched_at_unix=time.time(),
    )
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    assert (
        get_topology().dw_models_for_route("complex")[0]
        == "dynamic-only/winning"
    )
    # Hot-revert
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "false")
    assert get_topology().dw_models_for_route("complex") == yaml_models


# ---------------------------------------------------------------------------
# §10 — Topology disabled
# ---------------------------------------------------------------------------


def test_topology_disabled_returns_empty_regardless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the YAML's doubleword_topology section is missing, the
    topology object is enabled=False. dw_models_for_route returns ()
    on the very first check — catalog branch is skipped entirely."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    set_dynamic_catalog(
        {"complex": ("dynamic-only/should-not-leak",)},
        fetched_at_unix=time.time(),
    )
    # Manually construct a disabled topology — emulates yaml-absent
    from backend.core.ouroboros.governance.provider_topology import (
        ProviderTopology,
    )
    disabled = ProviderTopology(enabled=False)
    assert disabled.dw_models_for_route("complex") == ()


# ---------------------------------------------------------------------------
# §11 — IMMEDIATE route
# ---------------------------------------------------------------------------


def test_immediate_route_stays_empty_under_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IMMEDIATE has empty dw_models in YAML AND classifier never
    populates it. Both flags on → still ()."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "true")
    set_dynamic_catalog({}, fetched_at_unix=time.time())  # empty holder
    assert get_topology().dw_models_for_route("immediate") == ()


# ---------------------------------------------------------------------------
# §12 — Source-level pins
# ---------------------------------------------------------------------------


def test_source_fallback_tolerance_does_not_read_holder() -> None:
    """fallback_tolerance is policy, NOT catalog. The implementation
    must NOT consult get_dynamic_catalog or catalog_authoritative_enabled.
    Source-level pin so a future refactor can't accidentally couple
    the cost contract to the catalog."""
    import inspect
    from backend.core.ouroboros.governance.provider_topology import (
        ProviderTopology,
    )
    src = inspect.getsource(ProviderTopology.fallback_tolerance_for_route)
    assert "get_dynamic_catalog" not in src
    assert "catalog_authoritative_enabled" not in src


def test_source_dw_allowed_does_not_read_holder() -> None:
    import inspect
    from backend.core.ouroboros.governance.provider_topology import (
        ProviderTopology,
    )
    src = inspect.getsource(ProviderTopology.dw_allowed_for_route)
    assert "get_dynamic_catalog" not in src
    assert "catalog_authoritative_enabled" not in src


def test_source_block_mode_does_not_read_holder() -> None:
    import inspect
    from backend.core.ouroboros.governance.provider_topology import (
        ProviderTopology,
    )
    src = inspect.getsource(ProviderTopology.block_mode_for_route)
    assert "get_dynamic_catalog" not in src


def test_source_dw_models_for_route_consults_holder_first() -> None:
    """The catalog-first branch must fire BEFORE the YAML lookup
    (otherwise YAML always wins, defeating the purpose of Slice D)."""
    import inspect
    from backend.core.ouroboros.governance.provider_topology import (
        ProviderTopology,
    )
    src = inspect.getsource(ProviderTopology.dw_models_for_route)
    holder_idx = src.index("get_dynamic_catalog")
    yaml_idx = src.index("self.routes.get")
    assert holder_idx < yaml_idx, (
        "dynamic catalog branch must fire before YAML fallback"
    )
