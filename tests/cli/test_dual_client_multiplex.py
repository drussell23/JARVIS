"""Dual-client IPC multiplex spine (Campaign Step 2, 2026-07-19).

Mandate 4 verbatim: ov AND jarvis attached simultaneously; one
broadcast AUDIO_PLAYING state change updates the jarvis Agentic
Topology Map AND the ov prompt morph — no socket collision, no
dropped payloads under a frame storm.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.cockpit_attach import (
    CockpitAttachBridge,
    CockpitAttachClient,
)
from backend.core.ouroboros.cli.jarvis_thin import TopologyMap
from backend.core.ouroboros.cli.ov import AttachUI

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def sock_dir():
    d = Path(tempfile.mkdtemp(prefix="mux-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


async def _wait(cond, timeout=5.0):
    t0 = asyncio.get_running_loop().time()
    while not cond():
        if asyncio.get_running_loop().time() - t0 > timeout:
            return False
        await asyncio.sleep(0.02)
    return True


class TestDualClientMultiplex:
    async def test_one_broadcast_two_presentations_no_collision(
        self, sock_dir,
    ):
        """MANDATE 4 VERBATIM."""
        bridge = CockpitAttachBridge(path=sock_dir / "a.sock")
        assert await bridge.start()
        ov_ui = AttachUI()
        topo = TopologyMap()
        ov_client = CockpitAttachClient(
            path=sock_dir / "a.sock", on_audio_state=ov_ui.on_audio_state,
        )
        jv_client = CockpitAttachClient(
            path=sock_dir / "a.sock",
            on_hydration=topo.on_hydration,
            on_audio_state=topo.on_audio_state,
        )
        try:
            assert await ov_client.connect() and await jv_client.connect()
            assert bridge.client_count == 2            # true multiplexer
            bridge.publish_audio_state("SPEAKING")
            # BOTH clients update from the ONE broadcast:
            assert await _wait(lambda: ov_ui.audio_state == "SPEAKING")
            assert await _wait(lambda: topo.state["audio"] == "SPEAKING")
            # The caret is the LAST line: the live region (pulse + deck)
            # renders above it, so prompt() is a block rather than one string.
            # What this pins is unchanged — the audio state morphs the caret.
            assert ov_ui.prompt().splitlines()[-1] == "🗣 Karen (speaking) › "
            assert "🗣 speaking" in topo.render()               # jarvis map
            assert "lease: HELD" in topo.render()
        finally:
            await ov_client.close()
            await jv_client.close()
            await bridge.stop()

    async def test_frame_storm_no_drops_no_starvation(self, sock_dir):
        """50 rapid line-frames + interleaved state flips: every
        payload reaches BOTH subscribers, neither starves."""
        bridge = CockpitAttachBridge(path=sock_dir / "a.sock")
        assert await bridge.start()
        ov_lines, jv_lines = [], []
        ov_client = CockpitAttachClient(
            path=sock_dir / "a.sock", on_line=ov_lines.append,
        )
        jv_client = CockpitAttachClient(
            path=sock_dir / "a.sock", on_line=jv_lines.append,
        )
        try:
            assert await ov_client.connect() and await jv_client.connect()
            for i in range(50):
                bridge.publish_line(f"frame-{i}")
                if i % 10 == 0:
                    bridge.publish_audio_state(
                        "SPEAKING" if (i // 10) % 2 == 0 else "LISTENING",
                    )
            assert await _wait(lambda: len(ov_lines) == 50)
            assert await _wait(lambda: len(jv_lines) == 50)
            assert ov_lines == [f"frame-{i}" for i in range(50)]  # ordered
            assert jv_lines == ov_lines                            # equal
            assert bridge.stats["dropped"] == 0
        finally:
            await ov_client.close()
            await jv_client.close()
            await bridge.stop()

    async def test_one_dead_client_never_starves_the_other(self, sock_dir):
        bridge = CockpitAttachBridge(path=sock_dir / "a.sock")
        assert await bridge.start()
        ov_lines = []
        ov_client = CockpitAttachClient(
            path=sock_dir / "a.sock", on_line=ov_lines.append,
        )
        jv_client = CockpitAttachClient(path=sock_dir / "a.sock")
        try:
            assert await ov_client.connect() and await jv_client.connect()
            jv_client._writer.transport.abort()        # jarvis SIGKILL'd
            for i in range(10):
                bridge.publish_line(f"post-{i}")
                await asyncio.sleep(0.01)
            assert await _wait(lambda: len(ov_lines) == 10)  # ov unbothered
        finally:
            await ov_client.close()
            await jv_client.close()
            await bridge.stop()


class TestJarvisThinBoot:
    def test_import_surface_is_thin_pin(self):
        import ast
        src = (
            _REPO / "backend/core/ouroboros/cli/jarvis_thin.py"
        ).read_text()
        for node in ast.parse(src).body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                name = getattr(node, "module", "") or ""
                assert "governance.orchestrator" not in name
                assert "torch" not in name and "fastembed" not in name

    def test_entry_point_registered_pin(self):
        assert 'jarvis = "backend.core.ouroboros.cli.jarvis_thin:main"' in (
            _REPO / "pyproject.toml"
        ).read_text()

    def test_topology_renders_all_planes(self):
        topo = TopologyMap()
        topo.on_hydration({
            "status": {"phase": "GENERATE", "cost_spent_usd": 0.12,
                       "cost_budget_usd": 2.50},
            "ops": ["op-abc"], "audio": {"state": "LISTENING"},
        })
        out = topo.render()
        assert "GENERATE" in out and "$0.12/$2.50" in out
        assert "op-abc" in out and "🎙 listening" in out
        assert "Daniel" in out and "Karen" in out


class TestQuarantineSymbolNet:
    async def test_migrated_supervisor_zone_revives_with_beacon(self, caplog):
        """The REAL Slice-1 payload: OSResourceQuotaMonitor was
        physically migrated; the symbol net revives it with the
        breach beacon."""
        import logging
        from backend.core.quarantine_loader import attach_symbol_net
        fake_globals = {"__name__": "unified_supervisor_test_facade"}
        attach_symbol_net(fake_globals, {
            "OSResourceQuotaMonitor":
                "backend.core.quarantine.supervisor_slice1:"
                "OSResourceQuotaMonitor",
        })
        with caplog.at_level(logging.CRITICAL, logger="Ouroboros.Quarantine"):
            cls = fake_globals["__getattr__"]("OSResourceQuotaMonitor")
        assert cls.__name__ == "OSResourceQuotaMonitor"
        assert any("[QUARANTINE_BREACH]" in r.message for r in caplog.records)
        # Cached back — second access is beacon-free:
        assert fake_globals["OSResourceQuotaMonitor"] is cls

    def test_supervisor_carries_symbol_net_pin(self):
        src_tail = (_REPO / "unified_supervisor.py").read_text()[-3000:]
        assert "attach_symbol_net" in src_tail
        assert "OSResourceQuotaMonitor" in src_tail
