"""A spoken command must survive the process that was executing it.

The mandated scenario is `test_a_timeout_at_node_2_resumes_at_node_2`: three
nodes, a `TimeoutError` injected at the second, and a resume that skips node 1,
re-runs node 2, and completes node 3 with no operator intervention.

THE RULE THE SUITE EXISTS TO PROTECT
--------------------------------------
"Refactor the phases so they are strictly idempotent" is right for
classification and planning and IMPOSSIBLE for the rest. The CU executor's
steps are ``type``, ``click``, ``drag``, ``scroll``, ``hotkey`` — verified by
reading it. They act on the world. Re-running "step 3 of message Alice" does
not recompute a value; it sends a second message.

So an EFFECTFUL node that STARTED and never reported is not "failed" and not
"done" — it is UNKNOWN, and the plan asks instead of guessing. Same discipline
as `UNKNOWN != UNSET` in provenance and `unverified != unsafe` in the
coordination probe: a system that cannot tell must not pick the convenient
answer and call it a fact.

`test_an_interrupted_effectful_node_is_never_silently_replayed` is the one to
keep. Without it, this journal would be a machine for sending duplicate
messages.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from backend.hud import intent_journal as ij
from backend.hud.intent_journal import (
    IntentJournal,
    Node,
    NodeKind,
    ResumeAction,
    run_dag,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_INTENT_JOURNAL_PATH",
                       str(tmp_path / "intents" / "journal.jsonl"))
    ij.reset_intent_journal()
    yield tmp_path / "intents" / "journal.jsonl"
    ij.reset_intent_journal()


def _journal() -> IntentJournal:
    return IntentJournal(ij.journal_path())


class TestTheMandatedScenario:
    @pytest.mark.asyncio
    async def test_a_timeout_at_node_2_resumes_at_node_2(self):
        """Three nodes, TimeoutError at node 2, resume skips node 1 and
        finishes node 3 — no operator intervention."""
        calls = {"one": 0, "two": 0, "three": 0}
        explode = {"on": True}

        async def _one(ctx):
            calls["one"] += 1
            return "classified"

        async def _two(ctx):
            calls["two"] += 1
            if explode["on"]:
                raise asyncio.TimeoutError("J-Prime timed out mid-DAG")
            return "planned"

        async def _three(ctx):
            calls["three"] += 1
            return f"executed({ctx['one']}/{ctx['two']})"

        nodes = [Node("one", _one), Node("two", _two), Node("three", _three)]
        j = _journal()

        # First attempt — dies at node 2.
        with pytest.raises(asyncio.TimeoutError):
            await run_dag(nodes, command="message alice", journal=j)
        assert calls == {"one": 1, "two": 1, "three": 0}

        # The crash left exactly one unfinished intent.
        pending = j.unfinished()
        assert len(pending) == 1

        # Autonomic replay.
        explode["on"] = False
        out = await run_dag(nodes, command="message alice", journal=j,
                            intent_id=pending[0])

        assert out["completed"] is True
        assert calls["one"] == 1, "node 1 was re-executed — the journal was ignored"
        assert calls["two"] == 2, "node 2 was not retried"
        assert calls["three"] == 1, "node 3 never ran"
        assert out["results"]["three"] == "executed(classified/planned)"

    @pytest.mark.asyncio
    async def test_the_command_is_journalled_before_anything_runs(self):
        """The write-ahead half: if the process dies one instruction later, the
        operator's sentence still exists."""
        seen = {}

        async def _capture(ctx):
            seen["lines"] = _journal()._read()
            return 1

        await run_dag([Node("only", _capture)], command="lock the front door",
                      journal=_journal())
        kinds = [e.get("k") for e in seen["lines"]]
        assert "intent" in kinds
        assert any(e.get("command") == "lock the front door"
                   for e in seen["lines"] if e.get("k") == "intent")

    @pytest.mark.asyncio
    async def test_a_completed_pure_node_reuses_its_recorded_result(self):
        """Not merely skipped — the VALUE is reused, so downstream nodes see
        what they would have seen. Skipping without the result would leave the
        DAG resuming into a hole."""
        async def _expensive(ctx):
            return {"category": "vision_action", "cost": "one model call"}

        async def _boom(ctx):
            raise asyncio.TimeoutError("boom")

        j = _journal()
        with pytest.raises(asyncio.TimeoutError):
            await run_dag([Node("classify", _expensive), Node("plan", _boom)],
                          command="x", journal=j)
        plan = j.resume_plan(j.unfinished()[0])
        v = plan.for_node("classify")
        assert v.action is ResumeAction.SKIP
        assert v.result["category"] == "vision_action"


class TestTheReplaySafetyRule:
    @pytest.mark.asyncio
    async def test_an_interrupted_effectful_node_is_never_silently_replayed(self):
        """THE one to keep.

        A node that started, touched the world, and never reported has an
        UNKNOWN outcome. Auto-replaying it sends the message twice.
        """
        j = _journal()
        iid = await j.open_intent("message alice saying hi")
        await j.node_started(iid, "send", NodeKind.EFFECTFUL)   # …then death

        plan = j.resume_plan(iid)
        v = plan.for_node("send")
        assert v.action is ResumeAction.CONFIRM
        assert plan.resumable is False
        assert "UNKNOWN" in v.detail

    @pytest.mark.asyncio
    async def test_the_dag_refuses_to_resume_past_an_unknown_effect(self):
        ran = {"n": 0}

        async def _after(ctx):
            ran["n"] += 1
            return "should not happen"

        j = _journal()
        iid = await j.open_intent("message alice")
        await j.node_started(iid, "send", NodeKind.EFFECTFUL)

        out = await run_dag([Node("send", _after, NodeKind.EFFECTFUL),
                             Node("verify", _after)],
                            command="message alice", journal=j, intent_id=iid)
        assert out["completed"] is False
        assert out["blocked_on"] == ["send"]
        assert ran["n"] == 0, "a world-mutating node was replayed blind"

    @pytest.mark.asyncio
    async def test_a_completed_effectful_node_is_skipped_not_repeated(self):
        ran = {"n": 0}

        async def _send(ctx):
            ran["n"] += 1
            return "sent"

        j = _journal()
        iid = await j.open_intent("message alice")
        await j.node_started(iid, "send", NodeKind.EFFECTFUL)
        await j.node_completed(iid, "send", "sent", NodeKind.EFFECTFUL)

        out = await run_dag([Node("send", _send, NodeKind.EFFECTFUL)],
                            command="message alice", journal=j, intent_id=iid)
        assert ran["n"] == 0, "the message was sent twice"
        assert out["completed"] is True

    @pytest.mark.asyncio
    async def test_an_effectful_node_result_is_not_stored_for_reuse(self):
        """Storing it would invite exactly the reuse that is unsafe."""
        j = _journal()
        iid = await j.open_intent("c")
        await j.node_completed(iid, "click", {"pixel": [10, 20]},
                               NodeKind.EFFECTFUL)
        assert all("result" not in e for e in j._read()
                   if e.get("node") == "click")

    @pytest.mark.asyncio
    async def test_a_cleanly_failed_node_is_safe_to_redo(self):
        """Failure REPORTED is different from failure UNKNOWN — a node that
        raised is known not to have applied."""
        j = _journal()
        iid = await j.open_intent("c")
        await j.node_started(iid, "click", NodeKind.EFFECTFUL)
        await j.node_failed(iid, "click", "target not found", NodeKind.EFFECTFUL)
        assert j.resume_plan(iid).for_node("click").action is ResumeAction.REDO

    @pytest.mark.asyncio
    async def test_an_interrupted_PURE_node_is_simply_redone(self):
        """Recomputing a classification costs time, never correctness."""
        j = _journal()
        iid = await j.open_intent("c")
        await j.node_started(iid, "classify", NodeKind.PURE)
        assert j.resume_plan(iid).for_node("classify").action is ResumeAction.REDO


class TestJournalIntegrity:
    @pytest.mark.asyncio
    async def test_a_torn_final_line_does_not_lose_the_journal(self, _isolated):
        j = _journal()
        iid = await j.open_intent("survive me")
        await j.node_completed(iid, "a", 1)
        with _isolated.open("a") as fh:
            fh.write('{"k":"node","id":')          # killed mid-append
        assert j.resume_plan(iid).for_node("a").action is ResumeAction.SKIP
        assert j.stats()["corrupt_lines"] >= 1

    @pytest.mark.asyncio
    async def test_a_retry_then_crash_does_not_unmark_a_completed_node(self):
        """STARTED arriving after COMPLETED must not demote it — otherwise a
        retry that crashes makes finished work look unfinished."""
        j = _journal()
        iid = await j.open_intent("c")
        await j.node_completed(iid, "a", "done", NodeKind.PURE)
        await j.node_started(iid, "a", NodeKind.PURE)
        assert j.resume_plan(iid).for_node("a").action is ResumeAction.SKIP

    @pytest.mark.asyncio
    async def test_closed_intents_are_not_pending(self):
        j = _journal()
        await run_dag([Node("a", lambda ctx: _ok())], command="c", journal=j)
        assert j.unfinished() == []

    @pytest.mark.asyncio
    async def test_stale_unfinished_intents_fall_out_of_retention(
            self, monkeypatch):
        monkeypatch.setenv("JARVIS_INTENT_JOURNAL_RETENTION_S", "60")
        j = _journal()
        iid = await j.open_intent("ancient")
        # Rewrite its timestamp to long ago.
        path = ij.journal_path()
        lines = []
        for raw in path.read_text().splitlines():
            e = json.loads(raw)
            e["t"] = time.time() - 10_000
            lines.append(json.dumps(e))
        path.write_text("\n".join(lines) + "\n")
        assert iid not in j.unfinished()

    @pytest.mark.asyncio
    async def test_compaction_drops_closed_intents_past_retention(
            self, monkeypatch, _isolated):
        monkeypatch.setenv("JARVIS_INTENT_JOURNAL_RETENTION_S", "60")
        j = _journal()
        iid = await j.open_intent("old")
        await j.close_intent(iid, success=True)
        lines = []
        for raw in _isolated.read_text().splitlines():
            e = json.loads(raw)
            e["t"] = time.time() - 10_000
            lines.append(json.dumps(e))
        _isolated.write_text("\n".join(lines) + "\n")
        assert j.compact() == 0

    @pytest.mark.asyncio
    async def test_an_unwritable_journal_never_breaks_the_command(
            self, monkeypatch):
        """Fail-open: the operator's command matters more than the record of it."""
        monkeypatch.setenv("JARVIS_INTENT_JOURNAL_PATH", "/proc/nope/j.jsonl")
        ij.reset_intent_journal()
        out = await run_dag([Node("a", lambda ctx: _ok())], command="c")
        assert out["completed"] is True

    @pytest.mark.asyncio
    async def test_the_master_switch_disables_writes(self, monkeypatch, _isolated):
        monkeypatch.setenv("JARVIS_INTENT_JOURNAL_ENABLED", "0")
        await run_dag([Node("a", lambda ctx: _ok())], command="c",
                      journal=_journal())
        assert not _isolated.exists()

    def test_it_lives_beside_the_checkpoint_ledger(self, monkeypatch):
        """Same `.ouroboros/` convention as `fsm_checkpoint`, so an operator
        finds every resume artefact in one place."""
        monkeypatch.delenv("JARVIS_INTENT_JOURNAL_PATH", raising=False)
        monkeypatch.delenv("JARVIS_INTENT_JOURNAL_DIR", raising=False)
        assert ".ouroboros" in str(ij.journal_path())


async def _ok():
    return "ok"


class TestTheRouterIsWired:
    """The journal is only worth having if the front door actually uses it."""

    @staticmethod
    def _fake_router(fail_first: bool = True):
        from backend.hud.voice_command_router import VoiceCommandRouter

        calls = {"classify": 0, "dispatch": 0}

        class _Fake(VoiceCommandRouter):
            def __init__(self):
                pass

            async def _classify(self, command):
                calls["classify"] += 1
                return {"category": "app_action"}

            async def _execute_app_action(self, command):
                calls["dispatch"] += 1
                if fail_first and calls["dispatch"] == 1:
                    raise asyncio.TimeoutError("J-Prime timed out mid-DAG")

                class _R:
                    success = True
                return _R()

        return _Fake(), calls

    @pytest.mark.asyncio
    async def test_a_crashed_command_stays_replayable(self):
        """Closing the intent on failure would make it unrecoverable —
        `unfinished()` IS the replay queue. Measured as `unfinished: 0` before
        this was fixed."""
        router, _calls = self._fake_router()
        j = ij.get_intent_journal()
        with pytest.raises(asyncio.TimeoutError):
            await router.route("open safari")
        assert len(j.unfinished()) == 1, "the crashed intent was not replayable"

    @pytest.mark.asyncio
    async def test_the_raw_command_survives_the_crash(self):
        router, _calls = self._fake_router()
        j = ij.get_intent_journal()
        with pytest.raises(asyncio.TimeoutError):
            await router.route("open safari")
        assert j.resume_plan(j.unfinished()[0]).command == "open safari"

    @pytest.mark.asyncio
    async def test_resume_does_not_pay_for_classification_twice(self):
        router, calls = self._fake_router()
        j = ij.get_intent_journal()
        with pytest.raises(asyncio.TimeoutError):
            await router.route("open safari")
        iid = j.unfinished()[0]
        await router.route(j.resume_plan(iid).command, intent_id=iid)
        assert calls["classify"] == 1, "the model was called again on resume"
        assert calls["dispatch"] == 2
        assert j.unfinished() == []

    @pytest.mark.asyncio
    async def test_dispatch_is_journalled_as_EFFECTFUL(self):
        """Every branch of the dispatch can touch the world, so an interrupted
        one must resolve to CONFIRM rather than a silent second attempt."""
        router, _calls = self._fake_router(fail_first=False)
        j = ij.get_intent_journal()
        await router.route("open safari")
        assert any(e.get("node") == "dispatch" and e.get("kind") == "effectful"
                   for e in j._read())

    @pytest.mark.asyncio
    async def test_a_journal_failure_never_blocks_the_command(
            self, monkeypatch):
        monkeypatch.setenv("JARVIS_INTENT_JOURNAL_PATH", "/proc/nope/j.jsonl")
        ij.reset_intent_journal()
        router, calls = self._fake_router(fail_first=False)
        result = await router.route("open safari")
        assert result.success is True
        assert calls["dispatch"] == 1
