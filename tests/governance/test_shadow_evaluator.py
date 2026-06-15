from __future__ import annotations

from backend.core.ouroboros.governance.shadow_evaluator import (
    Alignment,
    evaluate_review,
)


def test_review_agree_allow():
    legacy = {"risk_tier": "SAFE_AUTO", "semantic_guard_hard": False}
    shadow = {"aggregate": "approve"}
    a = evaluate_review(legacy, shadow)
    assert isinstance(a, Alignment)
    assert a.aligned is True


def test_review_reservations_map_to_allow():
    legacy = {"risk_tier": "NOTIFY_APPLY", "semantic_guard_hard": False}
    shadow = {"aggregate": "approve_with_reservations"}
    assert evaluate_review(legacy, shadow).aligned is True


def test_review_disagree_shadow_blocks_legacy_allows():
    legacy = {"risk_tier": "SAFE_AUTO", "semantic_guard_hard": False}
    shadow = {"aggregate": "reject"}
    a = evaluate_review(legacy, shadow)
    assert a.aligned is False
    assert a.reason == "shadow=BLOCK legacy=ALLOW"


def test_review_agree_block_via_hard_finding():
    legacy = {"risk_tier": "SAFE_AUTO", "semantic_guard_hard": True}
    shadow = {"aggregate": "reject"}
    assert evaluate_review(legacy, shadow).aligned is True


def test_review_agree_block_via_approval_required():
    legacy = {"risk_tier": "APPROVAL_REQUIRED", "semantic_guard_hard": False}
    shadow = {"aggregate": "reject"}
    assert evaluate_review(legacy, shadow).aligned is True


def test_review_malformed_is_conservative_block():
    a = evaluate_review({}, {})
    assert a.aligned is False
    assert a.reason.startswith("malformed:")


from backend.core.ouroboros.governance.shadow_evaluator import evaluate_plan


# DAG shape: {"units": [{"id","owned_paths":[...],"deps":[...]}], }
def test_plan_valid_refinement_aligned():
    legacy = ["a.py", "b.py"]
    dag = {"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": []},
        {"id": "u2", "owned_paths": ["b.py"], "deps": ["u1"]},
    ]}
    assert evaluate_plan(legacy, dag).aligned is True


def test_plan_dropped_task_misaligned():
    legacy = ["a.py", "b.py", "c.py"]
    dag = {"units": [{"id": "u1", "owned_paths": ["a.py", "b.py"], "deps": []}]}
    a = evaluate_plan(legacy, dag)
    assert a.aligned is False
    assert a.reason.startswith("dropped_tasks:")
    assert "c.py" in a.reason


def test_plan_cyclical_misaligned():
    legacy = ["a.py", "b.py"]
    dag = {"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": ["u2"]},
        {"id": "u2", "owned_paths": ["b.py"], "deps": ["u1"]},
    ]}
    a = evaluate_plan(legacy, dag)
    assert a.aligned is False
    assert a.reason == "cyclical_dag"


def test_plan_owned_path_overlap_misaligned():
    legacy = ["a.py"]
    dag = {"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": []},
        {"id": "u2", "owned_paths": ["a.py"], "deps": []},
    ]}
    a = evaluate_plan(legacy, dag)
    assert a.aligned is False
    assert a.reason.startswith("owned_path_overlap:")


def test_plan_extra_structure_is_allowed():
    # DAG covers legacy AND adds a helper file -> still aligned (refinement).
    legacy = ["a.py"]
    dag = {"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": []},
        {"id": "u2", "owned_paths": ["helper.py"], "deps": ["u1"]},
    ]}
    assert evaluate_plan(legacy, dag).aligned is True


def test_plan_malformed_is_conservative_block():
    a = evaluate_plan(["a.py"], {"units": "not-a-list"})
    assert a.aligned is False
    assert a.reason.startswith("malformed:")


def test_plan_self_loop_is_cyclical():
    # A unit depending on itself is a cycle.
    legacy = ["a.py"]
    dag = {"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": ["u1"]},
    ]}
    a = evaluate_plan(legacy, dag)
    assert a.aligned is False
    assert a.reason == "cyclical_dag"


def test_plan_empty_dag_and_empty_legacy_is_aligned():
    # Zero-element boundary: nothing to cover, nothing to drop.
    assert evaluate_plan([], {"units": []}).aligned is True
