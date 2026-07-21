"""ov doctor — the 8-edge connectivity matrix (Slice B) + --live loop (C)."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.cli import ov_doctor
from backend.core.ouroboros.cli.ov_doctor import (
    DoctorReport, EdgeState, EdgeVerdict, _verdicts_from_hydration,
)


@pytest.fixture()
def sock_dir():
    d = Path(tempfile.mkdtemp(prefix="ovdoc-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# hydration-derived edges (3, 4, 6)
# ---------------------------------------------------------------------------


def _payload(**over):
    base = {
        "type": "hydration",
        "status": {"hydration": {"subsystems": {"governance_bridge": "ok",
                                                "ouroboros_daemon": "ok"},
                                 "loaded": 2, "total": 2}},
        "fabrics": {"trinity_subs": 10, "sse": True, "emitter": True,
                    "stats": {}},
        "liquidity": {"providers": {"anthropic": {"tokens_remaining": 11978000},
                                    "doubleword": {"tokens_remaining": 500000}},
                      "any_exhausted": False},
    }
    base.update(over)
    return base


def test_full_hydration_yields_three_ok_edges():
    e3, e4, e6 = _verdicts_from_hydration(_payload())
    assert (e3.state, e4.state, e6.state) == (
        EdgeState.OK, EdgeState.OK, EdgeState.OK)
    assert "trinity_subs=10" in e4.detail
    assert "anthropic:11,978,000" in e6.detail


def test_trinity_pending_is_degraded_not_severed():
    """trinity_subs=0 = the lazy-bus window — degraded with the re-attach
    note, never a red herring."""
    p = _payload(fabrics={"trinity_subs": 0, "sse": True, "emitter": True})
    _, e4, _ = _verdicts_from_hydration(p)
    assert e4.state is EdgeState.DEGRADED
    assert "re-attach pending" in e4.detail


def test_exhausted_runway_degrades_providers():
    p = _payload(liquidity={"providers": {"anthropic":
                                          {"tokens_remaining": 0}},
                            "any_exhausted": True})
    _, _, e6 = _verdicts_from_hydration(p)
    assert e6.state is EdgeState.DEGRADED
    assert "RUNWAY EXHAUSTED" in e6.detail


def test_no_hydration_severs_edge3_and_skips_dependents():
    e3, e4, e6 = _verdicts_from_hydration(None)
    assert e3.state is EdgeState.SEVERED
    assert e4.state is EdgeState.SKIPPED
    assert e6.state is EdgeState.SKIPPED


# ---------------------------------------------------------------------------
# report semantics
# ---------------------------------------------------------------------------


def test_exit_codes_and_first_broken_ordering():
    r = DoctorReport(verdicts=[
        EdgeVerdict("1 process", EdgeState.OK),
        EdgeVerdict("2 cockpit UDS", EdgeState.OK),
    ])
    assert r.exit_code == 0 and r.first_broken is None
    r.verdicts.append(EdgeVerdict("5 sensors", EdgeState.DEGRADED, "x"))
    assert r.exit_code == 1 and r.first_broken.edge == "5 sensors"
    r.verdicts.insert(1, EdgeVerdict("2 cockpit UDS", EdgeState.SEVERED, "y"))
    assert r.exit_code == 2
    assert r.first_broken.edge == "2 cockpit UDS"   # FIRST broken edge named


def test_absent_optional_surfaces_stay_green():
    """ABSENT (unconfigured MCP, no channel server) must not fail the chain."""
    r = DoctorReport(verdicts=[
        EdgeVerdict("1 process", EdgeState.OK),
        EdgeVerdict("8 mcp servers", EdgeState.ABSENT, "unset"),
    ])
    assert r.exit_code == 0


# ---------------------------------------------------------------------------
# the matrix: gating + parallel fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matrix_severed_socket_skips_uds_edges_probes_independents(
    monkeypatch,
):
    async def _proc():
        return EdgeVerdict("1 process", EdgeState.SEVERED, "none")

    async def _sock():
        return EdgeVerdict("2 cockpit UDS", EdgeState.SEVERED, "absent"), "absent"

    async def _sensors():
        return EdgeVerdict("5 sensors", EdgeState.ABSENT, "no server")

    async def _liveness():
        return EdgeVerdict("7 liveness", EdgeState.ABSENT, "no server")

    async def _mcp():
        return EdgeVerdict("8 mcp servers", EdgeState.ABSENT, "unset")

    monkeypatch.setattr(ov_doctor, "probe_edge_process", _proc)
    monkeypatch.setattr(ov_doctor, "probe_edge_socket", _sock)
    monkeypatch.setattr(ov_doctor, "probe_edge_sensors", _sensors)
    monkeypatch.setattr(ov_doctor, "probe_edge_liveness", _liveness)
    monkeypatch.setattr(ov_doctor, "probe_edge_mcp", _mcp)
    report = await ov_doctor.run_matrix()
    assert len(report.verdicts) == 8
    by_name = {v.edge: v for v in report.verdicts}
    assert by_name["3 hydration"].state is EdgeState.SKIPPED
    assert by_name["4 hive fabrics"].state is EdgeState.SKIPPED
    assert by_name["6 providers"].state is EdgeState.SKIPPED
    assert report.exit_code == 2
    assert report.first_broken.edge == "1 process"


@pytest.mark.asyncio
async def test_matrix_probes_independent_edges_concurrently(monkeypatch):
    """Mandate 1: the independent probes overlap under ONE gather — total
    wall-clock ≈ the slowest single probe, never the sum."""
    DELAY = 0.15
    active = {"n": 0, "peak": 0}

    def _slow(name, state=EdgeState.OK):
        async def _p():
            active["n"] += 1
            active["peak"] = max(active["peak"], active["n"])
            await asyncio.sleep(DELAY)
            active["n"] -= 1
            return EdgeVerdict(name, state, "")
        return _p

    async def _sock():
        return EdgeVerdict("2 cockpit UDS", EdgeState.OK, "served"), "live"

    async def _hyd():
        active["n"] += 1
        active["peak"] = max(active["peak"], active["n"])
        await asyncio.sleep(DELAY)
        active["n"] -= 1
        return _payload()

    monkeypatch.setattr(ov_doctor, "probe_edge_process",
                        _slow("1 process"))
    monkeypatch.setattr(ov_doctor, "probe_edge_socket", _sock)
    monkeypatch.setattr(ov_doctor, "_read_hydration", _hyd)
    monkeypatch.setattr(ov_doctor, "probe_edge_sensors", _slow("5 sensors"))
    monkeypatch.setattr(ov_doctor, "probe_edge_liveness", _slow("7 liveness"))
    monkeypatch.setattr(ov_doctor, "probe_edge_mcp", _slow("8 mcp servers"))

    t0 = asyncio.get_running_loop().time()
    report = await ov_doctor.run_matrix()
    elapsed = asyncio.get_running_loop().time() - t0
    assert len(report.verdicts) == 8
    assert active["peak"] >= 3            # probes genuinely overlapped
    assert elapsed < DELAY * 4            # not sequential (4 slow probes)


# ---------------------------------------------------------------------------
# --live: the client half against a scripted daemon
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_probe_observes_synthetic_frame(sock_dir, monkeypatch):
    """A scripted cockpit: hydration on accept; on '/doctor probe' input it
    replays the daemon's real behavior (verdict line + synthetic hive
    frame). The doctor must catch the frame and verdict OK."""
    path = sock_dir / "cockpit_attach.sock"
    monkeypatch.setenv("JARVIS_ATTACH_IPC_SOCKET", str(path))
    monkeypatch.setenv("JARVIS_DOCTOR_LIVE_WAIT_S", "5")

    async def _daemon(reader, writer):
        writer.write(
            b'{"type":"hydration","status":{},"fabrics":{"sse":true}}\n')
        line = await reader.readline()
        frame = json.loads(line.decode())
        assert frame == {"type": "input", "text": "/doctor probe"}
        writer.write(
            b'{"type":"line","text":"[doctor] synthetic probe firing"}\n')
        hive_frame = {
            "type": "telemetry", "hive": True, "subsystem": "web",
            "actor_id": "web.search",
            "action_summary": "[synthetic probe] web_search success 900ms",
            "detail": {"trace_class": "synthetic_probe"},
        }
        writer.write((json.dumps(hive_frame) + "\n").encode())
        await writer.drain()

    server = await asyncio.start_unix_server(_daemon, path=str(path))
    try:
        v = await ov_doctor.run_live_probe()
        assert v.state is EdgeState.OK
        assert "synthetic actor_edge frame observed" in v.detail
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_live_probe_degrades_when_probe_ran_but_no_frame(
    sock_dir, monkeypatch,
):
    path = sock_dir / "cockpit_attach.sock"
    monkeypatch.setenv("JARVIS_ATTACH_IPC_SOCKET", str(path))
    monkeypatch.setenv("JARVIS_DOCTOR_LIVE_WAIT_S", "1")

    async def _daemon(reader, writer):
        writer.write(
            b'{"type":"hydration","status":{},"fabrics":{"sse":true}}\n')
        await reader.readline()
        writer.write(b'{"type":"line","text":"[doctor] probe x: status=y"}\n')
        await writer.drain()
        await asyncio.sleep(2)

    server = await asyncio.start_unix_server(_daemon, path=str(path))
    try:
        v = await ov_doctor.run_live_probe()
        assert v.state is EdgeState.DEGRADED
        assert "no synthetic actor_edge frame" in v.detail
    finally:
        server.close()
        await server.wait_closed()


def test_render_never_raises_on_minimal_console():
    class _C:
        def print(self, *a, **k):
            pass

    ov_doctor.render_report(_C(), DoctorReport(verdicts=[
        EdgeVerdict("1 process", EdgeState.OK, "pid 1")]))


@pytest.mark.asyncio
async def test_live_probe_capability_gated_on_old_daemon(sock_dir, monkeypatch):
    """An old daemon (no fabrics signature in hydration) gets NOTHING
    injected — instant SKIPPED with the restart remedy, no blind wait."""
    path = sock_dir / "cockpit_attach.sock"
    monkeypatch.setenv("JARVIS_ATTACH_IPC_SOCKET", str(path))
    received = []

    async def _old_daemon(reader, writer):
        writer.write(b'{"type":"hydration","status":{}}\n')   # no fabrics
        await writer.drain()
        line = await reader.readline()
        if line:
            received.append(line)

    server = await asyncio.start_unix_server(_old_daemon, path=str(path))
    try:
        t0 = asyncio.get_running_loop().time()
        v = await ov_doctor.run_live_probe()
        elapsed = asyncio.get_running_loop().time() - t0
        assert v.state is EdgeState.SKIPPED
        assert "predates /doctor probe" in v.detail
        assert elapsed < 3.0                    # instant, not a 45s wait
        assert received == []                   # NOTHING was injected
    finally:
        server.close()
        await server.wait_closed()


def test_version_skew_synthesizer_names_the_remedy():
    lines = []

    class _C:
        def print(self, msg, **kw):
            lines.append(str(msg))

    ov_doctor.render_report(_C(), DoctorReport(verdicts=[
        EdgeVerdict("1 process", EdgeState.OK, "pid 4765"),
        EdgeVerdict("4 hive fabrics", EdgeState.DEGRADED,
                    "no fabrics block (older daemon?)"),
        EdgeVerdict("9 live probe", EdgeState.SKIPPED,
                    "daemon predates /doctor probe"),
    ]))
    remedy = [ln for ln in lines if "restart it" in ln]
    assert remedy and "kill 4765" in remedy[0]
