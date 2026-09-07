"""GoalReconciliationLedger — landed commits bound to cryptographic goals.

State is DERIVED from git reachability at every read: SATISFIED while the
bound commit is an ancestor of the landing ref, ACTIVE (with an audited
``reactivated`` row) the moment it is rolled back or amended away, and
rebuilt from ``Roadmap-Goal:`` trailers when the ledger has no row.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import goal_reconciliation_ledger as L
from backend.core.ouroboros.governance.roadmap_reader import GoalPriority, RoadmapGoal

SECRET = "test-roadmap-secret"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "README").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    for env in (L._ENV_ENABLED, L._ENV_LANDING_REF, L._ENV_SCAN_DEPTH, L._ENV_GIT_TIMEOUT):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(L._ENV_LEDGER_PATH, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(L._ENV_REPO_ROOT, str(r))
    monkeypatch.setenv("JARVIS_ROADMAP_READER_HMAC_SECRET", SECRET)
    monkeypatch.delenv("JARVIS_ROADMAP_READER_REQUIRE_SIGNATURE", raising=False)
    return r


def _goal(gid: str = "goal-a", files=("tests/test_a.py",)) -> RoadmapGoal:
    return RoadmapGoal(
        goal_id=gid, title="t", description="d", priority=GoalPriority.HIGH,
        target_files=tuple(files), success_criteria="s", depends_on=(),
        max_duration_s=60,
    )


def _land(repo: Path, goal: RoadmapGoal, path: str = "tests/test_a.py", *, trailers=True, digest=None) -> str:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(f"# {goal.goal_id}\n")
    _git(repo, "add", "-A")
    msg = "test: autonomous\n\nSignal: roadmap | Urgency: high\n"
    if trailers:
        msg += "\n".join(L.trailer_lines(goal.goal_id, digest if digest is not None else L.goal_digest(goal))) + "\n"
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def _run(coro):
    return asyncio.run(coro)


# -- identity ---------------------------------------------------------------

def test_goal_digest_is_deterministic_and_content_bound():
    a, b = _goal(), _goal()
    assert L.goal_digest(a) == L.goal_digest(b) and len(L.goal_digest(a)) == 64
    changed = RoadmapGoal(**{**a.__dict__, "description": "different intent"})
    assert L.goal_digest(changed) != L.goal_digest(a)
    assert L.goal_digest(object()) == ""


def test_binding_from_evidence_and_trailers():
    ev = json.dumps({"goal_id": "goal-a", "goal_digest": "ab" * 32})
    assert L.binding_from_evidence(ev) == ("goal-a", "ab" * 32)
    assert L.binding_from_evidence("not json") == ("", "")
    assert L.binding_from_evidence(json.dumps({"other": 1})) == ("", "")
    assert L.trailer_lines("goal-a", "ff" * 32) == (
        "Roadmap-Goal: goal-a", "Roadmap-Goal-Digest: " + "ff" * 32,
    )
    assert L.trailer_lines("", "x") == ()
    assert L.parse_trailers("Why: x\n\nRoadmap-Goal: g1\nRoadmap-Goal-Digest: d1\n") == {
        "Roadmap-Goal": "g1", "Roadmap-Goal-Digest": "d1",
    }


# -- ledger integrity -------------------------------------------------------

def test_record_landing_chains_and_macs(repo, tmp_path):
    goal = _goal()
    r1 = _run(L.record_landing(goal_id="goal-a", goal_digest_hex=L.goal_digest(goal), commit_sha="a" * 40, op_id="op1"))
    r2 = _run(L.record_landing(goal_id="goal-b", goal_digest_hex="", commit_sha="b" * 40, op_id="op2"))
    assert r1 and r2 and r2.prev_hash == r1.record_hash and r1.mac
    recs = L.read_records()
    assert [r.goal_id for r in recs] == ["goal-a", "goal-b"]


def test_tampered_row_is_ignored_with_its_tail(repo, tmp_path):
    _run(L.record_landing(goal_id="goal-a", goal_digest_hex="", commit_sha="a" * 40, op_id="op1"))
    _run(L.record_landing(goal_id="goal-b", goal_digest_hex="", commit_sha="b" * 40, op_id="op2"))
    p = L.ledger_path()
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    rows[0]["commit_sha"] = "f" * 40  # forge the landing
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert L.read_records() == ()


def test_wrong_secret_invalidates_mac(repo, monkeypatch):
    _run(L.record_landing(goal_id="goal-a", goal_digest_hex="", commit_sha="a" * 40, op_id="op1"))
    assert len(L.read_records()) == 1
    monkeypatch.setenv("JARVIS_ROADMAP_READER_HMAC_SECRET", "other")
    assert L.read_records() == ()


# -- derived state ----------------------------------------------------------

def test_landed_commit_satisfies_goal(repo):
    goal = _goal()
    sha = _land(repo, goal)
    _run(L.record_landing(goal_id=goal.goal_id, goal_digest_hex=L.goal_digest(goal), commit_sha=sha, op_id="op1"))
    rec = _run(L.reconcile_goal(goal))
    assert rec.state is L.GoalState.SATISFIED and rec.commit_sha == sha and rec.verified


def test_rollback_reactivates_and_audits(repo):
    goal = _goal()
    sha = _land(repo, goal)
    _run(L.record_landing(goal_id=goal.goal_id, goal_digest_hex=L.goal_digest(goal), commit_sha=sha, op_id="op1"))
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    rec = _run(L.reconcile_goal(goal))
    assert rec.state is L.GoalState.ACTIVE
    events = [r.event for r in L.read_records()]
    assert events == ["satisfied", "reactivated"]
    # stays active on a second read, with no duplicate audit rows
    assert _run(L.reconcile_goal(goal)).state is L.GoalState.ACTIVE
    assert [r.event for r in L.read_records()] == ["satisfied", "reactivated"]


def test_amend_that_keeps_the_work_rebinds_to_new_sha(repo):
    goal = _goal()
    sha = _land(repo, goal)
    _run(L.record_landing(goal_id=goal.goal_id, goal_digest_hex=L.goal_digest(goal), commit_sha=sha, op_id="op1"))
    # same-second --reset-author reproduces the identical object; amend the
    # MESSAGE (trailers kept) so the sha really changes
    _git(repo, "commit", "-q", "--amend", "-m",
         _git(repo, "log", "-1", "--format=%B") + "\nAmended: message only\n")
    new_sha = _git(repo, "rev-parse", "HEAD")
    assert new_sha != sha
    rec = _run(L.reconcile_goal(goal))
    assert rec.state is L.GoalState.SATISFIED and rec.commit_sha == new_sha
    assert [r.event for r in L.read_records()] == ["satisfied", "reactivated", "satisfied"]


def test_amend_that_drops_the_work_stays_active(repo):
    goal = _goal()
    sha = _land(repo, goal)
    _run(L.record_landing(goal_id=goal.goal_id, goal_digest_hex=L.goal_digest(goal), commit_sha=sha, op_id="op1"))
    # rewrite the landing so it no longer touches the goal's target file
    _git(repo, "rm", "-q", "tests/test_a.py")
    (repo / "other.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--amend", "--no-edit")
    rec = _run(L.reconcile_goal(goal))
    assert rec.state is L.GoalState.ACTIVE


def test_ledger_lost_is_rebuilt_from_trailers(repo):
    goal = _goal()
    sha = _land(repo, goal)
    assert not L.ledger_path().exists()
    rec = _run(L.reconcile_goal(goal))
    assert rec.state is L.GoalState.SATISFIED and rec.commit_sha == sha
    recs = L.read_records()
    assert len(recs) == 1 and recs[0].op_id == "rebuilt-from-git"


def test_changed_goal_text_is_a_new_goal(repo):
    goal = _goal()
    sha = _land(repo, goal)
    _run(L.record_landing(goal_id=goal.goal_id, goal_digest_hex=L.goal_digest(goal), commit_sha=sha, op_id="op1"))
    resigned = RoadmapGoal(**{**goal.__dict__, "description": "operator changed the intent"})
    assert _run(L.reconcile_goal(resigned)).state is L.GoalState.ACTIVE
    assert _run(L.reconcile_goal(goal)).state is L.GoalState.SATISFIED


def test_git_unavailable_degrades_to_active(repo, monkeypatch, tmp_path):
    goal = _goal()
    _run(L.record_landing(goal_id=goal.goal_id, goal_digest_hex=L.goal_digest(goal), commit_sha="a" * 40, op_id="op1"))
    monkeypatch.setenv(L._ENV_REPO_ROOT, str(tmp_path / "not-a-repo"))
    rec = _run(L.reconcile_goal(goal))
    assert rec.state is L.GoalState.ACTIVE and not rec.verified


def test_disabled_flag_is_inert(repo, monkeypatch):
    goal = _goal()
    _land(repo, goal)
    monkeypatch.setenv(L._ENV_ENABLED, "false")
    assert _run(L.record_landing(goal_id="goal-a", goal_digest_hex="", commit_sha="a" * 40, op_id="op")) is None
    assert _run(L.reconcile_goal(goal)).state is L.GoalState.ACTIVE
    assert not L.ledger_path().exists()


def test_reconcile_many_shares_one_ledger_read(repo):
    a, b = _goal("goal-a", ("tests/test_a.py",)), _goal("goal-b", ("tests/test_b.py",))
    sha = _land(repo, a)
    _run(L.record_landing(goal_id="goal-a", goal_digest_hex=L.goal_digest(a), commit_sha=sha, op_id="op1"))
    states = _run(L.reconcile([a, b]))
    assert states["goal-a"].state is L.GoalState.SATISFIED
    assert states["goal-b"].state is L.GoalState.ACTIVE


# -- seams ------------------------------------------------------------------

def test_roadmap_reader_suppresses_satisfied_goals(repo, monkeypatch):
    from backend.core.ouroboros.governance import roadmap_reader as rr
    goal = _goal()
    sha = _land(repo, goal)
    _run(L.record_landing(goal_id=goal.goal_id, goal_digest_hex=L.goal_digest(goal), commit_sha=sha, op_id="op1"))
    other = _goal("goal-b", ("tests/test_b.py",))
    doc = rr.RoadmapDocument(
        version=1, operator_id="op", signed_at_iso="", signature_hex="",
        signature_valid=True, goals=(goal, other), raw_bytes=0,
    )

    class _Router:
        seen: list = []

        async def ingest(self, env):
            self.seen.append(env)
            return "ikey"

    router = _Router()
    outcomes = _run(rr.emit_roadmap_envelopes(doc, router=router))
    by_id = {o.goal_id: o for o in outcomes}
    assert by_id["goal-a"].emitted is False and by_id["goal-a"].satisfied_by == sha and by_id["goal-a"].error == ""
    assert by_id["goal-b"].emitted is True
    assert [getattr(e, "evidence", {}).get("goal_id") for e in router.seen] == ["goal-b"]
    assert router.seen[0].evidence["goal_digest"] == L.goal_digest(other)


def test_auto_committer_message_carries_roadmap_trailers(tmp_path):
    from backend.core.ouroboros.governance.auto_committer import AutoCommitter
    ac = AutoCommitter(repo_root=tmp_path)
    msg = ac._build_commit_message(
        op_id="op-1", description="add tests", target_files=("tests/test_a.py",),
        signal_source="roadmap", signal_urgency="high",
        roadmap_goal_id="goal-a", roadmap_goal_digest="ab" * 32,
    )
    assert "Roadmap-Goal: goal-a" in msg and "Roadmap-Goal-Digest: " + "ab" * 32 in msg
    assert L.parse_trailers(msg)["Roadmap-Goal"] == "goal-a"
    plain = ac._build_commit_message(
        op_id="op-2", description="x", target_files=("a.py",), signal_source="test_failure",
    )
    assert "Roadmap-Goal" not in plain


def test_orchestrator_binding_kwargs():
    from backend.core.ouroboros.governance.orchestrator import _goal_binding_kwargs
    assert _goal_binding_kwargs(json.dumps({"goal_id": "g", "goal_digest": "d"})) == {
        "roadmap_goal_id": "g", "roadmap_goal_digest": "d",
    }
    assert _goal_binding_kwargs("") == {}


def test_in_flight_goal_is_deduplicated_at_ingest_until_terminal_or_ttl(repo, monkeypatch):
    goal = _goal()
    assert _run(L.record_dispatch(goal_id="goal-a", goal_digest_hex=L.goal_digest(goal), op_id="op-live"))
    rec = _run(L.reconcile_goal(goal))
    assert rec.state is L.GoalState.ACTIVE and rec.in_flight_op == "op-live"
    # the router's admission check: a different op for the same goal is superseded
    assert L.superseding_op("goal-a", "op-new") == "op-live"
    assert L.superseding_op("goal-a", "op-live") == ""
    # the READER keeps emitting (the composer discovers goals from envelopes)
    from backend.core.ouroboros.governance import roadmap_reader as rr
    doc = rr.RoadmapDocument(version=1, operator_id="op", signed_at_iso="", signature_hex="", signature_valid=True, goals=(goal,), raw_bytes=0)

    class _Router:
        seen: list = []

        async def ingest(self, env):
            self.seen.append(env); return "enqueued"

    r = _Router()
    out = _run(rr.emit_roadmap_envelopes(doc, router=r))
    assert out[0].emitted is True and len(r.seen) == 1
    # terminal releases the goal
    assert _run(L.record_terminal(goal_id="goal-a", op_id="op-live", outcome="failed"))
    assert _run(L.reconcile_goal(goal)).in_flight_op == "" and L.superseding_op("goal-a", "op-new") == ""
    # TTL lapse releases a crashed op
    assert _run(L.record_dispatch(goal_id="goal-a", goal_digest_hex="", op_id="op-crashed"))
    monkeypatch.setenv(L._ENV_INFLIGHT_TTL, "0.001")
    import time as _t; _t.sleep(0.01)
    assert _run(L.reconcile_goal(goal)).in_flight_op == ""


def test_router_ingest_wires_inflight_dedupe_and_dispatch():
    import inspect
    from backend.core.ouroboros.governance.intake import unified_intake_router as R
    src = inspect.getsource(R.UnifiedIntakeRouter._ingest_impl)
    assert "superseding_op" in src and "record_dispatch" in src


def test_inflight_ttl_defaults_to_twice_pipeline_wall(monkeypatch):
    monkeypatch.delenv(L._ENV_INFLIGHT_TTL, raising=False)
    monkeypatch.setenv(L._ENV_PIPELINE_TIMEOUT, "300")
    assert L.inflight_ttl_s() == 600.0
    monkeypatch.setenv(L._ENV_INFLIGHT_TTL, "42")
    assert L.inflight_ttl_s() == 42.0


def test_terminal_seam_is_wired():
    import inspect
    from backend.core.ouroboros.governance import orchestrator as orch
    assert "record_terminal" in inspect.getsource(orch.GovernedOrchestrator._publish_outcome)


def test_concurrent_appends_keep_the_chain_intact(repo):
    import threading
    goal = _goal()

    def _w(i):
        _run(L.record_dispatch(goal_id="goal-a", goal_digest_hex=L.goal_digest(goal), op_id=f"op-{i}"))

    ts = [threading.Thread(target=_w, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    recs = L.read_records()
    assert len(recs) == 8                      # every row verifies: no chain break
    assert {r.op_id for r in recs} == {f"op-{i}" for i in range(8)}


def test_unverifiable_ledger_is_never_appended_to(repo, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="Ouroboros.GoalReconciliation")
    _run(L.record_dispatch(goal_id="goal-a", goal_digest_hex="", op_id="op-1"))
    monkeypatch.setenv("JARVIS_ROADMAP_READER_HMAC_SECRET", "other-secret")
    assert _run(L.record_dispatch(goal_id="goal-a", goal_digest_hex="", op_id="op-2")) is None
    assert any("refusing to append" in r.getMessage() for r in caplog.records)
    assert sum(1 for l in L.ledger_path().read_text().splitlines() if l.strip()) == 1


def test_repair_chain_relinks_and_filters(repo):
    _run(L.record_dispatch(goal_id="goal-a", goal_digest_hex="", op_id="op-1"))
    _run(L.record_dispatch(goal_id="junk", goal_digest_hex="", op_id="op-x"))
    _run(L.record_dispatch(goal_id="goal-a", goal_digest_hex="", op_id="op-2"))
    p = L.ledger_path()
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    rows[1]["prev_hash"] = "0" * 64                # break the chain
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert len(L.read_records()) == 1
    assert L.repair_chain(keep=lambda r: r.get("goal_id") != "junk") == 2
    recs = L.read_records()
    assert [r.op_id for r in recs] == ["op-1", "op-2"] and all(r.mac for r in recs)


def test_default_ledger_path_is_colocated_with_reader_ledger(monkeypatch, tmp_path):
    monkeypatch.delenv(L._ENV_LEDGER_PATH, raising=False)
    monkeypatch.setenv("JARVIS_ROADMAP_READER_LEDGER_PATH", str(tmp_path / "iso" / "roadmap_reader_ledger.jsonl"))
    from backend.core.ouroboros.governance import roadmap_reader as rr
    assert L.ledger_path().parent == rr.ledger_path().parent
    assert L.ledger_path().name == "goal_reconciliation_ledger.jsonl"


def test_superseding_op_for_resume_dedupe(repo):
    goal = _goal()
    _run(L.record_dispatch(goal_id="goal-a", goal_digest_hex=L.goal_digest(goal), op_id="op-live"))
    assert L.superseding_op("goal-a", "op-old") == "op-live"
    assert L.superseding_op("goal-a", "op-live") == ""      # the live op itself resumes freely
    assert L.superseding_op("goal-none", "op-old") == ""
