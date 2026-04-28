"""Phase 12 Slice C — provider_topology dynamic catalog + YAML diff.

Pins:
  §1 set/get/clear holder lifecycle
  §2 set_dynamic_catalog NEVER raises on garbage input (defensive)
  §3 set_dynamic_catalog atomicity — concurrent set returns one consistent view
  §4 compute_yaml_diff — yaml_only / catalog_only / both partition
  §5 compute_yaml_diff route coverage (4 generative routes, no IMMEDIATE)
  §6 Shadow-mode invariant: dw_models_for_route still reads YAML even when
                            holder is populated
"""
from __future__ import annotations

import threading
import time
from typing import Any  # noqa: F401

import pytest

from backend.core.ouroboros.governance.provider_topology import (
    RouteDiff,
    clear_dynamic_catalog,
    compute_yaml_diff,
    get_dynamic_catalog,
    get_topology,
    set_dynamic_catalog,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_holder():
    """Each test starts with an empty holder."""
    clear_dynamic_catalog()
    yield
    clear_dynamic_catalog()


# ---------------------------------------------------------------------------
# §1 — Lifecycle
# ---------------------------------------------------------------------------


def test_holder_starts_empty() -> None:
    assert get_dynamic_catalog() is None


def test_set_then_get() -> None:
    set_dynamic_catalog(
        {"complex": ("vendor/m-50B", "vendor/m-30B")},
        fetched_at_unix=1234.0,
    )
    h = get_dynamic_catalog()
    assert h is not None
    assert h.fetched_at_unix == 1234.0
    assert h.assignments_by_route["complex"] == ("vendor/m-50B", "vendor/m-30B")


def test_set_replaces_atomically() -> None:
    """Subsequent set() fully replaces — no merge of prior state."""
    set_dynamic_catalog(
        {"complex": ("a/m-50B",)},
        fetched_at_unix=1.0,
    )
    set_dynamic_catalog(
        {"background": ("b/m-7B",)},
        fetched_at_unix=2.0,
    )
    h = get_dynamic_catalog()
    assert h is not None
    assert h.fetched_at_unix == 2.0
    assert "complex" not in h.assignments_by_route
    assert h.assignments_by_route["background"] == ("b/m-7B",)


def test_clear_resets_to_none() -> None:
    set_dynamic_catalog({"complex": ("x/m-50B",)}, fetched_at_unix=1.0)
    assert get_dynamic_catalog() is not None
    clear_dynamic_catalog()
    assert get_dynamic_catalog() is None


def test_set_with_failure_reason() -> None:
    set_dynamic_catalog(
        {},
        fetched_at_unix=1.0,
        fetch_failure_reason="http_503",
    )
    h = get_dynamic_catalog()
    assert h is not None
    assert h.fetch_failure_reason == "http_503"


# ---------------------------------------------------------------------------
# §2 — Defensive coercion
# ---------------------------------------------------------------------------


def test_set_tolerates_garbage_route_keys() -> None:
    """Empty/whitespace keys silently dropped; valid keys preserved."""
    set_dynamic_catalog(
        {
            "": ("garbage",),
            "   ": ("more-garbage",),
            "complex": ("good/m-50B",),
        },
        fetched_at_unix=1.0,
    )
    h = get_dynamic_catalog()
    assert h is not None
    assert "" not in h.assignments_by_route
    assert "complex" in h.assignments_by_route


def test_set_normalizes_route_keys() -> None:
    """Mixed-case route keys normalize to lowercase."""
    set_dynamic_catalog(
        {"COMPLEX": ("x/m-50B",), "Background": ("y/m-7B",)},
        fetched_at_unix=1.0,
    )
    h = get_dynamic_catalog()
    assert h is not None
    assert "complex" in h.assignments_by_route
    assert "background" in h.assignments_by_route


def test_set_tolerates_non_iterable_values() -> None:
    """Values that aren't list/tuple silently dropped."""
    set_dynamic_catalog(
        {
            "complex": ("good/m-50B",),
            "standard": "not-a-list",  # type: ignore[dict-item]
            "background": 42,           # type: ignore[dict-item]
        },
        fetched_at_unix=1.0,
    )
    h = get_dynamic_catalog()
    assert h is not None
    assert h.assignments_by_route["complex"] == ("good/m-50B",)
    assert "standard" not in h.assignments_by_route
    assert "background" not in h.assignments_by_route


def test_set_with_non_mapping_input_does_not_raise() -> None:
    """Defensive — pass a list instead of a dict."""
    set_dynamic_catalog(
        ["not-a-mapping"],  # type: ignore[arg-type]
        fetched_at_unix=1.0,
    )
    h = get_dynamic_catalog()
    assert h is not None
    # Empty assignments — coercion silently dropped everything
    assert dict(h.assignments_by_route) == {}


# ---------------------------------------------------------------------------
# §3 — Atomicity
# ---------------------------------------------------------------------------


def test_concurrent_set_does_not_corrupt() -> None:
    """Many threads writing in parallel — final view is one of the
    writes, never a torn read."""
    workers = []
    n_threads = 8
    for i in range(n_threads):
        def _worker(i=i):
            set_dynamic_catalog(
                {"complex": (f"thread-{i}/model-50B",)},
                fetched_at_unix=float(i),
            )
        t = threading.Thread(target=_worker)
        workers.append(t)
        t.start()
    for t in workers:
        t.join()
    h = get_dynamic_catalog()
    assert h is not None
    assert "complex" in h.assignments_by_route
    # Whichever thread won, the model_id matches the timestamp (no torn write)
    assert len(h.assignments_by_route["complex"]) == 1
    final_model = h.assignments_by_route["complex"][0]
    final_thread = int(final_model.split("-")[1].split("/")[0])
    assert h.fetched_at_unix == float(final_thread)


# ---------------------------------------------------------------------------
# §4 — compute_yaml_diff partitioning
# ---------------------------------------------------------------------------


def test_yaml_diff_yaml_only() -> None:
    """Models in YAML but missing from catalog → yaml_only."""
    yaml_topo = get_topology()
    if not yaml_topo.enabled:
        pytest.skip("yaml topology disabled in this env")
    yaml_complex = yaml_topo.dw_models_for_route("complex")
    if not yaml_complex:
        pytest.skip("no complex dw_models in yaml")
    diff = compute_yaml_diff(
        catalog_assignments={"complex": ()},  # catalog has nothing
    )
    assert set(diff["complex"].yaml_only) == set(yaml_complex)
    assert diff["complex"].catalog_only == ()
    assert diff["complex"].both == ()


def test_yaml_diff_catalog_only() -> None:
    """Models in catalog but missing from YAML → catalog_only."""
    diff = compute_yaml_diff(
        catalog_assignments={
            "complex": ("brand-new/exotic-model-99B",),
        },
    )
    assert "brand-new/exotic-model-99B" in diff["complex"].catalog_only
    # YAML's existing entries (if any) appear in yaml_only — they're
    # NOT in the catalog list


def test_yaml_diff_both_partition_disjoint() -> None:
    """For any route: yaml_only, catalog_only, and both are disjoint
    sets, and their union covers all model_ids in either source."""
    yaml_topo = get_topology()
    if not yaml_topo.enabled:
        pytest.skip("yaml topology disabled")
    yaml_models = yaml_topo.dw_models_for_route("complex")
    if not yaml_models:
        pytest.skip("no yaml complex models")
    overlapping = (yaml_models[0], "extra/catalog-only-30B")
    diff = compute_yaml_diff(
        catalog_assignments={"complex": overlapping},
    )
    only_yaml = set(diff["complex"].yaml_only)
    only_cat = set(diff["complex"].catalog_only)
    both = set(diff["complex"].both)
    assert only_yaml.isdisjoint(only_cat)
    assert only_yaml.isdisjoint(both)
    assert only_cat.isdisjoint(both)
    union = only_yaml | only_cat | both
    expected = set(yaml_models) | set(overlapping)
    assert union == expected


def test_yaml_diff_preserves_order() -> None:
    """yaml_order and catalog_order preserve the ranking from each
    source — operators can compare ordering, not just membership."""
    diff = compute_yaml_diff(
        catalog_assignments={
            "complex": ("a/m-50B", "b/m-50B", "c/m-50B"),
        },
    )
    assert diff["complex"].catalog_order == ("a/m-50B", "b/m-50B", "c/m-50B")


# ---------------------------------------------------------------------------
# §5 — Route coverage
# ---------------------------------------------------------------------------


def test_yaml_diff_covers_4_generative_routes() -> None:
    diff = compute_yaml_diff(catalog_assignments={})
    assert set(diff.keys()) == {
        "standard", "complex", "background", "speculative",
    }


def test_yaml_diff_excludes_immediate_route() -> None:
    """IMMEDIATE is Claude-direct by design — never appears in the diff."""
    diff = compute_yaml_diff(
        catalog_assignments={"immediate": ("should-not-appear",)},
    )
    assert "immediate" not in diff


# ---------------------------------------------------------------------------
# §6 — Shadow-mode invariant
# ---------------------------------------------------------------------------


def test_authoritative_off_dw_models_for_route_still_reads_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hot-revert path post-graduation: with ``JARVIS_DW_CATALOG_
    AUTHORITATIVE=false``, populating the dynamic catalog must NOT
    change what the dispatcher sees. ``dw_models_for_route`` returns
    YAML's empty list (post-purge ``dw_models: []``).

    Originally pinned the Slice C shadow-mode contract; rewritten at
    Slice E to pin the new authoritative-flag-off contract while
    preserving the same architectural invariant: holder is NOT
    consulted when authoritative is off."""
    monkeypatch.setenv("JARVIS_DW_CATALOG_AUTHORITATIVE", "false")
    set_dynamic_catalog(
        {"complex": ("dynamic-only/exotic-99B",)},
        fetched_at_unix=time.time(),
    )
    yaml_topo = get_topology()
    if not yaml_topo.enabled:
        pytest.skip("yaml topology disabled")
    yaml_models = yaml_topo.dw_models_for_route("complex")
    # Whatever YAML says, we get back YAML — NOT the dynamic holder
    assert "dynamic-only/exotic-99B" not in yaml_models


def test_holder_visible_via_explicit_get() -> None:
    """Operators / observers can still read the dynamic holder directly
    even though dispatcher doesn't consume it."""
    set_dynamic_catalog(
        {"complex": ("dynamic-only/exotic-99B",)},
        fetched_at_unix=1.0,
    )
    h = get_dynamic_catalog()
    assert h is not None
    assert h.assignments_by_route["complex"] == ("dynamic-only/exotic-99B",)
