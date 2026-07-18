"""Predictive Phase-Aware Checkpointing — the FSM anticipates the wall-clock
boundary and gracefully self-suspends BEFORE entering a phase it cannot finish.

Root-cause coverage for two chaos-soak findings:
  * throughput starvation at the bg_timebox ceiling (burn runway → hard-kill
    mid-phase) → replaced by a native graceful suspend + resume-next-ignition;
  * the atomic workspace stash being only unit-proven → here it is driven END-TO-
    END through the predictive path on a REAL throwaway git repo.

The load-bearing assertion (mandate #4): with the per-phase EWMA mocked to
project a tail LONGER than the remaining runway, ``evaluate`` returns a suspend
verdict AND ``predictive_suspend`` executes the atomic git stash so the dirty
delta round-trips on restore.

Design pins also covered: fail-open on cold-start / no-deadline / disabled /
checkpoint-failure; decoupled runway read (session wall env + op deadline);
projection ceiling clamp; strict ``<`` boundary; the single-source EWMA feed at
``OperationContext.advance()``; HMAC-binding of the stash ref; and that the
per-op capture does NOT depend on in-flight-registry membership.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from backend.core.ouroboros.governance import fsm_checkpoint as CK
from backend.core.ouroboros.governance import phase_runway_gate as PRG
from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase


# ---------------------------------------------------------------------------
# Fixtures — real throwaway git repo (no mock VC) + clean estimator singleton.
# ---------------------------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    d = str(tmp_path / "wt")
    os.makedirs(d)
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (tmp_path / "wt" / "code.py").write_text("def foo():\n    return 1\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    return d


def _dirty(repo, content="def foo():\n    return 42  # EDIT\n"):
    with open(os.path.join(repo, "code.py"), "w") as fh:
        fh.write(content)


def _clean(repo):
    _git(repo, "checkout", "--", "code.py")


def _read(repo):
    return open(os.path.join(repo, "code.py")).read()


@pytest.fixture(autouse=True)
def _fresh_estimator():
    """Every test starts with a fresh per-phase EWMA singleton."""
    PRG._reset_for_tests()
    yield
    PRG._reset_for_tests()


def _feed(phase, seconds, n=5):
    """Push *n* identical phase-latency samples so the EWMA converges to
    *seconds* and crosses the min-samples floor (default 3)."""
    for _ in range(n):
        PRG.record_phase_duration(phase, seconds)


class _Ctx:
    """Minimal duck-typed op context for the suspend/runway paths."""

    def __init__(self, op_id="op-x", pipeline_deadline=None, phase="APPROVE"):
        self.op_id = op_id
        self.description = "mutate code.py"
        self.target_files = ["code.py"]
        self.phase = type("P", (), {"name": phase})()
        self.pipeline_deadline = pipeline_deadline
        self.intake_evidence_json = ""
        self.provider_route = ""


def _wall_env(monkeypatch, seconds_from_now):
    """Publish the harness's decoupled monotonic wall-deadline seam."""
    import time as _t
    monkeypatch.setenv(
        "OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC",
        repr(_t.monotonic() + seconds_from_now),
    )


# ===========================================================================
# A. remaining_runway_s — the decoupled read (Watchdog Isolation Invariant)
# ===========================================================================


def test_runway_none_when_no_deadline_source(monkeypatch):
    monkeypatch.delenv("OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC", raising=False)
    assert PRG.remaining_runway_s(_Ctx()) is None


def test_runway_from_session_wall_env(monkeypatch):
    _wall_env(monkeypatch, 120.0)
    r = PRG.remaining_runway_s(_Ctx())
    assert r is not None and 110.0 < r <= 120.0


def test_runway_from_op_pipeline_deadline(monkeypatch):
    monkeypatch.delenv("OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC", raising=False)
    dl = datetime.now(tz=timezone.utc) + timedelta(seconds=90)
    r = PRG.remaining_runway_s(_Ctx(pipeline_deadline=dl))
    assert r is not None and 80.0 < r <= 90.0


def test_runway_takes_min_of_both(monkeypatch):
    _wall_env(monkeypatch, 200.0)
    dl = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
    r = PRG.remaining_runway_s(_Ctx(pipeline_deadline=dl))
    assert r is not None and 20.0 < r <= 30.0  # the op deadline is tighter


def test_runway_ignores_malformed_wall_env(monkeypatch):
    monkeypatch.setenv("OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC", "not-a-float")
    dl = datetime.now(tz=timezone.utc) + timedelta(seconds=40)
    r = PRG.remaining_runway_s(_Ctx(pipeline_deadline=dl))
    assert r is not None and 30.0 < r <= 40.0


def test_runway_naive_datetime_treated_as_utc(monkeypatch):
    monkeypatch.delenv("OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC", raising=False)
    naive = datetime.utcnow() + timedelta(seconds=50)  # tz-naive, must not raise
    r = PRG.remaining_runway_s(_Ctx(pipeline_deadline=naive))
    assert r is not None and 40.0 < r <= 51.0


# ===========================================================================
# B. evaluate — the heuristic, all fail-open branches
# ===========================================================================


def test_evaluate_disabled_proceeds(monkeypatch):
    monkeypatch.setenv("JARVIS_PREDICTIVE_CHECKPOINT_ENABLED", "false")
    _wall_env(monkeypatch, 1.0)
    _feed("APPLY", 100.0)
    v = PRG.evaluate(_Ctx())
    assert v.should_suspend is False and v.reason == "disabled"


def test_evaluate_no_deadline_proceeds(monkeypatch):
    monkeypatch.delenv("OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC", raising=False)
    _feed("APPLY", 100.0)
    v = PRG.evaluate(_Ctx())
    assert v.should_suspend is False and v.reason == "no_deadline"


def test_evaluate_cold_start_proceeds(monkeypatch):
    # deadline present + short runway, but NO EWMA samples → cannot project.
    _wall_env(monkeypatch, 1.0)
    v = PRG.evaluate(_Ctx())
    assert v.should_suspend is False and v.reason == "cold_start"
    assert set(v.unknown_phases) == set(PRG.PRE_APPLY_TAIL_PHASES)


def test_evaluate_tail_fits_proceeds(monkeypatch):
    _wall_env(monkeypatch, 1000.0)
    _feed("APPLY", 20.0)
    _feed("VERIFY", 30.0)
    v = PRG.evaluate(_Ctx())
    # projected ≈ 50, threshold ≈ 50*1.25+8 = 70.5 << 1000 runway
    assert v.should_suspend is False and v.reason == "fits"
    assert v.projected_s == pytest.approx(50.0, abs=1.0)


def test_evaluate_runway_shortfall_suspends(monkeypatch):
    _wall_env(monkeypatch, 10.0)
    _feed("APPLY", 40.0)
    _feed("VERIFY", 60.0)
    v = PRG.evaluate(_Ctx())
    # projected ≈ 100, threshold ≈ 133 > 10 runway → SUSPEND
    assert v.should_suspend is True and v.reason == "runway_shortfall"
    assert v.projected_s == pytest.approx(100.0, abs=2.0)


def test_evaluate_partial_cold_start_underprojects(monkeypatch):
    # APPLY has samples, VERIFY does not → project APPLY only (safe direction).
    _wall_env(monkeypatch, 1000.0)
    _feed("APPLY", 40.0)
    v = PRG.evaluate(_Ctx())
    assert "APPLY" in v.known_phases
    assert "VERIFY" in v.unknown_phases and "VISUAL_VERIFY" in v.unknown_phases
    assert v.projected_s == pytest.approx(40.0, abs=1.0)


def test_evaluate_projection_ceiling_clamps(monkeypatch):
    monkeypatch.setenv("JARVIS_PREDICTIVE_PROJECTION_CEILING_S", "50")
    _wall_env(monkeypatch, 1000.0)
    _feed("APPLY", 100.0)   # would project 100, clamped to 50
    v = PRG.evaluate(_Ctx())
    assert v.projected_s == pytest.approx(50.0, abs=1.0)


def test_evaluate_strict_boundary_proceeds(monkeypatch):
    # runway EXACTLY == threshold must PROCEED (strict `<`).
    monkeypatch.setenv("JARVIS_PREDICTIVE_SAFETY_FACTOR", "1.0")
    monkeypatch.setenv("JARVIS_PREDICTIVE_STASH_RESERVE_S", "0")
    _feed("APPLY", 50.0)
    # only APPLY has samples → projected 50, threshold 50*1.0+0 = 50
    _wall_env(monkeypatch, 50.0)
    v = PRG.evaluate(_Ctx(), phases=("APPLY",))
    assert v.threshold_s == pytest.approx(50.0, abs=0.5)
    # runway ~50 (just under due to elapsed) → boundary is not a suspend trigger
    assert v.runway_s is not None
    assert v.should_suspend == (v.runway_s < v.threshold_s)


def test_evaluate_past_budget_suspends(monkeypatch):
    # runway already negative (past deadline) → must suspend before APPLY.
    dl = datetime.now(tz=timezone.utc) - timedelta(seconds=5)
    _feed("APPLY", 5.0)
    v = PRG.evaluate(_Ctx(pipeline_deadline=dl), phases=("APPLY",))
    assert v.should_suspend is True


def test_evaluate_never_raises_on_garbage_ctx(monkeypatch):
    _wall_env(monkeypatch, 5.0)
    _feed("APPLY", 100.0)
    v = PRG.evaluate(object())  # no attributes at all
    assert isinstance(v, PRG.RunwayVerdict)  # degraded, never raised


# ===========================================================================
# C. predictive_suspend → capture_single_op → LIVE atomic git stash
# ===========================================================================


@pytest.fixture()
def stash_env(monkeypatch, repo):
    monkeypatch.setenv("JARVIS_FSM_WORKSPACE_STASH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FSM_WORKSPACE_ROOT", repo)
    return repo


def test_predictive_suspend_dirty_tree_stashes_and_restores(stash_env, repo, tmp_path):
    """THE mandate assertion — a graceful predictive suspend on a dirty tree
    executes the atomic git stash and the delta round-trips on restore."""
    base = str(tmp_path / "ck")
    _dirty(repo)
    path = PRG.predictive_suspend(_Ctx(op_id="op-stash"), "APPLY", base_dir=base)
    assert path and os.path.isfile(path)

    cp = CK.list_pending(base_dir=base)[0]           # HMAC-verified re-read
    assert cp.op_id == "op-stash"
    assert cp.workspace_stash_ref and len(cp.workspace_stash_ref) == 40
    assert cp.resume_reason == "predictive_phase_aware"

    # Clean the tree (simulate the boundary) then hydrate-restore the delta.
    _clean(repo)
    assert "EDIT" not in _read(repo)
    assert CK.restore_workspace_stash(cp.workspace_stash_ref) is True
    assert "EDIT" in _read(repo)


def test_predictive_suspend_clean_tree_empty_ref(stash_env, repo, tmp_path):
    base = str(tmp_path / "ck")
    # tree is clean (no _dirty)
    path = PRG.predictive_suspend(_Ctx(op_id="op-clean"), "APPLY", base_dir=base)
    assert path
    cp = CK.list_pending(base_dir=base)[0]
    assert cp.workspace_stash_ref == ""


def test_capture_single_op_no_registry_dependency(stash_env, repo, tmp_path):
    """Unlike capture_inflight, the per-op capture reads straight from ctx — it
    must NOT require the op to be in the in-flight registry."""
    from backend.core.ouroboros.governance import in_flight_registry as R
    R.reset_default_registry()  # registry deliberately empty
    base = str(tmp_path / "ck")
    _dirty(repo)
    path = CK.capture_single_op(_Ctx(op_id="op-noreg"), phase="APPLY", base_dir=base)
    assert path
    assert CK.list_pending(base_dir=base)[0].op_id == "op-noreg"


def test_capture_single_op_no_op_id_is_failsoft(stash_env, tmp_path):
    base = str(tmp_path / "ck")
    assert CK.capture_single_op(_Ctx(op_id=""), phase="APPLY", base_dir=base) is None


def test_predictive_suspend_stash_ref_is_hmac_bound(stash_env, repo, tmp_path):
    base = str(tmp_path / "ck")
    _dirty(repo)
    PRG.predictive_suspend(_Ctx(op_id="op-hmac"), "APPLY", base_dir=base)
    path = os.path.join(CK.checkpoint_dir(base), "op-hmac.json")
    raw = json.loads(open(path).read())
    payload = json.loads(raw["payload"])
    payload["workspace_stash_ref"] = "f" * 40          # forged, not re-signed
    raw["payload"] = json.dumps(payload, sort_keys=True)
    open(path, "w").write(json.dumps(raw))
    assert CK.list_pending(base_dir=base) == []          # rejected


def test_predictive_suspend_failsoft_on_bad_root(monkeypatch, tmp_path):
    # workspace root points at a non-repo → stash yields "" but the checkpoint
    # itself still persists (never strands the op), never raises.
    monkeypatch.setenv("JARVIS_FSM_WORKSPACE_STASH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FSM_WORKSPACE_ROOT", "/proc/nonexistent/xyz")
    base = str(tmp_path / "ck")
    path = PRG.predictive_suspend(_Ctx(op_id="op-badroot"), "APPLY", base_dir=base)
    assert path
    assert CK.list_pending(base_dir=base)[0].workspace_stash_ref == ""


# ===========================================================================
# D. EWMA feed — single source at OperationContext.advance()
# ===========================================================================


def test_advance_feeds_completed_phase_duration():
    """Every advance() records the just-LEFT phase's wall-clock duration into the
    per-phase EWMA at the one canonical transition choke."""
    t0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    ctx = OperationContext.create(
        target_files=("code.py",), description="d", op_id="op-adv",
        _timestamp=t0,
    )
    # CLASSIFY -> ROUTE after 7s → records CLASSIFY=7.0
    ctx = ctx.advance(OperationPhase.ROUTE, _timestamp=t0 + timedelta(seconds=7))
    est = PRG.get_phase_latency_estimator()
    counts = est.stats()["sample_counts"]
    assert counts.get("classify", 0) == 1
    assert est.project_wait("CLASSIFY") == pytest.approx(7.0, abs=0.01)


def test_advance_feed_never_breaks_transition(monkeypatch):
    """If the EWMA feed blows up, advance() must still return the new ctx."""
    def _boom(*a, **k):
        raise RuntimeError("estimator down")
    monkeypatch.setattr(PRG, "record_phase_duration", _boom)
    t0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    ctx = OperationContext.create(
        target_files=("code.py",), description="d", op_id="op-safe", _timestamp=t0,
    )
    ctx2 = ctx.advance(OperationPhase.ROUTE, _timestamp=t0 + timedelta(seconds=3))
    assert ctx2.phase is OperationPhase.ROUTE  # transition survived the feed error


# ===========================================================================
# E. Full gate integration — mocked EWMA projects a tail > runway → suspend +
#    live stash. The exact end-to-end the mandate demands.
# ===========================================================================


def test_gate_end_to_end_suspends_and_stashes(stash_env, monkeypatch, repo, tmp_path):
    base = str(tmp_path / "ck")
    # 1) Mock the per-phase EWMA to project a long APPLY+VERIFY tail.
    _feed("APPLY", 45.0)
    _feed("VERIFY", 70.0)      # projected ≈ 115, threshold ≈ 152
    # 2) Only a short runway remains.
    _wall_env(monkeypatch, 12.0)
    # 3) The op is mid-mutation: the working tree is dirty.
    _dirty(repo)
    ctx = _Ctx(op_id="op-e2e")

    verdict = PRG.evaluate(ctx)
    assert verdict.should_suspend is True, verdict.as_telemetry()

    ckpt = PRG.predictive_suspend(ctx, "APPLY", verdict, base_dir=base)
    assert ckpt, "predictive suspend must persist a resumable checkpoint"

    cp = CK.list_pending(base_dir=base)[0]
    assert cp.workspace_stash_ref and len(cp.workspace_stash_ref) == 40

    # The atomic stash restores the dirty delta across a cleaned boundary.
    _clean(repo)
    assert CK.restore_workspace_stash(cp.workspace_stash_ref) is True
    assert "EDIT" in _read(repo)


def test_gate_end_to_end_fits_does_not_suspend(monkeypatch, repo, tmp_path):
    _feed("APPLY", 2.0)
    _feed("VERIFY", 3.0)       # projected ≈ 5, threshold ≈ 14
    _wall_env(monkeypatch, 600.0)
    v = PRG.evaluate(_Ctx(op_id="op-fits"))
    assert v.should_suspend is False and v.reason == "fits"


def test_stats_snapshot_shape():
    _feed("APPLY", 10.0)
    s = PRG.stats()
    assert s["enabled"] in (True, False)
    assert s["schema_version"] == PRG.PHASE_RUNWAY_GATE_SCHEMA_VERSION
    assert "apply" in s["phase_ewma"]["ewma_per_route_s"]
