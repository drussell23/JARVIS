"""Foreground session: a conversation that reads, answers and proposes, and
never writes (docs/architecture/FOREGROUND_SESSION.md, invariants 1, 2, 6).

The live loop is ToolLoopCoordinator; here a fake coordinator stands in so
the session's own contract is pinned: read-only calls, addressed output,
proposals captured not applied, and an accepted change that reaches GENERATE
exactly once, with the reads that produced it.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import foreground_session as fs
from backend.core.ouroboros.governance import foreground_session_ops as ops

_GOV = Path(__file__).resolve().parents[2] / "backend/core/ouroboros/governance"

PROPOSAL = {"schema_version": "2b.1-diff", "candidates": [{
    "candidate_id": "c1", "file_path": "backend/x.py",
    "unified_diff": "@@ -1,1 +1,1 @@\n-a = 1\n+a = 2\n", "rationale": "bump a"}]}


class _FakeCoordinator:
    def __init__(self, on_tool_call, answer, tools=()):
        self.on_tool_call, self.answer, self.tools = on_tool_call, answer, tools
        self.kwargs = None

    async def run(self, **kw):
        self.kwargs = kw
        recs = []
        for name, target in self.tools:
            self.on_tool_call(op_id=kw["op_id"], tool_name=name, args_summary=target,
                              status="success")
            recs.append(SimpleNamespace(tool_name=name))
        return self.answer, recs


def _manager(answer, tools=(), sink=None):
    made = []

    def factory(on_tool_call):
        c = _FakeCoordinator(on_tool_call, answer, tools)
        made.append(c)
        return c

    out = sink if sink is not None else []
    m = fs.SessionManager(generate=lambda p: asyncio.sleep(0, "x"),
                          emit=lambda markup, cockpit: out.append((cockpit, markup)),
                          coordinator_factory=factory, log_dir=Path("/tmp/fs-test-log"))
    return m, made, out


@pytest.fixture(autouse=True)
def _clean():
    ops.reset_for_tests()
    fs.reset_for_tests()
    yield
    ops.reset_for_tests()
    fs.reset_for_tests()


# ---- invariant 1: the session never writes ------------------------------------

def test_every_turn_runs_the_loop_read_only():
    m, made, _ = _manager("it reads the config")
    s = m.start("ck1")
    asyncio.run(m.run_turn(s, "where is the config read?"))
    assert made[0].kwargs["is_read_only"] is True


@pytest.mark.parametrize("mod", ["foreground_session.py", "foreground_session_ops.py",
                                 "session_repl.py"])
def test_session_modules_import_no_mutation_path(mod):
    tree = ast.parse((_GOV / mod).read_text(encoding="utf-8"))
    names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    banned = ("change_engine", "auto_committer", "edit_file", "worktree_manager")
    assert not [n for n in names if any(b in n for b in banned)], names


# ---- turns ---------------------------------------------------------------------

def test_a_turn_answers_the_cockpit_that_asked_and_keeps_the_reads():
    m, _made, out = _manager("the floor is in risk_tier_floor.py",
                             tools=[("read_file", "governance/risk_tier_floor.py")])
    s = m.start("ck1")
    turn = asyncio.run(m.run_turn(s, "where is the risk floor?"))
    assert {c for c, _ in out} == {"ck1"}
    assert turn.answer.startswith("the floor") and turn.proposal is None
    assert [d.tool for d in turn.tools] == ["read_file"] and len(turn.records) == 1
    assert "governance/risk_tier_floor.py" in s.focus


def test_a_change_is_captured_as_a_proposal_not_an_answer():
    m, _made, out = _manager(json.dumps(PROPOSAL))
    s = m.start("ck1")
    turn = asyncio.run(m.run_turn(s, "bump a"))
    assert turn.proposal == PROPOSAL and turn.answer == ""
    assert "/session apply" in out[-1][1]


def test_older_turns_are_summarized_deterministically(monkeypatch):
    monkeypatch.setenv("JARVIS_SESSION_VERBATIM_TURNS", "1")
    s = fs.ForegroundSession(id="s1", cockpit="c", created=0.0)
    s.turns = [fs.SessionTurn(user="first q", answer="first a\n\nmore"),
               fs.SessionTurn(user="second q", answer="second a")]
    p = fs.build_prompt(s, "third q")
    assert "Earlier in this conversation" in p and "first a" in p and "more" not in p
    assert p.rstrip().endswith("third q")


def test_lifecycle_start_resume_end():
    m, *_ = _manager("x")
    a = m.start("ck1")
    assert m.start("ck1") is a                      # returning, not a second session
    assert m.end("ck1") is a and m.active("ck1") is None
    b = m.start("ck2")
    assert m.resume("ck3", b.id) is b and m.active("ck3") is b


def test_parse_proposal_rejects_prose_and_empty_envelopes():
    assert fs.parse_proposal("just prose") is None
    assert fs.parse_proposal('{"schema_version": "2b.1-diff", "candidates": []}') is None
    assert fs.parse_proposal("```json\n" + json.dumps(PROPOSAL) + "\n```") == PROPOSAL


# ---- the seed: an accepted change reaches GENERATE once ------------------------

def _ctx(ref):
    return SimpleNamespace(op_id="op-1", intake_evidence_json=json.dumps(
        {"foreground_session": {"session_id": "s1", "seed_ref": ref}}))


def test_a_seed_is_consumed_once_and_carries_the_reads():
    ref = ops._put_seed(json.dumps(PROPOSAL), "s1", ("rec",))
    assert ops.take_seed_with_records(_ctx(ref)) == (json.dumps(PROPOSAL), ("rec",))
    assert ops.take_seed(_ctx(ref)) is None


def test_evidence_alone_cannot_forge_a_seed():
    """A ref replayed from the intake WAL (or invented) is not in memory."""
    assert ops.take_seed(_ctx("deadbeef" * 4)) is None
    assert ops.take_seed(SimpleNamespace(intake_evidence_json="{}")) is None


def test_apply_refuses_a_file_the_session_never_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/x.py").write_text("a = 1\n")
    ops.set_injector(lambda **kw: "op-x")
    m, *_ = _manager(json.dumps(PROPOSAL))
    s = m.start("ck1")
    asyncio.run(m.run_turn(s, "bump a"))
    ok, text = ops.apply_last(m, "ck1")
    assert not ok and "never read backend/x.py" in text


def test_apply_sanctions_injects_and_owns_the_op(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/x.py").write_text("a = 1\n")
    seen = {}

    def inject(**kw):
        seen.update(kw)
        return "op-77"

    ops.set_injector(inject)
    import backend.core.ouroboros.governance.operator_goal_sanction as ogs
    monkeypatch.setattr(ogs, "author_and_sign_goal",
                        lambda spec, **_: SimpleNamespace(ok=True, goal_id=spec.goal_id))
    m, *_ = _manager(json.dumps(PROPOSAL), tools=[("read_file", "backend/x.py")])
    s = m.start("ck1")
    asyncio.run(m.run_turn(s, "bump a"))
    ok, text = ops.apply_last(m, "ck1")
    assert ok and "op-77" in text and s.owned_ops == ["op-77"]
    assert seen["target_files"] == ("backend/x.py",)
    ref = seen["extra_evidence"]["foreground_session"]["seed_ref"]
    raw, recs = ops.take_seed_with_records(_ctx(ref))
    assert json.loads(raw) == PROPOSAL and len(recs) == 1


# ---- the verb and plain-text routing --------------------------------------------

def test_plain_text_outside_a_session_is_not_taken(monkeypatch):
    from backend.core.ouroboros.governance import session_repl
    fs.reset_for_tests(_manager("x")[0])
    monkeypatch.setattr(session_repl, "_cockpit", lambda: "ck9")
    assert session_repl.route_plain_text("hello") is False
    assert session_repl.route_plain_text("/status") is False


def test_session_verb_starts_lists_and_ends(monkeypatch):
    from backend.core.ouroboros.governance import session_repl
    fs.reset_for_tests(_manager("x")[0])
    monkeypatch.setattr(session_repl, "_cockpit", lambda: "ck1")
    r = session_repl.dispatch_session_command("/session")
    assert r.ok and r.text.startswith("session s-")
    assert "← you" in session_repl.dispatch_session_command("/session list").text
    assert session_repl.dispatch_session_command("/session end").ok
    assert not session_repl.dispatch_session_command("/sessions").matched
