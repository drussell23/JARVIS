"""A /session and its approved change survive a daemon restart.

Before: the session log held only turns (no id, owned ops, end, records),
``resume`` looked in memory only, and the approved seed lived only in a
process dict -- so after a restart ``/session resume`` said "no live
session" and the replayed op silently REGENERATED instead of landing what
the operator approved. Checkpoint resume also dropped the operator keys.

Pinned here:
  * the log rebuilds a session (turns, records, owned ops, ended);
  * a sealed seed verifies after "restart" (memory cleared) and is consumed once;
  * a tampered or re-keyed seal is dropped with a CRYPTO warning;
  * no secret -> nothing written to disk;
  * a checkpoint resume carries the operator identity.
"""
from __future__ import annotations

import json
import logging
import time

import pytest

from backend.core.ouroboros.governance import foreground_session as fs
from backend.core.ouroboros.governance import foreground_session_ops as ops
from backend.core.ouroboros.governance.tool_executor import (
    ToolExecStatus, ToolExecutionRecord,
)

SECRET_ENV = "JARVIS_ROADMAP_READER_HMAC_SECRET"


def _record(i: int = 0) -> ToolExecutionRecord:
    return ToolExecutionRecord(
        schema_version="tool.exec.v1", op_id="session-s-1-t1", call_id=f"c{i}",
        round_index=i, tool_name="read_file", tool_version="1", arguments_hash="h",
        repo="jarvis", policy_decision="allow", policy_reason_code="ok",
        started_at_ns=1, ended_at_ns=2, duration_ms=0.5, output_bytes=10,
        error_class=None, status=ToolExecStatus.SUCCESS)


class _Ctx:
    def __init__(self, ref: str) -> None:
        self.op_id = "op-1"
        self.intake_evidence_json = json.dumps(
            {"foreground_session": {"session_id": "s-abc123", "seed_ref": ref}})


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SESSION_LOG_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("JARVIS_SESSION_SEED_DIR", raising=False)
    ops.reset_for_tests()
    yield tmp_path
    ops.reset_for_tests()


def _restart() -> None:
    """What a daemon restart does to the seed store: the process dict is gone."""
    with ops._LOCK:
        ops._SEEDS.clear()


class TestSessionLog:
    def test_session_rebuilds_from_its_log(self, dirs):
        mgr = fs.SessionManager()
        s = mgr.start("cockpit-A")
        turn = fs.SessionTurn(user="look at foo.py", answer="ok", started=time.time(),
                              tools=[fs.ToolDigest("read_file", "foo.py", "success")],
                              records=[_record()])
        s.turns.append(turn)
        mgr._log(s, turn)
        mgr.record_owned_op(s, "op-9")

        fresh = fs.SessionManager()                    # the restarted daemon
        back = fresh.resume("cockpit-B", s.id)
        assert back is not None and back.id == s.id
        assert back.cockpit == "cockpit-B"
        assert [t.user for t in back.turns] == ["look at foo.py"]
        assert back.turns[0].records == [_record()]
        assert back.owned_ops == ["op-9"]
        assert "foo.py" in back.focus

    def test_ended_session_is_not_resumable(self, dirs):
        mgr = fs.SessionManager()
        s = mgr.start("A")
        mgr.end("A")
        assert fs.SessionManager().resume("A", s.id) is None

    def test_torn_last_line_is_skipped(self, dirs):
        mgr = fs.SessionManager()
        s = mgr.start("A")
        with open(fs.sessions_dir() / f"{s.id}.jsonl", "a") as fh:
            fh.write('{"user": "half')
        assert fs.SessionManager().resume("A", s.id) is not None

    def test_path_traversal_id_is_refused(self, dirs):
        assert fs.load_session("../../etc/passwd", fs.sessions_dir()) is None

    def test_old_turn_only_logs_still_load(self, dirs):
        d = fs.sessions_dir()
        d.mkdir(parents=True)
        (d / "s-01d01d.jsonl").write_text(json.dumps({"user": "hi", "answer": "yo"}) + "\n")
        s = fs.load_session("s-01d01d", d)
        assert s is not None and s.turns[0].answer == "yo"


class TestSealedSeed:
    def test_seed_survives_restart_and_is_consumed_once(self, dirs, monkeypatch):
        monkeypatch.setenv(SECRET_ENV, "k1")
        ref = ops._put_seed('{"candidates": []}', "s-abc123", (_record(),))
        assert (ops.seed_dir() / f"{ref}.json").is_file()
        _restart()
        got = ops.take_seed_with_records(_Ctx(ref))
        assert got == ('{"candidates": []}', (_record(),))
        assert not (ops.seed_dir() / f"{ref}.json").exists()
        assert ops.take_seed_with_records(_Ctx(ref)) is None

    def test_in_memory_take_also_spends_the_seal(self, dirs, monkeypatch):
        monkeypatch.setenv(SECRET_ENV, "k1")
        ref = ops._put_seed("{}", "s-abc123")
        assert ops.take_seed(_Ctx(ref)) == "{}"
        _restart()
        assert ops.take_seed(_Ctx(ref)) is None

    def test_rotated_secret_drops_seed_with_crypto_warning(self, dirs, monkeypatch, caplog):
        monkeypatch.setenv(SECRET_ENV, "k1")
        ref = ops._put_seed("{}", "s-abc123")
        _restart()
        monkeypatch.setenv(SECRET_ENV, "k2")
        with caplog.at_level(logging.WARNING, logger="Ouroboros.ForegroundSessionOps"):
            assert ops.take_seed(_Ctx(ref)) is None
        assert "CRYPTO" in caplog.text and "rotated" in caplog.text
        assert not (ops.seed_dir() / f"{ref}.json").exists()

    def test_tampered_proposal_is_refused(self, dirs, monkeypatch, caplog):
        monkeypatch.setenv(SECRET_ENV, "k1")
        ref = ops._put_seed('{"candidates": []}', "s-abc123")
        p = ops.seed_dir() / f"{ref}.json"
        doc = json.loads(p.read_text())
        doc["body"]["proposal"] = '{"candidates": [{"file_path": "evil.py"}]}'
        p.write_text(json.dumps(doc))
        _restart()
        with caplog.at_level(logging.WARNING, logger="Ouroboros.ForegroundSessionOps"):
            assert ops.take_seed(_Ctx(ref)) is None
        assert "CRYPTO" in caplog.text

    def test_no_secret_writes_nothing(self, dirs, monkeypatch):
        monkeypatch.delenv(SECRET_ENV, raising=False)
        ref = ops._put_seed("{}", "s-abc123")
        assert not (ops.seed_dir() / f"{ref}.json").exists()
        assert ops.take_seed(_Ctx(ref)) == "{}"         # memory still works

    def test_expired_seal_is_not_used(self, dirs, monkeypatch):
        monkeypatch.setenv(SECRET_ENV, "k1")
        ref = ops._put_seed("{}", "s-abc123")
        _restart()
        monkeypatch.setenv("JARVIS_SESSION_SEED_TTL_S", "0.000001")
        time.sleep(0.01)
        assert ops.take_seed(_Ctx(ref)) is None

    def test_refused_apply_releases_the_seal(self, dirs, monkeypatch):
        monkeypatch.setenv(SECRET_ENV, "k1")
        ref = ops._put_seed("{}", "s-abc123")
        mgr = fs.SessionManager()
        s = mgr.start("A")
        ok, _ = ops._settle(mgr, "A", s, ref, ("a.py",), None)
        assert ok is False
        assert not (ops.seed_dir() / f"{ref}.json").exists()

    def test_accepted_apply_logs_the_owned_op(self, dirs, monkeypatch):
        mgr = fs.SessionManager(mode_notify=lambda *a: None)
        s = mgr.start("A")
        ops._settle(mgr, "A", s, "0" * 32, ("a.py",), "op-77")
        assert fs.SessionManager().resume("A", s.id).owned_ops == ["op-77"]

    def test_bad_ref_shape_never_touches_disk(self, dirs, monkeypatch):
        monkeypatch.setenv(SECRET_ENV, "k1")
        assert ops.take_seed(_Ctx("../../x")) is None


class TestCheckpointResume:
    def test_resume_carries_operator_identity(self):
        from backend.core.ouroboros.governance.intake.unified_intake_router import (
            _resume_envelope_kwargs,
        )
        ev = {"operator_origin": True, "goal_id": "g1",
              "foreground_session": {"session_id": "s-abc123", "seed_ref": "a" * 32}}
        kw = _resume_envelope_kwargs({"op_id": "op-1", "intake_evidence_json": json.dumps(ev),
                                      "target_files": ["a.py"]})
        evidence = kw["evidence"]
        assert evidence["operator_origin"] is True
        assert evidence["foreground_session"]["seed_ref"] == "a" * 32
        nested = json.loads(evidence["intake_evidence_json"])
        assert nested["foreground_session"]["seed_ref"] == "a" * 32

    def test_autonomous_checkpoint_gains_nothing(self):
        from backend.core.ouroboros.governance.intake.unified_intake_router import (
            _resume_envelope_kwargs,
        )
        kw = _resume_envelope_kwargs({"op_id": "op-1", "intake_evidence_json": "{}"})
        assert "operator_origin" not in kw["evidence"]
        assert "foreground_session" not in kw["evidence"]
