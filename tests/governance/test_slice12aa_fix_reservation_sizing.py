"""Slice 12AA-fix — Reservation sizing model.

# Wedge (bt-2026-05-23-235325)

Slice 12AA shipped the per-op reservation mechanism correctly: other
ops can't consume the owning op's runway, and the owning op can
spend across multiple calls. But the SBA-side ``acquire_reservation``
call inside the Claude provider's lazy-acquire was sized from
``self._max_cost_per_op`` (~$0.585 — the provider's per-CALL cap),
not from the orchestrator's per-OP cumulative cap (~$1.00 — the
authoritative "total budget for this op across all its provider
calls + retries"). The bt-2026-05-23-235325 fixture needed ~$1.04
cumulative across 7 Claude streaming chunks, which exceeded the
$0.585 reservation. The SBA correctly refused the next legitimate
call from the same op as "no room" — but only because the
reservation was systematically under-sized.

# Fix

The Claude provider's lazy acquire now sources the reservation
amount from :meth:`CostGovernor.get_op_cap_usd` (a new read-only
accessor) via the process-singleton
:func:`get_default_cost_governor`. Fallback to
``self._max_cost_per_op`` only when CostGovernor is unregistered,
disabled, or the op was never started — preserving the Slice 12AA
behavior in unit-test / legacy paths where no orchestrator is
running.

# Non-goals (operator bindings, verbatim)

* "Do not use an empirical multiplier"
* "Do not hardcode SWE-Bench fixture values"

The cap is derived structurally from CostGovernor's existing
config (baseline × route × complexity × headroom × readonly). No
new knobs. No fixture-specific values.

# Composition preserved

* Session cap — unchanged (the cap clamp inside
  ``CostGovernor._derive_cap`` already honors session_remaining).
* Background-spend ceiling (Slice 12Y) — unchanged; background ops
  STILL can't acquire reservations (master switch / signal-source
  filter is upstream of the sizing inside acquire_reservation).
* Provider per-call cap (``_max_cost_per_op``) — unchanged; remains
  the per-CALL ceiling enforced in the provider's own loop.
* Release-on-terminal (Slice 12Q chokepoint) — unchanged.

# Test surface

1. New ``CostGovernor.get_op_cap_usd`` returns the cumulative cap
   for an active op.
2. Returns ``None`` for unregistered / disabled / pruned ops.
3. The accessor is read-only — does not mutate the entry.
4. AST pin: the Claude provider's lazy-acquire site imports
   ``get_default_cost_governor`` and calls ``get_op_cap_usd`` —
   proves the sizing-source is structural, not hardcoded.
5. AST pin: ``acquire_reservation`` is NOT called with
   ``self._max_cost_per_op`` as the FIRST-CHOICE source — only as
   the fallback when CostGov returns None.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.cost_governor import (
    CostGovernor,
    CostGovernorConfig,
    get_default_cost_governor,
    set_default_cost_governor,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def cg() -> CostGovernor:
    """Fresh CostGovernor with a known cap shape per op."""
    cfg = CostGovernorConfig(
        enabled=True,
        baseline_usd=0.10,
        retry_headroom=2.0,
        readonly_factor=5.0,
        route_factors={
            "immediate": 5.0,
            "standard": 1.0,
            "complex": 2.0,
            "background": 0.5,
            "speculative": 0.25,
        },
        complexity_factors={
            "trivial": 0.5,
            "moderate": 1.0,
            "heavy_code": 2.0,
        },
    )
    return CostGovernor(cfg)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Make sure the singleton accessor doesn't bleed across tests."""
    set_default_cost_governor(None)  # type: ignore[arg-type]
    yield
    set_default_cost_governor(None)  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────
# get_op_cap_usd — the new accessor
# ──────────────────────────────────────────────────────────────────────


class TestGetOpCapUsd:
    def test_returns_cap_for_active_op(self, cg: CostGovernor):
        cap = cg.start("op-a", route="standard", complexity="moderate")
        assert cap > 0
        assert cg.get_op_cap_usd("op-a") == pytest.approx(cap)

    def test_returns_none_when_op_unknown(self, cg: CostGovernor):
        assert cg.get_op_cap_usd("never-started") is None

    def test_returns_none_when_op_finished(self, cg: CostGovernor):
        cg.start("op-b", route="standard", complexity="moderate")
        cg.finish("op-b")
        assert cg.get_op_cap_usd("op-b") is None

    def test_returns_none_when_disabled(self):
        disabled = CostGovernor(CostGovernorConfig(enabled=False))
        # When disabled, start() returns +inf as a soft no-op gate
        # and the entry is NOT stored — get_op_cap_usd MUST return
        # None to make callers fall through to the safe default.
        disabled.start("op-c", route="standard", complexity="moderate")
        assert disabled.get_op_cap_usd("op-c") is None

    def test_is_read_only(self, cg: CostGovernor):
        original_cap = cg.start(
            "op-d", route="standard", complexity="moderate",
        )
        for _ in range(5):
            cg.get_op_cap_usd("op-d")
        # The cap mustn't shift from repeated reads, and the
        # entry must still be present with unchanged cumulative
        # and call count — proves no side effects.
        # Re-read via the canonical accessor (snapshot shape
        # varies across versions; the accessor is what callers
        # actually rely on).
        assert cg.get_op_cap_usd("op-d") == pytest.approx(original_cap)
        summary = cg.summary("op-d")
        assert summary is not None
        assert summary["cumulative_usd"] == 0.0
        assert summary["call_count"] == 0

    def test_returns_float(self, cg: CostGovernor):
        cg.start("op-e", route="complex", complexity="heavy_code")
        val = cg.get_op_cap_usd("op-e")
        assert isinstance(val, float)

    def test_reflects_refresh_via_restart(self, cg: CostGovernor):
        """start() is idempotent — calling it with a different
        route/complexity updates the cap. get_op_cap_usd should
        return the NEW value (proves it reads live data, not a
        snapshot at first start)."""
        first = cg.start(
            "op-f", route="standard", complexity="trivial",
        )
        second = cg.start(
            "op-f", route="complex", complexity="heavy_code",
        )
        assert second > first
        assert cg.get_op_cap_usd("op-f") == pytest.approx(second)

    def test_never_raises_on_pathological_op_id(self, cg: CostGovernor):
        # Defensive: even with weird IDs, MUST NOT raise.
        for bad in ("", "  ", "x" * 10_000):
            assert cg.get_op_cap_usd(bad) is None


# ──────────────────────────────────────────────────────────────────────
# Singleton accessor wiring
# ──────────────────────────────────────────────────────────────────────


class TestSingletonAccessor:
    def test_default_unregistered_returns_none(self):
        assert get_default_cost_governor() is None

    def test_register_then_lookup(self, cg: CostGovernor):
        set_default_cost_governor(cg)
        assert get_default_cost_governor() is cg
        cg.start("op-g", route="standard", complexity="moderate")
        looked_up = get_default_cost_governor()
        assert looked_up is not None
        assert looked_up.get_op_cap_usd("op-g") is not None


# ──────────────────────────────────────────────────────────────────────
# AST pins — sizing-source is STRUCTURAL, not hardcoded
# ──────────────────────────────────────────────────────────────────────


PROVIDERS_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "providers.py"
)


def _load_providers_ast() -> ast.Module:
    return ast.parse(PROVIDERS_PATH.read_text())


def _find_claude_generate(tree: ast.Module) -> ast.AsyncFunctionDef:
    """Locate ClaudeProvider.generate via AST walk."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ClaudeProvider":
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "generate"
                ):
                    return child
    raise AssertionError(
        "ClaudeProvider.generate not found in providers.py",
    )


class TestProvidersASTPins:
    """Structural pins — the fix MUST be wired via the singleton
    accessor, never reverting to ``self._max_cost_per_op`` as the
    first-choice sizing source."""

    def test_claude_generate_imports_cost_governor_accessor(self):
        tree = _load_providers_ast()
        generate = _find_claude_generate(tree)
        src = ast.unparse(generate)
        assert "get_default_cost_governor" in src, (
            "ClaudeProvider.generate must import "
            "get_default_cost_governor for Slice 12AA-fix sizing"
        )
        assert "get_op_cap_usd" in src, (
            "ClaudeProvider.generate must call get_op_cap_usd to "
            "size the lazy reservation from the cumulative cap"
        )

    def test_acquire_reservation_uses_cumulative_cap_not_per_call_cap(
        self,
    ):
        """The new sizing model MUST route through a variable
        derived from CostGovernor — NOT pass
        ``self._max_cost_per_op`` directly to acquire_reservation
        as the estimated_total_usd."""
        tree = _load_providers_ast()
        generate = _find_claude_generate(tree)
        # Find every call to a function literally named
        # ``acquire_reservation`` (the import-as alias is
        # ``_sba_acquire``).
        for call in ast.walk(generate):
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            fname = ""
            if isinstance(fn, ast.Name):
                fname = fn.id
            elif isinstance(fn, ast.Attribute):
                fname = fn.attr
            if fname not in ("_sba_acquire", "acquire_reservation"):
                continue
            # Inspect the estimated_total_usd kwarg.
            for kw in call.keywords:
                if kw.arg != "estimated_total_usd":
                    continue
                # Render the expression and assert it does NOT
                # literally pass `self._max_cost_per_op` as the
                # immediate argument — it should be a Name
                # derived from CostGovernor lookup (with a
                # fallback variable that holds the per-call cap
                # only when CostGov is unavailable).
                rendered = ast.unparse(kw.value)
                assert "self._max_cost_per_op" not in rendered, (
                    "estimated_total_usd MUST NOT pass "
                    "self._max_cost_per_op directly — "
                    "Slice 12AA-fix requires the reservation be "
                    "sized from the CostGovernor cumulative cap, "
                    "with the per-call cap only as a fallback "
                    "wrapped behind a None check"
                )

    def test_lazy_acquire_has_none_fallback_to_per_call_cap(self):
        """Defense-in-depth: when CostGov returns None
        (unregistered / disabled / op not started), the
        lazy-acquire MUST fall back to the per-call cap —
        NOT zero, NOT skip the acquire — so legacy / unit-test
        paths still benefit from Slice 12AA."""
        tree = _load_providers_ast()
        generate = _find_claude_generate(tree)
        src = ast.unparse(generate)
        # Look for the fallback shape — the per-call cap must
        # still appear as a fallback expression even though it's
        # no longer the first-choice path.
        assert "_max_cost_per_op" in src, (
            "Slice 12AA-fix MUST keep self._max_cost_per_op as the "
            "fallback when CostGovernor.get_op_cap_usd returns None"
        )
        # And the fallback must be guarded by a None check —
        # this is a substring proxy that the code-shape has both
        # branches (the None-fallback and the CostGov-resolved
        # branch).
        assert "None" in src or "is None" in src


# ──────────────────────────────────────────────────────────────────────
# Anti-hardcode + anti-multiplier pins (operator bindings)
# ──────────────────────────────────────────────────────────────────────


class TestAntiHardcode:
    def test_no_fixture_specific_dollar_constants_in_acquire_site(self):
        """Operator binding: 'do not hardcode SWE-Bench fixture
        values'. The lazy-acquire site MUST NOT contain literals
        matching $1.04, $0.585, $1.20 — values observed in the
        bt-2026-05-23-235325 cost-shape audit. (Generic constants
        like 0.0 / 1.0 are excluded — they're not fixture values.)
        """
        tree = _load_providers_ast()
        generate = _find_claude_generate(tree)
        forbidden = (1.04, 0.585, 1.20)
        for node in ast.walk(generate):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, (int, float)):
                continue
            assert float(node.value) not in forbidden, (
                f"Forbidden fixture literal {node.value!r} appears "
                "in ClaudeProvider.generate — Slice 12AA-fix bans "
                "fixture-specific dollar constants. Source the "
                "value from CostGovernor instead."
            )

    def test_no_empirical_multiplier_on_acquire_call(self):
        """Operator binding: 'do not use an empirical multiplier'.
        The estimated_total_usd expression MUST NOT contain
        a BinOp Mult with a numeric literal (i.e., '_max * 2.0'
        or 'cap * 1.5')."""
        tree = _load_providers_ast()
        generate = _find_claude_generate(tree)
        for call in ast.walk(generate):
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            fname = (
                fn.id if isinstance(fn, ast.Name)
                else (fn.attr if isinstance(fn, ast.Attribute) else "")
            )
            if fname not in ("_sba_acquire", "acquire_reservation"):
                continue
            for kw in call.keywords:
                if kw.arg != "estimated_total_usd":
                    continue
                for sub in ast.walk(kw.value):
                    if (
                        isinstance(sub, ast.BinOp)
                        and isinstance(sub.op, ast.Mult)
                    ):
                        # Either operand a numeric Constant?
                        for operand in (sub.left, sub.right):
                            if (
                                isinstance(operand, ast.Constant)
                                and isinstance(
                                    operand.value, (int, float),
                                )
                                and operand.value not in (0, 1, 0.0, 1.0)
                            ):
                                raise AssertionError(
                                    "estimated_total_usd contains an "
                                    "empirical multiplier "
                                    f"({operand.value!r}) — "
                                    "Slice 12AA-fix bans tuning "
                                    "constants here"
                                )


# ──────────────────────────────────────────────────────────────────────
# Composition with Slice 12Y (background ceiling) — unchanged
# ──────────────────────────────────────────────────────────────────────


class TestCompositionPreserved:
    def test_get_op_cap_usd_is_independent_of_signal_source(
        self, cg: CostGovernor,
    ):
        """CostGov knows about route + complexity + is_read_only,
        NOT about signal_source. Slice 12Y's background-tier
        filtering happens upstream inside acquire_reservation, so
        this accessor MUST stay signal-source agnostic — that's
        the seam that keeps background ops gated by Slice 12Y
        even when this accessor returns a positive number."""
        cap = cg.start(
            "op-bg", route="background", complexity="moderate",
        )
        # The cap exists (CostGov doesn't filter by source); it's
        # SBA's acquire_reservation that refuses to materialize a
        # reservation for background sources.
        assert cg.get_op_cap_usd("op-bg") == pytest.approx(cap)

    def test_accessor_signature_is_minimal(self):
        """get_op_cap_usd MUST take exactly (self, op_id) — no
        optional kwargs that would invite scope-creep."""
        sig = inspect.signature(CostGovernor.get_op_cap_usd)
        params = list(sig.parameters)
        assert params == ["self", "op_id"], (
            f"get_op_cap_usd signature has drifted: {params!r}"
        )
