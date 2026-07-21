"""Layer-2 persona-council deliberator (Hive Step 3) — advisory, bounded."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.hive_council_layer import (
    CouncilDeliberator, council_enabled,
)


def _env(subsystem="governance", severity="error", intent="op_failed",
         actor="ov.governance", summary="operation terminal: fallback_failed",
         detail=None):
    return SimpleNamespace(
        subsystem=subsystem, severity=severity, intent=intent,
        actor_id=actor, action_summary=summary, detail=detail or {})


def _persona_json(reasoning, confidence=0.9, verdict=None):
    d = {"reasoning": reasoning, "confidence": confidence}
    if verdict:
        d["validate_verdict"] = verdict
    return json.dumps(d)


class _FakeDW:
    """Mirrors the REAL DoublewordProvider.prompt_only contract (the same
    shape backend/hive's own integration tests canonize)."""

    def __init__(self):
        self.calls = []
        self.responses = [
            _persona_json("Observed: repeated op failures in the feed."),
            _persona_json("Propose: inspect provider cascade budget."),
            _persona_json("Approved — advisory only.", verdict="approve"),
        ]

    async def prompt_only(self, prompt, *, model=None, caller_id="",
                          max_tokens=None, **kw):
        self.calls.append((caller_id, model, max_tokens))
        return self.responses[min(len(self.calls) - 1,
                                  len(self.responses) - 1)]


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------

def test_master_default_off():
    assert council_enabled() is False


@pytest.mark.asyncio
async def test_disabled_start_and_taps_noop(monkeypatch):
    monkeypatch.delenv("JARVIS_HIVE_COUNCIL_ENABLED", raising=False)
    cd = CouncilDeliberator(doubleword=_FakeDW(), emit_fn=lambda **k: None)
    assert await cd.start() is False
    cd.on_envelope(_env())
    assert cd.stats["triggers_accepted"] == 0


@pytest.mark.asyncio
async def test_feedback_loop_and_synthetic_guards(monkeypatch):
    """The council's own speech can NEVER convene it; doctor probes never
    convene it. Both are structural, not policy."""
    monkeypatch.setenv("JARVIS_HIVE_COUNCIL_ENABLED", "true")
    cd = CouncilDeliberator(doubleword=_FakeDW(), emit_fn=lambda **k: None)
    cd._running = True
    cd.on_envelope(_env(subsystem="persona", severity="error"))
    assert cd.stats["suppressed_feedback"] == 1
    cd.on_envelope(_env(detail={"trace_class": "synthetic_probe"}))
    assert cd.stats["suppressed_synthetic"] == 1
    cd.on_envelope(_env(severity="info"))            # below the gate
    assert cd.stats["triggers_accepted"] == 0
    cd.on_envelope(_env(severity="error"))
    assert cd.stats["triggers_accepted"] == 1


@pytest.mark.asyncio
async def test_cooldown_and_hourly_budget(monkeypatch):
    monkeypatch.setenv("JARVIS_HIVE_COUNCIL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HIVE_COUNCIL_MAX_PER_HOUR", "2")
    cd = CouncilDeliberator(doubleword=_FakeDW(), emit_fn=lambda **k: None)
    cd._running = True
    cd.on_envelope(_env(intent="a"))
    cd.on_envelope(_env(intent="a"))                 # same key → cooldown
    assert cd.stats["suppressed_cooldown"] == 1
    cd.on_envelope(_env(intent="b"))                 # budget slot 2/2
    cd.on_envelope(_env(intent="c"))                 # over budget
    assert cd.stats["suppressed_budget"] == 1
    assert cd.stats["triggers_accepted"] == 2


# ---------------------------------------------------------------------------
# Active-Speaker mutex + the consensus brake's deadline
# ---------------------------------------------------------------------------

class _FakeThreadMgr:
    def __init__(self):
        self.n = 0

    def create_thread(self, **kw):
        self.n += 1
        return SimpleNamespace(thread_id=f"t{self.n}")

    def transition(self, tid, state):
        pass


@pytest.mark.asyncio
async def test_active_speaker_mutex_serializes_deliberations(monkeypatch):
    monkeypatch.setenv("JARVIS_HIVE_COUNCIL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HIVE_COUNCIL_COOLDOWN_S", "0")
    cd = CouncilDeliberator(doubleword=_FakeDW(), emit_fn=lambda **k: None)
    spans = []

    async def _debate(tid):
        t0 = asyncio.get_running_loop().time()
        await asyncio.sleep(0.12)
        spans.append((t0, asyncio.get_running_loop().time()))

    cd._service = SimpleNamespace(
        thread_manager=_FakeThreadMgr(), _run_debate_round=_debate)
    await cd.start()
    cd.on_envelope(_env(intent="x"))
    cd.on_envelope(_env(intent="y"))
    await asyncio.sleep(0.5)
    await cd.stop()
    assert len(spans) == 2
    (a0, a1), (b0, b1) = sorted(spans)
    assert b0 >= a1 - 1e-3                     # strictly serial — no overlap


@pytest.mark.asyncio
async def test_deadline_brake_closes_wedged_deliberation(monkeypatch):
    """The council's stored-but-never-enforced debate deadline is REAL at
    this layer: a wedged debate is cancelled and announced."""
    monkeypatch.setenv("JARVIS_HIVE_COUNCIL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HIVE_COUNCIL_DEADLINE_S", "0.15")
    emitted = []
    cd = CouncilDeliberator(doubleword=_FakeDW(),
                            emit_fn=lambda **k: emitted.append(k))

    async def _wedged(tid):
        await asyncio.sleep(3600)

    cd._service = SimpleNamespace(
        thread_manager=_FakeThreadMgr(), _run_debate_round=_wedged)
    await cd.start()
    cd.on_envelope(_env())
    await asyncio.sleep(0.5)
    await cd.stop()
    assert cd.stats["deadline_hits"] == 1
    assert any("consensus brake" in e.get("summary", "") for e in emitted)


# ---------------------------------------------------------------------------
# the full REAL deliberation (fake DW mirroring the provider contract)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_deliberation_emits_persona_frames_and_stays_advisory(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("JARVIS_HIVE_COUNCIL_ENABLED", "true")
    emitted = []
    dw = _FakeDW()
    cd = CouncilDeliberator(doubleword=dw, emit_fn=lambda **k: emitted.append(k),
                            state_dir=Path(tmp_path))
    await cd.start()
    cd.on_envelope(_env(summary="operation terminal: fallback_failed"))
    for _ in range(80):                       # bounded wait for completion
        await asyncio.sleep(0.05)
        if cd.stats["completed"]:
            break
    await cd.stop()

    assert cd.stats["completed"] == 1
    assert len(dw.calls) == 3                  # OBSERVE → PROPOSE → VALIDATE
    personas = [e["actor_id"] for e in emitted
                if e.get("intent") not in ("thread_lifecycle",
                                           "consensus_advisory")]
    assert "persona.jarvis" in personas
    assert "persona.j_prime" in personas
    assert "persona.reactor" in personas
    assert all(e.get("subsystem") == "persona" for e in emitted)
    # consensus reached → the ADVISORY sink spoke; no real intake anywhere
    assert any(e.get("intent") == "consensus_advisory" for e in emitted)
    advisory = [e for e in emitted if e.get("intent") == "consensus_advisory"]
    assert advisory[0]["detail"]["advisory"] is True


# ---------------------------------------------------------------------------
# wiring pins
# ---------------------------------------------------------------------------

def test_host_helper_wires_the_council_behind_the_master():
    import ast as _ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    tree = _ast.parse(
        (root / "backend" / "api" / "hive_aggregator.py").read_text())
    calls = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                and node.name == "start_hive_relay":
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Call):
                    f = sub.func
                    calls.add(getattr(f, "id", getattr(f, "attr", "")))
    assert "council_enabled" in calls          # gated
    assert "CouncilDeliberator" in calls       # constructed
    assert "attach_child" in calls             # torn down with the aggregator


def test_flag_delegate_reaches_council_flags():
    from backend.core.ouroboros.governance.hive_flags import register_flags

    class _Reg:
        def __init__(self):
            self.names = []

        def register(self, spec):
            self.names.append(spec.name)

    reg = _Reg()
    n = register_flags(reg)
    assert n == 9
    assert "JARVIS_HIVE_COUNCIL_ENABLED" in reg.names
