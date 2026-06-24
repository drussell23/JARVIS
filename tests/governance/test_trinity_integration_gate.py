"""Tests for the air-gapped Trinity sandbox gate (Guardrail 2).

NO real Docker. A fake DockerRunner records the compose commands and returns
scripted up/health/probe results. The egress-mock is exercised purely in-process
(no socket bind) via its ``synthetic_response`` pure function.
"""
from __future__ import annotations

import os
from typing import List, Optional

import pytest

from backend.core.ouroboros.governance.saga import trinity_integration_gate as gate
from backend.core.ouroboros.governance.saga.trinity_integration_gate import (
    EGRESS_MOCK_SERVICE,
    LIVE_PROVIDER_HOSTS,
    RunResult,
    SandboxVerdict,
    assert_air_gapped,
    build_airgap_compose,
    gate_enabled,
    run_trinity_sandbox_gate,
)

yaml = pytest.importorskip("yaml")

_BASE_COMPOSE = """\
services:
  jarvis:
    image: jarvis:latest
    environment:
      JARVIS_FOO: "1"
    networks:
      - host_net
  jarvis-prime:
    image: prime:latest
    environment:
      - PRIME_BAR=2
  reactor-core:
    image: reactor:latest
networks:
  host_net:
    driver: bridge
"""


# --------------------------------------------------------------------------- #
# Fake runner
# --------------------------------------------------------------------------- #
class FakeRunner:
    """Records compose commands; returns scripted results.

    Scripts:
      up_rc           -> compose_up returncode
      internal        -> inspect_network stdout ("true"/"false")
      inspect_rc      -> inspect_network returncode
      provider_reachable -> if True, probe SUCCEEDS (air-gap breached)
      handshake_rc    -> health_handshake returncode
      raise_on        -> name of a phase to raise on ("up"/"handshake"/...)
    """

    def __init__(
        self,
        *,
        up_rc: int = 0,
        internal: str = "true",
        inspect_rc: int = 0,
        provider_reachable: bool = False,
        handshake_rc: int = 0,
        raise_on: Optional[str] = None,
    ) -> None:
        self.up_rc = up_rc
        self.internal = internal
        self.inspect_rc = inspect_rc
        self.provider_reachable = provider_reachable
        self.handshake_rc = handshake_rc
        self.raise_on = raise_on
        self.calls: List[str] = []

    async def compose_up(self, compose_path: str) -> RunResult:
        self.calls.append("up:%s" % compose_path)
        if self.raise_on == "up":
            raise RuntimeError("docker absent")
        return RunResult(self.up_rc, stdout="", stderr="boom" if self.up_rc else "")

    async def compose_down(self, compose_path: str) -> RunResult:
        self.calls.append("down:%s" % compose_path)
        if self.raise_on == "down":
            raise RuntimeError("down boom")
        return RunResult(0)

    async def inspect_network(self, network: str) -> RunResult:
        self.calls.append("inspect:%s" % network)
        return RunResult(self.inspect_rc, stdout=self.internal)

    async def probe_provider_host(self, service: str, host: str) -> RunResult:
        self.calls.append("probe:%s:%s" % (service, host))
        # ok (rc=0) == reachable == air-gap breached.
        return RunResult(0 if self.provider_reachable else 7)

    async def health_handshake(self, service: str) -> RunResult:
        self.calls.append("handshake:%s" % service)
        if self.raise_on == "handshake":
            raise RuntimeError("handshake boom")
        return RunResult(self.handshake_rc)


@pytest.fixture
def overlay_writer(tmp_path):
    written = {}

    def _write(yaml_str: str) -> str:
        p = tmp_path / "overlay.yml"
        p.write_text(yaml_str, encoding="utf-8")
        written["yaml"] = yaml_str
        written["path"] = str(p)
        return str(p)

    _write.written = written  # type: ignore[attr-defined]
    return _write


@pytest.fixture(autouse=True)
def _enable_gate(monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_SANDBOX_GATE_ENABLED", "true")
    yield


def _run(**kwargs) -> SandboxVerdict:
    import asyncio

    return asyncio.run(
        run_trinity_sandbox_gate(
            candidate_root="/tmp/candidate",
            op_id="op-test-1",
            base_compose_path=kwargs.pop("base_compose_path"),
            runner=kwargs.pop("runner"),
            overlay_writer=kwargs.pop("overlay_writer"),
            **kwargs,
        )
    )


# --------------------------------------------------------------------------- #
# (a) build_airgap_compose
# --------------------------------------------------------------------------- #
def test_build_airgap_compose_internal_and_mock(tmp_path):
    base = tmp_path / "base.yml"
    base.write_text(_BASE_COMPOSE, encoding="utf-8")

    rendered = build_airgap_compose(str(base), mock_port=9001, network="testnet")
    doc = yaml.safe_load(rendered)

    # Network declared internal: true.
    assert doc["networks"]["testnet"]["internal"] is True

    # Egress-mock service present and the ONLY external endpoint.
    assert EGRESS_MOCK_SERVICE in doc["services"]
    mock = doc["services"][EGRESS_MOCK_SERVICE]
    assert "9001" in " ".join(str(x) for x in mock["command"])

    # Every original service attached to the internal network + has overrides.
    for name in ("jarvis", "jarvis-prime", "reactor-core"):
        svc = doc["services"][name]
        assert svc["networks"] == ["testnet"]
        env = svc["environment"]
        assert env["DOUBLEWORD_BASE_URL"].startswith("http://%s:" % EGRESS_MOCK_SERVICE)
        assert env["ANTHROPIC_BASE_URL"].startswith("http://%s:" % EGRESS_MOCK_SERVICE)


def test_build_airgap_compose_no_live_provider_host(tmp_path):
    base = tmp_path / "base.yml"
    base.write_text(_BASE_COMPOSE, encoding="utf-8")
    rendered = build_airgap_compose(str(base), mock_port=9001, network="testnet")
    # Assert the live provider hosts are NOT reachable in the rendered config.
    for host in LIVE_PROVIDER_HOSTS:
        assert host not in rendered


def test_build_airgap_compose_unparseable_raises(tmp_path):
    base = tmp_path / "bad.yml"
    base.write_text("::: not valid yaml : : [", encoding="utf-8")
    with pytest.raises(RuntimeError):
        build_airgap_compose(str(base), mock_port=9001, network="testnet")


# --------------------------------------------------------------------------- #
# (b) assert_air_gapped
# --------------------------------------------------------------------------- #
def test_assert_air_gapped_true_when_internal_and_unreachable():
    import asyncio

    runner = FakeRunner(internal="true", provider_reachable=False)
    ok = asyncio.run(
        assert_air_gapped(
            runner, network="net", services=("jarvis",), overlay_yaml=None
        )
    )
    assert ok is True
    # It probed every live provider host.
    for host in LIVE_PROVIDER_HOSTS:
        assert any(host in c for c in runner.calls)


def test_assert_air_gapped_false_when_not_internal():
    import asyncio

    runner = FakeRunner(internal="false")
    ok = asyncio.run(
        assert_air_gapped(runner, network="net", services=("jarvis",))
    )
    assert ok is False


def test_assert_air_gapped_false_when_provider_reachable():
    import asyncio

    runner = FakeRunner(internal="true", provider_reachable=True)
    ok = asyncio.run(
        assert_air_gapped(runner, network="net", services=("jarvis",))
    )
    assert ok is False


def test_assert_air_gapped_false_when_no_services():
    import asyncio

    runner = FakeRunner(internal="true")
    ok = asyncio.run(assert_air_gapped(runner, network="net", services=()))
    assert ok is False


def test_assert_air_gapped_rejects_leaky_overlay():
    import asyncio

    # A rendered overlay that leaks a live provider host -> rejected statically.
    leaky = yaml.safe_dump(
        {
            "services": {
                "jarvis": {"environment": {"X": "https://api.anthropic.com"}}
            },
            "networks": {"net": {"internal": True}},
        }
    )
    runner = FakeRunner(internal="true")
    ok = asyncio.run(
        assert_air_gapped(
            runner, network="net", services=("jarvis",), overlay_yaml=leaky
        )
    )
    assert ok is False


# --------------------------------------------------------------------------- #
# (c) handshake pass -> passed=True
# --------------------------------------------------------------------------- #
def test_handshake_pass(tmp_path, overlay_writer):
    base = tmp_path / "base.yml"
    base.write_text(_BASE_COMPOSE, encoding="utf-8")
    runner = FakeRunner(up_rc=0, internal="true", handshake_rc=0)

    v = _run(
        base_compose_path=str(base), runner=runner, overlay_writer=overlay_writer
    )
    assert v.passed is True
    assert v.fracture is False
    assert v.air_gapped is True
    assert v.handshake_ok is True
    assert "down:" + overlay_writer.written["path"] in runner.calls  # teardown ran


# --------------------------------------------------------------------------- #
# (d) handshake fail -> fracture + emit_sovereign_yield(CROSS-REPO FRACTURE)
# --------------------------------------------------------------------------- #
def test_handshake_fail_fracture_emits_yield(tmp_path, overlay_writer, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_text(_BASE_COMPOSE, encoding="utf-8")
    runner = FakeRunner(up_rc=0, internal="true", handshake_rc=11)

    captured = {}

    def _fake_emit(op_id, **kw):
        captured["op_id"] = op_id
        captured["reason"] = kw.get("reason")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.convergence_watchdog.emit_sovereign_yield",
        _fake_emit,
    )

    v = _run(
        base_compose_path=str(base), runner=runner, overlay_writer=overlay_writer
    )
    assert v.fracture is True
    assert v.passed is False
    assert captured.get("reason") == "CROSS-REPO FRACTURE"


# --------------------------------------------------------------------------- #
# (e) Docker absent / up-fails -> FRACTURE (fail-CLOSED, NOT a silent pass)
# --------------------------------------------------------------------------- #
def test_up_fails_is_fracture(tmp_path, overlay_writer):
    base = tmp_path / "base.yml"
    base.write_text(_BASE_COMPOSE, encoding="utf-8")
    runner = FakeRunner(up_rc=1)

    v = _run(
        base_compose_path=str(base), runner=runner, overlay_writer=overlay_writer
    )
    assert v.fracture is True
    assert v.passed is False
    assert v.reason == "compose_up_failed"


def test_docker_absent_is_fracture(tmp_path, overlay_writer):
    base = tmp_path / "base.yml"
    base.write_text(_BASE_COMPOSE, encoding="utf-8")
    runner = FakeRunner(raise_on="up")  # simulates `docker` not found -> raises

    v = _run(
        base_compose_path=str(base), runner=runner, overlay_writer=overlay_writer
    )
    assert v.fracture is True
    assert v.passed is False


def test_air_gap_breach_is_fracture(tmp_path, overlay_writer):
    base = tmp_path / "base.yml"
    base.write_text(_BASE_COMPOSE, encoding="utf-8")
    runner = FakeRunner(up_rc=0, internal="true", provider_reachable=True)

    v = _run(
        base_compose_path=str(base), runner=runner, overlay_writer=overlay_writer
    )
    assert v.fracture is True
    assert v.air_gapped is False
    assert v.reason == "air_gap_unverified"


# --------------------------------------------------------------------------- #
# (f) teardown (down -v) runs in finally even when the gate raises/times out
# --------------------------------------------------------------------------- #
def test_teardown_runs_on_handshake_exception(tmp_path, overlay_writer):
    base = tmp_path / "base.yml"
    base.write_text(_BASE_COMPOSE, encoding="utf-8")
    runner = FakeRunner(up_rc=0, internal="true", raise_on="handshake")

    v = _run(
        base_compose_path=str(base), runner=runner, overlay_writer=overlay_writer
    )
    # Exception during handshake -> FRACTURE, but teardown STILL ran.
    assert v.fracture is True
    assert any(c.startswith("down:") for c in runner.calls)


def test_teardown_runs_on_timeout(tmp_path, overlay_writer, monkeypatch):
    base = tmp_path / "base.yml"
    base.write_text(_BASE_COMPOSE, encoding="utf-8")

    # Force a near-zero timeout and make compose_up hang past it.
    monkeypatch.setenv("JARVIS_TRINITY_SANDBOX_TIMEOUT_S", "0.05")

    class HangRunner(FakeRunner):
        async def compose_up(self, compose_path: str) -> RunResult:
            import asyncio

            self.calls.append("up:%s" % compose_path)
            await asyncio.sleep(5)
            return RunResult(0)

    runner = HangRunner(internal="true")
    v = _run(
        base_compose_path=str(base), runner=runner, overlay_writer=overlay_writer
    )
    assert v.fracture is True
    assert v.reason == "sandbox_timeout"
    assert any(c.startswith("down:") for c in runner.calls)


def test_teardown_failure_is_fail_soft(tmp_path, overlay_writer):
    base = tmp_path / "base.yml"
    base.write_text(_BASE_COMPOSE, encoding="utf-8")
    runner = FakeRunner(up_rc=0, internal="true", handshake_rc=0, raise_on="down")

    # Teardown raises, but the verdict is still returned (fail-soft).
    v = _run(
        base_compose_path=str(base), runner=runner, overlay_writer=overlay_writer
    )
    assert v.passed is True  # handshake passed; teardown error swallowed


# --------------------------------------------------------------------------- #
# (g) OFF gate behavior
# --------------------------------------------------------------------------- #
def test_gate_off_is_noop_pass(tmp_path, overlay_writer, monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_SANDBOX_GATE_ENABLED", "false")
    base = tmp_path / "base.yml"
    base.write_text(_BASE_COMPOSE, encoding="utf-8")
    runner = FakeRunner()

    v = _run(
        base_compose_path=str(base), runner=runner, overlay_writer=overlay_writer
    )
    assert v.passed is True
    assert v.fracture is False
    assert v.reason == "gate_disabled"
    # No Docker touched.
    assert runner.calls == []


def test_gate_enabled_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_TRINITY_SANDBOX_GATE_ENABLED", raising=False)
    assert gate_enabled() is True


def test_verdict_to_dict_roundtrip():
    v = SandboxVerdict(
        passed=False,
        fracture=True,
        reason="x",
        air_gapped=False,
        handshake_ok=False,
        containers=("a", "b"),
    )
    d = v.to_dict()
    assert d["fracture"] is True
    assert d["containers"] == ["a", "b"]


# --------------------------------------------------------------------------- #
# Egress mock: well-formed synthetic responses
# --------------------------------------------------------------------------- #
def test_egress_mock_synthetic_shapes():
    from scripts.trinity_sandbox_egress_mock import SYNTHETIC_MARKER, synthetic_response

    # DoubleWord chat completion.
    status, body = synthetic_response("/v1/chat/completions")
    assert status == 200
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "ok"
    assert body["_source"] == SYNTHETIC_MARKER

    # Claude messages.
    status, body = synthetic_response("/v1/messages")
    assert status == 200
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "ok"

    # DW batches.
    status, body = synthetic_response("/v1/batches")
    assert status == 200
    assert body["status"] == "completed"

    # GCP metadata token.
    status, body = synthetic_response("/computeMetadata/v1/instance/service-accounts/default/token")
    assert status == 200
    assert body["access_token"].startswith("synthetic-")

    # Unmapped -> 404, never a live passthrough.
    status, body = synthetic_response("/some/unknown")
    assert status == 404
    assert body["error"] == "egress_sinkhole_unmapped"
