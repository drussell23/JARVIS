"""Landing provenance: corpus rows labelled from git truth, by content hash."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.change_engine import (
    _inject_ouroboros_signature, strip_ouroboros_signature,
)
from backend.core.ouroboros.governance.observability import landing_provenance as lp
from backend.core.ouroboros.governance.observability.trajectory_recorder import (
    classify_terminal_reason,
)

OP = "op-01a10eb7-5cce-7a4d-9ca1-438f97b08afa-cau"
OTHER_OP = "op-01a10eb2-c44f-7840-b337-e8d9930ef59a-cau"


# ------------------------------------------------------- the signature inverse
@pytest.mark.parametrize("content", [
    "import os\n\ndef f():\n    return 1\n",
    "#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n\nx = 1\n",
    "",
    "no trailing newline",
    "# [Ouroboros] Modified by Ouroboros (op=op-0000000000) at 2026-01-01 00:00 UTC\n"
    "# Reason: older op\n\nx = 1\n",
])
def test_strip_is_the_exact_inverse_of_inject(content):
    signed = _inject_ouroboros_signature(content, OP, "add tests", "tests/test_x.py")
    assert signed != content
    assert strip_ouroboros_signature(signed, OP) == content


def test_strip_only_removes_this_ops_block():
    once = _inject_ouroboros_signature("x = 1\n", OTHER_OP, "earlier", "a.py")
    twice = _inject_ouroboros_signature(once, OP, "later", "a.py")
    assert strip_ouroboros_signature(twice, OP) == once


def test_strip_without_block_is_identity():
    assert strip_ouroboros_signature("x = 1\n", OP) == "x = 1\n"


def test_reinjection_by_the_same_op_strips_back_to_its_input():
    # The injector nests a second block INSIDE the first (it skips header
    # lines, not reason lines), so the inverse removes exactly the newest one.
    signed = _inject_ouroboros_signature("x = 1\n", OP, "r", "a.py")
    doubled = _inject_ouroboros_signature(signed, OP, "r", "a.py")
    assert strip_ouroboros_signature(doubled, OP) == signed


def test_two_complete_blocks_for_one_op_are_ambiguous_and_refused():
    block = f"# [Ouroboros] Modified by Ouroboros (op={OP[:12]}) at 2026-10-07 00:00 UTC\n# Reason: r\n\n"
    assert strip_ouroboros_signature(block + "x = 1\n" + block + "y = 2\n", OP) is None


# ------------------------------------------------------- the outcome policy fix
def test_complete_phase_is_success_as_the_orchestrator_spells_it():
    from backend.core.ouroboros.governance.op_context import OperationPhase
    assert classify_terminal_reason("complete", OperationPhase.COMPLETE.name)[0] == "success"
    assert classify_terminal_reason("", "POSTMORTEM")[0] == "unknown"
    assert classify_terminal_reason("tests_failed", OperationPhase.COMPLETE.name)[0] == "failure"


# ------------------------------------------------------------- the whole pass
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "base.py").write_text("x = 1\n")
    _git(repo, "add", "."); _git(repo, "commit", "-q", "-m", "base")

    def land(op: str, path: str, candidate: str) -> str:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(_inject_ouroboros_signature(candidate, op, "goal", path))
        _git(repo, "add", "."); _git(repo, "commit", "-q", "-m", f"test: x\n\nOp-ID: {op}\nFiles: {path}")
        return _git(repo, "rev-parse", "HEAD").strip()

    events = tmp_path / "events"
    events.mkdir()

    def row(op: str, candidate: str, eid: str) -> dict:
        return {"event_id": eid, "event_type": "interaction", "user_input": "p",
                "assistant_output": candidate,
                "metadata": {"op_id": op, "candidate_hash": hashlib.sha256(candidate.encode()).hexdigest()}}

    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_DIR", str(events))
    monkeypatch.setenv("JARVIS_LANDING_PROVENANCE_REF", "main")

    async def fake_resolve():
        return repo, "main"
    monkeypatch.setattr(lp, "resolve_repo_and_ref", fake_resolve)
    monkeypatch.setattr(lp, "_secret", lambda: "s3cret")
    return repo, events, land, row


def _write_rows(events: Path, rows):
    with (events / "experience_20261007.jsonl").open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_only_the_candidate_that_landed_is_labelled(world):
    repo, events, land, row = world
    winner, loser = "def test_a():\n    assert 1\n", "def test_a():\n    pass\n"
    sha = land(OP, "tests/test_a.py", winner)
    _write_rows(events, [row(OP, loser, "e-lose"), row(OP, winner, "e-win")])
    rep = asyncio.run(lp.label_landings())
    assert rep.proven == 1 and rep.labels_written == 1, rep.summary()
    labels, _, rejected = lp.read_labels()
    assert set(labels) == {"e-win"} and rejected == 0
    assert labels["e-win"]["commit_sha"] == sha and labels["e-win"]["surviving"] is True


def test_rerun_is_idempotent_and_revert_appends_not_rewrites(world):
    repo, events, land, row = world
    cand = "def test_b():\n    assert True\n"
    land(OP, "tests/test_b.py", cand)
    _write_rows(events, [row(OP, cand, "e-b")])
    asyncio.run(lp.label_landings())
    again = asyncio.run(lp.label_landings())
    assert again.labels_written == 0 and again.unchanged == 1
    _git(repo, "rm", "-q", "tests/test_b.py"); _git(repo, "commit", "-q", "-m", "revert hollow test")
    after = asyncio.run(lp.label_landings())
    assert after.labels_written == 1
    labels, _, _ = lp.read_labels()
    assert labels["e-b"]["landed"] is True and labels["e-b"]["surviving"] is False
    assert len(lp.ledger_path().read_text().splitlines()) == 2


def test_unprovable_commits_are_reported_not_guessed(world):
    repo, events, land, row = world
    land(OP, "tests/test_c.py", "def test_c():\n    assert 2\n")
    land(OTHER_OP, "tests/test_d.py", "def test_d():\n    assert 3\n")
    _write_rows(events, [row(OP, "something else entirely\n", "e-c")])
    rep = asyncio.run(lp.label_landings())
    assert rep.proven == 0 and rep.labels_written == 0
    assert sorted(rep.unproven.values()) == ["no_hash_match", "no_rows"]


def test_tampered_ledger_rows_are_ignored(world):
    repo, events, land, row = world
    cand = "def test_e():\n    assert 4\n"
    land(OP, "tests/test_e.py", cand)
    _write_rows(events, [row(OP, cand, "e-e")])
    asyncio.run(lp.label_landings())
    p = lp.ledger_path()
    rec = json.loads(p.read_text().splitlines()[0])
    rec["payload"]["subject_event_id"] = "e-forged"
    p.write_text(json.dumps(rec) + "\n")
    labels, _, rejected = lp.read_labels()
    assert labels == {} and rejected == 1


def test_labels_live_outside_the_corpus_glob(world):
    _, events, _, _ = world
    assert lp.ledger_path().parent != events
    assert lp.ledger_path().parent.parent == events
