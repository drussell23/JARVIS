"""Tests for Wave 3 (6) Slice 2 — ``build_execution_graph`` primitive.

Scope: memory/project_wave3_item6_scope.md §9 Slice 2 — post-GENERATE
seam helper converting (candidate_files, FanoutEligibility) into an
ExecutionGraph that Slice 3+ can submit to SubagentScheduler without
recomputing eligibility.

Coverage matrix (operator ask, 2026-04-23):

- Empty / single / multi candidate inputs.
- Duplicate file_paths.
- Invalid edges (unknown key, unknown target).
- Cycle rejection (via ExecutionGraph._validate_unit_dag cascade).
- Self-dependency.
- Interaction with eligibility (allowed=False rejected; n_allowed<2 rejected).
- Determinism: same inputs → same graph_id + plan_digest + unit_ids.
- Authority-import ban reconfirmed on updated module.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
)
from backend.core.ouroboros.governance.parallel_dispatch import (
    CandidateFile,
    DEFAULT_UNIT_MAX_ATTEMPTS,
    DEFAULT_UNIT_TIMEOUT_S,
    FanoutEligibility,
    GRAPH_SCHEMA_VERSION,
    PLANNER_ID,
    ReasonCode,
    build_execution_graph,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _elig(n_allowed: int = 3, n_requested: int = 3, allowed: bool = True) -> FanoutEligibility:
    return FanoutEligibility(
        allowed=allowed,
        n_requested=n_requested,
        n_allowed=n_allowed,
        reason_code=ReasonCode.ALLOWED if allowed else ReasonCode.POSTURE_CLAMP,
    )


def _candidates(n: int = 3) -> list:
    return [
        CandidateFile(
            file_path=f"pkg/mod_{i}.py",
            full_content=f"# module {i}\npass\n",
            rationale=f"unit {i} rationale",
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# (1) CandidateFile dataclass validation
# ---------------------------------------------------------------------------


def test_candidate_file_requires_non_empty_path():
    with pytest.raises(ValueError, match="file_path must be non-empty"):
        CandidateFile(file_path="", full_content="x")


def test_candidate_file_requires_whitespace_path_rejected():
    with pytest.raises(ValueError, match="file_path must be non-empty"):
        CandidateFile(file_path="   ", full_content="x")


def test_candidate_file_rejects_none_content():
    with pytest.raises(ValueError, match="full_content may not be None"):
        CandidateFile(file_path="a.py", full_content=None)  # type: ignore[arg-type]


def test_candidate_file_accepts_empty_string_content():
    """Empty string is a valid candidate (file-clearing edit); None is not."""
    cf = CandidateFile(file_path="a.py", full_content="")
    assert cf.full_content == ""


def test_candidate_file_is_frozen():
    cf = CandidateFile(file_path="a.py", full_content="x")
    with pytest.raises((AttributeError, Exception)):
        cf.file_path = "b.py"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# (2) Happy path — multi-file graph construction
# ---------------------------------------------------------------------------


def test_build_multi_file_graph_happy_path():
    graph = build_execution_graph(
        op_id="op-test-001",
        repo="jarvis",
        candidate_files=_candidates(3),
        eligibility=_elig(n_allowed=3),
    )
    assert isinstance(graph, ExecutionGraph)
    assert graph.op_id == "op-test-001"
    assert graph.planner_id == PLANNER_ID
    assert graph.schema_version == GRAPH_SCHEMA_VERSION
    assert graph.concurrency_limit == 3
    assert len(graph.units) == 3
    # Each unit ships with its candidate file as target.
    target_files = [u.target_files[0] for u in graph.units]
    assert target_files == ["pkg/mod_0.py", "pkg/mod_1.py", "pkg/mod_2.py"]


def test_concurrency_limit_reflects_eligibility_n_allowed():
    # n_requested=5, n_allowed=2 → 2 concurrent units even though 5 candidate files.
    graph = build_execution_graph(
        op_id="op-test-002",
        repo="jarvis",
        candidate_files=_candidates(5),
        eligibility=_elig(n_allowed=2, n_requested=5),
    )
    assert graph.concurrency_limit == 2
    assert len(graph.units) == 5  # unit count = n_candidate_files


def test_unit_ids_are_deterministic():
    """Same op_id + file_path → same unit_id on every call."""
    g1 = build_execution_graph(
        op_id="op-test-003",
        repo="jarvis",
        candidate_files=_candidates(3),
        eligibility=_elig(),
    )
    g2 = build_execution_graph(
        op_id="op-test-003",
        repo="jarvis",
        candidate_files=_candidates(3),
        eligibility=_elig(),
    )
    assert [u.unit_id for u in g1.units] == [u.unit_id for u in g2.units]


def test_graph_id_and_plan_digest_are_deterministic():
    g1 = build_execution_graph(
        op_id="op-test-004",
        repo="jarvis",
        candidate_files=_candidates(3),
        eligibility=_elig(),
    )
    g2 = build_execution_graph(
        op_id="op-test-004",
        repo="jarvis",
        candidate_files=_candidates(3),
        eligibility=_elig(),
    )
    assert g1.graph_id == g2.graph_id
    assert g1.plan_digest == g2.plan_digest
    assert g1.causal_trace_id == g2.causal_trace_id


def test_different_op_ids_yield_different_unit_ids():
    g1 = build_execution_graph(
        op_id="op-test-005-a",
        repo="jarvis",
        candidate_files=_candidates(2),
        eligibility=_elig(n_allowed=2, n_requested=2),
    )
    g2 = build_execution_graph(
        op_id="op-test-005-b",
        repo="jarvis",
        candidate_files=_candidates(2),
        eligibility=_elig(n_allowed=2, n_requested=2),
    )
    assert [u.unit_id for u in g1.units] != [u.unit_id for u in g2.units]


def test_rationale_threads_into_unit_goal():
    cf = CandidateFile(
        file_path="pkg/important.py",
        full_content="x",
        rationale="reduce cyclomatic complexity",
    )
    graph = build_execution_graph(
        op_id="op-test-006",
        repo="jarvis",
        candidate_files=[cf, _candidates(1)[0]],
        eligibility=_elig(n_allowed=2, n_requested=2),
    )
    unit_for_cf = next(u for u in graph.units if u.target_files[0] == "pkg/important.py")
    assert unit_for_cf.goal == "reduce cyclomatic complexity"


def test_empty_rationale_gets_default_goal():
    cf = CandidateFile(file_path="pkg/a.py", full_content="x", rationale="")
    graph = build_execution_graph(
        op_id="op-test-007",
        repo="jarvis",
        candidate_files=[cf, _candidates(1)[0]],
        eligibility=_elig(n_allowed=2, n_requested=2),
    )
    unit_for_cf = next(u for u in graph.units if u.target_files[0] == "pkg/a.py")
    assert "pkg/a.py" in unit_for_cf.goal


def test_default_timeout_and_attempts():
    graph = build_execution_graph(
        op_id="op-test-008",
        repo="jarvis",
        candidate_files=_candidates(2),
        eligibility=_elig(n_allowed=2, n_requested=2),
    )
    for u in graph.units:
        assert u.timeout_s == DEFAULT_UNIT_TIMEOUT_S
        assert u.max_attempts == DEFAULT_UNIT_MAX_ATTEMPTS


def test_custom_timeout_and_attempts_threaded():
    graph = build_execution_graph(
        op_id="op-test-009",
        repo="jarvis",
        candidate_files=_candidates(2),
        eligibility=_elig(n_allowed=2, n_requested=2),
        per_unit_timeout_s=60.0,
        per_unit_max_attempts=3,
    )
    for u in graph.units:
        assert u.timeout_s == 60.0
        assert u.max_attempts == 3


# ---------------------------------------------------------------------------
# (3) Empty / single / invalid input rejection
# ---------------------------------------------------------------------------


def test_empty_op_id_rejected():
    with pytest.raises(ValueError, match="op_id must be non-empty"):
        build_execution_graph(
            op_id="",
            repo="jarvis",
            candidate_files=_candidates(2),
            eligibility=_elig(n_allowed=2, n_requested=2),
        )


def test_empty_repo_rejected():
    with pytest.raises(ValueError, match="repo must be non-empty"):
        build_execution_graph(
            op_id="op-test-010",
            repo="",
            candidate_files=_candidates(2),
            eligibility=_elig(n_allowed=2, n_requested=2),
        )


def test_empty_candidate_list_rejected():
    with pytest.raises(ValueError, match="candidate_files must be non-empty"):
        build_execution_graph(
            op_id="op-test-011",
            repo="jarvis",
            candidate_files=[],
            eligibility=_elig(n_allowed=2, n_requested=2),
        )


def test_single_file_rejected():
    with pytest.raises(ValueError, match="fan-out requires >=2 candidate files"):
        build_execution_graph(
            op_id="op-test-012",
            repo="jarvis",
            candidate_files=_candidates(1),
            eligibility=_elig(n_allowed=2, n_requested=2),
        )


def test_duplicate_file_paths_rejected():
    dupe = [
        CandidateFile(file_path="pkg/a.py", full_content="x"),
        CandidateFile(file_path="pkg/a.py", full_content="y"),
    ]
    with pytest.raises(ValueError, match="duplicate file_path"):
        build_execution_graph(
            op_id="op-test-013",
            repo="jarvis",
            candidate_files=dupe,
            eligibility=_elig(n_allowed=2, n_requested=2),
        )


def test_invalid_per_unit_timeout_rejected():
    with pytest.raises(ValueError, match="per_unit_timeout_s must be > 0"):
        build_execution_graph(
            op_id="op-test-014",
            repo="jarvis",
            candidate_files=_candidates(2),
            eligibility=_elig(n_allowed=2, n_requested=2),
            per_unit_timeout_s=0.0,
        )


def test_invalid_per_unit_max_attempts_rejected():
    with pytest.raises(ValueError, match="per_unit_max_attempts must be >= 1"):
        build_execution_graph(
            op_id="op-test-015",
            repo="jarvis",
            candidate_files=_candidates(2),
            eligibility=_elig(n_allowed=2, n_requested=2),
            per_unit_max_attempts=0,
        )


# ---------------------------------------------------------------------------
# (4) Eligibility interaction (§4 invariant — no graph without allowed=True)
# ---------------------------------------------------------------------------


def test_eligibility_none_rejected():
    with pytest.raises(ValueError, match="eligibility must not be None"):
        build_execution_graph(
            op_id="op-test-016",
            repo="jarvis",
            candidate_files=_candidates(2),
            eligibility=None,  # type: ignore[arg-type]
        )


def test_eligibility_not_allowed_rejected():
    """Callers must gate on is_fanout_eligible first; graph build does not
    silently treat denied eligibility as serial."""
    denied = FanoutEligibility(
        allowed=False,
        n_requested=3,
        n_allowed=1,
        reason_code=ReasonCode.POSTURE_CLAMP,
    )
    with pytest.raises(ValueError, match="eligibility.allowed=False"):
        build_execution_graph(
            op_id="op-test-017",
            repo="jarvis",
            candidate_files=_candidates(3),
            eligibility=denied,
        )


def test_eligibility_allowed_but_n_allowed_below_two_rejected():
    """Guard against malformed eligibility records (allowed=True + n_allowed<2)."""
    bad = FanoutEligibility(
        allowed=True,  # inconsistent — should be False for n_allowed<2
        n_requested=2,
        n_allowed=1,
        reason_code=ReasonCode.ALLOWED,
    )
    with pytest.raises(ValueError, match="eligibility.n_allowed must be >= 2"):
        build_execution_graph(
            op_id="op-test-018",
            repo="jarvis",
            candidate_files=_candidates(2),
            eligibility=bad,
        )


# ---------------------------------------------------------------------------
# (5) Dependency edges — validation + threading
# ---------------------------------------------------------------------------


def test_no_edges_means_fully_parallel_dag():
    graph = build_execution_graph(
        op_id="op-test-019",
        repo="jarvis",
        candidate_files=_candidates(3),
        eligibility=_elig(n_allowed=3, n_requested=3),
        dependency_edges=None,
    )
    for u in graph.units:
        assert u.dependency_ids == ()


def test_edges_thread_into_work_unit_specs():
    files = _candidates(3)
    edges = {
        "pkg/mod_1.py": ["pkg/mod_0.py"],
        "pkg/mod_2.py": ["pkg/mod_0.py", "pkg/mod_1.py"],
    }
    graph = build_execution_graph(
        op_id="op-test-020",
        repo="jarvis",
        candidate_files=files,
        eligibility=_elig(n_allowed=3, n_requested=3),
        dependency_edges=edges,
    )
    units_by_target = {u.target_files[0]: u for u in graph.units}
    u0 = units_by_target["pkg/mod_0.py"]
    u1 = units_by_target["pkg/mod_1.py"]
    u2 = units_by_target["pkg/mod_2.py"]
    assert u0.dependency_ids == ()
    assert u1.dependency_ids == (u0.unit_id,)
    assert u2.dependency_ids == (u0.unit_id, u1.unit_id)


def test_unknown_dependency_key_rejected():
    with pytest.raises(ValueError, match="unknown file_paths"):
        build_execution_graph(
            op_id="op-test-021",
            repo="jarvis",
            candidate_files=_candidates(2),
            eligibility=_elig(n_allowed=2, n_requested=2),
            dependency_edges={"pkg/does_not_exist.py": []},
        )


def test_unknown_dependency_target_rejected():
    with pytest.raises(ValueError, match="unknown file_path 'pkg/ghost.py'"):
        build_execution_graph(
            op_id="op-test-022",
            repo="jarvis",
            candidate_files=_candidates(2),
            eligibility=_elig(n_allowed=2, n_requested=2),
            dependency_edges={"pkg/mod_1.py": ["pkg/ghost.py"]},
        )


def test_self_dependency_rejected_with_clear_message():
    with pytest.raises(ValueError, match="self-dependency"):
        build_execution_graph(
            op_id="op-test-023",
            repo="jarvis",
            candidate_files=_candidates(2),
            eligibility=_elig(n_allowed=2, n_requested=2),
            dependency_edges={"pkg/mod_0.py": ["pkg/mod_0.py"]},
        )


def test_cycle_rejected_via_dag_validator():
    """a→b, b→a cycles caught by ExecutionGraph._validate_unit_dag."""
    with pytest.raises(ValueError, match="dependency cycle detected"):
        build_execution_graph(
            op_id="op-test-024",
            repo="jarvis",
            candidate_files=_candidates(2),
            eligibility=_elig(n_allowed=2, n_requested=2),
            dependency_edges={
                "pkg/mod_0.py": ["pkg/mod_1.py"],
                "pkg/mod_1.py": ["pkg/mod_0.py"],
            },
        )


def test_three_cycle_rejected():
    with pytest.raises(ValueError, match="dependency cycle detected"):
        build_execution_graph(
            op_id="op-test-025",
            repo="jarvis",
            candidate_files=_candidates(3),
            eligibility=_elig(n_allowed=3, n_requested=3),
            dependency_edges={
                "pkg/mod_0.py": ["pkg/mod_2.py"],
                "pkg/mod_1.py": ["pkg/mod_0.py"],
                "pkg/mod_2.py": ["pkg/mod_1.py"],
            },
        )


# ---------------------------------------------------------------------------
# (6) Authority-import ban re-confirm after Slice 2 additions
# ---------------------------------------------------------------------------


def test_parallel_dispatch_still_has_no_authority_imports():
    """§4 invariant #3 reconfirmed after Slice 2 additions. autonomy.subagent_types
    is NOT on the ban list (it exposes primitive types; no authority).
    """
    module_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "parallel_dispatch.py"
    )
    source = module_path.read_text()
    banned_patterns = [
        r"from\s+backend\.core\.ouroboros\.governance\.orchestrator\b",
        r"from\s+backend\.core\.ouroboros\.governance\.policy\b",
        r"from\s+backend\.core\.ouroboros\.governance\.iron_gate\b",
        r"from\s+backend\.core\.ouroboros\.governance\.risk_tier\b",
        r"from\s+backend\.core\.ouroboros\.governance\.change_engine\b",
        r"from\s+backend\.core\.ouroboros\.governance\.candidate_generator\b",
        r"from\s+backend\.core\.ouroboros\.governance\.gate\b",
        r"from\s+backend\.core\.ouroboros\.governance\.phase_runners\.gate_runner\b",
        r"import\s+backend\.core\.ouroboros\.governance\.orchestrator\b",
        r"import\s+backend\.core\.ouroboros\.governance\.policy\b",
        r"import\s+backend\.core\.ouroboros\.governance\.iron_gate\b",
        r"import\s+backend\.core\.ouroboros\.governance\.risk_tier\b",
        r"import\s+backend\.core\.ouroboros\.governance\.change_engine\b",
        r"import\s+backend\.core\.ouroboros\.governance\.candidate_generator\b",
        r"import\s+backend\.core\.ouroboros\.governance\.gate\b",
    ]
    for pattern in banned_patterns:
        matches = re.findall(pattern, source)
        assert not matches, (
            f"parallel_dispatch.py Slice 2 added banned import: "
            f"pattern {pattern!r} matched {matches!r}"
        )
