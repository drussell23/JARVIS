"""Move 7 — Cross-op Semantic Budget Slice 4 REPL regression
spine (PRD §29.4, 2026-05-05).

Verifies:

  * `/semantic_budget` verb auto-discovers via §32.11 Slice 4
    registry (zero-edit registration via §33.3 naming-cage)
  * `dispatch_semantic_budget_command` shape: matched/ok/text
    + frozen result
  * Master-flag gate: help bypasses; subcommands return
    operator-friendly disabled message when off
  * Subcommand routing — status / recent / window / help /
    unknown
  * Recent rendering integrates Slice 2 reader correctly
  * Status rendering integrates Slices 1+2 end-to-end
  * Window rendering projects env knobs accurately
  * shlex parse-error defensive path
  * 2 AST pins auto-registered + green
  * Authority asymmetry — pure substrate
  * /help dispatcher auto-discovers register_verbs
  * Public API stability
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Help — bypasses master gate
# ---------------------------------------------------------------------------


def test_help_bypasses_master_gate(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        dispatch_semantic_budget_command,
    )
    r = dispatch_semantic_budget_command("/semantic_budget help")
    assert r.matched is True
    assert r.ok is True
    assert "Subcommands" in r.text
    assert "verdict ladder" in r.text.lower()


def test_help_short_form(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        dispatch_semantic_budget_command,
    )
    r = dispatch_semantic_budget_command("/semantic_budget ?")
    assert r.matched is True
    assert r.ok is True


# ---------------------------------------------------------------------------
# Master flag gate
# ---------------------------------------------------------------------------


def test_status_master_off_returns_friendly_error(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        dispatch_semantic_budget_command,
    )
    r = dispatch_semantic_budget_command(
        "/semantic_budget status",
    )
    assert r.matched is True
    assert r.ok is False
    assert "disabled" in r.text.lower()
    assert "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED" in r.text


def test_unmatched_line(monkeypatch):
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        dispatch_semantic_budget_command,
    )
    r = dispatch_semantic_budget_command("/decisions recent")
    assert r.matched is False


# ---------------------------------------------------------------------------
# Subcommand routing
# ---------------------------------------------------------------------------


def _seed_two_centroids(target: Path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_PATH", str(target),
    )
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid,
    )
    record_op_centroid(
        "op-a", centroid=(1.0, 0.0), ts_unix=1.0, path=target,
    )
    record_op_centroid(
        "op-b", centroid=(0.99, 0.14), ts_unix=2.0, path=target,
    )


def test_status_renders_verdict(monkeypatch, tmp_path):
    target = tmp_path / "centroids.jsonl"
    _seed_two_centroids(target, monkeypatch)
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        dispatch_semantic_budget_command,
    )
    r = dispatch_semantic_budget_command(
        "/semantic_budget status",
    )
    assert r.ok is True
    assert "verdict:" in r.text
    assert "within_budget" in r.text
    assert "integrated_drift:" in r.text
    assert "threshold:" in r.text
    assert "drift_pct_of_budget:" in r.text


def test_status_default_alias_no_subcmd(monkeypatch, tmp_path):
    """Bare `/semantic_budget` defaults to status."""
    target = tmp_path / "centroids.jsonl"
    _seed_two_centroids(target, monkeypatch)
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        dispatch_semantic_budget_command,
    )
    r = dispatch_semantic_budget_command("/semantic_budget")
    assert r.ok is True
    assert "verdict:" in r.text


def test_recent_renders_centroid_rows(
    monkeypatch, tmp_path,
):
    target = tmp_path / "centroids.jsonl"
    _seed_two_centroids(target, monkeypatch)
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        dispatch_semantic_budget_command,
    )
    r = dispatch_semantic_budget_command(
        "/semantic_budget recent 10",
    )
    assert r.ok is True
    assert "op-a" in r.text
    assert "op-b" in r.text
    assert "dim=" in r.text
    assert "hash=" in r.text


def test_recent_empty_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_PATH",
        str(tmp_path / "absent.jsonl"),
    )
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        dispatch_semantic_budget_command,
    )
    r = dispatch_semantic_budget_command(
        "/semantic_budget recent",
    )
    assert r.ok is True
    assert "no centroids" in r.text


def test_recent_clamps_limit(monkeypatch, tmp_path):
    target = tmp_path / "c.jsonl"
    _seed_two_centroids(target, monkeypatch)
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        dispatch_semantic_budget_command,
    )
    # garbage limit → falls back to default (10)
    r = dispatch_semantic_budget_command(
        "/semantic_budget recent garbage",
    )
    assert r.ok is True
    # extreme high → clamped (max 200)
    r = dispatch_semantic_budget_command(
        "/semantic_budget recent 99999",
    )
    assert r.ok is True


def test_window_renders_env_knobs(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_WINDOW_SIZE", "25",
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_THRESHOLD", "0.5",
    )
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        dispatch_semantic_budget_command,
    )
    r = dispatch_semantic_budget_command(
        "/semantic_budget window",
    )
    assert r.ok is True
    assert "window_size:" in r.text
    assert "25" in r.text
    assert "drift_threshold:" in r.text
    assert "0.5000" in r.text
    assert "approaching_ratio:" in r.text
    assert "ledger_path:" in r.text


def test_unknown_subcommand(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        dispatch_semantic_budget_command,
    )
    r = dispatch_semantic_budget_command(
        "/semantic_budget fnord",
    )
    assert r.matched is True
    assert r.ok is False
    assert "unknown subcommand" in r.text


def test_shlex_parse_error_defensive():
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        dispatch_semantic_budget_command,
    )
    # Unclosed quote → shlex.ValueError, must NOT raise.
    r = dispatch_semantic_budget_command(
        "/semantic_budget recent 'unclosed",
    )
    assert r.matched is True
    assert r.ok is False
    assert "parse error" in r.text


# ---------------------------------------------------------------------------
# Auto-discovery proof — §32.11 Slice 4 registry zero-edit
# ---------------------------------------------------------------------------


def test_verb_auto_discovered_via_slice4_registry(monkeypatch):
    """The §33.3 naming-cage convention guarantees the verb
    `semantic_budget` auto-registers via §32.11 Slice 4
    `repl_dispatch_registry`. Zero-edit inheritance —
    operators can immediately type `/semantic_budget help`."""
    monkeypatch.setenv(
        "JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        prime_registry,
        reset_registry_for_tests,
        try_dispatch,
    )
    reset_registry_for_tests()
    try:
        report = prime_registry(force=True)
        assert "semantic_budget" in report.verbs, (
            "Move 7 Slice 4 verb MUST auto-register via "
            "§32.11 Slice 4 registry — naming-cage zero-edit "
            "inheritance"
        )
    finally:
        reset_registry_for_tests()


def test_try_dispatch_routes_to_semantic_budget(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_PATH",
        str(tmp_path / "absent.jsonl"),
    )
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch, reset_registry_for_tests,
    )
    reset_registry_for_tests()
    try:
        out = try_dispatch("/semantic_budget help")
        assert out.matched is True
        assert out.ok is True
        assert out.verb == "semantic_budget"
    finally:
        reset_registry_for_tests()


# ---------------------------------------------------------------------------
# /help auto-discovery hook
# ---------------------------------------------------------------------------


def test_register_verbs_returns_one():
    """register_verbs must succeed without raising and return
    1 (one verb registered)."""
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        register_verbs,
    )

    class FakeRegistry:
        def __init__(self):
            self.entries = []

        def register(self, spec):
            self.entries.append(spec)

    registry = FakeRegistry()
    n = register_verbs(registry)
    assert n == 1
    assert len(registry.entries) == 1
    assert registry.entries[0].name == "/semantic_budget"


# ---------------------------------------------------------------------------
# Frozen result + public API
# ---------------------------------------------------------------------------


def test_result_is_frozen():
    from backend.core.ouroboros.governance.semantic_budget_repl import (  # noqa: E501
        SemanticBudgetReplDispatchResult,
    )
    r = SemanticBudgetReplDispatchResult(ok=True, text="x")
    with pytest.raises(Exception):
        r.ok = False  # type: ignore


def test_public_api_exports():
    from backend.core.ouroboros.governance import (
        semantic_budget_repl as r,
    )
    expected = (
        "dispatch_semantic_budget_command",
        "SemanticBudgetReplDispatchResult",
        "register_verbs",
        "register_shipped_invariants",
        "SEMANTIC_BUDGET_REPL_SCHEMA_VERSION",
    )
    for name in expected:
        assert hasattr(r, name), f"missing public symbol: {name}"


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


_EXPECTED_PIN_NAMES = {
    "semantic_budget_repl_authority_asymmetry",
    "semantic_budget_repl_composes_substrate",
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
        f"missing Slice 4 REPL pins: {missing}"
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
        "Slice 4 REPL pin violations: " + "; ".join(
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
        "semantic_budget_repl.py"
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
                        f"semantic_budget_repl.py MUST NOT "
                        f"import {module!r}"
                    )
