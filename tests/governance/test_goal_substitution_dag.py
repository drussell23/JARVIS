"""Substitution is a DAG, never a mutation.

When VALIDATE answers ``no_covering_test`` the tempting fix is to widen the
running op's scope so it can write the test too. That would forge its
signature: ``target_files`` is what the operator attested, and writing outside
it is exactly ``self_modification_unsanctioned_source`` — a refusal this
repository has live failures to prove.

So the op is SHED and two new goals are filed:

    A  writes tests/test_X.py
    B  carries the original work on X.py, signed depends_on=(A,)
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance.autonomy import goal_dag as DAG
from backend.core.ouroboros.governance.autonomy.goal_dag import (
    BLOCKED,
    DEPENDENCY_FAILED,
    READY,
    dependency_verdict,
    plan_substitution,
)

# NOT bound to a `test_`-prefixed module-level name. `goal_dag.test_path_for`
# starts with `test_`, so importing it -- or aliasing it under its own name --
# makes pytest COLLECT the production function as a test case and error on its
# required argument. The alias is deliberately renamed.
derive_test_path = DAG.test_path_for

SUBJECT = "backend/api/sse_contract.py"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(DAG.ENV_ENABLED, raising=False)
    monkeypatch.delenv(DAG.ENV_EXHAUSTION_FAILURES, raising=False)
    yield


# --------------------------------------------------------------------------
# The running goal is never mutated
# --------------------------------------------------------------------------

def test_substitution_only_ever_adds_goals():
    """Structural: nothing in this module writes to an existing goal.

    Checked against the AST, not the source text. A substring scan matched the
    module's own docstring — which explains that goals are never mutated — and
    failed the test for saying so. Prose is not code, and a test that cannot
    tell them apart is measuring the wrong thing.
    """
    import ast

    tree = ast.parse(inspect.getsource(DAG))

    # No attribute assignment: `goal.target_files = ...` mutates a goal.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                assert not isinstance(t, ast.Attribute), (
                    f"attribute assignment at line {node.lineno} — a goal may "
                    "not be mutated in place"
                )

    # No dataclasses.replace(...) — the in-place-edit idiom for frozen specs.
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "replace" not in called, "the DAG rewrites an existing spec"


def test_the_two_goals_have_disjoint_scopes():
    plan = plan_substitution(subject_file=SUBJECT)
    a, b = DAG._spec_a(plan), DAG._spec_b(plan)
    assert a.target_files == (plan.test_file,)
    assert b.target_files == (plan.subject_file,)
    assert set(a.target_files).isdisjoint(b.target_files)


def test_goal_A_authorises_only_the_test_file():
    """A must not be able to touch the module — that is B's scope, and the
    whole reason the pair exists rather than one widened goal."""
    plan = plan_substitution(subject_file=SUBJECT)
    a = DAG._spec_a(plan)
    assert plan.subject_file not in a.target_files
    assert "do NOT modify it" in a.description


# --------------------------------------------------------------------------
# The edge is SIGNED, not advisory
# --------------------------------------------------------------------------

def test_depends_on_reaches_the_signed_payload():
    """`roadmap_reader` has parsed this field all along, calling it
    "(advisory)". It is only meaningful once it is inside what the signature
    covers."""
    plan = plan_substitution(subject_file=SUBJECT)
    entry = DAG._spec_b(plan).to_entry()
    assert entry.get("depends_on") == [plan.goal_a_id]


def test_the_prerequisite_depends_on_nothing():
    plan = plan_substitution(subject_file=SUBJECT)
    assert "depends_on" not in DAG._spec_a(plan).to_entry()


def test_goalspec_omits_an_empty_edge():
    from backend.core.ouroboros.governance.operator_goal_sanction import GoalSpec

    entry = GoalSpec(
        goal_id="g", title="t", description="d", target_files=("x.py",),
    ).to_entry()
    assert "depends_on" not in entry, "an empty edge must not bloat the document"


def test_both_goals_go_through_the_ONE_signer():
    src = inspect.getsource(DAG.file_substitution)
    assert "author_and_sign_goal" in src
    assert src.count("author_and_sign_goal") >= 2


def test_the_prerequisite_is_filed_first():
    """If B were filed first and A failed, the graph would hold a dependent
    nothing can ever satisfy."""
    src = inspect.getsource(DAG.file_substitution)
    assert src.index("_spec_a") < src.index("_spec_b")
    assert "NOT filing the" in src


# --------------------------------------------------------------------------
# Naming is derived
# --------------------------------------------------------------------------

def test_the_test_path_is_derived_not_supplied():
    assert derive_test_path(SUBJECT) == "tests/test_sse_contract.py"
    sig = inspect.signature(plan_substitution)
    assert "test_file" not in sig.parameters, (
        "a caller-supplied test path is a caller-chosen SCOPE"
    )


def test_the_derived_path_matches_what_resolution_will_look_for():
    """If Goal A wrote its test somewhere Strategy 1 does not search, A could
    'succeed' and leave B still uncovered — a DAG that satisfies itself while
    the deficit remains."""
    assert derive_test_path("backend/x.py").startswith("tests/")
    assert derive_test_path("backend/x.py").endswith("test_x.py")


def test_goal_ids_are_stable_for_the_same_subject():
    a1 = plan_substitution(subject_file=SUBJECT)
    a2 = plan_substitution(subject_file=SUBJECT)
    assert (a1.goal_a_id, a1.goal_b_id) == (a2.goal_a_id, a2.goal_b_id)


def test_a_test_file_needs_no_test():
    assert plan_substitution(subject_file="tests/test_x.py") is None


@pytest.mark.parametrize("bad", [None, "", "x.txt", 123, object()])
def test_planning_never_raises(bad):
    assert plan_substitution(subject_file=bad) is None or True


def test_the_kill_switch_disables_substitution(monkeypatch):
    monkeypatch.setenv(DAG.ENV_ENABLED, "false")
    assert plan_substitution(subject_file=SUBJECT) is None


# --------------------------------------------------------------------------
# Phase 3 — the deadlock guarantee
# --------------------------------------------------------------------------

def test_B_is_never_ready_while_A_is_unsatisfied():
    """The guarantee, checked over the whole state space rather than asserted."""
    for satisfied in (frozenset(), frozenset({"other"})):
        for exhausted in (frozenset(), frozenset({"a"}), frozenset({"other"})):
            v = dependency_verdict(
                "b", ("a",), satisfied=satisfied, exhausted=exhausted,
            )
            assert v.state != READY, (satisfied, exhausted, v.render())


def test_a_landed_prerequisite_releases_the_dependent():
    v = dependency_verdict("b", ("a",), satisfied=frozenset({"a"}))
    assert v.state == READY and v.runnable


def test_an_exhausted_prerequisite_FAILS_the_dependent_rather_than_parking_it():
    """A dependent that can never run must be shed. Parking it forever is the
    leak a dependency graph introduces."""
    v = dependency_verdict("b", ("a",), satisfied=frozenset(), exhausted=frozenset({"a"}))
    assert v.state == DEPENDENCY_FAILED
    assert v.failed == ("a",)


def test_an_unmet_prerequisite_merely_blocks():
    v = dependency_verdict("b", ("a",), satisfied=frozenset())
    assert v.state == BLOCKED and v.unmet == ("a",)


def test_a_self_edge_fails_rather_than_blocking_forever():
    v = dependency_verdict("b", ("b",), satisfied=frozenset())
    assert v.state == DEPENDENCY_FAILED


def test_no_dependencies_is_always_ready():
    assert dependency_verdict("b", (), satisfied=frozenset()).state == READY


def test_exhaustion_reads_the_sentinels_own_cooldown_vocabulary():
    class _Entry:
        consecutive_failures = 9

    class _CD:
        def entry_for(self, target):
            return _Entry()

    got = DAG.exhausted_goal_ids(
        ["a"], roadmap_targets={"a": "tests/test_a.py"}, cooldown=_CD(),
    )
    assert got == frozenset({"a"})


def test_a_goal_absent_from_the_roadmap_is_unreachable():
    """Nothing will ever dispatch it, so its dependents must not wait."""
    got = DAG.exhausted_goal_ids(["ghost"], roadmap_targets={}, cooldown=None)
    assert got == frozenset({"ghost"})


def test_a_healthy_prerequisite_is_not_exhausted():
    class _Entry:
        consecutive_failures = 1

    class _CD:
        def entry_for(self, target):
            return _Entry()

    got = DAG.exhausted_goal_ids(
        ["a"], roadmap_targets={"a": "t.py"}, cooldown=_CD(),
    )
    assert got == frozenset()


def test_the_gate_fails_OPEN_not_closed():
    """A dependency check that fails closed wedges every goal the moment it
    breaks."""
    v = dependency_verdict("b", object(), satisfied=frozenset())
    assert v.state == READY


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

def test_validate_sheds_the_op_instead_of_widening_it():
    from backend.core.ouroboros.governance.phase_runners import validate_runner

    src = inspect.getsource(validate_runner.VALIDATERunner.run)
    assert 'validation.failure_class == "no_covering_test"' in src
    assert "test_coverage_deficit" in src
    assert "_substitute_for_coverage_deficit" in src


def test_the_deficit_is_recorded_as_a_lesson():
    from backend.core.ouroboros.governance.phase_runners import validate_runner

    src = inspect.getsource(validate_runner._substitute_for_coverage_deficit)
    assert "record_lesson" in src
    assert "TestCoverageDeficit" in src


def test_discovery_enforces_the_edge_at_SELECTION():
    """Enforcing at selection is what makes "B never runs before A" structural
    rather than something checked at execution time."""
    from backend.core.ouroboros.governance.autonomy import goal_discovery as GD

    src = inspect.getsource(GD.discover)
    assert "_dependency_state(" in src
    assert "_dep.runnable" in src


def test_the_edges_are_read_from_the_ROADMAP():
    """The roadmap is where the signature covers them; anywhere else is an
    unattested copy."""
    from backend.core.ouroboros.governance.autonomy import goal_discovery as GD

    src = inspect.getsource(GD._dag_index)
    assert "roadmap_reader" in src
    assert "depends_on" in src
