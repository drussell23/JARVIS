"""Phase 4 P3 — Cognitive metrics — graduation pin suite.

Mirrors P0 / P0.5 / P1 / P1.5 graduation pin patterns. Closes the
P3 un-stranding arc (un-strand → wire → graduate).

Sections:
    (A) Master flag — default true post-graduation; literal pinned
    (B) Hot-revert — explicit false → wrapper short-circuits + no
        ledger writes + accessor returns None
    (C) Authority invariants — banned-import grep across both new modules
        + orchestrator integration is via duck-typed best-effort wiring
        only (no NEW orchestrator import paths flagged)
    (D) Schema invariants — CognitiveMetricRecord frozen +
        schema_version pinned
    (E) Wiring source-grep pins — orchestrator __init__ wires the
        singleton when master flag on; CONTEXT_EXPANSION calls the
        pre-score helper after PostmortemRecall + before SemanticIndex
    (F) Bounded safety — score_pre_apply / reflect_post_apply ALWAYS
        return a result (never raise); ledger persistence is best-effort;
        oracle failure → neutral fallback
    (G) Backwards-compat — orchestrator constructor with no oracle on
        the stack → singleton stays uninitialised (graceful)
    (H) Repl read-only invariant unchanged
    (I) End-to-end integration — wired singleton + pre-score call →
        ledger row appears
    (J) Telemetry — INFO marker fires when wired + flag on
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.cognitive_metrics import (
    COGNITIVE_METRICS_SCHEMA_VERSION,
    CognitiveMetricRecord,
    CognitiveMetricsService,
    get_default_service,
    is_enabled,
    reset_default_service,
    set_default_service,
)
from backend.core.ouroboros.governance.cognitive_metrics_repl import (
    dispatch_cognitive_command as REPL,
)
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.orchestrator import (
    _score_cognitive_metrics_pre_apply_impl,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JARVIS_COGNITIVE_METRICS_ENABLED", raising=False)
    reset_default_service()
    yield
    reset_default_service()


@pytest.fixture
def stub_oracle():
    o = MagicMock()
    o.compute_blast_radius.return_value = MagicMock(
        risk_level="LOW", total_affected=5,
    )
    o.get_dependencies.return_value = []
    o.get_dependents.return_value = []
    return o


@pytest.fixture
def service(stub_oracle, tmp_path):
    return CognitiveMetricsService(
        oracle=stub_oracle, project_root=tmp_path,
    )


def _make_ctx(target_files=("a.py",)) -> OperationContext:
    return OperationContext.create(
        target_files=tuple(target_files),
        description="cognitive-metrics graduation pin",
    )


# ===========================================================================
# A — Master flag
# ===========================================================================


def test_master_flag_default_true_post_graduation(monkeypatch):
    monkeypatch.delenv("JARVIS_COGNITIVE_METRICS_ENABLED", raising=False)
    assert is_enabled() is True


def test_pin_master_env_reader_default_true_literal():
    src = _read("backend/core/ouroboros/governance/cognitive_metrics.py")
    assert (
        '"JARVIS_COGNITIVE_METRICS_ENABLED", "true"' in src
    ), (
        "Master flag default literal moved or changed. If P3 was rolled "
        "back, update both the source AND this pin (rename to "
        "test_pin_master_env_reader_default_false_literal)."
    )


# ===========================================================================
# B — Hot-revert
# ===========================================================================


def test_hot_revert_explicit_false_disables_writes(monkeypatch, service):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "false")
    service.score_pre_apply("op-1", ["a.py"], max_complexity=5)
    service.reflect_post_apply(
        "op-1", ["a.py"],
        coupling_after=0, blast_radius_after=0,
        complexity_after=0, complexity_before=0,
    )
    assert not service.ledger_path.exists()


def test_hot_revert_accessor_returns_none(monkeypatch, stub_oracle, tmp_path):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "false")
    assert get_default_service(oracle=stub_oracle, project_root=tmp_path) is None


# ===========================================================================
# C — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


def test_cognitive_metrics_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/cognitive_metrics.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_cognitive_metrics_repl_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/cognitive_metrics_repl.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


# ===========================================================================
# D — Schema
# ===========================================================================


def test_record_schema_version_frozen():
    assert COGNITIVE_METRICS_SCHEMA_VERSION == "cognitive_metrics.1"


def test_record_is_frozen_dataclass():
    r = CognitiveMetricRecord(
        schema_version=COGNITIVE_METRICS_SCHEMA_VERSION,
        op_id="x", kind="pre_score", target_files=("a.py",),
    )
    with pytest.raises(Exception):
        r.kind = "vindication"  # type: ignore[misc]


# ===========================================================================
# E — Wiring source-grep pins
# ===========================================================================


def test_pin_orchestrator_init_wires_singleton():
    src = _read("backend/core/ouroboros/governance/orchestrator.py")
    # Must import the wrapper + accessor at boot.
    assert (
        "from backend.core.ouroboros.governance.cognitive_metrics import" in src
    )
    assert "set_default_service as _set_default_cm" in src
    # Must consult the master flag before constructing.
    assert "if _cm_enabled():" in src
    # Must source the oracle from the stack (not constructing fresh).
    assert 'getattr(self._stack, "oracle", None)' in src or \
           "getattr(self._stack, \"oracle\", None)" in src


def test_pin_orchestrator_calls_pre_score_at_context_expansion():
    src = _read("backend/core/ouroboros/governance/orchestrator.py")
    assert (
        "_score_cognitive_metrics_pre_apply_impl(ctx)" in src
    ), "pre-score helper not invoked from CONTEXT_EXPANSION area"


def test_pin_pre_score_call_after_postmortem_recall():
    """Sequence pin: cognitive metrics pre-score follows the
    PostmortemRecall hook so the prompt-injection ordering is preserved
    (Bridge → PostmortemRecall → CognitiveMetrics → SemanticIndex)."""
    src = _read("backend/core/ouroboros/governance/orchestrator.py")
    pm_idx = src.rfind("ctx = _inject_postmortem_recall_impl(ctx)")
    cm_idx = src.rfind("_score_cognitive_metrics_pre_apply_impl(ctx)")
    assert pm_idx > 0 and cm_idx > 0
    assert pm_idx < cm_idx, (
        "CognitiveMetrics pre-score call must follow PostmortemRecall"
    )


# ===========================================================================
# F — Bounded safety
# ===========================================================================


def test_score_pre_apply_oracle_raises_returns_neutral(monkeypatch, tmp_path):
    """Even when the oracle blows up, score_pre_apply returns a result."""
    monkeypatch.delenv("JARVIS_COGNITIVE_METRICS_ENABLED", raising=False)
    boom = MagicMock()
    boom.compute_blast_radius.side_effect = RuntimeError("oracle down")
    boom.get_dependencies.side_effect = RuntimeError("oracle down")
    boom.get_dependents.side_effect = RuntimeError("oracle down")
    svc = CognitiveMetricsService(oracle=boom, project_root=tmp_path)
    r = svc.score_pre_apply("op-1", ["a.py"])
    assert r.gate == "NORMAL"
    assert r.pre_score == 0.5


def test_reflect_post_apply_oracle_raises_returns_neutral(
    monkeypatch, tmp_path,
):
    monkeypatch.delenv("JARVIS_COGNITIVE_METRICS_ENABLED", raising=False)
    boom = MagicMock()
    boom.get_dependencies.side_effect = RuntimeError("oracle down")
    svc = CognitiveMetricsService(oracle=boom, project_root=tmp_path)
    r = svc.reflect_post_apply(
        "op-1", ["a.py"],
        coupling_after=0, blast_radius_after=0,
        complexity_after=0, complexity_before=0,
    )
    assert r.advisory == "neutral"


def test_helper_swallows_when_singleton_uninitialised(monkeypatch):
    """Pin: orchestrator helper never raises when the singleton hasn't
    been wired (e.g. orchestrator boot didn't see an oracle)."""
    monkeypatch.delenv("JARVIS_COGNITIVE_METRICS_ENABLED", raising=False)
    reset_default_service()
    ctx = _make_ctx()
    # Should not raise, should not crash even though no oracle set.
    _score_cognitive_metrics_pre_apply_impl(ctx)


def test_helper_swallows_on_empty_target_files(monkeypatch, service):
    """Pin: empty target_files → helper short-circuits cleanly."""
    monkeypatch.delenv("JARVIS_COGNITIVE_METRICS_ENABLED", raising=False)
    set_default_service(service)
    ctx = _make_ctx(target_files=())
    _score_cognitive_metrics_pre_apply_impl(ctx)
    # No ledger row should have been written for an empty op.
    assert not service.ledger_path.exists()


# ===========================================================================
# G — Backwards-compat
# ===========================================================================


def test_helper_is_no_op_when_flag_off(monkeypatch, service):
    """Pin: helper is a no-op when master flag is off, even if the
    singleton is wired."""
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "false")
    set_default_service(service)
    ctx = _make_ctx()
    _score_cognitive_metrics_pre_apply_impl(ctx)
    assert not service.ledger_path.exists()


# ===========================================================================
# H — REPL read-only invariant unchanged
# ===========================================================================


def test_repl_still_does_not_call_score_methods_post_graduation():
    src = _read("backend/core/ouroboros/governance/cognitive_metrics_repl.py")
    forbidden = [
        "service.score_pre_apply(",
        "service.reflect_post_apply(",
        "resolved.score_pre_apply(",
        "resolved.reflect_post_apply(",
        "set_default_service(",
    ]
    for c in forbidden:
        assert c not in src


# ===========================================================================
# I — End-to-end integration
# ===========================================================================


def test_end_to_end_p3(monkeypatch, service, stub_oracle):
    """The whole P3 chain in one test:
      1. set_default_service(svc) (simulates orchestrator boot wiring)
      2. _score_cognitive_metrics_pre_apply_impl(ctx) at CONTEXT_EXPANSION
      3. Ledger row appears with kind=pre_score + correct op_id
      4. REPL `/cognitive list` surfaces the row
      5. stats() reflect the new row
    """
    monkeypatch.delenv("JARVIS_COGNITIVE_METRICS_ENABLED", raising=False)
    set_default_service(service)
    ctx = _make_ctx(target_files=("backend/x.py", "backend/y.py"))

    _score_cognitive_metrics_pre_apply_impl(ctx)

    rows = service.load_records()
    assert len(rows) == 1
    assert rows[0].kind == "pre_score"
    assert rows[0].op_id == ctx.op_id
    assert rows[0].pre_score is not None
    assert rows[0].pre_score_gate in ("FAST_TRACK", "NORMAL", "WARN")

    # REPL spot-check
    r = REPL("/cognitive list", service=service)
    assert r.ok is True
    assert ctx.op_id[:22] in r.text or ctx.op_id in r.text

    # stats()
    s = service.stats()
    assert s["total"] == 1
    assert s["pre_score_count"] == 1


# ===========================================================================
# J — Telemetry
# ===========================================================================


def test_telemetry_info_marker_fires_on_score(monkeypatch, service, caplog):
    monkeypatch.delenv("JARVIS_COGNITIVE_METRICS_ENABLED", raising=False)
    with caplog.at_level(logging.INFO):
        service.score_pre_apply("op-1", ["a.py"], max_complexity=5)
    msgs = [r.getMessage() for r in caplog.records]
    cm_msgs = [m for m in msgs if m.startswith("[CognitiveMetrics]")]
    assert cm_msgs, f"telemetry missing; got: {msgs}"
    assert any(
        "pre_score=" in m and "gate=" in m and "(Phase 4 P3)" in m
        for m in cm_msgs
    ), f"telemetry shape mismatch: {cm_msgs}"
