"""Slice 5b consolidation Slice 3 — observability_route_registry
regression spine (PRD §32.5 / §32.11).

Verifies:

  * Master flag asymmetric env semantics
  * 5 dormant observability surfaces auto-mount
    (decisions/curiosity/epistemic_budget/m10/action_outcome)
  * Idempotency at module-name granularity
  * Signature rejection (off-shape `register_routes` symbols)
  * Per-module handler failure isolation
  * MountReport schema + as_dict projection
  * Substrate exclusion list (no recursion / no class-routers)
  * action_outcome_memory_observability has BOTH the canonical
    ``register_routes`` name AND the legacy alias
  * Composes the Slice 2 module_discovery primitive (no parallel
    walker)
  * Authority asymmetry — pure substrate
"""
from __future__ import annotations

from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty mounted-modules set so
    discovery is repeatable."""
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        reset_registry_for_tests,
    )
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("v", ["true", "1", "yes", "on", "TRUE"])
def test_master_flag_truthy(monkeypatch, v):
    monkeypatch.setenv(
        "JARVIS_OBSERVABILITY_AUTODISCOVERY_ENABLED", v,
    )
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        observability_autodiscovery_enabled,
    )
    assert observability_autodiscovery_enabled() is True


@pytest.mark.parametrize("v", ["false", "0", "no", "off"])
def test_master_flag_falsy(monkeypatch, v):
    monkeypatch.setenv(
        "JARVIS_OBSERVABILITY_AUTODISCOVERY_ENABLED", v,
    )
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        observability_autodiscovery_enabled,
    )
    assert observability_autodiscovery_enabled() is False


def test_master_flag_default_true(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_OBSERVABILITY_AUTODISCOVERY_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        observability_autodiscovery_enabled,
    )
    assert observability_autodiscovery_enabled() is True


def test_master_off_returns_zero_count(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OBSERVABILITY_AUTODISCOVERY_ENABLED", "false",
    )
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        discover_and_mount_observability_routes,
    )
    app = web.Application()
    report = discover_and_mount_observability_routes(app)
    assert report.mounted_count == 0
    assert report.master_flag_on is False


# ---------------------------------------------------------------------------
# Happy-path mount of all 5 dormant surfaces
# ---------------------------------------------------------------------------


_EXPECTED_DORMANT_MODULES = {
    "backend.core.ouroboros.governance.decisions_observability",
    "backend.core.ouroboros.governance.curiosity_observability",
    "backend.core.ouroboros.governance.epistemic_budget_observability",  # noqa: E501
    "backend.core.ouroboros.governance.m10.observability",
    "backend.core.ouroboros.governance.action_outcome_memory_observability",  # noqa: E501
}


def test_auto_mounts_all_five_dormant_surfaces():
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        discover_and_mount_observability_routes,
    )
    app = web.Application()
    report = discover_and_mount_observability_routes(app)
    mounted_names = {r.module_full_name for r in report.mounted}
    missing = _EXPECTED_DORMANT_MODULES - mounted_names
    assert not missing, (
        f"missing auto-mounts: {missing}"
    )
    assert report.mounted_count >= 5
    assert report.handler_failed == 0


def test_auto_mounted_routes_appear_on_app():
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        discover_and_mount_observability_routes,
    )
    app = web.Application()
    discover_and_mount_observability_routes(app)
    # Each shipped surface registers GET routes; collect canonical
    # paths and assert every expected family mounted.
    paths = set()
    for route in app.router.routes():
        try:
            paths.add(route.resource.canonical)
        except Exception:
            pass
    expected_path_prefixes = (
        "/observability/decisions",
        "/observability/curiosity",
        "/observability/budget",
        "/observability/m10",
        "/observability/action-outcomes",
    )
    for prefix in expected_path_prefixes:
        assert any(p.startswith(prefix) for p in paths), (
            f"no route mounted under {prefix}: paths={sorted(paths)}"
        )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_second_call_short_circuits_already_mounted():
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        discover_and_mount_observability_routes,
    )
    app = web.Application()
    first = discover_and_mount_observability_routes(app)
    second = discover_and_mount_observability_routes(app)
    assert first.mounted_count >= 5
    assert second.mounted_count == 0
    assert second.already_mounted >= 5


def test_list_mounted_modules_snapshot():
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        discover_and_mount_observability_routes,
        list_mounted_modules,
    )
    app = web.Application()
    discover_and_mount_observability_routes(app)
    snapshot = list_mounted_modules()
    assert isinstance(snapshot, tuple)
    assert all(isinstance(n, str) for n in snapshot)
    assert len(snapshot) >= 5


# ---------------------------------------------------------------------------
# Signature rejection
# ---------------------------------------------------------------------------


def test_signature_rejection_synthetic_package(
    tmp_path, monkeypatch,
):
    """Synthetic package with off-shape ``register_routes`` MUST
    be rejected at the signature gate, not invoked."""
    pytest.importorskip("aiohttp")
    pkg = tmp_path / "synth_obs_sig"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # Bad signature — accepts wrong number of params; no kwargs.
    (pkg / "bad_sig.py").write_text(
        "def register_routes(): pass\n",
        encoding="utf-8",
    )
    # Good signature — accepts (app, **kwargs).
    (pkg / "good.py").write_text(
        "def register_routes(app, **kwargs):\n"
        "    app._marker_ = 'ok'\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import sys
    for k in list(sys.modules.keys()):
        if k.startswith("synth_obs_sig"):
            del sys.modules[k]

    from aiohttp import web
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        discover_and_mount_observability_routes,
    )
    app = web.Application()
    report = discover_and_mount_observability_routes(
        app, packages=["synth_obs_sig"], excluded_modules=[],
    )
    assert report.signature_rejected == 1
    bad_skips = [
        (m, r) for m, r in report.skipped_reasons
        if "bad_sig" in m
    ]
    assert len(bad_skips) == 1
    assert "signature_rejected" in bad_skips[0][1]
    # Good module mounted.
    assert any(
        "good" in r.module_full_name for r in report.mounted
    )


# ---------------------------------------------------------------------------
# Handler failure isolation
# ---------------------------------------------------------------------------


def test_handler_failure_isolation(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp")
    pkg = tmp_path / "synth_obs_fail"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "boom.py").write_text(
        "def register_routes(app, **kwargs):\n"
        "    raise RuntimeError('boom')\n",
        encoding="utf-8",
    )
    (pkg / "ok.py").write_text(
        "def register_routes(app, **kwargs):\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import sys
    for k in list(sys.modules.keys()):
        if k.startswith("synth_obs_fail"):
            del sys.modules[k]

    from aiohttp import web
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        discover_and_mount_observability_routes,
    )
    app = web.Application()
    report = discover_and_mount_observability_routes(
        app,
        packages=["synth_obs_fail"],
        excluded_modules=[],
    )
    assert report.handler_failed == 1
    boom_skips = [
        (m, r) for m, r in report.skipped_reasons
        if "boom" in m
    ]
    assert len(boom_skips) == 1
    assert "mount_raised" in boom_skips[0][1]
    # The good sibling still mounts.
    assert any("ok" in r.module_full_name for r in report.mounted)


# ---------------------------------------------------------------------------
# Substrate exclusion list
# ---------------------------------------------------------------------------


def test_substrate_exclusions_skipped():
    """The registry MUST NOT auto-discover its own module nor
    class-based routers (which expose register_routes as a
    method, not a module-level function)."""
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        discover_and_mount_observability_routes,
    )
    app = web.Application()
    report = discover_and_mount_observability_routes(app)
    mounted_names = {r.module_full_name for r in report.mounted}
    forbidden = (
        "backend.core.ouroboros.governance.observability_route_registry",  # noqa: E501
        "backend.core.ouroboros.governance.event_channel",
        "backend.core.ouroboros.governance.ide_observability",
        "backend.core.ouroboros.governance.ide_observability_stream",  # noqa: E501
    )
    for f in forbidden:
        assert f not in mounted_names, (
            f"substrate exclusion violated: {f} got auto-mounted"
        )


# ---------------------------------------------------------------------------
# action_outcome canonical name + alias
# ---------------------------------------------------------------------------


def test_action_outcome_exposes_canonical_register_routes():
    from backend.core.ouroboros.governance import (
        action_outcome_memory_observability as ao,
    )
    assert callable(getattr(ao, "register_routes", None))
    # Backward-compat alias retained for existing callers.
    assert callable(
        getattr(ao, "register_action_outcome_routes", None),
    )
    # The two MUST be the same callable (alias semantics).
    assert ao.register_routes is ao.register_action_outcome_routes


# ---------------------------------------------------------------------------
# Composes Slice 2 primitive
# ---------------------------------------------------------------------------


def test_registry_composes_module_discovery_primitive():
    """The registry MUST delegate to
    module_discovery.discover_module_provided_callable; no
    parallel pkgutil walker."""
    import inspect
    from backend.core.ouroboros.governance import (
        observability_route_registry,
    )
    src = inspect.getsource(
        observability_route_registry.discover_and_mount_observability_routes,  # noqa: E501
    )
    assert "discover_module_provided_callable" in src
    assert "pkgutil.iter_modules" not in src


# ---------------------------------------------------------------------------
# MountReport schema + projection
# ---------------------------------------------------------------------------


def test_mount_report_is_frozen():
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        MountReport,
    )
    report = MountReport(
        mounted_count=1,
        already_mounted=0,
        signature_rejected=0,
        handler_failed=0,
    )
    with pytest.raises(Exception):
        report.mounted_count = 99  # type: ignore


def test_mount_report_as_dict_projection():
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        MountReport,
        MountedRoute,
        OBSERVABILITY_REGISTRY_SCHEMA_VERSION,
    )
    report = MountReport(
        mounted_count=1,
        already_mounted=2,
        signature_rejected=3,
        handler_failed=0,
        mounted=(
            MountedRoute(
                module_full_name="example.module",
                mounted_at_unix=1.0,
            ),
        ),
        skipped_reasons=(
            ("other.module", "signature_rejected: no_parameters"),
        ),
        elapsed_s=0.05,
    )
    d = report.as_dict()
    assert (
        d["schema_version"]
        == OBSERVABILITY_REGISTRY_SCHEMA_VERSION
    )
    assert d["mounted_count"] == 1
    assert d["already_mounted"] == 2
    assert d["signature_rejected"] == 3
    assert len(d["mounted"]) == 1
    assert d["mounted"][0]["module_full_name"] == "example.module"


# ---------------------------------------------------------------------------
# Authority asymmetry
# ---------------------------------------------------------------------------


def test_observability_registry_authority_asymmetry():
    """observability_route_registry.py MUST stay pure substrate
    over module_discovery — no orchestrator/iron_gate/policy/
    providers/etc imports."""
    import ast as _ast
    from pathlib import Path
    target = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "observability_route_registry.py"
    )
    tree = _ast.parse(target.read_text(encoding="utf-8"))
    forbidden = (
        "orchestrator",
        "iron_gate",
        "policy",
        "providers",
        "candidate_generator",
        "urgency_router",
        "change_engine",
        "semantic_guardian",
    )
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            for f in forbidden:
                if f in module:
                    pytest.fail(
                        f"observability_route_registry.py MUST "
                        f"NOT import {module!r}"
                    )


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


def test_public_api_exports():
    from backend.core.ouroboros.governance import (
        observability_route_registry as r,
    )
    expected = (
        "discover_and_mount_observability_routes",
        "list_mounted_modules",
        "observability_autodiscovery_enabled",
        "reset_registry_for_tests",
        "MountReport",
        "MountedRoute",
        "OBSERVABILITY_REGISTRY_SCHEMA_VERSION",
    )
    for name in expected:
        assert hasattr(r, name), f"missing public symbol: {name}"
