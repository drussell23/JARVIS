"""A1-T3 — DAG-weight pre-flight tag regression spine.

At intake dispatch, a heavy multi-file strategic GOAL is tagged
``evidence["dag_weight"] = "heavy"`` so the orchestrator's Epistemic
prefetch + downstream observers see the intake-origin heaviness. The tag
is observability/explicitness only — it reuses ``is_heavy_goal`` and does
NOT duplicate the prefetch (which independently recomputes at GENERATE).

Tested surface: ``unified_intake_router.stamp_dag_weight`` (the pure,
fail-soft helper the dispatch path calls).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.intake import unified_intake_router as uir


@pytest.fixture(autouse=True)
def _prefetch_on(monkeypatch):
    # The tag is gated by the existing prefetch flag (default true). Pin it
    # explicitly so the test is independent of ambient env.
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    yield


def _env(target_files, evidence, *, blast_radius=0):
    return SimpleNamespace(
        target_files=tuple(target_files),
        evidence=evidence,
        causal_id="goal-x",
        blast_radius=blast_radius,
    )


def test_heavy_multifile_goal_is_tagged():
    ev: dict = {}
    env = _env(("a.py", "b.py", "c.py"), ev)
    assert uir.stamp_dag_weight(env) is True
    assert ev["dag_weight"] == "heavy"


def test_light_single_file_goal_not_tagged():
    ev: dict = {}
    env = _env(("only.py",), ev)
    assert uir.stamp_dag_weight(env) is False
    assert "dag_weight" not in ev


def test_high_blast_radius_single_file_is_tagged():
    # is_heavy_goal also trips on blast_radius > threshold (default 5).
    ev: dict = {}
    env = _env(("only.py",), ev, blast_radius=99)
    assert uir.stamp_dag_weight(env) is True
    assert ev["dag_weight"] == "heavy"


def test_disabled_prefetch_flag_no_op(monkeypatch):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "false")
    ev: dict = {}
    env = _env(("a.py", "b.py"), ev)
    assert uir.stamp_dag_weight(env) is False
    assert "dag_weight" not in ev


def test_non_dict_evidence_fail_soft():
    # evidence is None / not a dict -> no-op, never raises.
    env = _env(("a.py", "b.py"), None)
    assert uir.stamp_dag_weight(env) is False
    env2 = _env(("a.py", "b.py"), "not-a-dict")
    assert uir.stamp_dag_weight(env2) is False


def test_bad_envelope_fail_soft():
    # Missing attributes -> never raises, returns False.
    assert uir.stamp_dag_weight(object()) is False
    assert uir.stamp_dag_weight(None) is False


def test_existing_evidence_preserved():
    ev = {"vision_signal": {"frame_path": "/x.png"}}
    env = _env(("a.py", "b.py"), ev)
    assert uir.stamp_dag_weight(env) is True
    assert ev["dag_weight"] == "heavy"
    assert ev["vision_signal"] == {"frame_path": "/x.png"}  # untouched
