# -*- coding: utf-8 -*-
"""GenerationFence (Stage-4 Task 4): the Brain-side split-brain ACTIVE fence.

Contract under proof:

  (a) A ``console.keeper_heartbeat`` carrying a HIGHER gen than the fence's
      own triggers the deterministic ordered transition -- boundary arm ->
      shutdown request -> checkpoint capture -> idle touch -- EXACTLY ONCE
      (a second higher-gen observation no-ops: idempotent latch).
  (b) Equal / lower gen -> zero arms.
  (c) Malformed payload (missing gen, non-int gen) -> ignored, no raise.
  (d) One arm raising -> the remaining arms are STILL attempted (fail-soft
      per step, deterministic transition).
  (e) AST pin: generation_fence.py imports no LLM/provider machinery --
      pure code, no model in the fence path.
  (f) organism_bus_host wiring: JARVIS_BRAIN_GENERATION set (>0) -> a fence
      is constructed with own_gen and started (and stopped in stop());
      env absent/invalid -> nothing constructed (byte-identical).
  (g) CHOKEPOINT INTEGRATION (review Critical fix): a REAL fence trip
      (real-bus heartbeat) sets the process-global latch, after which the
      REAL ``ChangeEngine.execute`` refuses a tmp-file mutation with
      ``POLICY_DENIED reason=generation_fenced`` and the REAL
      ``AutoCommitter.commit`` skips with ``generation_fenced`` -- and an
      unfenced (reset) engine mutates normally.

(a)-(d),(g) run against a REAL TrinityEventBus (the fence's subscribe path
is real); TRINITY_MULTICAST_ENABLED=false suppresses the in-process UDP
shortcut (precedent: test_trinity_bus_bridge.py::_mk_bus). Delivery waits
poll ``bus._metrics.events_delivered`` -- incremented only AFTER every
matching handler completed (trinity_event_bus._process_queue), so the
waits are deterministic, not sleep-guesses. All publishes use
``persist=False`` (no event-store WAL writes from a test).
"""
from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from typing import Any, List

import pytest

import backend.core.ouroboros.governance.generation_fence as gf
from backend.core.ouroboros.governance.generation_fence import (
    HEARTBEAT_TOPIC,
    GenerationFence,
)
from backend.core.trinity_event_bus import RepoType, TrinityEventBus


@pytest.fixture(autouse=True)
def _clean_latch():
    """The chokepoint latch is process-global (one-way in production) --
    every test starts and ends unfenced so a trip never leaks across tests."""
    gf._reset_for_tests()
    yield
    gf._reset_for_tests()

_FENCE_SOURCE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "backend", "core", "ouroboros", "governance", "generation_fence.py",
)


async def _mk_bus() -> TrinityEventBus:
    """A REAL bus with the in-process UDP multicast artifact suppressed
    (rationale + precedent: test_trinity_bus_bridge.py::_mk_bus)."""
    prev = os.environ.get("TRINITY_MULTICAST_ENABLED")
    os.environ["TRINITY_MULTICAST_ENABLED"] = "false"
    try:
        return await TrinityEventBus.create(local_repo=RepoType.JARVIS)
    finally:
        if prev is None:
            os.environ.pop("TRINITY_MULTICAST_ENABLED", None)
        else:
            os.environ["TRINITY_MULTICAST_ENABLED"] = prev


async def _await_delivered(bus: TrinityEventBus, count: int,
                           timeout_s: float = 5.0) -> None:
    """Wait until the bus has fully delivered ``count`` events (every
    matching handler awaited -- the counter increments after the gather)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if bus._metrics.events_delivered >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        "bus never delivered %d event(s) (delivered=%d)"
        % (count, bus._metrics.events_delivered))


def _recorders(order: List[str], *, boom: str = ""):
    """Four recording arm seams appending their name to ``order``; the one
    named by ``boom`` raises AFTER recording (fail-soft proof)."""
    def _mk(name: str):
        def _arm() -> None:
            order.append(name)
            if name == boom:
                raise RuntimeError("%s arm exploded" % name)
        return _arm
    return dict(
        boundary_arm_fn=_mk("boundary"),
        shutdown_fn=_mk("shutdown"),
        capture_fn=_mk("capture"),
        idle_touch_fn=_mk("idle"),
    )


# --------------------------------------------------------------------------- #
# (a) higher gen -> ordered transition, exactly once
# --------------------------------------------------------------------------- #
def test_higher_gen_fences_all_four_arms_in_order_exactly_once():
    async def scenario() -> List[str]:
        bus = await _mk_bus()
        try:
            order: List[str] = []
            fence = GenerationFence(bus, 2, **_recorders(order))
            await fence.start()

            await bus.publish_raw(
                HEARTBEAT_TOPIC, {"gen": 3}, persist=False)
            await _await_delivered(bus, 1)
            assert fence.fenced is True
            # Arm 0 -- the process-global chokepoint latch -- is intrinsic:
            # it fires even with all four graceful arms injected.
            assert gf.is_fenced() is True
            assert gf.fence_reason() == "generation_fenced"

            # Second higher-gen observation (distinct payload -- the bus
            # dedups identical fingerprints) MUST no-op: idempotent latch.
            await bus.publish_raw(
                HEARTBEAT_TOPIC, {"gen": 4}, persist=False)
            await _await_delivered(bus, 2)

            await fence.stop()
            return order
        finally:
            await bus.stop()

    order = asyncio.run(scenario())
    assert order == ["boundary", "shutdown", "capture", "idle"], (
        "the transition must run all four arms IN ORDER exactly once: %r"
        % order)


# --------------------------------------------------------------------------- #
# (b) equal / lower gen -> zero arms
# --------------------------------------------------------------------------- #
def test_equal_and_lower_gen_never_fence():
    async def scenario() -> List[str]:
        bus = await _mk_bus()
        try:
            order: List[str] = []
            fence = GenerationFence(bus, 3, **_recorders(order))
            await fence.start()
            await bus.publish_raw(
                HEARTBEAT_TOPIC, {"gen": 3}, persist=False)  # equal
            await bus.publish_raw(
                HEARTBEAT_TOPIC, {"gen": 1}, persist=False)  # lower
            await _await_delivered(bus, 2)
            assert fence.fenced is False
            assert gf.is_fenced() is False, (
                "equal/lower gen must never set the chokepoint latch")
            await fence.stop()
            return order
        finally:
            await bus.stop()

    assert asyncio.run(scenario()) == []


# --------------------------------------------------------------------------- #
# (c) malformed payloads -> ignored, no raise
# --------------------------------------------------------------------------- #
def test_malformed_heartbeats_are_ignored():
    async def scenario() -> List[str]:
        bus = await _mk_bus()
        try:
            order: List[str] = []
            fence = GenerationFence(bus, 1, **_recorders(order))
            await fence.start()
            await bus.publish_raw(
                HEARTBEAT_TOPIC, {"note": "no gen key"}, persist=False)
            await bus.publish_raw(
                HEARTBEAT_TOPIC, {"gen": "not-an-int"}, persist=False)
            await bus.publish_raw(
                HEARTBEAT_TOPIC, {"gen": None}, persist=False)
            await _await_delivered(bus, 3)
            assert fence.fenced is False
            await fence.stop()
            return order
        finally:
            await bus.stop()

    assert asyncio.run(scenario()) == []


# --------------------------------------------------------------------------- #
# (d) one arm raising -> the rest still attempted
# --------------------------------------------------------------------------- #
def test_failing_arm_does_not_stop_the_remaining_arms():
    async def scenario() -> List[str]:
        bus = await _mk_bus()
        try:
            order: List[str] = []
            fence = GenerationFence(bus, 2, **_recorders(order, boom="boundary"))
            await fence.start()
            await bus.publish_raw(
                HEARTBEAT_TOPIC, {"gen": 9}, persist=False)
            await _await_delivered(bus, 1)
            assert fence.fenced is True
            await fence.stop()
            return order
        finally:
            await bus.stop()

    order = asyncio.run(scenario())
    assert order == ["boundary", "shutdown", "capture", "idle"], (
        "a raising arm must not stop the remaining arms: %r" % order)


# --------------------------------------------------------------------------- #
# (e) AST pin: no LLM/provider imports in the fence module
# --------------------------------------------------------------------------- #
def test_ast_pin_no_llm_or_provider_imports():
    """The fence is pure code -- Mandate: deterministic, no LLM. Any import
    (top-level OR function-local) of provider/LLM machinery fails this pin."""
    forbidden = (
        "providers", "doubleword", "candidate_generator", "anthropic",
        "openai", "llm", "tool_executor", "plan_generator",
    )
    with open(_FENCE_SOURCE, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    imported: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imported.append(mod)
            imported.extend("%s.%s" % (mod, alias.name) for alias in node.names)
    bad = [name for name in imported
           if any(f in name.lower() for f in forbidden)]
    assert not bad, (
        "generation_fence.py must import no LLM/provider machinery: %r" % bad)


# --------------------------------------------------------------------------- #
# (f) organism_bus_host wiring: env-gated construction
# --------------------------------------------------------------------------- #
class _RecordingFence:
    instances: List["_RecordingFence"] = []

    def __init__(self, bus: Any, own_gen: int, **kwargs: Any) -> None:
        self.bus = bus
        self.own_gen = own_gen
        self.started = False
        self.stopped = False
        _RecordingFence.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def _patched_host_module(monkeypatch):
    """Patch the lazy-import target: organism_bus_host does
    ``from ... import generation_fence`` then ``generation_fence.
    GenerationFence`` -- patching the module attribute intercepts
    construction (the test_organism_bus_host.py router-seam precedent)."""
    import backend.core.ouroboros.governance.generation_fence as gf
    monkeypatch.setattr(gf, "GenerationFence", _RecordingFence)
    _RecordingFence.instances = []
    from backend.core.ouroboros.governance.transport.organism_bus_host import (
        OrganismBusHost,
    )
    return OrganismBusHost


def test_host_arms_fence_when_generation_env_set(monkeypatch):
    OrganismBusHost = _patched_host_module(monkeypatch)
    monkeypatch.setenv("JARVIS_BRAIN_GENERATION", "7")
    host = OrganismBusHost()
    fake_bus = object()

    async def scenario() -> None:
        await host._maybe_start_generation_fence(fake_bus)
        assert len(_RecordingFence.instances) == 1
        fence = _RecordingFence.instances[0]
        assert fence.bus is fake_bus, "the fence must ride the ORGANISM bus"
        assert fence.own_gen == 7
        assert fence.started is True
        # stop() must unwind the fence.
        await host.stop()
        assert fence.stopped is True
        assert host._generation_fence is None

    asyncio.run(scenario())


def test_host_stays_dark_when_generation_env_absent_or_invalid(monkeypatch):
    OrganismBusHost = _patched_host_module(monkeypatch)
    host = OrganismBusHost()

    async def scenario() -> None:
        for value in (None, "", "  ", "0", "-3", "abc"):
            if value is None:
                monkeypatch.delenv("JARVIS_BRAIN_GENERATION", raising=False)
            else:
                monkeypatch.setenv("JARVIS_BRAIN_GENERATION", value)
            await host._maybe_start_generation_fence(object())
            assert host._generation_fence is None, (
                "env %r must not arm a fence" % (value,))
        assert _RecordingFence.instances == [], (
            "no fence may be constructed without a positive generation")

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (g) chokepoint integration: a REAL trip denies the REAL mutation surfaces
# --------------------------------------------------------------------------- #
def test_real_fence_trip_denies_change_engine_and_auto_committer(
        tmp_path, monkeypatch):
    """The test the review demanded: after a REAL fence trip (real-bus
    heartbeat -> arm-0 latch), the REAL ChangeEngine refuses an APPLY write
    and the REAL AutoCommitter refuses a commit -- proving the fence
    structurally terminates the mutation pathways for a CURRENT process,
    mid-flight ops included (Mandate 2). Reset -> the same engine mutates
    normally (zero behavior change unfenced)."""
    from backend.core.ouroboros.governance.auto_committer import AutoCommitter
    from backend.core.ouroboros.governance.change_engine import (
        ChangeEngine,
        ChangeRequest,
    )
    from backend.core.ouroboros.governance.ledger import OperationLedger
    from backend.core.ouroboros.governance.risk_engine import (
        ChangeType,
        OperationProfile,
    )

    # Writes must land under tmp_path, not a leaked harness workspace.
    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "app.py"

    def _request(op_id: str) -> ChangeRequest:
        # SAFE_AUTO profile so an UNfenced op reaches the APPLY write
        # (the test_change_engine_chokepoint.py construction precedent).
        return ChangeRequest(
            goal="generation fence chokepoint integration",
            target_file=target,
            proposed_content="x = 1\n",
            profile=OperationProfile(
                files_affected=[target],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=1.0,
            ),
            op_id=op_id,
        )

    async def scenario() -> None:
        # 1. REAL fence trip: real bus, real subscribe path, real heartbeat.
        bus = await _mk_bus()
        try:
            order: List[str] = []
            fence = GenerationFence(bus, 1, **_recorders(order))
            await fence.start()
            await bus.publish_raw(HEARTBEAT_TOPIC, {"gen": 2}, persist=False)
            await _await_delivered(bus, 1)
            assert gf.is_fenced() is True, "the trip must set the arm-0 latch"
            await fence.stop()
        finally:
            await bus.stop()

        # 2. REAL ChangeEngine refuses the mutation at its chokepoint.
        engine = ChangeEngine(
            project_root=tmp_path,
            ledger=OperationLedger(storage_dir=tmp_path / "ledger"),
        )
        res = await engine.execute(_request("op-fenced"))
        assert res.success is False
        assert res.error == "POLICY_DENIED reason=generation_fenced"
        assert not target.exists(), "a fenced APPLY must never touch disk"

        # 3. REAL AutoCommitter refuses at its entry -- FIRST, even before
        #    the no_target_files / disabled guards (gate precedence pin).
        committer = AutoCommitter(tmp_path)
        cres = await committer.commit(
            op_id="op-fenced", description="fenced", target_files=())
        assert cres.committed is False
        assert cres.skipped_reason == "generation_fenced"

        # 4. Unfenced (latch reset) -> the SAME engine mutates normally.
        gf._reset_for_tests()
        res2 = await engine.execute(_request("op-ok"))
        assert res2.success is True, res2.error
        assert target.exists() and "x = 1" in target.read_text()
        # ... and the commit gate is passed cleanly: the NEXT guard answers
        # (env-dependent disabled/no-files), never the fence denial.
        cres2 = await committer.commit(
            op_id="op-ok", description="unfenced", target_files=())
        assert cres2.skipped_reason in (
            "auto_commit_disabled", "no_target_files")

    asyncio.run(scenario())
