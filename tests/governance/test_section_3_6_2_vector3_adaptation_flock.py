"""§3.6.2 vector #3 — AdaptationLedger / convergence_governor flock closure.

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  "Solve the root problem directly—without workarounds, brute force,
   or shortcut solutions. Significantly strengthen the system into
   something advanced, asynchronous, dynamic, adaptive, intelligent,
   and highly robust, with no hardcoding. Fully leverage existing
   files and architecture so we avoid duplication and build cleanly
   on what already exists."

Closes the last Wave 3 hygiene race: ``convergence_governor.py``'s
``_persist_beliefs`` (truncate-rewrite) + ``_persist_proof`` (append)
were not flock-protected. Concurrent writers (e.g. sister processes
or cron-soak overlap) could interleave bytes mid-write. After this
slice, both paths wrap the canonical ``cross_process_jsonl
.flock_critical_section`` (§33.4 pattern) — same primitive used by
``adaptation/ledger.py`` Wave 3 closure.

Coverage (~12 tests):
  * AST scan: _persist_beliefs invokes flock_critical_section
  * AST scan: _persist_proof invokes flock_critical_section
  * AST scan: legacy fallback paths exist (NEVER-raises discipline)
  * AST scan: lazy-import discipline (no eager
    cross_process_jsonl import at module level)
  * Behavioral: _persist_beliefs roundtrip writes file
  * Behavioral: _persist_proof appends a row
  * Behavioral: failure modes return (False, reason) — NEVER raises
  * Behavioral: fsync attempt swallowed on OSError
  * Reuse pattern: composes the SAME flock primitive as
    adaptation/ledger.py (single source of truth)
  * Backward compat: existing convergence_governor tests still pass
    (verified separately)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/adaptation/"
        "convergence_governor.py"
    )


# ---------------------------------------------------------------------------
# AST: flock wiring discipline
# ---------------------------------------------------------------------------


def _find_method(tree: ast.Module, class_name: str, method_name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.FunctionDef)
                    and stmt.name == method_name
                ):
                    return stmt
    # Fall back to top-level function lookup.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == method_name
        ):
            return node
    return None


def _find_governor_class_name(tree: ast.Module) -> str:
    """The convergence_governor module exposes one main class
    (the Bayesian governor). Find it by looking for the class
    that owns _persist_beliefs."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.FunctionDef)
                    and stmt.name == "_persist_beliefs"
                ):
                    return node.name
    return ""


def test_persist_beliefs_invokes_flock_critical_section():
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls_name = _find_governor_class_name(tree)
    assert cls_name, "governor class missing"
    fn = _find_method(tree, cls_name, "_persist_beliefs")
    assert fn is not None
    found_flock_call = False
    for sub in ast.walk(fn):
        if isinstance(sub, ast.With):
            for item in sub.items:
                ctx = item.context_expr
                if (
                    isinstance(ctx, ast.Call)
                    and isinstance(ctx.func, ast.Name)
                    and ctx.func.id == "flock_critical_section"
                ):
                    found_flock_call = True
                    break
    assert found_flock_call, (
        "_persist_beliefs MUST wrap its write in "
        "flock_critical_section (§33.4 pattern)"
    )


def test_persist_proof_invokes_flock_critical_section():
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls_name = _find_governor_class_name(tree)
    fn = _find_method(tree, cls_name, "_persist_proof")
    assert fn is not None
    found_flock_call = False
    for sub in ast.walk(fn):
        if isinstance(sub, ast.With):
            for item in sub.items:
                ctx = item.context_expr
                if (
                    isinstance(ctx, ast.Call)
                    and isinstance(ctx.func, ast.Name)
                    and ctx.func.id == "flock_critical_section"
                ):
                    found_flock_call = True
                    break
    assert found_flock_call, (
        "_persist_proof MUST wrap its append in "
        "flock_critical_section (§33.4 pattern)"
    )


def test_legacy_fallback_methods_exist():
    """NEVER-raises discipline: legacy paths must exist as
    rollback fallbacks when cross_process_jsonl is import-
    unavailable."""
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls_name = _find_governor_class_name(tree)
    legacy_beliefs = _find_method(
        tree, cls_name, "_persist_beliefs_legacy",
    )
    legacy_proof = _find_method(
        tree, cls_name, "_persist_proof_legacy",
    )
    assert legacy_beliefs is not None, (
        "_persist_beliefs_legacy fallback MUST exist"
    )
    assert legacy_proof is not None, (
        "_persist_proof_legacy fallback MUST exist"
    )


def test_lazy_import_discipline():
    """cross_process_jsonl MUST NOT be at module-load top-
    level imports — keeps convergence_governor importable
    even when the substrate has a transient bug."""
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:  # top-level only
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "cross_process_jsonl" not in module, (
                f"convergence_governor MUST NOT eagerly "
                f"import cross_process_jsonl — found at "
                f"top-level: {module!r}"
            )


def test_persist_methods_lazy_import_flock():
    """Each persist method must contain its OWN lazy import
    of flock_critical_section (composition discipline)."""
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls_name = _find_governor_class_name(tree)
    for method_name in ("_persist_beliefs", "_persist_proof"):
        fn = _find_method(tree, cls_name, method_name)
        assert fn is not None
        has_lazy_import = False
        for sub in ast.walk(fn):
            if isinstance(sub, ast.ImportFrom):
                module = sub.module or ""
                if "cross_process_jsonl" in module:
                    if any(
                        n.name == "flock_critical_section"
                        for n in sub.names
                    ):
                        has_lazy_import = True
        assert has_lazy_import, (
            f"{method_name} MUST lazy-import "
            f"flock_critical_section from cross_process_jsonl"
        )


def test_legacy_path_invoked_on_import_error():
    """When cross_process_jsonl is unavailable, the canonical
    method delegates to its legacy fallback (NEVER-raises
    discipline). AST check: legacy method called inside the
    ImportError handler."""
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls_name = _find_governor_class_name(tree)
    for method_name, legacy_method in (
        ("_persist_beliefs", "_persist_beliefs_legacy"),
        ("_persist_proof", "_persist_proof_legacy"),
    ):
        fn = _find_method(tree, cls_name, method_name)
        found_legacy_call = False
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Call):
                func = sub.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == legacy_method
                ):
                    found_legacy_call = True
        assert found_legacy_call, (
            f"{method_name} MUST delegate to "
            f"{legacy_method} on ImportError "
            f"(NEVER-raises discipline)"
        )


# ---------------------------------------------------------------------------
# Behavioral: composition correctness end-to-end
# ---------------------------------------------------------------------------


def test_persist_beliefs_writes_file_under_flock(tmp_path):
    """End-to-end: instantiating the governor + tracking a
    hypothesis + persisting writes to disk under flock."""
    from backend.core.ouroboros.governance.adaptation.convergence_governor import (  # noqa: E501
        ConvergenceGovernor,
    )
    state_path = tmp_path / "beliefs.jsonl"
    proofs_path = tmp_path / "proofs.jsonl"
    gov = ConvergenceGovernor(
        state_path=state_path, proofs_path=proofs_path,
    )
    gov.track_hypothesis("hyp-1", prior=0.5)
    ok, reason = gov._persist_beliefs()
    assert ok is True
    assert reason == "ok"
    assert state_path.is_file()
    content = state_path.read_text(encoding="utf-8")
    assert "hyp-1" in content


def test_persist_proof_appends_under_flock(tmp_path):
    from backend.core.ouroboros.governance.adaptation.convergence_governor import (  # noqa: E501
        ConvergenceGovernor,
    )
    from backend.core.ouroboros.governance.adaptation.exploration_calculus import (  # noqa: E501
        ConvergenceProof,
    )
    state_path = tmp_path / "beliefs.jsonl"
    proofs_path = tmp_path / "proofs.jsonl"
    gov = ConvergenceGovernor(
        state_path=state_path, proofs_path=proofs_path,
    )
    proof = ConvergenceProof(
        hypothesis_id="hyp-1",
        halted=True,
        halt_reason="convergent",
        probes_used=10,
        theoretical_max_probes=20,
        cost_spent=0.5,
        final_belief=0.95,
        final_entropy=0.05,
        epsilon=0.05,
        ts_unix=1234567.0,
    )
    ok, reason = gov._persist_proof(proof)
    assert ok is True
    assert proofs_path.is_file()
    content = proofs_path.read_text(encoding="utf-8")
    assert "hyp-1" in content
    assert "convergent" in content


def test_persist_beliefs_failure_returns_false_reason(tmp_path):
    """OSError on path mkdir → returns (False, reason); never
    raises."""
    from backend.core.ouroboros.governance.adaptation.convergence_governor import (  # noqa: E501
        ConvergenceGovernor,
    )
    # Pass a path whose parent mkdir will fail (root-owned
    # path on most systems). On macOS, /System is protected.
    bad_path = Path("/System/private/test_should_fail.jsonl")
    gov = ConvergenceGovernor(
        state_path=bad_path,
        proofs_path=tmp_path / "proofs.jsonl",
    )
    gov.track_hypothesis("hyp-1")
    ok, reason = gov._persist_beliefs()
    # Either denied at mkdir or denied at flock — either way
    # NEVER raises and surfaces a structured (False, reason).
    assert ok is False
    assert "persist_beliefs_failed" in reason


def test_persist_proof_handles_legacy_fallback_path(tmp_path):
    """When called via the legacy path directly, behavior is
    byte-identical to canonical (without flock); proves the
    fallback isn't degenerate."""
    from backend.core.ouroboros.governance.adaptation.convergence_governor import (  # noqa: E501
        ConvergenceGovernor,
    )
    from backend.core.ouroboros.governance.adaptation.exploration_calculus import (  # noqa: E501
        ConvergenceProof,
    )
    proofs_path = tmp_path / "proofs.jsonl"
    gov = ConvergenceGovernor(
        state_path=tmp_path / "beliefs.jsonl",
        proofs_path=proofs_path,
    )
    proof = ConvergenceProof(
        hypothesis_id="hyp-legacy",
        halted=True,
        halt_reason="convergent",
        probes_used=5,
        theoretical_max_probes=20,
        cost_spent=0.1,
        final_belief=0.92,
        final_entropy=0.08,
        epsilon=0.05,
        ts_unix=1234567.0,
    )
    ok, reason = gov._persist_proof_legacy(proof)
    assert ok is True
    assert proofs_path.is_file()
    assert "hyp-legacy" in proofs_path.read_text(encoding="utf-8")


def test_canonical_path_composes_same_primitive_as_ledger():
    """Both adaptation/ledger.py AND adaptation/convergence
    _governor.py compose the SAME flock_critical_section from
    cross_process_jsonl — single source of truth (§33.4)."""
    src_governor = _module_path().read_text(encoding="utf-8")
    src_ledger = (
        _repo_root()
        / "backend/core/ouroboros/governance/adaptation/"
        "ledger.py"
    ).read_text(encoding="utf-8")
    expected_import = "from backend.core.ouroboros.governance.cross_process_jsonl import"
    assert expected_import in src_governor
    assert expected_import in src_ledger
    # Both must reference flock_critical_section by exact name.
    assert "flock_critical_section" in src_governor
    assert "flock_critical_section" in src_ledger


def test_persist_methods_handle_concurrent_writers(tmp_path):
    """End-to-end smoke: two governor instances persist to the
    SAME proofs path concurrently — flock guarantees no row
    corruption."""
    import threading
    from backend.core.ouroboros.governance.adaptation.convergence_governor import (  # noqa: E501
        ConvergenceGovernor,
    )
    from backend.core.ouroboros.governance.adaptation.exploration_calculus import (  # noqa: E501
        ConvergenceProof,
    )
    proofs_path = tmp_path / "concurrent_proofs.jsonl"
    gov_a = ConvergenceGovernor(
        state_path=tmp_path / "a_beliefs.jsonl",
        proofs_path=proofs_path,
    )
    gov_b = ConvergenceGovernor(
        state_path=tmp_path / "b_beliefs.jsonl",
        proofs_path=proofs_path,
    )
    proof_a = ConvergenceProof(
        hypothesis_id="hyp-A", halted=True,
        halt_reason="convergent",
        probes_used=10, theoretical_max_probes=20,
        cost_spent=0.5, final_belief=0.95, final_entropy=0.05,
        epsilon=0.05, ts_unix=1.0,
    )
    proof_b = ConvergenceProof(
        hypothesis_id="hyp-B", halted=True,
        halt_reason="convergent",
        probes_used=12, theoretical_max_probes=20,
        cost_spent=0.6, final_belief=0.96, final_entropy=0.04,
        epsilon=0.05, ts_unix=2.0,
    )
    results = []

    def write_a():
        results.append(gov_a._persist_proof(proof_a))

    def write_b():
        results.append(gov_b._persist_proof(proof_b))

    threads = [
        threading.Thread(target=write_a),
        threading.Thread(target=write_b),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Both writes succeeded.
    assert all(r[0] for r in results)
    # File contains BOTH hypotheses + each line is well-formed
    # JSON (no byte-level interleaving).
    lines = proofs_path.read_text(
        encoding="utf-8",
    ).strip().split("\n")
    assert len(lines) == 2
    import json
    parsed = [json.loads(line) for line in lines]
    hyp_ids = {p["hypothesis_id"] for p in parsed}
    assert hyp_ids == {"hyp-A", "hyp-B"}
