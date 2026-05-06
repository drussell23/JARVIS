"""Path D — operator-facing surfaces for the previously-unwired
autonomy modules:

  * D.1 — `/graph` REPL + `GET /observability/execution-graph`
    composing :class:`ExecutionGraphProgressTracker`
  * D.2 — `/monitor` REPL + `GET /observability/execution-monitor`
    composing :class:`ExecutionMonitor` (new
    :func:`get_default_monitor` singleton; SafetyNet now composes
    via singleton instead of allocating inline)

Pins per operator binding 2026-05-05:

  * Auto-discovered via §32.11 Slice 4 naming-cage (zero edits
    to repl_dispatch_registry.py)
  * Both REPLs are read-only (AST-pinned)
  * Authority asymmetry — substrate purity (AST-pinned)
  * Both compose canonical singletons (no parallel construction)
  * SafetyNet now composes get_default_monitor (single source
    of truth — operator surfaces read THE SAME instance the
    runtime is recording into)
  * NEVER raises across all paths
  * Public APIs stable

Verifies (28 tests).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# D.1 — ExecutionGraphProgress / `/graph`
# ---------------------------------------------------------------------------


def test_graph_help_renders_help():
    from backend.core.ouroboros.governance.graph_repl import (
        dispatch_graph_command,
    )
    r = dispatch_graph_command("/graph help")
    assert r.ok is True
    assert "L3 execution-graph browser" in r.text


def test_graph_bare_renders_active_or_empty():
    from backend.core.ouroboros.governance.graph_repl import (
        dispatch_graph_command,
    )
    r = dispatch_graph_command("/graph")
    # Either lists active or shows empty-state — never raises
    assert r.ok is True


def test_graph_all_renders_or_empty():
    from backend.core.ouroboros.governance.graph_repl import (
        dispatch_graph_command,
    )
    r = dispatch_graph_command("/graph all")
    assert r.ok is True


def test_graph_stats_renders():
    from backend.core.ouroboros.governance.graph_repl import (
        dispatch_graph_command,
    )
    r = dispatch_graph_command("/graph stats")
    # Either shows stats or substrate-unavailable
    assert r is not None


def test_graph_show_without_arg_errors():
    from backend.core.ouroboros.governance.graph_repl import (
        dispatch_graph_command,
    )
    r = dispatch_graph_command("/graph show")
    assert r.ok is False
    assert "argument required" in r.text


def test_graph_unknown_subcommand():
    from backend.core.ouroboros.governance.graph_repl import (
        dispatch_graph_command,
    )
    r = dispatch_graph_command("/graph nonsense")
    assert r.ok is False


def test_graph_non_match_returns_unmatched():
    from backend.core.ouroboros.governance.graph_repl import (
        dispatch_graph_command,
    )
    r = dispatch_graph_command("/health")
    assert r.matched is False


def test_graph_auto_discovered():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch,
    )
    r = try_dispatch("/graph help")
    assert r.matched is True
    assert r.ok is True


def test_graph_observability_register_routes_signature():
    from backend.core.ouroboros.governance import (
        graph_observability,
    )
    sig = inspect.signature(graph_observability.register_routes)
    params = sig.parameters
    assert "app" in params
    assert "rate_limit_check" in params
    assert "cors_headers" in params


def test_graph_observability_mounts_routes():
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance import (
        graph_observability,
    )
    app = web.Application()
    graph_observability.register_routes(app)
    canonical_paths = [
        getattr(getattr(r, "resource", None), "canonical", None)
        for r in app.router.routes()
    ]
    assert any(
        cp and cp.startswith("/observability/execution-graph")
        for cp in canonical_paths
    )


def test_graph_repl_pins_validate_clean():
    from backend.core.ouroboros.governance.graph_repl import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/graph_repl.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_graph_observability_pins_validate_clean():
    from backend.core.ouroboros.governance.graph_observability import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/graph_observability.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_graph_repl_register_verbs_returns_one():
    from backend.core.ouroboros.governance.graph_repl import (
        register_verbs,
    )

    class _Reg:
        def __init__(self):
            self.calls = []
        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Reg()
    assert register_verbs(reg) == 1
    assert reg.calls[0]["verb"] == "graph"


# ---------------------------------------------------------------------------
# D.2 — ExecutionMonitor / `/monitor`
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_monitor():
    """Reset the monitor singleton between tests."""
    from backend.core.ouroboros.governance.autonomy.execution_monitor import (  # noqa: E501
        reset_default_monitor_for_tests,
    )
    reset_default_monitor_for_tests()
    yield
    reset_default_monitor_for_tests()


def test_get_default_monitor_singleton(fresh_monitor):
    from backend.core.ouroboros.governance.autonomy.execution_monitor import (  # noqa: E501
        get_default_monitor,
    )
    a = get_default_monitor()
    b = get_default_monitor()
    assert a is b


def test_safety_net_composes_singleton():
    """ProductionSafetyNet.__init__ MUST compose
    get_default_monitor — single source of truth so operator
    surfaces read the same instance the runtime records into."""
    from backend.core.ouroboros.governance.autonomy.safety_net import (  # noqa: E501
        ProductionSafetyNet,
    )
    src = inspect.getsource(ProductionSafetyNet.__init__)
    assert "get_default_monitor" in src, (
        "ProductionSafetyNet.__init__ must compose "
        "get_default_monitor (Path D.2 single source of truth)"
    )


def test_monitor_help_renders():
    from backend.core.ouroboros.governance.monitor_repl import (
        dispatch_monitor_command,
    )
    r = dispatch_monitor_command("/monitor help")
    assert r.ok is True
    assert "execution-monitor browser" in r.text


def test_monitor_bare_renders_snapshot(fresh_monitor):
    from backend.core.ouroboros.governance.monitor_repl import (
        dispatch_monitor_command,
    )
    r = dispatch_monitor_command("/monitor")
    assert r.ok is True
    assert "snapshot" in r.text.lower() or "ExecutionMonitor" in r.text


def test_monitor_recent_renders(fresh_monitor):
    from backend.core.ouroboros.governance.monitor_repl import (
        dispatch_monitor_command,
    )
    r = dispatch_monitor_command("/monitor recent 5")
    assert r.ok is True


def test_monitor_recent_clamps_invalid_limit(fresh_monitor):
    from backend.core.ouroboros.governance.monitor_repl import (
        dispatch_monitor_command,
    )
    # Garbage limit falls back to default
    r = dispatch_monitor_command("/monitor recent garbage")
    assert r.ok is True


def test_monitor_stats_renders(fresh_monitor):
    from backend.core.ouroboros.governance.monitor_repl import (
        dispatch_monitor_command,
    )
    r = dispatch_monitor_command("/monitor stats")
    assert r.ok is True


def test_monitor_unknown_subcommand():
    from backend.core.ouroboros.governance.monitor_repl import (
        dispatch_monitor_command,
    )
    r = dispatch_monitor_command("/monitor garbage")
    assert r.ok is False


def test_monitor_auto_discovered():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch,
    )
    r = try_dispatch("/monitor help")
    assert r.matched is True
    assert r.ok is True


def test_monitor_observability_register_routes_signature():
    from backend.core.ouroboros.governance import (
        monitor_observability,
    )
    sig = inspect.signature(
        monitor_observability.register_routes,
    )
    params = sig.parameters
    assert "app" in params
    assert "rate_limit_check" in params
    assert "cors_headers" in params


def test_monitor_observability_mounts_routes():
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance import (
        monitor_observability,
    )
    app = web.Application()
    monitor_observability.register_routes(app)
    canonical_paths = [
        getattr(getattr(r, "resource", None), "canonical", None)
        for r in app.router.routes()
    ]
    assert any(
        cp and cp.startswith(
            "/observability/execution-monitor",
        )
        for cp in canonical_paths
    )


def test_monitor_repl_pins_validate_clean():
    from backend.core.ouroboros.governance.monitor_repl import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/monitor_repl.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_monitor_observability_pins_validate_clean():
    from backend.core.ouroboros.governance.monitor_observability import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/monitor_observability.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_graph_repl_public_api():
    from backend.core.ouroboros.governance import graph_repl
    assert set(graph_repl.__all__) == {
        "GraphReplDispatchResult",
        "dispatch_graph_command",
        "register_shipped_invariants",
        "register_verbs",
    }


def test_graph_observability_public_api():
    from backend.core.ouroboros.governance import (
        graph_observability,
    )
    assert set(graph_observability.__all__) == {
        "GRAPH_OBSERVABILITY_SCHEMA_VERSION",
        "register_routes",
        "register_shipped_invariants",
    }


def test_monitor_repl_public_api():
    from backend.core.ouroboros.governance import monitor_repl
    assert set(monitor_repl.__all__) == {
        "MonitorReplDispatchResult",
        "dispatch_monitor_command",
        "register_shipped_invariants",
        "register_verbs",
    }


def test_monitor_observability_public_api():
    from backend.core.ouroboros.governance import (
        monitor_observability,
    )
    assert set(monitor_observability.__all__) == {
        "MONITOR_OBSERVABILITY_SCHEMA_VERSION",
        "register_routes",
        "register_shipped_invariants",
    }


# ---------------------------------------------------------------------------
# Singleton + Read-API Extension Pattern — safety_net integration
# ---------------------------------------------------------------------------


def test_safety_net_runtime_writes_visible_via_singleton(
    fresh_monitor,
):
    """Operator binding: SafetyNet.record() writes MUST be
    visible via /monitor's read API. Single source of truth
    enforced through the singleton."""
    from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
        CommandBus,
    )
    from backend.core.ouroboros.governance.autonomy.execution_monitor import (  # noqa: E501
        get_default_monitor,
    )
    from backend.core.ouroboros.governance.autonomy.safety_net import (  # noqa: E501
        ProductionSafetyNet, SafetyNetConfig,
    )
    # Construct a SafetyNet — its inline composition must use
    # the canonical singleton.
    bus = CommandBus()
    cfg = SafetyNetConfig()
    sn = ProductionSafetyNet(command_bus=bus, config=cfg)
    monitor_via_safety_net = sn._execution_monitor  # noqa: SLF001
    monitor_via_singleton = get_default_monitor()
    # Same instance — single source of truth
    assert monitor_via_safety_net is monitor_via_singleton


def test_no_orchestrator_imports_in_repl_modules():
    """Path D substrate purity — none of the new modules
    import orchestrator/iron_gate/policy/providers."""
    forbidden = (
        "orchestrator", "iron_gate", "policy", "providers",
        "candidate_generator", "urgency_router",
        "change_engine", "semantic_guardian",
    )
    targets = (
        "backend/core/ouroboros/governance/graph_repl.py",
        "backend/core/ouroboros/governance/graph_observability.py",
        "backend/core/ouroboros/governance/monitor_repl.py",
        "backend/core/ouroboros/governance/monitor_observability.py",
    )
    for t in targets:
        path = _repo_root() / t
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        pytest.fail(
                            f"{t} imports forbidden {module!r}"
                        )
