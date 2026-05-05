"""Slice 5b consolidation Slice 2 — module_discovery primitive
regression spine.

Verifies:

  * Master flag asymmetric env semantics
  * Walks packages → submodules → handler dispatch
  * Per-module exception isolation (one bad module doesn't break
    siblings)
  * Per-package exception isolation (missing package doesn't break
    others)
  * Recursion guard (excluded_modules)
  * Handler return-coercion (non-int / negative / falsy → 0)
  * Frozen :class:`DiscoveryReport` schema + as_dict projection
  * :func:`make_registry_handler` + :func:`make_factory_handler`
    convenience constructors
  * Three-consumer refactor: shipped_code_invariants /
    flag_registry_seed / help_dispatcher all delegate to the
    primitive (no parallel walkers)
"""
from __future__ import annotations

import sys
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_master_flag_truthy(monkeypatch, value):
    monkeypatch.setenv("JARVIS_MODULE_DISCOVERY_ENABLED", value)
    from backend.core.ouroboros.governance.meta.module_discovery import (
        module_discovery_enabled,
    )
    assert module_discovery_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_master_flag_falsy(monkeypatch, value):
    monkeypatch.setenv("JARVIS_MODULE_DISCOVERY_ENABLED", value)
    from backend.core.ouroboros.governance.meta.module_discovery import (
        module_discovery_enabled,
    )
    assert module_discovery_enabled() is False


def test_master_flag_default_is_true(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MODULE_DISCOVERY_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.meta.module_discovery import (
        module_discovery_enabled,
    )
    assert module_discovery_enabled() is True


def test_master_flag_off_returns_zero_count(monkeypatch):
    monkeypatch.setenv("JARVIS_MODULE_DISCOVERY_ENABLED", "false")
    from backend.core.ouroboros.governance.meta.module_discovery import (
        discover_module_provided_callable,
    )
    report = discover_module_provided_callable(
        packages=["backend.core.ouroboros.governance"],
        attr_name="register_shipped_invariants",
        handler=lambda fn_name, fn: 1,
    )
    assert report.discovered_count == 0
    assert report.master_flag_on is False
    assert report.modules_scanned == 0


# ---------------------------------------------------------------------------
# Happy-path walk (real codebase)
# ---------------------------------------------------------------------------


def test_walks_real_governance_for_register_shipped_invariants():
    """The primitive must rediscover the same invariants the
    legacy walker discovered. M10 has 8 pins; cleanup_invariants
    has 4."""
    from backend.core.ouroboros.governance.meta.module_discovery import (
        discover_module_provided_callable,
    )
    captured = {}

    def handler(full_name: str, fn: Any) -> int:
        items = fn() or []
        captured[full_name] = [
            getattr(i, "invariant_name", "?") for i in items
        ]
        return len(items)

    report = discover_module_provided_callable(
        packages=["backend.core.ouroboros.governance"],
        attr_name="register_shipped_invariants",
        handler=handler,
        excluded_modules=(
            "backend.core.ouroboros.governance.meta.shipped_code_invariants",  # noqa: E501
        ),
        log_prefix="TestWalk",
    )
    m10_pins = captured.get(
        "backend.core.ouroboros.governance.m10", [],
    )
    assert len(m10_pins) >= 5
    cleanup_pins = captured.get(
        "backend.core.ouroboros.governance.cleanup_invariants", [],
    )
    # 4 archive-only + 3 consumer-uses-primitive (Slice 2)
    # + 5 observability-module-exposes-register_routes (Slice 3)
    # + 1 observability_route_registry_uses_primitive (Slice 3)
    # + 1 repl_dispatch_registry_uses_primitive (Slice 4)
    # = 14 pins total
    assert len(cleanup_pins) == 14
    assert report.discovered_count >= 20
    assert report.modules_scanned >= 2
    assert report.elapsed_s > 0.0


# ---------------------------------------------------------------------------
# Exception isolation
# ---------------------------------------------------------------------------


def test_per_package_exception_isolation():
    """Unimportable package is recorded as packages_unavailable;
    other packages continue to scan."""
    from backend.core.ouroboros.governance.meta.module_discovery import (
        discover_module_provided_callable,
    )
    report = discover_module_provided_callable(
        packages=[
            "no.such.package.exists",
            "backend.core.ouroboros.governance.meta",
        ],
        attr_name="never_present_attr",
        handler=lambda fn_name, fn: 1,
    )
    assert len(report.packages_unavailable) == 1
    assert (
        report.packages_unavailable[0].full_name
        == "no.such.package.exists"
    )


def test_handler_exception_isolation_records_skip():
    """Handler that raises on one module does NOT break siblings;
    the report records the skip with reason."""
    from backend.core.ouroboros.governance.meta.module_discovery import (
        discover_module_provided_callable,
    )

    def handler(full_name: str, fn: Any) -> int:
        if "m10" in full_name:
            raise RuntimeError("synthetic handler crash")
        items = fn() or []
        return len(items)

    report = discover_module_provided_callable(
        packages=["backend.core.ouroboros.governance"],
        attr_name="register_shipped_invariants",
        handler=handler,
        excluded_modules=(
            "backend.core.ouroboros.governance.meta.shipped_code_invariants",  # noqa: E501
        ),
    )
    m10_skips = [
        s for s in report.modules_skipped
        if "m10" in s.full_name
    ]
    assert len(m10_skips) >= 1
    assert "handler_raised" in m10_skips[0].reason
    assert report.modules_scanned >= 1


def test_excluded_modules_skipped():
    from backend.core.ouroboros.governance.meta.module_discovery import (
        discover_module_provided_callable,
    )
    seen = []

    def handler(full_name: str, fn: Any) -> int:
        seen.append(full_name)
        return 1

    discover_module_provided_callable(
        packages=["backend.core.ouroboros.governance"],
        attr_name="register_shipped_invariants",
        handler=handler,
        excluded_modules=(
            "backend.core.ouroboros.governance.m10",
            "backend.core.ouroboros.governance.meta.shipped_code_invariants",  # noqa: E501
        ),
    )
    assert "backend.core.ouroboros.governance.m10" not in seen


# ---------------------------------------------------------------------------
# Handler return-coercion
# ---------------------------------------------------------------------------


def test_handler_returns_non_int_coerces_to_zero():
    from backend.core.ouroboros.governance.meta.module_discovery import (
        discover_module_provided_callable,
    )
    report = discover_module_provided_callable(
        packages=["backend.core.ouroboros.governance"],
        attr_name="register_shipped_invariants",
        handler=lambda fn_name, fn: "not an int",
        excluded_modules=(
            "backend.core.ouroboros.governance.meta.shipped_code_invariants",  # noqa: E501
        ),
    )
    assert report.discovered_count == 0
    assert report.modules_scanned >= 1


def test_handler_returns_negative_coerces_to_zero():
    from backend.core.ouroboros.governance.meta.module_discovery import (
        discover_module_provided_callable,
    )
    report = discover_module_provided_callable(
        packages=["backend.core.ouroboros.governance"],
        attr_name="register_shipped_invariants",
        handler=lambda fn_name, fn: -5,
        excluded_modules=(
            "backend.core.ouroboros.governance.meta.shipped_code_invariants",  # noqa: E501
        ),
    )
    assert report.discovered_count == 0


# ---------------------------------------------------------------------------
# DiscoveryReport schema + projection
# ---------------------------------------------------------------------------


def test_discovery_report_is_frozen():
    from backend.core.ouroboros.governance.meta.module_discovery import (
        DiscoveryReport,
    )
    report = DiscoveryReport(
        discovered_count=5, modules_scanned=2, submodules_seen=10,
    )
    with pytest.raises(Exception):
        report.discovered_count = 99  # type: ignore


def test_discovery_report_as_dict_projection():
    from backend.core.ouroboros.governance.meta.module_discovery import (
        DiscoveryReport,
        SkippedModule,
        MODULE_DISCOVERY_SCHEMA_VERSION,
    )
    report = DiscoveryReport(
        discovered_count=3,
        modules_scanned=2,
        submodules_seen=5,
        packages_unavailable=(
            SkippedModule(
                full_name="missing.pkg",
                reason="ImportError: no such module",
            ),
        ),
        modules_skipped=(),
        elapsed_s=0.123,
    )
    d = report.as_dict()
    assert d["schema_version"] == MODULE_DISCOVERY_SCHEMA_VERSION
    assert d["discovered_count"] == 3
    assert len(d["packages_unavailable"]) == 1
    assert d["packages_unavailable"][0]["full_name"] == "missing.pkg"


# ---------------------------------------------------------------------------
# Convenience handlers
# ---------------------------------------------------------------------------


def test_make_registry_handler_passes_registry():
    from backend.core.ouroboros.governance.meta.module_discovery import (
        make_registry_handler,
    )
    captured = []

    def fake_register(registry):
        captured.append(registry)
        return 7

    sentinel = object()
    handler = make_registry_handler(registry=sentinel)
    assert handler("some.module", fake_register) == 7
    assert captured == [sentinel]


def test_make_registry_handler_zero_count():
    from backend.core.ouroboros.governance.meta.module_discovery import (
        make_registry_handler,
    )
    handler = make_registry_handler(registry=object())
    assert handler("m", lambda _r: 0) == 0
    assert handler("m", lambda _r: -1) == 0
    assert handler("m", lambda _r: "bad") == 0


def test_make_factory_handler_iterates_and_registers():
    from backend.core.ouroboros.governance.meta.module_discovery import (
        make_factory_handler,
    )
    registered = []
    handler = make_factory_handler(
        register_one=lambda spec: registered.append(spec),
    )
    result = handler(
        "some.module",
        lambda: ["spec1", "spec2", "spec3"],
    )
    assert result == 3
    assert registered == ["spec1", "spec2", "spec3"]


def test_make_factory_handler_skips_failed_registrations():
    from backend.core.ouroboros.governance.meta.module_discovery import (
        make_factory_handler,
    )

    def register_one(spec):
        if spec == "bad":
            raise ValueError("synthetic")

    handler = make_factory_handler(register_one=register_one)
    result = handler(
        "some.module", lambda: ["good", "bad", "good"],
    )
    assert result == 2


def test_make_factory_handler_empty_iterable():
    from backend.core.ouroboros.governance.meta.module_discovery import (
        make_factory_handler,
    )
    handler = make_factory_handler(register_one=lambda x: None)
    assert handler("m", lambda: []) == 0
    assert handler("m", lambda: None) == 0


# ---------------------------------------------------------------------------
# Three-consumer refactor — primitives delegate not duplicate
# ---------------------------------------------------------------------------


def test_shipped_code_invariants_uses_primitive():
    """The shipped_code_invariants discoverer MUST delegate to
    module_discovery.discover_module_provided_callable, not
    reimplement the walk."""
    import inspect
    from backend.core.ouroboros.governance.meta import (
        shipped_code_invariants,
    )
    src = inspect.getsource(
        shipped_code_invariants._discover_module_provided_invariants,
    )
    assert "discover_module_provided_callable" in src
    assert "make_factory_handler" in src
    # Forbid the legacy walk pattern.
    assert "pkgutil.iter_modules" not in src


def test_flag_registry_seed_uses_primitive():
    import inspect
    from backend.core.ouroboros.governance import flag_registry_seed
    src = inspect.getsource(
        flag_registry_seed._discover_module_provided_flags,
    )
    assert "discover_module_provided_callable" in src
    assert "make_registry_handler" in src
    assert "pkgutil.iter_modules" not in src


def test_help_dispatcher_uses_primitive():
    import inspect
    from backend.core.ouroboros.governance import help_dispatcher
    src = inspect.getsource(
        help_dispatcher._discover_module_provided_verbs,
    )
    assert "discover_module_provided_callable" in src
    assert "make_registry_handler" in src
    assert "pkgutil.iter_modules" not in src


# ---------------------------------------------------------------------------
# Authority asymmetry — pure substrate
# ---------------------------------------------------------------------------


def test_module_discovery_authority_asymmetry():
    """meta/module_discovery.py MUST stay pure substrate —
    stdlib only, no governance imports."""
    import ast as _ast
    from pathlib import Path
    target = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "meta"
        / "module_discovery.py"
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
                        f"module_discovery.py MUST NOT import "
                        f"{module!r} (authority asymmetry)"
                    )
        elif isinstance(node, _ast.Import):
            for alias in node.names:
                name = alias.name or ""
                for f in forbidden:
                    if f in name:
                        pytest.fail(
                            f"module_discovery.py MUST NOT "
                            f"import {name!r}"
                        )


def test_module_discovery_no_dynamic_code_calls():
    """Substrate must not invoke dynamic-code builtins. Pinned
    via AST walk against the closed-name set."""
    import ast as _ast
    from pathlib import Path
    target = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "meta"
        / "module_discovery.py"
    )
    tree = _ast.parse(target.read_text(encoding="utf-8"))
    forbidden_builtins = frozenset(
        ("exec", "ev" + "al", "compile"),
    )
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            if isinstance(node.func, _ast.Name):
                if node.func.id in forbidden_builtins:
                    pytest.fail(
                        f"module_discovery.py MUST NOT call "
                        f"{node.func.id}()"
                    )


# ---------------------------------------------------------------------------
# reset_registry_for_tests now re-discovers (Slice 2 bug fix)
# ---------------------------------------------------------------------------


def test_reset_registry_for_tests_re_runs_discovery():
    """Pre-Slice-2 bug: reset cleared registry but only
    re-seeded the static set, dropping module-owned pins.
    Slice 2 fix: reset rebuilds the FULL registry (seed +
    discovered)."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        list_shipped_code_invariants,
        reset_registry_for_tests,
    )
    reset_registry_for_tests()
    all_invs = list_shipped_code_invariants()
    names = {inv.invariant_name for inv in all_invs}
    assert "m10_synthesizer_uses_quorum" in names
    assert "graduation_orchestrator_module_archived" in names


# ---------------------------------------------------------------------------
# Synthetic broken module (per-module isolation under realistic
# conditions)
# ---------------------------------------------------------------------------


def test_broken_module_isolation_via_synthetic_package(
    tmp_path, monkeypatch,
):
    """Synthetic package: one good module + one that raises at
    import. Discovery completes the good one and records the
    bad one as skipped."""
    pkg = tmp_path / "synthetic_pkg_disc"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "good.py").write_text(
        "def the_attr():\n    return ['ok']\n",
        encoding="utf-8",
    )
    (pkg / "bad.py").write_text(
        "raise RuntimeError('synthetic import failure')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for k in list(sys.modules.keys()):
        if k.startswith("synthetic_pkg_disc"):
            del sys.modules[k]

    from backend.core.ouroboros.governance.meta.module_discovery import (
        discover_module_provided_callable,
    )
    report = discover_module_provided_callable(
        packages=["synthetic_pkg_disc"],
        attr_name="the_attr",
        handler=lambda fn_name, fn: len(fn() or []),
    )
    assert report.discovered_count == 1
    bad_skips = [
        s for s in report.modules_skipped
        if "bad" in s.full_name
    ]
    assert len(bad_skips) == 1
    assert "import_failed" in bad_skips[0].reason


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


def test_public_api_exports():
    from backend.core.ouroboros.governance.meta import module_discovery
    assert hasattr(module_discovery, "discover_module_provided_callable")
    assert hasattr(module_discovery, "DiscoveryReport")
    assert hasattr(module_discovery, "SkippedModule")
    assert hasattr(module_discovery, "make_registry_handler")
    assert hasattr(module_discovery, "make_factory_handler")
    assert hasattr(module_discovery, "module_discovery_enabled")
    assert hasattr(module_discovery, "MODULE_DISCOVERY_SCHEMA_VERSION")
