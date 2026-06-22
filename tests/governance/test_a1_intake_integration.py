"""A1-T5 — intake pipe integration + OFF byte-identical.

Proves the A1 pieces COMPOSE on the real roadmap->intake boundary using the
actual ``_TeeRouter`` (roadmap emit point), ``a1_trace``, ``intake_dlq``,
``stamp_dag_weight``, and the router-ready valve — with light fakes for the
heavyweight downstream (the real UnifiedIntakeRouter dispatch loop + GLS are
exercised by their own suites; the deep hop sites are structurally verified
in test_a1_trace_breadcrumbs.py).

Invariants asserted:
  I1  no strategic GOAL is silently lost (orphaned -> loud + DLQ'd).
  I2  the daemon never emits before the router is ready (valve).
  I3  trace OFF is byte-identical (no [A1Trace] lines); DLQ default-on is the
      no-silent-drop guarantee; replay recovers orphaned GOALs.
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

from backend.core.ouroboros.governance import a1_trace
from backend.core.ouroboros.governance import intake_dlq as dlq
from backend.core.ouroboros.governance.intake import unified_intake_router as uir
from backend.core.ouroboros.governance.roadmap_orchestrator import _TeeRouter


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("JARVIS_INTAKE_DLQ_ENABLED", "true")
    monkeypatch.setenv("JARVIS_A1_TRACE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    uir._reset_router_ready_for_tests()
    yield
    uir._reset_router_ready_for_tests()


class _Env:
    """Minimal envelope stand-in (the real IntentEnvelope is frozen + heavy
    to construct; the boundary code only reads causal_id/target_files/evidence).

    Exposes ``to_dict()`` exactly like the real IntentEnvelope so the DLQ
    serializes it to a faithful JSON object (the round-trip the production
    orphaned-GOAL path depends on) rather than a lossy repr."""

    def __init__(self, gid, target_files=(), evidence=None, blast_radius=0):
        self.causal_id = gid
        self.goal_id = gid
        self.target_files = tuple(target_files)
        self.evidence = {} if evidence is None else evidence
        self.blast_radius = blast_radius

    def to_dict(self):
        return {
            "goal_id": self.goal_id,
            "causal_id": self.causal_id,
            "target_files": list(self.target_files),
            "evidence": dict(self.evidence),
            "blast_radius": self.blast_radius,
        }


class _FakeUpstream:
    def __init__(self):
        self.ingested = []

    async def ingest(self, envelope):
        self.ingested.append(envelope)
        return "enqueued"


# --- I2: the router-ready valve gates the first emit -----------------------

async def test_valve_blocks_until_ready_then_proceeds():
    bus = None  # degraded flag-poll path

    async def _attach_later():
        await asyncio.sleep(0.05)
        uir.mark_router_ready()

    t = asyncio.create_task(_attach_later())
    ready = await uir.await_router_ready(bus, 5.0)
    await t
    assert ready is True


async def test_valve_times_out_when_router_never_ready():
    ready = await uir.await_router_ready(None, 0.1)
    assert ready is False  # daemon would DLQ a router_ready_timeout marker


# --- I1: orphaned GOAL is loud + persisted (NOT silently captured) ---------

async def test_orphaned_goal_is_dlqd_not_silent(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)  # append_dlq default path -> tmp/.jarvis/...
    tee = _TeeRouter(upstream=None)
    env = _Env("g-orphan", target_files=("a.py", "b.py"))
    with caplog.at_level(logging.WARNING):
        result = await tee.ingest(env)
    assert result == "captured"  # legacy report path preserved
    # Loud: emit breadcrumb fired.
    assert any("[A1Trace] emit goal=g-orphan" in r.getMessage()
               for r in caplog.records)
    # Persisted: the orphan is in the DLQ (the no-silent-drop guarantee).
    rows = dlq.read_dlq(os.path.join(".jarvis", "intake_dlq.jsonl"))
    assert any(r.get("reason") == "no_router"
               and r.get("envelope", {}).get("goal_id") == "g-orphan"
               for r in rows)


async def test_attached_router_forwards_and_traces(caplog):
    up = _FakeUpstream()
    tee = _TeeRouter(upstream=up)
    env = _Env("g-attached", target_files=("x.py",))
    with caplog.at_level(logging.WARNING):
        result = await tee.ingest(env)
    assert result == "enqueued"
    assert up.ingested and up.ingested[0].causal_id == "g-attached"
    assert any("[A1Trace] emit goal=g-attached" in r.getMessage()
               for r in caplog.records)


# --- I1: DLQ replay recovers orphaned GOALs after the router attaches -------

async def test_dlq_replay_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = os.path.join(".jarvis", "intake_dlq.jsonl")
    tee = _TeeRouter(upstream=None)
    await tee.ingest(_Env("g1", target_files=("a.py", "b.py")))
    await tee.ingest(_Env("g2", target_files=("c.py",)))
    assert len(dlq.read_dlq(path)) == 2

    seen = []

    async def _reingest(env):
        seen.append(env.get("goal_id"))
        return "enqueued"

    drained = await dlq.replay_dlq(path, _reingest)
    assert drained == 2
    assert set(seen) == {"g1", "g2"}
    assert dlq.read_dlq(path) == []  # survivors empty -> recovered


# --- I3: heavy GOAL is tagged through the real helper -----------------------

def test_heavy_goal_tagged_for_epistemic_matrix():
    env = _Env("g-heavy", target_files=("a.py", "b.py", "c.py"))
    assert uir.stamp_dag_weight(env) is True
    assert env.evidence["dag_weight"] == "heavy"


# --- I3: trace OFF is byte-identical (no [A1Trace] lines) -------------------

async def test_trace_off_is_silent(monkeypatch, caplog):
    monkeypatch.setenv("JARVIS_A1_TRACE_ENABLED", "false")
    up = _FakeUpstream()
    tee = _TeeRouter(upstream=up)
    with caplog.at_level(logging.WARNING):
        await tee.ingest(_Env("g-quiet", target_files=("x.py",)))
    assert not any("[A1Trace]" in r.getMessage() for r in caplog.records)
    # ...but forwarding still happens (trace is purely observational).
    assert up.ingested and up.ingested[0].causal_id == "g-quiet"


def test_dlq_off_is_no_op(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JARVIS_INTAKE_DLQ_ENABLED", "false")
    dlq.append_dlq({"goal_id": "x"}, reason="no_router")
    assert dlq.read_dlq(os.path.join(".jarvis", "intake_dlq.jsonl")) == []
