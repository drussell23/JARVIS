"""Move 7 — Cross-op Semantic Budget Slice 5 graduation contract
regression spine (PRD §29.4 / §33.1, 2026-05-05).

Verifies:

  * 5-value `SemanticBudgetGraduationVerdict` closed taxonomy
  * Master flag asymmetric env semantics (default-true)
  * Verdict ladder: DISABLED / INSUFFICIENT_OP_SAMPLES /
    PRODUCER_INACTIVE / EXCESSIVE_DRIFT_DETECTED /
    READY_FOR_GRADUATION
  * Env knobs clamping (required_samples / freshness / stable_n)
  * Frozen `WindowSnapshot` + `SemanticBudgetGraduationReport`
    schema + `to_dict()` projection
  * Defensive paths — Slice 1/2 unavailable, ledger read failure
    all map to defensive verdicts (NEVER raises)
  * Composes Slices 1+2 — no parallel math/ledger-read
  * 3 AST pins auto-registered + green
  * Authority asymmetry — pure substrate
  * Public API stability
  * §33.1 graduation contract pattern compliance
    (mirrors phase10_graduation_contract structure)
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Closed-taxonomy verdict
# ---------------------------------------------------------------------------


def test_verdict_taxonomy_is_closed_5_values():
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        SemanticBudgetGraduationVerdict,
    )
    expected = {
        "ready_for_graduation",
        "insufficient_op_samples",
        "producer_inactive",
        "excessive_drift_detected",
        "disabled",
    }
    assert (
        {v.value for v in SemanticBudgetGraduationVerdict}
        == expected
    )
    assert len(SemanticBudgetGraduationVerdict) == 5


# ---------------------------------------------------------------------------
# Master flag — default-TRUE per §33.1 (the contract is queryable;
# operator binding lives on Slice 1's master flag)
# ---------------------------------------------------------------------------


def test_master_flag_default_true(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CROSS_OP_SEMANTIC_GRADUATION_CONTRACT_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        graduation_contract_enabled,
    )
    assert graduation_contract_enabled() is True


@pytest.mark.parametrize("v", ["true", "1", "yes", "on", "TRUE"])
def test_master_flag_truthy(monkeypatch, v):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_GRADUATION_CONTRACT_ENABLED", v,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        graduation_contract_enabled,
    )
    assert graduation_contract_enabled() is True


@pytest.mark.parametrize("v", ["false", "0", "no", "off"])
def test_master_flag_falsy(monkeypatch, v):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_GRADUATION_CONTRACT_ENABLED", v,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        graduation_contract_enabled,
    )
    assert graduation_contract_enabled() is False


def test_disabled_verdict_when_master_off(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_GRADUATION_CONTRACT_ENABLED",
        "false",
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        SemanticBudgetGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        ledger_path=tmp_path / "absent.jsonl",
    )
    assert r.verdict == SemanticBudgetGraduationVerdict.DISABLED


def test_disabled_via_explicit_override(tmp_path):
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        SemanticBudgetGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        ledger_path=tmp_path / "absent.jsonl",
        enabled_override=False,
    )
    assert r.verdict == SemanticBudgetGraduationVerdict.DISABLED


# ---------------------------------------------------------------------------
# Env knobs — clamping
# ---------------------------------------------------------------------------


def test_required_samples_clamp(monkeypatch):
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        required_op_samples,
    )
    monkeypatch.delenv(
        "JARVIS_CROSS_OP_SEMANTIC_REQUIRED_OP_SAMPLES",
        raising=False,
    )
    assert required_op_samples() == 100
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_REQUIRED_OP_SAMPLES", "0",
    )
    assert required_op_samples() == 10
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_REQUIRED_OP_SAMPLES", "999999",
    )
    assert required_op_samples() == 100_000
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_REQUIRED_OP_SAMPLES", "junk",
    )
    assert required_op_samples() == 100


def test_freshness_clamp(monkeypatch):
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        producer_freshness_max_age_s,
    )
    monkeypatch.delenv(
        "JARVIS_CROSS_OP_SEMANTIC_PRODUCER_FRESHNESS_S",
        raising=False,
    )
    assert producer_freshness_max_age_s() == 86400.0
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_PRODUCER_FRESHNESS_S", "10",
    )
    assert producer_freshness_max_age_s() == 60.0
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_PRODUCER_FRESHNESS_S",
        str(60 * 86400.0),
    )
    assert producer_freshness_max_age_s() == 30.0 * 86400.0


def test_stable_windows_clamp(monkeypatch):
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        stable_windows_required,
    )
    monkeypatch.delenv(
        "JARVIS_CROSS_OP_SEMANTIC_STABLE_WINDOWS_REQUIRED",
        raising=False,
    )
    assert stable_windows_required() == 3
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_STABLE_WINDOWS_REQUIRED", "0",
    )
    assert stable_windows_required() == 1
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_STABLE_WINDOWS_REQUIRED", "999",
    )
    assert stable_windows_required() == 100


# ---------------------------------------------------------------------------
# Verdict ladder — full sequence
# ---------------------------------------------------------------------------


def _seed(target: Path, monkeypatch, n: int, ts_base: float):
    """Seed N centroids with mild drift, ts_base..ts_base+N."""
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid,
    )
    for i in range(n):
        # Tiny drift per step (close to identical centroids)
        record_op_centroid(
            f"op-{i}",
            centroid=(1.0 - i * 0.001, i * 0.001),
            ts_unix=ts_base + float(i),
            path=target,
        )


def test_insufficient_op_samples_empty_ledger(
    monkeypatch, tmp_path,
):
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        SemanticBudgetGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        ledger_path=tmp_path / "empty.jsonl",
        required_samples=10,
    )
    assert (
        r.verdict
        == SemanticBudgetGraduationVerdict.INSUFFICIENT_OP_SAMPLES
    )
    assert r.centroids_seen == 0


def test_insufficient_op_samples_below_threshold(
    monkeypatch, tmp_path,
):
    target = tmp_path / "c.jsonl"
    _seed(target, monkeypatch, n=5, ts_base=time.time())
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        SemanticBudgetGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        ledger_path=target, required_samples=10,
    )
    assert (
        r.verdict
        == SemanticBudgetGraduationVerdict.INSUFFICIENT_OP_SAMPLES
    )
    assert r.centroids_seen == 5


def test_producer_inactive_stale_ledger(
    monkeypatch, tmp_path,
):
    target = tmp_path / "c.jsonl"
    # Seed 10 centroids 7 days ago.
    week_ago = time.time() - 7 * 86400.0
    _seed(target, monkeypatch, n=10, ts_base=week_ago)
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        SemanticBudgetGraduationVerdict,
        is_ready_for_graduation,
    )
    # Default freshness 86400s (1d) → 7-day-old should be stale.
    r = is_ready_for_graduation(
        ledger_path=target,
        required_samples=5,
        freshness_max_age_s=86400.0,
        stable_windows_n=1,
    )
    assert (
        r.verdict
        == SemanticBudgetGraduationVerdict.PRODUCER_INACTIVE
    )
    assert r.newest_centroid_age_s > 86400.0


def test_excessive_drift_detected_orthogonal_jump(
    monkeypatch, tmp_path,
):
    """Seed mild drift then orthogonal jump → window should
    EXCEED → contract returns EXCESSIVE_DRIFT_DETECTED."""
    target = tmp_path / "c.jsonl"
    now = time.time()
    _seed(target, monkeypatch, n=5, ts_base=now)
    # Now append an orthogonal centroid → integrated drift jumps
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid,
    )
    record_op_centroid(
        "op-orthog",
        centroid=(0.0, 1.0),
        ts_unix=now + 6.0,
        path=target,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        SemanticBudgetGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        ledger_path=target,
        required_samples=5,
        stable_windows_n=1,
    )
    assert (
        r.verdict
        == SemanticBudgetGraduationVerdict.EXCESSIVE_DRIFT_DETECTED  # noqa: E501
    )
    assert len(r.recent_windows) >= 1


def test_ready_for_graduation_happy_path(
    monkeypatch, tmp_path,
):
    target = tmp_path / "c.jsonl"
    now = time.time()
    _seed(target, monkeypatch, n=10, ts_base=now)
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        SemanticBudgetGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        ledger_path=target,
        required_samples=5,
        stable_windows_n=1,
    )
    assert (
        r.verdict
        == SemanticBudgetGraduationVerdict.READY_FOR_GRADUATION
    )
    assert r.centroids_seen >= 5
    assert r.stable_windows_seen >= 1


# ---------------------------------------------------------------------------
# Frozen schema + projection
# ---------------------------------------------------------------------------


def test_window_snapshot_is_frozen():
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        WindowSnapshot,
    )
    w = WindowSnapshot(
        verdict="within_budget",
        integrated_drift=0.1,
        threshold=0.3,
        centroids_in_window=10,
    )
    with pytest.raises(Exception):
        w.verdict = "exceeded"  # type: ignore


def test_report_to_dict_projection(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        CROSS_OP_SEMANTIC_BUDGET_GRADUATION_CONTRACT_SCHEMA_VERSION,  # noqa: E501
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        ledger_path=tmp_path / "absent.jsonl",
        required_samples=10,
    )
    d = r.to_dict()
    assert (
        d["schema_version"]
        == CROSS_OP_SEMANTIC_BUDGET_GRADUATION_CONTRACT_SCHEMA_VERSION  # noqa: E501
    )
    assert d["verdict"] == "insufficient_op_samples"
    assert "elapsed_s" in d
    assert "recent_windows" in d


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------


def test_handles_empty_centroids_in_window(
    monkeypatch, tmp_path,
):
    """Ledger with valid rows but window logic gracefully
    handles edge cases."""
    target = tmp_path / "c.jsonl"
    now = time.time()
    _seed(target, monkeypatch, n=2, ts_base=now)
    from backend.core.ouroboros.governance.cross_op_semantic_budget_graduation_contract import (  # noqa: E501
        is_ready_for_graduation,
    )
    # required_samples ≤ 2 + small windows shouldn't crash
    r = is_ready_for_graduation(
        ledger_path=target,
        required_samples=2,
        stable_windows_n=10,  # ask for more windows than data
    )
    # Verdict shouldn't be DISABLED — the substrate IS available.
    assert r.verdict.value in (
        "ready_for_graduation",
        "excessive_drift_detected",
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


_EXPECTED_PIN_NAMES = {
    (
        "cross_op_semantic_budget_graduation_contract_"
        "authority_asymmetry"
    ),
    (
        "cross_op_semantic_budget_graduation_contract_"
        "composes_substrate"
    ),
    (
        "cross_op_semantic_budget_graduation_contract_"
        "verdict_taxonomy_5_values"
    ),
}


def test_pins_auto_registered():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        list_shipped_code_invariants,
    )
    registered = {
        inv.invariant_name
        for inv in list_shipped_code_invariants()
    }
    missing = _EXPECTED_PIN_NAMES - registered
    assert not missing, (
        f"missing Slice 5 pins: {missing}"
    )


def test_pins_pass_validation():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        validate_all,
    )
    violations = validate_all()
    relevant = [
        v for v in violations
        if v.invariant_name in _EXPECTED_PIN_NAMES
    ]
    assert not relevant, (
        "Slice 5 pin violations: " + "; ".join(
            f"{v.invariant_name}: {v.detail}"
            for v in relevant
        )
    )


# ---------------------------------------------------------------------------
# Authority asymmetry — file-level walk
# ---------------------------------------------------------------------------


def test_authority_asymmetry():
    import ast as _ast
    target = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/"
        "cross_op_semantic_budget_graduation_contract.py"
    )
    tree = _ast.parse(target.read_text(encoding="utf-8"))
    forbidden = (
        "orchestrator", "iron_gate", "policy", "providers",
        "candidate_generator", "urgency_router",
        "change_engine", "semantic_guardian",
    )
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            for f in forbidden:
                if f in module:
                    pytest.fail(
                        f"graduation contract MUST NOT import "
                        f"{module!r}"
                    )


# ---------------------------------------------------------------------------
# §33.1 pattern compliance — mirrors phase10 contract structure
# ---------------------------------------------------------------------------


def test_pattern_compliance_with_phase10():
    """The Move 7 graduation contract MUST mirror
    `phase10_graduation_contract`'s public surface (per §33.1
    canonical pattern). Both expose:
      * `is_ready_for_*()` predicate
      * Closed-enum `*Verdict` taxonomy
      * Frozen `*Report` with to_dict() projection
      * `*_enabled()` master flag helper (default-true)
      * `register_shipped_invariants()` auto-discovery hook"""
    from backend.core.ouroboros.governance import (
        cross_op_semantic_budget_graduation_contract as m7,
        phase10_graduation_contract as p10,
    )
    # Both expose the predicate
    assert hasattr(m7, "is_ready_for_graduation")
    assert hasattr(p10, "is_ready_for_purge")
    # Both expose closed-enum verdict
    assert hasattr(m7, "SemanticBudgetGraduationVerdict")
    assert hasattr(p10, "ContractVerdict")
    # Both expose master flag helper
    assert hasattr(m7, "graduation_contract_enabled")
    assert hasattr(p10, "graduation_contract_enabled")
    # Both auto-discover via register_shipped_invariants
    assert hasattr(m7, "register_shipped_invariants")
    assert hasattr(p10, "register_shipped_invariants")


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_exports():
    from backend.core.ouroboros.governance import (
        cross_op_semantic_budget_graduation_contract as m,
    )
    expected = (
        "SemanticBudgetGraduationVerdict",
        "SemanticBudgetGraduationReport",
        "WindowSnapshot",
        "is_ready_for_graduation",
        "graduation_contract_enabled",
        "required_op_samples",
        "producer_freshness_max_age_s",
        "stable_windows_required",
        "register_shipped_invariants",
        "CROSS_OP_SEMANTIC_BUDGET_GRADUATION_CONTRACT_SCHEMA_VERSION",  # noqa: E501
    )
    for name in expected:
        assert hasattr(m, name), (
            f"missing public symbol: {name}"
        )
