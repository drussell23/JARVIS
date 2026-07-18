"""Autonomous Epistemic Planning — the gap→sub-roadmap→IncubationStore spine.

The mandate's core assertion: simulate an incomplete JARVIS module, and the
organism READS the PRD, identifies the unmet A-level components, and generates
corresponding SPECULATIVE-lane blueprint operations into the incubation
registry (or routes them when EV clears the bar) — through the REAL
ConceptionProposalBridge, no parallel task store.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import epistemic_planner as ep
from backend.core.ouroboros.governance.north_star_context import a_level_criteria


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


@pytest.fixture()
def frontier_repo(tmp_path):
    """An 'incomplete module' frontier: a git repo whose recent commits build
    backend/jpro/ but the module is unfinished."""
    d = tmp_path / "repo"
    (d / "backend" / "jpro").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "backend" / "jpro" / "core.py").write_text("def start():\n    pass  # TODO\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "feat(jpro): scaffold core module")
    (d / "backend" / "jpro" / "bridge.py").write_text("def link():\n    pass\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "feat(jpro): begin GCP bridge (incomplete)")
    return d


@pytest.fixture()
def prd(tmp_path, monkeypatch):
    p = tmp_path / "prd.md"
    p.write_text(
        "# PRD\n"
        "## 6. Target State (A-Level Execution from A-Level Vision)\n"
        "| Dimension | Criterion |\n"
        "|---|---|\n"
        "| Autonomous initiation | >= 3 self-formed goals per session |\n"
        "| Reliability | >= 90% session completion rate |\n"
        "## 7. Next\n"
    )
    monkeypatch.setenv("JARVIS_PRD_DOC", str(p))
    return p


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in ("JARVIS_EPISTEMIC_PLANNER_ENABLED", "JARVIS_EPISTEMIC_MAX_STEPS",
              "JARVIS_EPISTEMIC_MAX_FILES_PER_STEP", "JARVIS_PRD_DOC"):
        monkeypatch.delenv(v, raising=False)
    yield


@pytest.fixture()
def bridge_armed(monkeypatch):
    """route() early-returns [] unless the conception-bridge master is on
    (JARVIS_CONCEPTION_BRIDGE_ENABLED default FALSE) — arm it for the
    integration tests, exactly as the soak env does."""
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_ENABLED", "true")
    yield


def _momentum(subjects=("feat(jpro): begin GCP bridge (incomplete)",)):
    return SimpleNamespace(
        commit_count=2,
        top_scopes=lambda n=5: [("jpro", 2)],
        top_types=lambda n=4: [("feat", 2)],
        latest_subjects=tuple(subjects),
        is_empty=lambda: False,
    )


# ===========================================================================
# A. Structured PRD reading (a_level_criteria)
# ===========================================================================


def test_reads_prd_criteria_structured(prd, tmp_path):
    rows = a_level_criteria(str(tmp_path))
    assert ("Autonomous initiation", ">= 3 self-formed goals per session") in rows
    assert ("Reliability", ">= 90% session completion rate") in rows
    assert all(r[0] != "Dimension" for r in rows)      # header skipped


def test_missing_prd_yields_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PRD_DOC", str(tmp_path / "none.md"))
    assert a_level_criteria(str(tmp_path)) == ()


# ===========================================================================
# B. Pure synthesis — grounded, deterministic, bounded
# ===========================================================================


def test_synthesizes_grounded_steps(frontier_repo, prd, tmp_path):
    steps = ep.synthesize_gap_blueprints(
        momentum=_momentum(),
        recent_files=["backend/jpro/bridge.py", "backend/jpro/core.py", "ghost.py"],
        criteria=a_level_criteria(str(tmp_path)),
        repo_root=str(frontier_repo),
        tree_fingerprint="deadbeef",
    )
    assert steps, "an active frontier + unmet criteria must yield a plan"
    s = steps[0]
    # Reads the PRD: the unmet criterion is IN the step.
    assert "Autonomous initiation" in s.description
    # Grounded: only files that exist under the frontier module; fiction dropped.
    assert set(s.target_files) <= {"backend/jpro/bridge.py", "backend/jpro/core.py"}
    assert "ghost.py" not in s.target_files
    # Frontier continuation intent + lineage.
    assert "jpro" in s.title and "step=1/" in s.description
    # Valid bridge candidate (non-empty id + target_files → not silently dropped).
    assert s.blueprint_id.startswith("epistemic-") and s.target_files


def test_ids_are_deterministic_and_idempotent(frontier_repo, prd, tmp_path):
    kw = dict(
        momentum=_momentum(),
        recent_files=["backend/jpro/core.py"],
        criteria=a_level_criteria(str(tmp_path)),
        repo_root=str(frontier_repo),
        tree_fingerprint="deadbeef",
    )
    a1 = ep.synthesize_gap_blueprints(**kw)
    a2 = ep.synthesize_gap_blueprints(**kw)
    assert [b.blueprint_id for b in a1] == [b.blueprint_id for b in a2]


def test_silence_over_fabrication():
    # No momentum → () ; no criteria → () ; no grounded files → ().
    assert ep.synthesize_gap_blueprints(
        momentum=None, recent_files=["a.py"], criteria=[("D", "C")],
        repo_root=".", tree_fingerprint="x") == ()
    assert ep.synthesize_gap_blueprints(
        momentum=_momentum(), recent_files=["a.py"], criteria=[],
        repo_root=".", tree_fingerprint="x") == ()
    assert ep.synthesize_gap_blueprints(
        momentum=_momentum(), recent_files=["does/not/exist.py"],
        criteria=[("D", "C")], repo_root="/nonexistent", tree_fingerprint="x") == ()


def test_step_count_bounded(frontier_repo, prd, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_EPISTEMIC_MAX_STEPS", "1")
    steps = ep.synthesize_gap_blueprints(
        momentum=_momentum(),
        recent_files=["backend/jpro/core.py", "backend/jpro/bridge.py"],
        criteria=a_level_criteria(str(tmp_path)),
        repo_root=str(frontier_repo),
        tree_fingerprint="deadbeef",
    )
    assert len(steps) == 1


# ===========================================================================
# C. THE mandate spine — steps land in the REAL bridge's incubation registry
# ===========================================================================


class _CaptureRouter:
    def __init__(self):
        self.envelopes = []

    async def ingest(self, env):
        self.envelopes.append(env)
        return "enqueued"


@pytest.mark.asyncio
async def test_low_ev_steps_incubate_in_real_bridge(frontier_repo, prd, tmp_path, bridge_armed):
    """Below-threshold sub-roadmap steps must land in the IncubationStore as
    pending records (the organism's self-filled backlog)."""
    from backend.core.ouroboros.governance.conception_proposal_bridge import (
        ConceptionProposalBridge,
    )
    steps = ep.synthesize_gap_blueprints(
        momentum=_momentum(),
        recent_files=["backend/jpro/core.py", "backend/jpro/bridge.py"],
        criteria=a_level_criteria(str(tmp_path)),
        repo_root=str(frontier_repo),
        tree_fingerprint="cafebabe",
    )
    assert steps
    # Deterministic value scorer: EV below any batch threshold → incubate.
    low_ev = SimpleNamespace(ev=0.01, rationale="low", to_dict=lambda: {"ev": 0.01})
    bridge = ConceptionProposalBridge(value_scorer=lambda bp: low_ev)
    router = _CaptureRouter()
    await bridge.route(list(steps), router)
    incubating = set(bridge._incubating.keys())
    assert {s.blueprint_id for s in steps} <= incubating, (
        "sub-roadmap steps must persist in the incubation registry"
    )


@pytest.mark.asyncio
async def test_high_ev_steps_route_as_speculative_envelopes(frontier_repo, prd, tmp_path, bridge_armed):
    """High-EV steps route immediately as auto_proposed envelopes (the
    SPECULATIVE-lane source) instead of waiting in incubation."""
    from backend.core.ouroboros.governance.conception_proposal_bridge import (
        ConceptionProposalBridge,
    )
    steps = ep.synthesize_gap_blueprints(
        momentum=_momentum(),
        recent_files=["backend/jpro/core.py"],
        criteria=a_level_criteria(str(tmp_path)),
        repo_root=str(frontier_repo),
        tree_fingerprint="cafebabe",
    )
    high_ev = SimpleNamespace(ev=0.99, rationale="high", to_dict=lambda: {"ev": 0.99})
    bridge = ConceptionProposalBridge(value_scorer=lambda bp: high_ev)
    router = _CaptureRouter()
    await bridge.route(list(steps), router)
    assert router.envelopes, "high-EV steps must route into intake"
    env = router.envelopes[0]
    assert getattr(env, "source", "") == "auto_proposed"


@pytest.mark.asyncio
async def test_plan_and_route_disabled_is_inert(frontier_repo):
    """§33.1: default-FALSE master → the full cycle is a no-op."""
    n = await ep.plan_and_route_once(_CaptureRouter(), repo_root=str(frontier_repo))
    assert n == 0


@pytest.mark.asyncio
async def test_plan_and_route_full_cycle_real_repo(frontier_repo, prd, monkeypatch, bridge_armed):
    """End-to-end on a REAL git repo: enabled → git momentum + PRD read →
    steps submitted to the bridge (count > 0)."""
    monkeypatch.setenv("JARVIS_EPISTEMIC_PLANNER_ENABLED", "true")
    # Isolate the process-default bridge with a deterministic low-EV scorer so
    # the cycle provably lands records in incubation.
    from backend.core.ouroboros.governance import conception_proposal_bridge as cpb
    low_ev = SimpleNamespace(ev=0.01, rationale="low", to_dict=lambda: {"ev": 0.01})
    bridge = cpb.ConceptionProposalBridge(value_scorer=lambda bp: low_ev)
    monkeypatch.setattr(cpb, "get_default_bridge", lambda: bridge)
    router = _CaptureRouter()
    n = await ep.plan_and_route_once(router, repo_root=str(frontier_repo))
    assert n > 0
    assert bridge._incubating, "cycle must fill the backlog (incubation) autonomously"
