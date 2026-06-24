"""Tests for the real TrinityDockerRunner (G2 production path).

NO real Docker. A fake cmd runner records argv + scripts returncodes; a fake
HTTP runner scripts handshake responses. Asserts the up->health-gate->handshake
->down sequence AND that teardown (down -v) ALWAYS fires in finally.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.saga.trinity_docker_runner import (
    CmdResult,
    TrinityDockerRunner,
)
from backend.core.ouroboros.governance.saga.trinity_handshake_suite import (
    HttpResponse,
    MutatedEndpoint,
)

pytest.importorskip("yaml")

_EPS = [MutatedEndpoint("reactor", "GET", "/metrics", ("count",))]


class FakeCmd:
    """Records argv. Scripts: up_rc, down_rc. Tracks whether 'down' ran."""

    def __init__(self, up_rc=0, down_rc=0, raise_on_up=False):
        self.up_rc = up_rc
        self.down_rc = down_rc
        self.raise_on_up = raise_on_up
        self.argvs = []
        self.down_called = False

    async def __call__(self, argv):
        argv = list(argv)
        self.argvs.append(argv)
        if "up" in argv:
            if self.raise_on_up:
                raise RuntimeError("up boom")
            return CmdResult(self.up_rc)
        if "down" in argv:
            self.down_called = True
            return CmdResult(self.down_rc)
        return CmdResult(0)


class FakeHttp:
    def __init__(self, resp=None):
        self._resp = resp or HttpResponse(200, {"count": 1})

    async def call(self, method, url, *, timeout):
        return self._resp


def _runner(cmd, http, *, health_ok=True, writer_path="/tmp/x.yml"):
    async def health_checker():
        return health_ok

    return TrinityDockerRunner(
        jarvis_root="/repos/jarvis",
        prime_root="/repos/prime",
        reactor_root="/repos/reactor",
        http_runner=http,
        cmd_runner=cmd,
        health_checker=health_checker,
    )


async def _run(runner):
    return await runner.run(
        mutated_endpoints=_EPS,
        overlay_writer=lambda y: "/tmp/trinity_test.yml",
    )


@pytest.mark.asyncio
async def test_happy_path_passes_and_tears_down():
    cmd = FakeCmd()
    runner = _runner(cmd, FakeHttp())
    verdict = await _run(runner)
    assert verdict.passed and not verdict.fracture
    assert verdict.sinkhole_ok
    assert cmd.down_called  # teardown always


@pytest.mark.asyncio
async def test_handshake_fracture_still_tears_down():
    cmd = FakeCmd()
    runner = _runner(cmd, FakeHttp(HttpResponse(500, {"x": 1})))
    verdict = await _run(runner)
    assert verdict.fracture and not verdict.passed
    assert "handshake_fracture" in verdict.reason
    assert cmd.down_called  # teardown in finally even on fracture


@pytest.mark.asyncio
async def test_compose_up_failure_is_fracture_and_tears_down():
    cmd = FakeCmd(up_rc=1)
    runner = _runner(cmd, FakeHttp())
    verdict = await _run(runner)
    assert verdict.fracture
    assert "compose_up_failed" in verdict.reason
    assert cmd.down_called


@pytest.mark.asyncio
async def test_health_gate_timeout_is_fracture_and_tears_down():
    cmd = FakeCmd()
    runner = _runner(cmd, FakeHttp(), health_ok=False)
    verdict = await _run(runner)
    assert verdict.fracture
    assert verdict.reason == "health_gate_timeout"
    assert cmd.down_called


@pytest.mark.asyncio
async def test_exception_during_up_is_fracture_and_tears_down():
    cmd = FakeCmd(raise_on_up=True)
    runner = _runner(cmd, FakeHttp())
    verdict = await _run(runner)
    assert verdict.fracture
    assert "boot_error" in verdict.reason
    assert cmd.down_called  # finally still ran


@pytest.mark.asyncio
async def test_ordering_up_before_handshake_before_down():
    cmd = FakeCmd()
    runner = _runner(cmd, FakeHttp())
    await _run(runner)
    phases = ["up" if "up" in a else "down" if "down" in a else "ps" for a in cmd.argvs]
    assert phases[0] == "up"
    assert phases[-1] == "down"


@pytest.mark.asyncio
async def test_sinkhole_failure_short_circuits_no_up(monkeypatch):
    # Force a non-air-gapped compose by monkeypatching the generator.
    import backend.core.ouroboros.governance.saga.trinity_docker_runner as mod

    def bad_compose(**kw):
        return {"services": {"x": {"networks": ["a"]}}, "networks": {"a": {"internal": False}}}

    monkeypatch.setattr(mod, "generate_trinity_compose", bad_compose)
    cmd = FakeCmd()
    runner = _runner(cmd, FakeHttp())
    verdict = await runner.run(mutated_endpoints=_EPS, overlay_writer=lambda y: "/tmp/x.yml")
    assert verdict.fracture
    assert "sinkhole_unverified" in verdict.reason
    # Never booted -> never tore down (nothing to tear down).
    assert not cmd.down_called
