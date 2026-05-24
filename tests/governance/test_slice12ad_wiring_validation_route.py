"""Slice 12AD — Budget-aware wiring-validation route.

# Wedge (bt-2026-05-24-033510)

Phase 1 wiring-validation soak at $2.00 cap spent $1.81 on the
``jarvis__harness-smoke-001`` smoke fixture and still didn't reach
COMPLETE — the governance pipeline imposes a real minimum-spend floor
on any op (IronGate exploration mandate + Venom multi-round tool
loop + GENERATE retry headroom) that doesn't scale with actual
problem difficulty. Even a ``gold_patch=""`` smoke fixture burns
through $1.81+ before SBA preflight refuses the next chunk.

Slice 12P closed half of this — drops IronGate exploration floor to 0
for wiring-validation envelopes (always-on, no master flag). But
Venom + the per-op CostGov cap derivation still treat the fixture
as a COMPLEX op.

# Fix (Slice 12AD)

New ``ProviderRoute.WIRING_VALIDATION`` enum value + new
``UrgencyRouter.classify`` Priority 0.6 + new low ``CostGov``
route_factor + Venom-skip via a new pure-data ``route_predicates``
module. All gated by ``JARVIS_WIRING_VALIDATION_ROUTE_ENABLED``
(default-FALSE per §33.1).

The detector composes two envelope-metadata signals:
  * ``fixture_purpose == "wiring_validation"``
  * ``real_benchmark is False`` (exact False, not falsy)

# Test surface (per operator spec)

  1. Wiring-validation fixture metadata takes the route when flag enabled.
  2. Real benchmark metadata is rejected from this route.
  3. Missing/ambiguous metadata does not take this route.
  4. Exploration gate bypassed only for wiring-validation route — out
     of scope here (Slice 12P pin lives in test_slice12p tests).
  5. Venom loop is skipped only for wiring-validation route.
  6. CostGov route factor applies only under flag.
  7. Master flag default-FALSE preserves existing behavior.

# Architectural pins

  * ``route_predicates.VENOM_SKIP_ROUTES`` is exactly the closed set
    {background, speculative, wiring_validation}.
  * providers.py no longer has duplicate inline
    ``("background", "speculative")`` route-skip literals (all
    3 historical sites refactored to use the new helper).
  * providers.py changes limited to importing/calling the predicate
    helper — no credential/auth/base_url/provider-client logic
    touched.
  * Master flag accessor present + default-FALSE pinned.
  * Detector requires ``real_benchmark is False`` (exact-False),
    not ``== False`` (defense vs missing/None/string-false).
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.envelope_metadata import (
    EVIDENCE_KEY_FIXTURE_PURPOSE,
    EVIDENCE_KEY_GOLD_PATCH_EMPTY,
    EVIDENCE_KEY_REAL_BENCHMARK,
    EVIDENCE_KEY_SWE_BENCH_PRO,
    is_route_wiring_validation_envelope,
)
from backend.core.ouroboros.governance.route_predicates import (
    VENOM_SKIP_ROUTES,
    should_skip_venom_for_route,
)
from backend.core.ouroboros.governance.urgency_router import (
    WIRING_VALIDATION_ROUTE_ENABLED_ENV_VAR,
    ProviderRoute,
    UrgencyRouter,
    _wiring_validation_route_enabled,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset env flags this slice owns; each test sets what it needs."""
    for var in (
        WIRING_VALIDATION_ROUTE_ENABLED_ENV_VAR,
        "JARVIS_OP_COST_ROUTE_WIRING_VALIDATION",
        "JARVIS_BACKLOG_URGENCY_HINT_ENABLED",
        "JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _make_ctx(
    evidence: dict | None = None,
    *,
    signal_urgency: str = "normal",
    signal_source: str = "swe_bench_pro",
    task_complexity: str = "moderate",
) -> MagicMock:
    """Build a duck-typed OperationContext stub for UrgencyRouter."""
    ctx = MagicMock()
    ctx.intake_evidence_json = json.dumps(evidence) if evidence else ""
    ctx.signal_urgency = signal_urgency
    ctx.signal_source = signal_source
    ctx.task_complexity = task_complexity
    ctx.target_files = ()
    ctx.cross_repo = False
    ctx.is_read_only = False
    ctx.provider_route = ""
    ctx.provider_route_reason = ""
    ctx.op_id = "op-test-12ad"
    return ctx


def _wiring_evidence() -> dict:
    """Canonical wiring-validation fixture envelope (matches the
    real smoke fixture's envelope_builder output)."""
    return {
        EVIDENCE_KEY_SWE_BENCH_PRO: True,
        EVIDENCE_KEY_GOLD_PATCH_EMPTY: True,
        EVIDENCE_KEY_REAL_BENCHMARK: False,
        EVIDENCE_KEY_FIXTURE_PURPOSE: "wiring_validation",
    }


def _real_benchmark_evidence() -> dict:
    """Canonical real SWE-Bench-Pro benchmark envelope."""
    return {
        EVIDENCE_KEY_SWE_BENCH_PRO: True,
        EVIDENCE_KEY_GOLD_PATCH_EMPTY: False,
        EVIDENCE_KEY_REAL_BENCHMARK: True,
        EVIDENCE_KEY_FIXTURE_PURPOSE: "",
    }


# ──────────────────────────────────────────────────────────────────────
# Operator scenario 1: fixture metadata takes the route when flag enabled
# ──────────────────────────────────────────────────────────────────────


class TestFixtureTakesRouteWhenFlagEnabled:
    def test_detector_returns_true_for_canonical_fixture(self):
        ctx = _make_ctx(_wiring_evidence())
        assert is_route_wiring_validation_envelope(ctx) is True

    def test_classify_routes_to_wiring_validation_when_flag_on(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv(WIRING_VALIDATION_ROUTE_ENABLED_ENV_VAR, "true")
        ctx = _make_ctx(_wiring_evidence())
        route, reason = UrgencyRouter().classify(ctx)
        assert route is ProviderRoute.WIRING_VALIDATION
        assert "wiring_validation_envelope" in reason

    def test_classify_routes_to_wiring_validation_even_for_high_urgency(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Priority 0.6 is BEFORE Priority 1 (IMMEDIATE) — the
        wiring-validation route wins even when urgency would
        otherwise route IMMEDIATE. This is intentional: an envelope
        carrying purpose=wiring_validation IS a smoke test, not a
        real critical op."""
        monkeypatch.setenv(WIRING_VALIDATION_ROUTE_ENABLED_ENV_VAR, "true")
        ctx = _make_ctx(_wiring_evidence(), signal_urgency="critical")
        route, _reason = UrgencyRouter().classify(ctx)
        assert route is ProviderRoute.WIRING_VALIDATION


# ──────────────────────────────────────────────────────────────────────
# Operator scenario 2: real_benchmark MUST NEVER take this route
# ──────────────────────────────────────────────────────────────────────


class TestRealBenchmarkRejected:
    def test_detector_rejects_real_benchmark(self):
        ctx = _make_ctx(_real_benchmark_evidence())
        assert is_route_wiring_validation_envelope(ctx) is False

    def test_detector_rejects_real_benchmark_even_with_wiring_purpose(self):
        """Defense-in-depth: even if a real benchmark accidentally
        had purpose=wiring_validation set, real_benchmark=True must
        block the route."""
        ev = _real_benchmark_evidence()
        ev[EVIDENCE_KEY_FIXTURE_PURPOSE] = "wiring_validation"
        ev[EVIDENCE_KEY_REAL_BENCHMARK] = True
        ctx = _make_ctx(ev)
        assert is_route_wiring_validation_envelope(ctx) is False

    def test_classify_does_not_route_real_benchmark_to_wiring_validation(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv(WIRING_VALIDATION_ROUTE_ENABLED_ENV_VAR, "true")
        ctx = _make_ctx(_real_benchmark_evidence())
        route, _reason = UrgencyRouter().classify(ctx)
        assert route is not ProviderRoute.WIRING_VALIDATION

    def test_real_benchmark_is_exact_false_required(self):
        """``real_benchmark is False`` is exact-False — not falsy.
        None / 0 / "" / "false" all read as default-true (assume
        real benchmark) and the route is blocked."""
        for bad_value in (None, 0, "", "false", "False"):
            ev = _wiring_evidence()
            ev[EVIDENCE_KEY_REAL_BENCHMARK] = bad_value
            ctx = _make_ctx(ev)
            assert is_route_wiring_validation_envelope(ctx) is False, (
                f"real_benchmark={bad_value!r} should NOT qualify as "
                "explicit False — the detector should refuse the route"
            )


# ──────────────────────────────────────────────────────────────────────
# Operator scenario 3: missing/ambiguous metadata does not take route
# ──────────────────────────────────────────────────────────────────────


class TestMissingMetadataRejected:
    def test_empty_envelope_rejected(self):
        ctx = _make_ctx(None)
        assert is_route_wiring_validation_envelope(ctx) is False

    def test_envelope_without_purpose_rejected(self):
        ev = _wiring_evidence()
        del ev[EVIDENCE_KEY_FIXTURE_PURPOSE]
        ctx = _make_ctx(ev)
        assert is_route_wiring_validation_envelope(ctx) is False

    def test_envelope_with_wrong_purpose_rejected(self):
        for bad_purpose in ("real_benchmark", "graduation_soak", "", "WIRING_VALIDATION"):
            ev = _wiring_evidence()
            ev[EVIDENCE_KEY_FIXTURE_PURPOSE] = bad_purpose
            ctx = _make_ctx(ev)
            assert is_route_wiring_validation_envelope(ctx) is False, (
                f"purpose={bad_purpose!r} should NOT qualify "
                "(exact 'wiring_validation' required)"
            )

    def test_malformed_json_rejected(self):
        ctx = _make_ctx(None)
        ctx.intake_evidence_json = "{not valid json"
        assert is_route_wiring_validation_envelope(ctx) is False


# ──────────────────────────────────────────────────────────────────────
# Operator scenario 5: Venom loop skipped only for wiring-validation
# ──────────────────────────────────────────────────────────────────────


class TestVenomSkip:
    def test_venom_skip_routes_contains_canonical_three(self):
        """The frozenset is EXACTLY the closed-3 set the operator
        approved: legacy {background, speculative} + Slice 12AD's
        new {wiring_validation}."""
        assert VENOM_SKIP_ROUTES == frozenset({
            "background",
            "speculative",
            "wiring_validation",
        })

    @pytest.mark.parametrize("route_name", [
        "background", "speculative", "wiring_validation",
    ])
    def test_should_skip_venom_true_for_skip_routes(self, route_name: str):
        assert should_skip_venom_for_route(route_name) is True

    @pytest.mark.parametrize("route_name", [
        "immediate", "standard", "complex", "informational",
        # Empty / case-variant / unknown — must NOT skip Venom (defense
        # against typos in route stamping).
        "", "BACKGROUND", "Wiring_Validation", "unknown",
    ])
    def test_should_skip_venom_false_for_non_skip_routes(self, route_name: str):
        assert should_skip_venom_for_route(route_name) is False


# ──────────────────────────────────────────────────────────────────────
# Operator scenario 6: CostGov route factor applies only under flag
# ──────────────────────────────────────────────────────────────────────


class TestCostGovRouteFactor:
    def test_wiring_validation_factor_present_in_defaults(self):
        from backend.core.ouroboros.governance.cost_governor import (
            CostGovernorConfig,
        )
        cfg = CostGovernorConfig()
        assert "wiring_validation" in cfg.route_factors
        assert cfg.route_factors["wiring_validation"] == pytest.approx(0.1)

    def test_wiring_validation_factor_is_lowest_route(self):
        """0.1 must be strictly the lowest factor — otherwise the
        route isn't actually 'budget-aware'."""
        from backend.core.ouroboros.governance.cost_governor import (
            CostGovernorConfig,
        )
        cfg = CostGovernorConfig()
        wv = cfg.route_factors["wiring_validation"]
        for name, factor in cfg.route_factors.items():
            if name == "wiring_validation":
                continue
            assert wv <= factor, (
                f"wiring_validation factor ({wv}) must be ≤ "
                f"{name} factor ({factor})"
            )

    def test_env_override_honored(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("JARVIS_OP_COST_ROUTE_WIRING_VALIDATION", "0.05")
        from backend.core.ouroboros.governance.cost_governor import (
            CostGovernorConfig,
        )
        cfg = CostGovernorConfig()
        assert cfg.route_factors["wiring_validation"] == pytest.approx(0.05)

    def test_derived_cap_uses_wiring_validation_factor(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """End-to-end: a WIRING_VALIDATION op's per-op cap should be
        ~0.1× a COMPLEX op's cap (same baseline + complexity)."""
        from backend.core.ouroboros.governance.cost_governor import (
            CostGovernor, CostGovernorConfig,
        )
        cfg = CostGovernorConfig(enabled=True)
        cg = CostGovernor(cfg)
        wv_cap = cg.start("op-wv", route="wiring_validation", complexity="moderate")
        cx_cap = cg.start("op-cx", route="complex", complexity="moderate")
        # complex factor=4.0, wiring_validation factor=0.1 → ratio = 0.025
        # WV cap should be much lower than COMPLEX cap
        assert wv_cap < cx_cap
        assert wv_cap < cx_cap * 0.1


# ──────────────────────────────────────────────────────────────────────
# Operator scenario 7: master flag default-FALSE preserves behavior
# ──────────────────────────────────────────────────────────────────────


class TestMasterFlagDefaultFalse:
    def test_flag_accessor_default_false(self):
        assert _wiring_validation_route_enabled() is False

    @pytest.mark.parametrize("env_val", [
        # Negative cases — must read as disabled
        "", "false", "False", "0", "no", "off", "garbage",
    ])
    def test_flag_off_for_negative_env_values(
        self, env_val: str, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv(WIRING_VALIDATION_ROUTE_ENABLED_ENV_VAR, env_val)
        assert _wiring_validation_route_enabled() is False

    @pytest.mark.parametrize("env_val", [
        "true", "True", "TRUE", "1", "yes", "Yes", "on", "ON",
    ])
    def test_flag_on_for_positive_env_values(
        self, env_val: str, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv(WIRING_VALIDATION_ROUTE_ENABLED_ENV_VAR, env_val)
        assert _wiring_validation_route_enabled() is True

    def test_classify_does_not_route_when_flag_off(self):
        """The wedge: flag OFF (default) → fixture envelope still
        falls through to Priority 1-5 matrix → typically STANDARD."""
        ctx = _make_ctx(_wiring_evidence())
        route, _reason = UrgencyRouter().classify(ctx)
        assert route is not ProviderRoute.WIRING_VALIDATION


# ──────────────────────────────────────────────────────────────────────
# Architectural AST pins
# ──────────────────────────────────────────────────────────────────────


REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDERS_PATH = REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "providers.py"
ROUTE_PREDICATES_PATH = REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "route_predicates.py"


class TestArchitecturalPins:
    def test_providers_no_inlined_background_speculative_tuple(self):
        """The 3 historical sites (lean-prompt + 2× Venom-skip) MUST
        have been refactored — no remaining inlined
        ``("background", "speculative")`` tuple literal in providers.py."""
        src = PROVIDERS_PATH.read_text()
        # Comments / docstrings allowed (they reference the historical
        # pattern). Only ACTIVE Python source must not contain the
        # tuple-call expression form.
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # Look for `Compare` with `In` op and a Tuple right side
            # containing exactly the strings 'background' + 'speculative'.
            if not isinstance(node, ast.Compare):
                continue
            if not any(isinstance(op, ast.In) for op in node.ops):
                continue
            for comp in node.comparators:
                if not isinstance(comp, ast.Tuple):
                    continue
                values = []
                for elt in comp.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        values.append(elt.value)
                if set(values) == {"background", "speculative"}:
                    raise AssertionError(
                        "providers.py contains an inlined "
                        "('background', 'speculative') Compare-In tuple "
                        "literal — Slice 12AD requires the predicate be "
                        "centralised in route_predicates.VENOM_SKIP_ROUTES"
                    )

    def test_providers_imports_route_predicates_helper(self):
        """providers.py MUST import + call the canonical helper at
        each refactored site."""
        src = PROVIDERS_PATH.read_text()
        assert "should_skip_venom_for_route" in src, (
            "providers.py must use the canonical "
            "should_skip_venom_for_route() helper"
        )
        # Three refactored sites → at least three imports
        assert src.count("should_skip_venom_for_route") >= 3, (
            "Expected at least 3 references to should_skip_venom_for_route "
            "(lean-prompt + ClaudeProvider Venom-skip + Prime Venom-skip)"
        )

    def test_providers_no_credential_auth_logic_changed(self):
        """Sanity bound on the providers.py touch — the slice
        touches predicate sites only. No credential/auth/base_url
        identifier name in any added line should appear in
        Slice 12AD's deltas. (Coarse but effective: Slice 12AD's
        only providers.py change is route_predicates import + helper
        call swap.)"""
        src = PROVIDERS_PATH.read_text()
        # The helper name MUST appear (positive control); credential
        # / auth / base_url MUST appear ONLY in pre-existing code,
        # never on the same line as the new helper call.
        for line in src.splitlines():
            if "should_skip_venom_for_route" not in line:
                continue
            for forbidden in ("credential", "auth_scheme", "base_url",
                              "api_key", "bearer", "authorization"):
                assert forbidden.lower() not in line.lower(), (
                    f"Line {line!r} mixes Slice 12AD helper call "
                    f"with credential/auth term {forbidden!r}"
                )

    def test_route_predicates_module_has_canonical_exports(self):
        """The new module MUST export exactly the named seam."""
        src = ROUTE_PREDICATES_PATH.read_text()
        assert "VENOM_SKIP_ROUTES" in src
        assert "should_skip_venom_for_route" in src
        assert "frozenset" in src, (
            "VENOM_SKIP_ROUTES must be a frozenset (immutable; safe "
            "to share + use as default arg)"
        )

    def test_provider_route_enum_includes_wiring_validation(self):
        """The 7-value closed taxonomy includes WIRING_VALIDATION."""
        assert ProviderRoute.WIRING_VALIDATION.value == "wiring_validation"
        # Also: the enum-derived _VALID_ROUTE_VALUES set must now
        # accept "wiring_validation" as a pre-stamped/override value.
        from backend.core.ouroboros.governance.urgency_router import (
            _VALID_ROUTE_VALUES,
        )
        assert "wiring_validation" in _VALID_ROUTE_VALUES

    def test_master_flag_env_var_name_pinned(self):
        """The env var name is part of the operator contract; if it
        renames, FlagRegistry + docs + this pin all break together."""
        assert WIRING_VALIDATION_ROUTE_ENABLED_ENV_VAR == (
            "JARVIS_WIRING_VALIDATION_ROUTE_ENABLED"
        )
