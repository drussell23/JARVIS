"""UMF integration test -- full pipeline (Task 21)."""
import asyncio
import time
import pytest


class TestUmfFullPipeline:

    @pytest.mark.asyncio
    async def test_command_ack_roundtrip(self, tmp_path):
        from backend.core.umf.client import UmfClient

        # Supervisor client
        supervisor = UmfClient(
            repo="jarvis", component="supervisor",
            instance_id="i-sup", session_id="s-1",
            dedup_db_path=tmp_path / "sup-dedup.db",
        )
        await supervisor.start()

        # Track received commands
        commands = []
        await supervisor.subscribe("command", lambda m: commands.append(m))

        # Publish a command
        result = await supervisor.publish_command(
            target_repo="jarvis-prime", target_component="orch",
            payload={"action": "start_training"},
        )
        assert result.delivered is True
        await asyncio.sleep(0.05)
        assert len(commands) == 1

        await supervisor.stop()

    @pytest.mark.asyncio
    async def test_heartbeat_updates_projection(self, tmp_path):
        from backend.core.umf.client import UmfClient
        from backend.core.umf.heartbeat_projection import HeartbeatProjection

        client = UmfClient(
            repo="jarvis-prime", component="orchestrator",
            instance_id="i-1", session_id="s-1",
            dedup_db_path=tmp_path / "dedup.db",
        )
        await client.start()

        projection = HeartbeatProjection(stale_timeout_s=30.0)
        await client.subscribe("lifecycle", lambda m: projection.ingest(m))

        await client.send_heartbeat(state="ready", liveness=True, readiness=True)
        await asyncio.sleep(0.05)

        state = projection.get_state("orchestrator")
        assert state is not None
        assert state["state"] == "ready"
        assert state["liveness"] is True
        await client.stop()

    @pytest.mark.asyncio
    async def test_expired_message_not_delivered(self, tmp_path):
        from backend.core.umf.client import UmfClient
        from backend.core.umf.types import UmfMessage, MessageSource, MessageTarget

        client = UmfClient(
            repo="jarvis", component="sup",
            instance_id="i", session_id="s",
            dedup_db_path=tmp_path / "dedup.db",
        )
        await client.start()

        received = []
        await client.subscribe("command", lambda m: received.append(m))

        # Manually create an expired message
        msg = UmfMessage(
            stream="command", kind="command",
            source=MessageSource(repo="jarvis", component="sup",
                                 instance_id="i", session_id="s"),
            target=MessageTarget(repo="jarvis-prime", component="orch"),
            payload={"expired": True},
            routing_ttl_ms=1,
            observed_at_unix_ms=int((time.time() - 60) * 1000),
        )
        result = await client._engine.publish(msg)
        assert result.delivered is False
        assert result.reject_reason == "ttl_expired"
        await client.stop()
