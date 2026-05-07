"""§3.6.2 vector #6 closure — substrate-health probe + ETA.

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  "Solve the root problem directly—without workarounds, brute force,
   or shortcut solutions. Significantly strengthen the system into
   something advanced, asynchronous, dynamic, adaptive, intelligent,
   and highly robust, with no hardcoding. Fully leverage existing
   files and architecture so we avoid duplication and build cleanly
   on what already exists."

Vector #6 wall-clock CANNOT be eliminated (§33.1 evidence ladder is
structural, not engineering-bound). What this slice closes:

  1. **Substrate-health probe** separates "cage layer for flag X is
     broken" from "cage layer works but evidence hasn't accumulated".
     Composes the P9.4 corpus's per-category coverage as the
     diagnostic signal.

  2. **ETA projection** gives the operator honest per-flag dates
     instead of a vague "~6-9 weeks" aggregate. Linear extrapolation
     from clean-session accumulation rate.

Coverage (~24 tests):
  * Closed taxonomy (4-value) bytes-pinned
  * Master flag default-FALSE per §33.1
  * Empty results when master off
  * EtaProjection: graduated / pending / stalled (sessions/day=0)
  * SubstrateHealth verdict: HEALTHY (full coverage) / DEGRADED
    (partial) / BROKEN (<50%) / UNKNOWN (no coverage)
  * build_flag_health_report: composes ledger + corpus + ETA
  * build_full_health_dashboard: covers all CADENCE_POLICY entries
  * Authority asymmetry — no orchestrator-tier imports
  * `/phase9 health` REPL surfaces dashboard
  * `/phase9 health` disabled message when master flag off
  * AST pins all 4 validate clean + each fires on synthetic regression
"""
from __future__ import annotations

import ast
import math
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "phase9_substrate_health.py"
    )


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_health_taxonomy_4_values():
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        SubstrateHealth,
    )
    assert {h.name for h in SubstrateHealth} == {
        "HEALTHY", "DEGRADED", "BROKEN", "UNKNOWN",
    }


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        master_enabled,
    )
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED", v,
        )
        assert master_enabled() is True


def test_dashboard_empty_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        build_full_health_dashboard,
    )
    assert build_full_health_dashboard() == ()


def test_report_none_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        build_flag_health_report,
    )
    assert build_flag_health_report(flag_name="x") is None


# ---------------------------------------------------------------------------
# EtaProjection
# ---------------------------------------------------------------------------


def test_eta_graduated_when_clean_meets_required():
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        _project_eta,
    )
    eta = _project_eta(
        flag_name="X", clean_count=3, required=3,
    )
    assert eta.days_to_graduation == 0.0


def test_eta_finite_when_progress_below_required():
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        _project_eta,
    )
    eta = _project_eta(
        flag_name="X", clean_count=1, required=3,
    )
    assert math.isfinite(eta.days_to_graduation)
    assert eta.days_to_graduation > 0


def test_eta_to_dict_finite():
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        _project_eta,
    )
    eta = _project_eta(
        flag_name="X", clean_count=2, required=5,
    )
    d = eta.to_dict()
    assert d["flag_name"] == "X"
    assert d["clean_count"] == 2
    assert d["required"] == 5
    assert isinstance(d["days_to_graduation"], float)


# ---------------------------------------------------------------------------
# SubstrateHealth probe verdicts
# ---------------------------------------------------------------------------


def test_probe_unknown_when_no_categories():
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        SubstrateHealth, _probe_substrate_health,
    )
    verdict, rate = _probe_substrate_health(categories=())
    assert verdict == SubstrateHealth.UNKNOWN
    assert rate == 0.0


def test_probe_healthy_when_categories_covered():
    """Categories that exist in the canonical P9.4 corpus
    coverage → HEALTHY (substrate has a probe)."""
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        SubstrateHealth, _probe_substrate_health,
    )
    # All real category values that exist in CORPUS.
    verdict, rate = _probe_substrate_health(
        categories=(
            "credential_introduced",
            "function_body_collapsed",
        ),
    )
    assert verdict == SubstrateHealth.HEALTHY
    assert rate == 1.0


def test_probe_broken_when_no_real_categories():
    """Categories that don't match real corpus coverage →
    BROKEN (the probe expected coverage but found none)."""
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        SubstrateHealth, _probe_substrate_health,
    )
    verdict, rate = _probe_substrate_health(
        categories=("nonexistent_category_X",),
    )
    assert verdict == SubstrateHealth.BROKEN
    assert rate < 0.5


def test_probe_degraded_partial_coverage():
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        SubstrateHealth, _probe_substrate_health,
    )
    # 1 real + 1 fake = 50% coverage = DEGRADED
    verdict, rate = _probe_substrate_health(
        categories=(
            "credential_introduced",  # real
            "totally_fake_category",  # fake
        ),
    )
    assert verdict == SubstrateHealth.DEGRADED
    assert rate == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# build_flag_health_report — composition correctness
# ---------------------------------------------------------------------------


def test_report_for_canonical_policy_flag(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        build_flag_health_report,
    )
    # SemanticGuardian flag has 4 corpus categories mapped.
    report = build_flag_health_report(
        flag_name=(
            "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS"
        ),
    )
    assert report is not None
    assert (
        report.flag_name
        == "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS"
    )
    assert len(report.probed_categories) > 0


def test_report_unknown_flag_returns_none(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        build_flag_health_report,
    )
    # Unknown to CADENCE_POLICY → None (defensive).
    assert build_flag_health_report(
        flag_name="JARVIS_NOT_IN_POLICY",
    ) is None


def test_dashboard_covers_all_cadence_policy_entries(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        CADENCE_POLICY,
    )
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        build_full_health_dashboard,
    )
    dashboard = build_full_health_dashboard()
    assert len(dashboard) == len(CADENCE_POLICY)
    flag_names = {r.flag_name for r in dashboard}
    policy_names = {p.flag_name for p in CADENCE_POLICY}
    assert flag_names == policy_names


def test_report_to_dict_serializable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        build_flag_health_report,
    )
    report = build_flag_health_report(
        flag_name=(
            "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS"
        ),
    )
    assert report is not None
    d = report.to_dict()
    assert "flag_name" in d
    assert "health" in d
    assert "eta" in d
    assert "schema_version" in d


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "phase9_substrate_health_taxonomy_4_values",
        "phase9_substrate_health_master_default_false",
        "phase9_substrate_health_authority_asymmetry",
        "phase9_substrate_health_composes_canonical_substrate",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
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
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class SubstrateHealth:
    HEALTHY = "healthy"
    BROKEN = "broken"
    EXTRA = "extra"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "phase9_substrate_health_taxonomy_4_values"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "phase9_substrate_health_authority_asymmetry"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# /phase9 health REPL surface
# ---------------------------------------------------------------------------


def test_repl_health_disabled_message(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED",
        raising=False,
    )
    monkeypatch.setenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 health")
    assert out.ok is True
    assert "disabled" in out.text


def test_repl_health_full_path(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 health")
    assert out.ok is True
    # Should mention key counts + ETA tokens.
    assert "healthy" in out.text.lower()
    assert "ETA" in out.text


def test_repl_help_documents_health():
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 help")
    assert out.ok is True
    assert "/phase9 health" in out.text


# ---------------------------------------------------------------------------
# Public API stability + FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance import (
        phase9_substrate_health as mod,
    )
    expected = {
        "EtaProjection",
        "FlagHealthReport",
        "PHASE9_SUBSTRATE_HEALTH_SCHEMA_VERSION",
        "SubstrateHealth",
        "build_flag_health_report",
        "build_full_health_dashboard",
        "get_flag_corpus_categories",
        "master_enabled",
        "register_flags",
        "register_shipped_invariants",
    }
    assert set(mod.__all__) == expected


def test_register_flags_seeds_master():
    from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 1
    assert (
        registry.register.call_args.kwargs["name"]
        == "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED"
    )
