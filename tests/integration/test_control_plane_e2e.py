# tests/integration/test_control_plane_e2e.py
"""Integration test: full control plane bootstrap → lifecycle → shutdown."""

import asyncio
import os
import tempfile
import pytest
from pathlib import Path


@pytest.fixture
def short_tmp_path(request):
    """Short temp directory for macOS AF_UNIX 104-byte sun_path limit."""
    short_base = tempfile.mkdtemp(prefix="jt_", dir="/tmp")
    p = Path(short_base)
    import shutil
    request.addfinalizer(lambda: shutil.rmtree(str(p), ignore_errors=True))
    return p


class TestControlPlaneE2E:
    async def test_journal_to_engine_to_fabric(self, short_tmp_path):
        """Full flow: journal init → lease → engine start → UDS emit → shutdown."""
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.lifecycle_engine import (
            LifecycleEngine, ComponentDeclaration, ComponentLocality,
        )
        from backend.core.uds_event_fabric import EventFabric

        db_path = short_tmp_path / "orchestration.db"
        sock_path = short_tmp_path / "control.sock"

        # 1. Initialize journal
        journal = OrchestrationJournal()
        await journal.initialize(db_path)

        # 2. Acquire lease
        ok = await journal.acquire_lease(f"test:{os.getpid()}:e2e")
        assert ok is True

        # 3. Start event fabric
        fabric = EventFabric(journal)
        await fabric.start(sock_path)

        # 4. Create engine with simple components
        components = (
            ComponentDeclaration(
                name="test_a", locality=ComponentLocality.IN_PROCESS,
                is_critical=True,
            ),
            ComponentDeclaration(
                name="test_b", locality=ComponentLocality.IN_PROCESS,
                dependencies=("test_a",),
            ),
        )
        engine = LifecycleEngine(journal, components)

        # 5. Simulate lifecycle
        await engine.transition_component("test_a", "STARTING", reason="test")
        await engine.transition_component("test_a", "HANDSHAKING", reason="test")
        await engine.transition_component("test_a", "READY", reason="test")

        assert engine.get_status("test_a") == "READY"

        # 6. Verify journal contains transitions
        entries = await journal.replay_from(0, action_filter=["state_transition"])
        targets = [e["target"] for e in entries]
        assert "test_a" in targets

        # 7. Shutdown — only test_a is READY; test_b is still REGISTERED
        #    shutdown_all drains+stops active components (READY/DEGRADED/STARTING/HANDSHAKING)
        await engine.shutdown_all("test_complete")
        assert engine.get_status("test_a") == "STOPPED"

        await fabric.stop()
        await journal.close()
