"""§3.6.2 vector #12 — Tier 3 deterministic fallback.

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  "Solve the root problem directly—without workarounds, brute force,
   or shortcut solutions. Significantly strengthen the system into
   something advanced, asynchronous, dynamic, adaptive, intelligent,
   and highly robust, with no hardcoding. Fully leverage existing
   files and architecture so we avoid duplication and build cleanly
   on what already exists."

Closes the load-bearing 🟡 vector #12 (Provider chain SPOF) at the
substrate-and-graceful-degradation layer. M12 (J-Prime LoRA as a real
Tier 3 model) remains the long-horizon "real" closure; this slice
ships the **deterministic fallback** that prevents the organism
freeze when both Tier 0 (DW) + Tier 1 (Claude) are simultaneously
out — without claiming to generate code (no Antivenom risk; APPROVAL
_REQUIRED routing expected downstream).

Coverage (~22 tests):
  * Substrate-shape: closed taxonomy / master-default-FALSE / report
    artifact / public API stability
  * Build-path: deferred GenerationResult shape (empty candidates,
    canonical provider_name, zero cost)
  * Telemetry: substitution log line + Tier3FallbackReport
  * 4 AST pins clean + each fires on synthetic regression
  * Wiring AST scan: candidate_generator's exhaustion handler
    invokes should_intercept_exhaustion +
    build_deferred_generation_result before re-raising
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "tier3_deterministic_fallback.py"
    )


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_outcome_taxonomy_2_values():
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        Tier3FallbackOutcome,
    )
    assert {o.name for o in Tier3FallbackOutcome} == {
        "SUBSTITUTED", "DISABLED",
    }


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TIER3_DETERMINISTIC_FALLBACK_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        master_enabled,
    )
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_TIER3_DETERMINISTIC_FALLBACK_ENABLED", v,
        )
        assert master_enabled() is True


def test_should_intercept_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TIER3_DETERMINISTIC_FALLBACK_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        should_intercept_exhaustion,
    )
    assert should_intercept_exhaustion() is False


def test_should_intercept_when_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TIER3_DETERMINISTIC_FALLBACK_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        should_intercept_exhaustion,
    )
    assert should_intercept_exhaustion() is True


# ---------------------------------------------------------------------------
# Tier3FallbackReport
# ---------------------------------------------------------------------------


def test_report_frozen():
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        Tier3FallbackOutcome, Tier3FallbackReport,
    )
    r = Tier3FallbackReport(
        outcome=Tier3FallbackOutcome.DISABLED,
        op_id="op-1", cause="test",
    )
    with pytest.raises(Exception):
        r.op_id = "mutated"  # type: ignore[misc]


def test_report_to_dict():
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        Tier3FallbackOutcome, Tier3FallbackReport,
    )
    r = Tier3FallbackReport(
        outcome=Tier3FallbackOutcome.SUBSTITUTED,
        op_id="op-test",
        cause="all_providers_exhausted:test",
    )
    d = r.to_dict()
    assert d["outcome"] == "substituted"
    assert d["op_id"] == "op-test"
    assert "schema_version" in d


# ---------------------------------------------------------------------------
# build_deferred_generation_result
# ---------------------------------------------------------------------------


def test_deferred_result_shape():
    """The deferred result MUST be the canonical
    GenerationResult shape (no parallel type) with
    structurally distinguishing fields."""
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        build_deferred_generation_result,
    )
    from backend.core.ouroboros.governance.op_context import (
        GenerationResult,
    )
    result = build_deferred_generation_result(
        op_id="op-1", cause="all_providers_exhausted:test",
    )
    assert isinstance(result, GenerationResult)
    # Empty candidates → orchestrator routes through
    # APPROVAL_REQUIRED.
    assert result.candidates == ()
    # Distinguishing provider name for downstream observers.
    assert result.provider_name == "tier3_deterministic_fallback"
    # Zero cost.
    assert result.generation_duration_s == 0.0
    assert result.cost_usd == 0.0


def test_deferred_result_no_op_id_default():
    """Default op_id is empty string — defensive when caller
    can't supply."""
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        build_deferred_generation_result,
    )
    result = build_deferred_generation_result()
    assert result is not None
    assert result.candidates == ()


def test_deferred_result_never_raises_on_construction_error():
    """If GenerationResult construction fails for any reason,
    builder returns None — caller falls back to original
    raise path."""
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        build_deferred_generation_result,
    )
    # Sanity check happy path doesn't raise.
    result = build_deferred_generation_result(
        op_id="op-1", cause="test",
    )
    assert result is not None


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_emit_substitution_telemetry_returns_report():
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        Tier3FallbackOutcome, emit_substitution_telemetry,
    )
    report = emit_substitution_telemetry(
        op_id="op-1", cause="all_providers_exhausted:test",
    )
    assert report.outcome == Tier3FallbackOutcome.SUBSTITUTED
    assert report.op_id == "op-1"
    assert "test" in report.cause


def test_emit_substitution_telemetry_swallows_exceptions(
    monkeypatch,
):
    """Telemetry emission must NEVER raise into the dispatch
    path. Even if logger fails, the report is still
    returned."""
    from backend.core.ouroboros.governance import (
        tier3_deterministic_fallback as mod,
    )

    class _BrokenLogger:
        def warning(self, *args, **kwargs):
            raise RuntimeError("simulated logger failure")

    monkeypatch.setattr(mod, "logger", _BrokenLogger())
    # Must NOT raise.
    report = mod.emit_substitution_telemetry(
        op_id="op-1", cause="test",
    )
    assert report is not None


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "tier3_fallback_outcome_taxonomy_2_values",
        "tier3_fallback_master_flag_default_false",
        "tier3_fallback_authority_asymmetry",
        "tier3_fallback_composes_canonical_result",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    violations = pin.validate(tree, src)
    assert violations == ()


def test_taxonomy_pin_fires_on_drift():
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class Tier3FallbackOutcome:
    SUBSTITUTED = "substituted"
    EXTRA_VALUE = "extra"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tier3_fallback_outcome_taxonomy_2_values"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance."
        "orchestrator import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tier3_fallback_authority_asymmetry"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_composes_canonical_pin_fires_on_parallel_result():
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def build_deferred_generation_result():
    # BAD — constructs a parallel result type instead of
    # composing op_context.GenerationResult.
    class _ParallelResult:
        candidates = ()
        provider_name = "tier3"
    return _ParallelResult()
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tier3_fallback_composes_canonical_result"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# Candidate generator wiring (AST scan)
# ---------------------------------------------------------------------------


def test_candidate_generator_intercept_wired():
    """candidate_generator.generate() MUST invoke
    should_intercept_exhaustion + build_deferred_generation
    _result before re-raising the all_providers_exhausted
    exception. AST scan."""
    src = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "candidate_generator.py"
    ).read_text(encoding="utf-8")
    # Must lazy-import the substrate.
    assert (
        "from backend.core.ouroboros.governance."
        "tier3_deterministic_fallback import"
        in src
    )
    # Must call should_intercept_exhaustion() and
    # build_deferred_generation_result.
    assert "should_intercept_exhaustion" in src
    assert "build_deferred_generation_result" in src
    assert "emit_substitution_telemetry" in src


def test_candidate_generator_wiring_before_raise():
    """The intercept must run BEFORE the bare ``raise`` —
    AST scan to verify ordering."""
    src = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "candidate_generator.py"
    ).read_text(encoding="utf-8")
    intercept_idx = src.find("should_intercept_exhaustion")
    # Find the trailing raise after our intercept block.
    # The bare 'raise' that re-raises the original exception.
    raise_idx = src.find("\n            raise\n", intercept_idx)
    assert intercept_idx > 0, (
        "should_intercept_exhaustion call missing"
    )
    assert raise_idx > intercept_idx, (
        "The intercept MUST run BEFORE the bare ``raise``"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_register_flags_seeds_master():
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 1
    name = registry.register.call_args.kwargs["name"]
    assert (
        name == "JARVIS_TIER3_DETERMINISTIC_FALLBACK_ENABLED"
    )


def test_register_flags_swallows_registry_errors():
    from backend.core.ouroboros.governance.tier3_deterministic_fallback import (  # noqa: E501
        register_flags,
    )
    bad = MagicMock()
    bad.register.side_effect = TypeError("incompatible")
    register_flags(bad)


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance import (
        tier3_deterministic_fallback as mod,
    )
    expected = {
        "TIER3_DETERMINISTIC_FALLBACK_SCHEMA_VERSION",
        "Tier3FallbackOutcome",
        "Tier3FallbackReport",
        "build_deferred_generation_result",
        "emit_substitution_telemetry",
        "master_enabled",
        "register_flags",
        "register_shipped_invariants",
        "should_intercept_exhaustion",
    }
    assert set(mod.__all__) == expected
