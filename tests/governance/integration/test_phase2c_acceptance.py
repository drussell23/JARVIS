"""
Phase 2C.1 acceptance tests.

Acceptance criteria (ACs):
AC1: All four sensors produce IntentEnvelope(schema_version="2c.1")
AC2: Sensor D (ai_miner) envelopes always have requires_human_ack=True
AC3: Router routes voice_human at higher priority than backlog
AC4: WAL persists pending entries; router replays on restart
AC5: Deduplicated envelopes never reach GLS.submit()
AC6: Causal chain flows: causal_id → OperationContext.op_id
AC7: Human ACK gate: pending_ack envelopes dispatched only after acknowledge()
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake import (
    IntentEnvelope,
    make_envelope,
    UnifiedIntakeRouter,
    IntakeRouterConfig,
    SCHEMA_VERSION,
)
from backend.core.ouroboros.governance.intake.sensors import (
    BacklogSensor,
    TestFailureSensor,
    VoiceCommandSensor,
    VoiceCommandPayload,
    OpportunityMinerSensor,
)
from backend.core.ouroboros.governance.intent.signals import IntentSignal


# ── AC1: All sensors produce schema_version = "2c.1" ─────────────────────────

async def test_ac1_backlog_sensor_schema_version(tmp_path):
    bp = tmp_path / ".jarvis" / "backlog.json"
    bp.parent.mkdir(parents=True, exist_ok=True)
    bp.write_text(json.dumps([{
        "task_id": "t1", "description": "fix x",
        "target_files": ["backend/core/x.py"],
        "priority": 3, "repo": "jarvis", "status": "pending",
    }]))
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = BacklogSensor(backlog_path=bp, repo_root=tmp_path, router=router)
    envelopes = await sensor.scan_once()
    assert len(envelopes) == 1
    assert envelopes[0].schema_version == SCHEMA_VERSION


async def test_ac1_test_failure_sensor_schema_version():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = TestFailureSensor(repo="jarvis", router=router)
    sig = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_x.py",),
        repo="jarvis",
        description="Stable failure: test_x",
        evidence={"signature": "err:test_x.py", "test_id": "tests/test_x.py::test_x"},
        confidence=0.8,
        stable=True,
    )
    env = await sensor._signal_to_envelope_and_ingest(sig)
    assert env is not None
    assert env.schema_version == SCHEMA_VERSION


async def test_ac1_voice_command_sensor_schema_version():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = VoiceCommandSensor(router=router, repo="jarvis")
    await sensor.handle_voice_command(VoiceCommandPayload(
        description="fix the auth module",
        target_files=["backend/core/auth.py"],
        repo="jarvis",
        stt_confidence=0.95,
    ))
    env = router.ingest.call_args.args[0]
    assert env.schema_version == SCHEMA_VERSION


# ── AC2: Sensor D always requires_human_ack=True ─────────────────────────────

async def test_ac2_miner_always_requires_human_ack(tmp_path):
    src = tmp_path / "complex.py"
    lines = ["def foo(x):\n"] + [f"    if x=={i}: return {i}\n" for i in range(15)] + ["    return -1\n"]
    src.write_text("".join(lines))
    router = MagicMock()
    router.ingest = AsyncMock(return_value="pending_ack")
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path, router=router,
        scan_paths=["."], complexity_threshold=5,
    )
    candidates = await sensor.scan_once()
    assert len(candidates) >= 1
    # The envelopes are the first positional arg passed to router.ingest
    ingested_envelopes = [call.args[0] for call in router.ingest.call_args_list]
    assert len(ingested_envelopes) >= 1
    for env in ingested_envelopes:
        assert env.requires_human_ack is True


# ── AC5: Deduplicated envelopes never reach GLS.submit() ─────────────────────

async def test_ac5_dedup_prevents_double_submit(tmp_path):
    submitted_op_ids = []

    async def mock_submit(ctx, trigger_source=""):
        submitted_op_ids.append(ctx.op_id)

    gls = MagicMock()
    gls.submit = mock_submit

    config = IntakeRouterConfig(project_root=tmp_path, dedup_window_s=60.0)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    env1 = make_envelope(
        source="backlog", description="fix y",
        target_files=("backend/y.py",), repo="jarvis",
        confidence=0.8, urgency="normal",
        evidence={"signature": "ac5_sig"},
        requires_human_ack=False,
    )
    env2 = make_envelope(
        source="backlog", description="fix y",
        target_files=("backend/y.py",), repo="jarvis",
        confidence=0.8, urgency="normal",
        evidence={"signature": "ac5_sig"},
        requires_human_ack=False,
    )
    assert env1.dedup_key == env2.dedup_key

    r1 = await router.ingest(env1)
    r2 = await router.ingest(env2)
    assert r1 == "enqueued"
    assert r2 == "deduplicated"

    await asyncio.sleep(0.15)
    await router.stop()
    assert len(submitted_op_ids) == 1


# ── AC6: Causal chain: causal_id → OperationContext.op_id ────────────────────

async def test_ac6_causal_id_becomes_op_id(tmp_path):
    captured_op_ids = []

    async def mock_submit(ctx, trigger_source=""):
        captured_op_ids.append(ctx.op_id)

    gls = MagicMock()
    gls.submit = mock_submit

    config = IntakeRouterConfig(project_root=tmp_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    env = make_envelope(
        source="voice_human", description="fix auth now",
        target_files=("backend/core/auth.py",), repo="jarvis",
        confidence=0.95, urgency="critical",
        evidence={"signature": "causal_chain_test"},
        requires_human_ack=False,
    )
    await router.ingest(env)
    await asyncio.sleep(0.15)
    await router.stop()

    assert len(captured_op_ids) == 1
    # OperationContext.op_id must equal IntentEnvelope.causal_id
    assert captured_op_ids[0] == env.causal_id


# ── AC7: Human ACK gate ───────────────────────────────────────────────────────

async def test_ac7_human_ack_gate(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeRouterConfig(project_root=tmp_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    env = make_envelope(
        source="ai_miner", description="miner candidate",
        target_files=("backend/core/complex.py",), repo="jarvis",
        confidence=0.4, urgency="low",
        evidence={"signature": "ac7_test"},
        requires_human_ack=True,
    )
    result = await router.ingest(env)
    assert result == "pending_ack"
    assert router.pending_ack_count() == 1

    await asyncio.sleep(0.05)
    gls.submit.assert_not_called()  # Not dispatched yet

    released = await router.acknowledge(env.idempotency_key)
    assert released is True

    await asyncio.sleep(0.15)
    await router.stop()
    gls.submit.assert_called_once()
