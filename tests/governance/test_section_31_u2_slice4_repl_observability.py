"""§31 U2 empirical wiring Slice 4 — `/causal` REPL +
`GET /observability/causal/{record_id}` regression spine.

Pins per operator binding 2026-05-05:

  * /causal REPL composes Slice 1 compute_op_causal_features —
    NO parallel feature extraction (AST-pinned)
  * Authority asymmetry — REPL + observability both forbid
    orchestrator/iron_gate/policy/providers imports + .record()
    calls (AST-pinned)
  * Read-only — REPL never calls apply_replay_from_record_env
    (AST-pinned)
  * /causal auto-discovered via §32.11 Slice 4 naming-cage
    (zero edits to repl_dispatch_registry.py — AST scan)
  * Observability auto-mounted via §32.11 Slice 3 registry
    (module-level register_routes signature)
  * NEVER raises across all paths
  * Public API stable

Verifies (22 tests).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# /causal REPL dispatcher
# ---------------------------------------------------------------------------


def test_dispatch_help_renders_help():
    from backend.core.ouroboros.governance.causal_repl import (
        dispatch_causal_command,
    )
    r = dispatch_causal_command("/causal help")
    assert r.ok is True
    assert r.matched is True
    assert "causal-lineage browser" in r.text


def test_dispatch_bare_renders_observer_state():
    from backend.core.ouroboros.governance.causal_repl import (
        dispatch_causal_command,
    )
    r = dispatch_causal_command("/causal")
    assert r.ok is True
    # Should render observer-state header (or empty-state)
    assert "/causal" in r.text or "observer" in r.text.lower()


def test_dispatch_show_without_args_errors():
    from backend.core.ouroboros.governance.causal_repl import (
        dispatch_causal_command,
    )
    r = dispatch_causal_command("/causal show")
    assert r.ok is False
    assert "argument required" in r.text


def test_dispatch_show_colon_form():
    from backend.core.ouroboros.governance.causal_repl import (
        dispatch_causal_command,
    )
    r = dispatch_causal_command(
        "/causal show fake-session:fake-rec",
    )
    assert r.ok is True


def test_dispatch_show_space_form():
    from backend.core.ouroboros.governance.causal_repl import (
        dispatch_causal_command,
    )
    r = dispatch_causal_command(
        "/causal show fake-session fake-rec",
    )
    assert r.ok is True


def test_dispatch_unknown_subcommand():
    from backend.core.ouroboros.governance.causal_repl import (
        dispatch_causal_command,
    )
    r = dispatch_causal_command("/causal nonsense")
    assert r.ok is False
    assert "unknown subcommand" in r.text


def test_dispatch_non_causal_returns_unmatched():
    from backend.core.ouroboros.governance.causal_repl import (
        dispatch_causal_command,
    )
    r = dispatch_causal_command("/health")
    assert r.matched is False


def test_dispatch_does_not_raise_on_garbage():
    from backend.core.ouroboros.governance.causal_repl import (
        dispatch_causal_command,
    )
    for line in (
        "/causal show 'bad",
        "/causal show :",
        "/causal show :::",
    ):
        r = dispatch_causal_command(line)
        # Returns a result, never raises
        assert r is not None


# ---------------------------------------------------------------------------
# Auto-discovery via §32.11 naming-cage
# ---------------------------------------------------------------------------


def test_repl_dispatch_registry_routes_causal():
    """§32.11 Slice 4 naming-cage: file ends `_repl.py` →
    verb `/causal` → routes WITHOUT any edit to
    repl_dispatch_registry.py."""
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch,
    )
    r = try_dispatch("/causal help")
    assert r.matched is True
    assert r.ok is True
    assert "causal-lineage browser" in r.text


def test_register_verbs_returns_one():
    from backend.core.ouroboros.governance.causal_repl import (
        register_verbs,
    )

    class _Reg:
        def __init__(self):
            self.calls = []
        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Reg()
    n = register_verbs(reg)
    assert n == 1
    assert reg.calls[0]["verb"] == "causal"


# ---------------------------------------------------------------------------
# /observability/causal HTTP route
# ---------------------------------------------------------------------------


def test_observability_module_exposes_register_routes():
    """§32.11 Slice 3 contract: module exposes module-level
    register_routes(app, *, rate_limit_check, cors_headers)."""
    from backend.core.ouroboros.governance import (
        causal_observability,
    )
    assert hasattr(causal_observability, "register_routes")
    sig = inspect.signature(causal_observability.register_routes)
    params = sig.parameters
    assert "app" in params
    assert "rate_limit_check" in params
    assert "cors_headers" in params


def test_register_routes_with_aiohttp_unavailable_no_op():
    """If aiohttp can't be imported, register_routes silently
    returns without raising."""
    from backend.core.ouroboros.governance import (
        causal_observability,
    )
    # Pass a dummy app — register_routes must be no-op safe
    class DummyApp:
        class router:
            @staticmethod
            def add_get(*a, **kw):
                pass

    # Either aiohttp is available (route mounts) or not
    # (silent no-op); never raises.
    causal_observability.register_routes(DummyApp())


def test_register_routes_mounts_get_path():
    """When aiohttp is available, the GET route is registered
    at the canonical path."""
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance import (
        causal_observability,
    )
    app = web.Application()
    causal_observability.register_routes(app)
    # Canonical path under /observability/
    paths = [
        getattr(r, "resource", None) for r in app.router.routes()
    ]
    canonical_paths = [
        getattr(p, "canonical", None)
        for p in paths if p is not None
    ]
    assert any(
        cp and cp.startswith("/observability/causal/")
        for cp in canonical_paths
    )


# ---------------------------------------------------------------------------
# AST pins — REPL
# ---------------------------------------------------------------------------


def test_repl_register_shipped_invariants_returns_3():
    from backend.core.ouroboros.governance.causal_repl import (
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "causal_repl_authority_read_only",
        "causal_repl_authority_asymmetry",
        "causal_repl_composes_slice_1",
    }


def test_repl_pins_validate_clean():
    from backend.core.ouroboros.governance.causal_repl import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/causal_repl.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_repl_read_only_pin_fires_on_apply_call():
    from backend.core.ouroboros.governance.causal_repl import (
        register_shipped_invariants,
    )
    bad = '''
def f():
    apply_replay_from_record_env(plan)
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "read_only" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_repl_asymmetry_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.causal_repl import (
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.providers "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_repl_composes_slice1_pin_fires_on_missing_compose():
    from backend.core.ouroboros.governance.causal_repl import (
        register_shipped_invariants,
    )
    bad = "import os\n# no compose import"
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "composes_slice_1" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# AST pins — observability
# ---------------------------------------------------------------------------


def test_observability_register_shipped_invariants_returns_1():
    from backend.core.ouroboros.governance.causal_observability import (  # noqa: E501
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "causal_observability_authority_asymmetry",
    }


def test_observability_pins_validate_clean():
    from backend.core.ouroboros.governance.causal_observability import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/causal_observability.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_observability_asymmetry_pin_fires_on_iron_gate_import():
    from backend.core.ouroboros.governance.causal_observability import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.iron_gate "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(iter(register_shipped_invariants()))
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def test_repl_public_api_stable():
    from backend.core.ouroboros.governance import causal_repl
    expected = {
        "CausalReplDispatchResult",
        "dispatch_causal_command",
        "register_shipped_invariants",
        "register_verbs",
    }
    assert set(causal_repl.__all__) == expected


def test_observability_public_api_stable():
    from backend.core.ouroboros.governance import (
        causal_observability,
    )
    expected = {
        "CAUSAL_OBSERVABILITY_SCHEMA_VERSION",
        "register_routes",
        "register_shipped_invariants",
    }
    assert set(causal_observability.__all__) == expected
