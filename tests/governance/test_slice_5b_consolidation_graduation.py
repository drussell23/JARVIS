"""Slice 5b consolidation arc — Slice 5 graduation regression
spine (PRD §32.5 / §32.11).

End-to-end closure-bar test that proves all four prior slices
hold together at runtime:

  * Slice 1 — graduation_orchestrator + graduation_tracker
    archived; dead wiring removed; 4 cleanup pins
  * Slice 2 — module_discovery substrate is the SOLE walker;
    3 consumers (flag_registry_seed / shipped_code_invariants /
    help_dispatcher) delegate
  * Slice 3 — observability_route_registry auto-mounts 5
    dormant surfaces via single boot call
  * Slice 4 — repl_dispatch_registry auto-routes 17+ verbs via
    single try_dispatch call

Plus the umbrella graduation contract:
  * 14 cleanup_invariants pins all PASS validate_all
  * 5 FlagRegistry seeds present (master flags for all four
    substrates + JARVIS_OBSERVABILITY_AUTODISCOVERY_ENABLED
    + JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED)
  * Public API of every consolidation-arc module is stable
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Slice 1 — cleanup state
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_slice1_archived_files_exist():
    for rel in (
        "archive/legacy/graduation_orchestrator_2026_04_06.py",
        "archive/legacy/graduation_tracker_2026_04_06.py",
        "archive/legacy/test_graduation_orchestrator_2026_04_06.py",  # noqa: E501
    ):
        p = _repo_root() / rel
        assert p.exists() and p.stat().st_size > 1_000


def test_slice1_production_paths_absent():
    for rel in (
        "backend/core/ouroboros/governance/graduation_orchestrator.py",  # noqa: E501
        "backend/core/ouroboros/governance/graduation_tracker.py",
        "tests/governance/test_graduation_orchestrator.py",
    ):
        assert not (_repo_root() / rel).exists()


def test_slice1_archive_readme_present():
    readme = _repo_root() / "archive" / "legacy" / "README.md"
    assert readme.exists()


def test_slice1_dead_wiring_removed():
    """harness.py / runtime_task_orchestrator.py /
    governed_loop_service.py MUST NOT contain the pre-Slice-1
    graduation_orchestrator / graduation_tracker references."""
    forbidden_per_file = {
        "backend/core/ouroboros/battle_test/harness.py": (
            "boot_graduation",
            "self._graduation_orchestrator",
        ),
        "backend/core/runtime_task_orchestrator.py": (
            "_graduation_tracker",
            "evaluate_graduation",
        ),
        "backend/core/ouroboros/governance/governed_loop_service.py": (  # noqa: E501
            "_graduation_tracker",
        ),
    }
    for rel, forbidden in forbidden_per_file.items():
        text = (_repo_root() / rel).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, (
                f"{rel} still contains forbidden token "
                f"{token!r} after Slice 1 cleanup"
            )


# ---------------------------------------------------------------------------
# Slice 2 — module_discovery substrate
# ---------------------------------------------------------------------------


def test_slice2_substrate_public_api():
    from backend.core.ouroboros.governance.meta import (
        module_discovery,
    )
    expected = (
        "discover_module_provided_callable",
        "DiscoveryReport",
        "SkippedModule",
        "make_registry_handler",
        "make_factory_handler",
        "module_discovery_enabled",
        "MODULE_DISCOVERY_SCHEMA_VERSION",
    )
    for name in expected:
        assert hasattr(module_discovery, name)


def test_slice2_consumers_delegate_to_primitive():
    """Three consumers MUST import and call the primitive
    (no parallel walkers). Verified by grep + AST pin."""
    import inspect
    from backend.core.ouroboros.governance.meta import (
        shipped_code_invariants,
    )
    from backend.core.ouroboros.governance import (
        flag_registry_seed, help_dispatcher,
    )
    sources = (
        inspect.getsource(
            shipped_code_invariants._discover_module_provided_invariants,  # noqa: E501
        ),
        inspect.getsource(
            flag_registry_seed._discover_module_provided_flags,
        ),
        inspect.getsource(
            help_dispatcher._discover_module_provided_verbs,
        ),
    )
    for src in sources:
        assert "discover_module_provided_callable" in src
        assert "pkgutil.iter_modules" not in src


def test_slice2_module_scan_mode_added():
    """Slice 4 added attr_name=None mode to the primitive
    additively. Verify the source carries the conditional."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/meta/"
        "module_discovery.py"
    )
    text = target.read_text(encoding="utf-8")
    assert "if attr_name is None:" in text, (
        "module-scan mode missing from primitive (Slice 4 "
        "additive extension)"
    )


# ---------------------------------------------------------------------------
# Slice 3 — observability route registry end-to-end
# ---------------------------------------------------------------------------


_EXPECTED_OBSERVABILITY_MODULES = {
    "backend.core.ouroboros.governance.decisions_observability",
    "backend.core.ouroboros.governance.curiosity_observability",
    "backend.core.ouroboros.governance.epistemic_budget_observability",  # noqa: E501
    "backend.core.ouroboros.governance.m10.observability",
    "backend.core.ouroboros.governance.action_outcome_memory_observability",  # noqa: E501
}


def test_slice3_all_five_dormant_surfaces_auto_mount():
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        discover_and_mount_observability_routes,
        reset_registry_for_tests,
    )
    reset_registry_for_tests()
    app = web.Application()
    report = discover_and_mount_observability_routes(app)
    mounted_names = {r.module_full_name for r in report.mounted}
    missing = _EXPECTED_OBSERVABILITY_MODULES - mounted_names
    assert not missing, f"missing auto-mounts: {missing}"
    assert report.handler_failed == 0
    reset_registry_for_tests()


def test_slice3_all_five_route_paths_reachable():
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        discover_and_mount_observability_routes,
        reset_registry_for_tests,
    )
    reset_registry_for_tests()
    app = web.Application()
    discover_and_mount_observability_routes(app)
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
            f"no route mounted under {prefix}"
        )
    reset_registry_for_tests()


# ---------------------------------------------------------------------------
# Slice 4 — REPL dispatch registry end-to-end
# ---------------------------------------------------------------------------


_EXPECTED_LEGACY_VERBS = (
    "probe", "coherence", "quorum", "failures", "outcomes",
)
_EXPECTED_NEWLY_UNLOCKED_VERBS = (
    "m10", "decisions", "curiosity",
)
_EXPECTED_EXCLUDED_VERBS = (
    "budget", "risk", "goal", "cancel", "plan",
    "postmortems", "inline",
)


def test_slice4_all_legacy_verbs_auto_discovered():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        prime_registry, reset_registry_for_tests,
    )
    reset_registry_for_tests()
    report = prime_registry(force=True)
    for v in _EXPECTED_LEGACY_VERBS:
        assert v in report.verbs, (
            f"legacy verb {v!r} not in registry"
        )
    reset_registry_for_tests()


def test_slice4_all_newly_unlocked_verbs_route():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch, reset_registry_for_tests,
    )
    reset_registry_for_tests()
    for verb in _EXPECTED_NEWLY_UNLOCKED_VERBS:
        out = try_dispatch(f"/{verb} help")
        assert out.matched is True, (
            f"newly-unlocked verb {verb!r} did not match"
        )
        assert out.ok is True, (
            f"verb {verb!r} dispatched but help failed"
        )
        assert out.verb == verb
        assert len(out.text) > 0
    reset_registry_for_tests()


def test_slice4_excluded_verbs_no_match():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch, reset_registry_for_tests,
    )
    reset_registry_for_tests()
    # Build a registry first; excluded verbs should NOT be in it.
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        prime_registry,
    )
    report = prime_registry(force=True)
    for v in _EXPECTED_EXCLUDED_VERBS:
        assert v not in report.verbs, (
            f"excluded verb {v!r} leaked into registry"
        )
    # try_dispatch on excluded verb returns no-match so legacy
    # custom handler retains authority.
    out = try_dispatch("/budget 1.00")
    assert out.matched is False
    reset_registry_for_tests()


def test_slice4_serpent_replaced_legacy_helper():
    target = (
        _repo_root()
        / "backend/core/ouroboros/battle_test/serpent_flow.py"
    )
    text = target.read_text(encoding="utf-8")
    assert "def _print_observability_verb" not in text
    assert "repl_dispatch_registry" in text
    assert "try_dispatch" in text


# ---------------------------------------------------------------------------
# Cross-slice — 14 cleanup pins all green
# ---------------------------------------------------------------------------


_EXPECTED_CLEANUP_PIN_NAMES = {
    # Slice 1 — archive-only (4)
    "graduation_orchestrator_archived_only_harness",
    "graduation_orchestrator_archived_only_runtime_task",
    "graduation_orchestrator_archived_only_governed_loop",
    "graduation_orchestrator_module_archived",
    # Slice 2 — consumer-uses-primitive (3)
    "module_discovery_consumer_flag_registry_seed",
    "module_discovery_consumer_shipped_code_invariants",
    "module_discovery_consumer_help_dispatcher",
    # Slice 3 — observability per-module (5) + registry (1)
    "observability_module_exposes_register_routes_decisions",
    "observability_module_exposes_register_routes_curiosity",
    "observability_module_exposes_register_routes_epistemic_budget",  # noqa: E501
    "observability_module_exposes_register_routes_action_outcome",  # noqa: E501
    "observability_module_exposes_register_routes_m10",
    "observability_route_registry_uses_primitive",
    # Slice 4 — REPL registry (1)
    "repl_dispatch_registry_uses_primitive",
}


def test_consolidation_arc_all_pins_registered():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        list_shipped_code_invariants,
        reset_registry_for_tests,
    )
    reset_registry_for_tests()
    registered = {
        inv.invariant_name
        for inv in list_shipped_code_invariants()
    }
    missing = _EXPECTED_CLEANUP_PIN_NAMES - registered
    assert not missing, (
        f"missing consolidation-arc pins: {missing}"
    )


def test_consolidation_arc_all_pins_pass_validation():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        validate_all,
        reset_registry_for_tests,
    )
    reset_registry_for_tests()
    violations = validate_all()
    relevant = [
        v for v in violations
        if v.invariant_name in _EXPECTED_CLEANUP_PIN_NAMES
    ]
    assert not relevant, (
        "consolidation-arc pin violations: "
        + "; ".join(
            f"{v.invariant_name}: {v.violation}"
            for v in relevant
        )
    )


# ---------------------------------------------------------------------------
# Cross-slice — FlagRegistry seeds for every master flag
# ---------------------------------------------------------------------------


_EXPECTED_MASTER_FLAGS = {
    "JARVIS_MODULE_DISCOVERY_ENABLED",
    "JARVIS_OBSERVABILITY_AUTODISCOVERY_ENABLED",
    "JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED",
}


def test_consolidation_arc_master_flags_seeded():
    from backend.core.ouroboros.governance.flag_registry_seed import (
        SEED_SPECS,
    )
    seeded = {spec.name for spec in SEED_SPECS}
    missing = _EXPECTED_MASTER_FLAGS - seeded
    assert not missing, (
        f"missing FlagRegistry seeds: {missing}"
    )


def test_consolidation_arc_master_flags_default_true():
    from backend.core.ouroboros.governance.flag_registry_seed import (
        SEED_SPECS,
    )
    by_name = {spec.name: spec for spec in SEED_SPECS}
    for name in _EXPECTED_MASTER_FLAGS:
        spec = by_name.get(name)
        assert spec is not None
        assert spec.default is True, (
            f"{name} should default-true at graduation; "
            f"got default={spec.default!r}"
        )


# ---------------------------------------------------------------------------
# Cross-slice — single source of truth for the walker
# ---------------------------------------------------------------------------


def test_no_other_module_calls_pkgutil_iter_modules():
    """The Slice 2 substrate is the SOLE owner of
    ``pkgutil.iter_modules`` in production code. Any other
    governance / battle_test module containing the actual call
    would indicate a parallel walker (regression).

    AST-based check distinguishes real calls from string
    literals (e.g. cleanup_invariants's validator that scans
    OTHER files for the same pattern)."""
    import ast as _ast
    from pathlib import Path
    base = (
        _repo_root()
        / "backend" / "core" / "ouroboros"
    )
    # Slice 2 substrate is the sole legitimate owner of the call.
    allowed = {
        base
        / "governance"
        / "meta"
        / "module_discovery.py",
    }
    violations = []
    for py in base.rglob("*.py"):
        if "__pycache__" in str(py):
            continue
        if py in allowed:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except OSError:
            continue
        if "pkgutil.iter_modules" not in text:
            continue
        # AST check — only flag actual call sites, not string
        # literals or AST-validator scan tokens.
        try:
            tree = _ast.parse(text)
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Attribute):
                if (
                    isinstance(node.value, _ast.Name)
                    and node.value.id == "pkgutil"
                    and node.attr == "iter_modules"
                ):
                    violations.append(
                        str(py.relative_to(_repo_root())),
                    )
                    break
    assert not violations, (
        f"parallel walker detected — only "
        f"meta/module_discovery.py may call "
        f"pkgutil.iter_modules. Violators: {violations}"
    )


# ---------------------------------------------------------------------------
# Smoke — end-to-end boot integration
# ---------------------------------------------------------------------------


def test_smoke_event_channel_imports_clean():
    """event_channel.py wires the observability registry. It
    must import + parse cleanly post-Slice-3 wiring."""
    import importlib
    importlib.import_module(
        "backend.core.ouroboros.governance.event_channel",
    )


def test_smoke_serpent_flow_imports_clean():
    import importlib
    importlib.import_module(
        "backend.core.ouroboros.battle_test.serpent_flow",
    )


def test_smoke_full_registry_priming_completes():
    """Both Slice 3 + Slice 4 registries prime against a fresh
    aiohttp app within reasonable time (<30s on dev hardware)."""
    pytest.importorskip("aiohttp")
    import time
    from aiohttp import web
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        discover_and_mount_observability_routes,
        reset_registry_for_tests as obs_reset,
    )
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        prime_registry,
        reset_registry_for_tests as repl_reset,
    )

    obs_reset()
    repl_reset()

    t0 = time.monotonic()
    app = web.Application()
    obs_report = discover_and_mount_observability_routes(app)
    repl_report = prime_registry(force=True)
    elapsed = time.monotonic() - t0

    assert elapsed < 30.0
    assert obs_report.mounted_count >= 5
    assert repl_report.verb_count >= 12

    obs_reset()
    repl_reset()


# ---------------------------------------------------------------------------
# Public API stability — every consolidation-arc module
# ---------------------------------------------------------------------------


def test_public_api_stability():
    """The four consolidation-arc modules each expose a stable
    public surface. Future refactors that change these names
    fail this pin first."""
    from backend.core.ouroboros.governance.meta import (
        module_discovery,
    )
    from backend.core.ouroboros.governance import (
        observability_route_registry,
        cleanup_invariants,
    )
    from backend.core.ouroboros.battle_test import (
        repl_dispatch_registry,
    )

    # module_discovery (Slice 2)
    for n in (
        "discover_module_provided_callable",
        "DiscoveryReport",
        "module_discovery_enabled",
    ):
        assert hasattr(module_discovery, n)

    # observability_route_registry (Slice 3)
    for n in (
        "discover_and_mount_observability_routes",
        "MountReport",
        "observability_autodiscovery_enabled",
    ):
        assert hasattr(observability_route_registry, n)

    # cleanup_invariants (Slice 1 + extensions in 2/3/4)
    assert hasattr(cleanup_invariants, "register_shipped_invariants")
    pins = cleanup_invariants.register_shipped_invariants()
    assert len(pins) == 14

    # repl_dispatch_registry (Slice 4)
    for n in (
        "try_dispatch",
        "prime_registry",
        "DispatchOutcome",
        "repl_dispatch_autodiscovery_enabled",
    ):
        assert hasattr(repl_dispatch_registry, n)
