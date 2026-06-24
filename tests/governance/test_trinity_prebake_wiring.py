"""Tests that the Pre-Flight Cache Manager is wired into TrinityDockerRunner.

NO real Docker. Proves:
  * the runner runs prebake BEFORE `up`, and passes the resulting image map into
    generate_trinity_compose (so the air-gapped boot uses cached images);
  * prebake disabled (default) -> existing path (images=None, no extra commands);
  * a bake FAILURE -> FRACTURE, and the air-gapped `up` is NEVER reached
    (fail-CLOSED: never boot air-gapped with a missing image).
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.saga.trinity_docker_runner as runner_mod
from backend.core.ouroboros.governance.saga.trinity_docker_runner import (
    CmdResult,
    TrinityDockerRunner,
)
from backend.core.ouroboros.governance.saga.trinity_prebake_manager import PrebakeResult
from backend.core.ouroboros.governance.saga.trinity_handshake_suite import (
    HttpResponse,
    MutatedEndpoint,
)

pytest.importorskip("yaml")

_EPS = [MutatedEndpoint("reactor", "GET", "/metrics", ("count",))]


class FakeCmd:
    """Records argv in order. up/down rc scriptable."""

    def __init__(self, up_rc=0, down_rc=0):
        self.up_rc = up_rc
        self.down_rc = down_rc
        self.argvs = []
        self.up_called = False
        self.down_called = False

    async def __call__(self, argv):
        argv = list(argv)
        self.argvs.append(argv)
        if "up" in argv:
            self.up_called = True
            return CmdResult(self.up_rc)
        if "down" in argv:
            self.down_called = True
            return CmdResult(self.down_rc)
        return CmdResult(0)


class FakeHttp:
    async def call(self, method, url, *, timeout):
        return HttpResponse(200, {"count": 1})


def _runner(cmd):
    async def health_checker():
        return True

    return TrinityDockerRunner(
        jarvis_root="/repos/jarvis",
        prime_root="/repos/prime",
        reactor_root="/repos/reactor",
        http_runner=FakeHttp(),
        cmd_runner=cmd,
        health_checker=health_checker,
    )


async def _run(runner):
    return await runner.run(
        mutated_endpoints=_EPS,
        overlay_writer=lambda y: "/tmp/trinity_wire_test.yml",
    )


@pytest.mark.asyncio
async def test_prebake_runs_before_up_and_images_passed_to_generator(monkeypatch):
    images = {
        "jarvis": "pfx-jarvis:aaaa",
        "prime": "pfx-prime:bbbb",
        "reactor": "pfx-reactor:cccc",
    }
    order = []

    async def fake_prebake(**kwargs):
        order.append("prebake")
        return PrebakeResult(
            images=dict(images), baked=("jarvis",), cached=("prime", "reactor"),
            skipped=False, reason="baked",
        )

    seen = {}
    real_generate = runner_mod.generate_trinity_compose

    def spy_generate(**kwargs):
        order.append("generate")
        seen["images"] = kwargs.get("images")
        return real_generate(**kwargs)

    monkeypatch.setattr(runner_mod, "prebake_if_needed", fake_prebake)
    monkeypatch.setattr(runner_mod, "generate_trinity_compose", spy_generate)

    cmd = FakeCmd()
    verdict = await _run(_runner(cmd))

    # prebake ran, then generate, then up.
    assert order[0] == "prebake"
    assert order.index("prebake") < order.index("generate")
    # The cached image map reached the generator.
    assert seen["images"] == images
    assert verdict.passed
    assert cmd.up_called


@pytest.mark.asyncio
async def test_prebake_disabled_uses_existing_path_images_none(monkeypatch):
    async def fake_prebake(**kwargs):
        return PrebakeResult(skipped=True, reason="prebake_disabled")

    seen = {}
    real_generate = runner_mod.generate_trinity_compose

    def spy_generate(**kwargs):
        seen["images"] = kwargs.get("images")
        return real_generate(**kwargs)

    monkeypatch.setattr(runner_mod, "prebake_if_needed", fake_prebake)
    monkeypatch.setattr(runner_mod, "generate_trinity_compose", spy_generate)

    cmd = FakeCmd()
    verdict = await _run(_runner(cmd))

    # Disabled -> images=None -> generator falls back to its base-image path.
    assert seen["images"] is None
    assert verdict.passed
    assert cmd.up_called


@pytest.mark.asyncio
async def test_bake_failure_fractures_and_never_ups(monkeypatch):
    async def fake_prebake(**kwargs):
        return PrebakeResult(
            images={"jarvis": "pfx-jarvis:aaaa"},
            baked=(), cached=(), skipped=False,
            reason="bake_failed:reactor:rc=1",
        )

    monkeypatch.setattr(runner_mod, "prebake_if_needed", fake_prebake)

    cmd = FakeCmd()
    verdict = await _run(_runner(cmd))

    assert verdict.fracture is True
    assert not verdict.passed
    assert verdict.reason.startswith("prebake_failed:bake_failed")
    # Fail-CLOSED: the air-gapped `up` was NEVER reached.
    assert cmd.up_called is False


@pytest.mark.asyncio
async def test_default_runner_unwired_still_works_prebake_disabled():
    # No monkeypatch: real prebake_if_needed, default-OFF env -> skipped -> the
    # legacy base-image path is used and the boot passes (proves OFF byte-ident).
    cmd = FakeCmd()
    verdict = await _run(_runner(cmd))
    assert verdict.passed
    # Only up + down + (health is injected) -> no docker build/inspect issued.
    flat = [" ".join(a) for a in cmd.argvs]
    assert not any("build" in f or "image inspect" in f for f in flat)
