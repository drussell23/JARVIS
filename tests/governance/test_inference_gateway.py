"""The LAN bridge: arm the watchdog, degrade instead of hanging, keep the books.

Three properties, each guarding a specific way this could be wrong:

  1. The Inter-Token Watchdog must be ARMED on the remote path. It already
     exists inside `LocalPrimeClient._complete_streaming`, but `complete()`
     only takes the streaming path when `num_ctx` is set -- so on an
     un-negotiated remote it is present and DISARMED, and a wedged peer hangs
     this process against a socket.
  2. An unreachable host must become a ROUTING decision, not an error. Triage
     work has somewhere else to run.
  3. Remote measurements must land in the REMOTE host's physics ledger. Before
     the hardware-signature work they would have poisoned this Mac's entry,
     and the ThroughputGovernor would have sized the Mac's queue from LAN
     numbers.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance import inference_gateway as ig
from backend.core.ouroboros.governance import local_inference_director as lid

REMOTE = "http://192.168.1.50:11434"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    for k in (ig.ENABLED_ENV, ig.REMOTE_ENDPOINT_ENV, ig.REMOTE_MODEL_ENV,
              ig.LOCAL_TRIAGE_MODEL_ENV, ig.FAILURE_THRESHOLD_ENV,
              ig.COOLDOWN_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("JARVIS_LATENCY_LEDGER_PATH", str(tmp_path / "l.json"))
    ig.reset_for_tests()
    yield
    ig.reset_for_tests()


class _Completion:
    """Mirrors `LocalCompletion` -- the fields the profiler actually records."""

    def __init__(self, text="ok", output_tokens=10, ttft_ms=50.0,
                 total_ms=900.0):
        self.text = text
        self.output_tokens = output_tokens
        self.ttft_ms = ttft_ms
        self.total_ms = total_ms


class _FakeClient:
    """Mirrors LocalPrimeClient's real contract: the same keyword-only
    `complete` signature, an `aclose`, and a profiler it records into.

    Mirroring matters -- a fake that accepts **kwargs would have hidden
    whether `stream=True` was actually passed, which is the single most
    important assertion in this file."""

    def __init__(self, cfg, profiler, *, fail: Optional[BaseException] = None):
        self.cfg = cfg
        self.profiler = profiler
        self.calls: List[Dict[str, Any]] = []
        self.closed = False
        self._fail = fail

    async def complete(self, *, system: str, user: str, prompt_tokens: int,
                       temperature: float = 0.2,
                       max_tokens: Optional[int] = None,
                       stream: Optional[bool] = None,
                       prefill: str = "") -> Any:
        self.calls.append({"stream": stream, "base_url": self.cfg.base_url,
                           "prompt_tokens": prompt_tokens})
        if self._fail is not None:
            raise self._fail
        c = _Completion()
        self.profiler.record(ttft_ms=c.ttft_ms, total_ms=c.total_ms,
                             output_tokens=c.output_tokens)
        return c

    async def aclose(self) -> None:
        self.closed = True


def _gw(fail_for: Optional[Dict[str, BaseException]] = None):
    made: Dict[str, _FakeClient] = {}

    def factory(cfg, profiler):
        exc = (fail_for or {}).get(cfg.base_url)
        c = _FakeClient(cfg, profiler, fail=exc)
        made[cfg.base_url] = c
        return c

    g = ig.InferenceGateway(client_factory=factory)
    return g, made


async def _run(g, **kw):
    return await g.dispatch(system="s", user="u", prompt_tokens=100, **kw)


class TestTheWatchdogIsArmed:
    @pytest.mark.asyncio
    async def test_streaming_is_forced_on_the_remote_path(self, monkeypatch):
        """`complete()` chooses the non-streaming path whenever num_ctx is
        unset, and the Inter-Token Watchdog lives INSIDE the streaming path.
        Without this the guard is present and disarmed over the LAN."""
        monkeypatch.setenv(ig.REMOTE_ENDPOINT_ENV, REMOTE)
        g, made = _gw()
        await _run(g, route="background")
        assert made[REMOTE].calls[0]["stream"] is True

    @pytest.mark.asyncio
    async def test_the_local_path_keeps_the_clients_own_policy(self):
        """Locally a stall is slowness, not a dead peer -- the client's own
        num_ctx-based choice is correct and must not be overridden."""
        g, made = _gw()
        await _run(g, route="background")
        assert list(made.values())[0].calls[0]["stream"] is None


class TestDegradationIsARoutingDecision:
    @pytest.mark.asyncio
    async def test_an_inter_token_stall_falls_back_to_local(self, monkeypatch):
        monkeypatch.setenv(ig.REMOTE_ENDPOINT_ENV, REMOTE)
        g, made = _gw({REMOTE: lid.InterTokenStall("wedged")})
        out = await _run(g, route="speculative")
        assert out is not None                      # the op still completed
        assert len(made) == 2                       # remote tried, local ran
        assert made[REMOTE].calls and any(
            k != REMOTE for k in made)

    @pytest.mark.asyncio
    async def test_the_breaker_opens_and_stops_trying_the_lan(self,
                                                              monkeypatch):
        monkeypatch.setenv(ig.REMOTE_ENDPOINT_ENV, REMOTE)
        monkeypatch.setenv(ig.FAILURE_THRESHOLD_ENV, "2")
        g, made = _gw({REMOTE: ConnectionRefusedError("down")})
        for _ in range(2):
            await _run(g, route="speculative")
        assert g.target_for().scope == "local"
        before = len(made[REMOTE].calls)
        await _run(g, route="speculative")
        assert len(made[REMOTE].calls) == before   # LAN not attempted again

    @pytest.mark.asyncio
    async def test_a_request_fault_does_not_take_a_healthy_host_down(
            self, monkeypatch):
        """A 400 for a malformed body says nothing about whether the machine
        is up. Counting it would remove a working 5090 from service because a
        prompt was wrong."""
        monkeypatch.setenv(ig.REMOTE_ENDPOINT_ENV, REMOTE)
        monkeypatch.setenv(ig.FAILURE_THRESHOLD_ENV, "1")
        g, _made = _gw({REMOTE: ValueError("bad request body")})
        with pytest.raises(ValueError):
            await _run(g, route="background")
        assert g.snapshot()["hosts"][REMOTE]["state"] != "unreachable"

    def test_infrastructure_faults_are_classified_by_type(self):
        assert ig.is_infrastructure_fault(lid.InterTokenStall("x"))
        assert ig.is_infrastructure_fault(ConnectionRefusedError())
        assert ig.is_infrastructure_fault(asyncio.TimeoutError())
        assert not ig.is_infrastructure_fault(ValueError("bad prompt"))
        assert not ig.is_infrastructure_fault(KeyError("k"))


class TestRecovery:
    def test_half_open_admits_exactly_one_probe(self, monkeypatch):
        """Every queued op would otherwise stampede a recovering host the
        instant the cooldown expired -- and the first thing it does on
        recovery is load a model, so the stampede lands when it is least able
        to absorb it."""
        monkeypatch.setenv(ig.FAILURE_THRESHOLD_ENV, "1")
        monkeypatch.setenv(ig.COOLDOWN_ENV, "10")
        h = ig._HostHealth()
        h.record_failure("boom", now=100.0)
        assert h.state(now=105.0) is ig.HostState.UNREACHABLE
        assert h.state(now=111.0) is ig.HostState.PROBING
        assert h.claim_probe(now=111.0) is True
        assert h.claim_probe(now=111.0) is False     # only one
        assert h.state(now=111.0) is ig.HostState.UNREACHABLE

    def test_a_failed_probe_re_arms_the_cooldown(self, monkeypatch):
        """Its cooldown had already elapsed, so without re-stamping the open
        time a host that fails its probe would be retried immediately -- a
        breaker that stops breaking exactly when the host is worst."""
        monkeypatch.setenv(ig.FAILURE_THRESHOLD_ENV, "1")
        monkeypatch.setenv(ig.COOLDOWN_ENV, "10")
        h = ig._HostHealth()
        h.record_failure("boom", now=100.0)
        h.claim_probe(now=111.0)
        h.record_failure("still down", now=111.0)
        assert h.state(now=112.0) is ig.HostState.UNREACHABLE
        assert h.state(now=122.0) is ig.HostState.PROBING

    def test_success_clears_the_streak(self, monkeypatch):
        monkeypatch.setenv(ig.FAILURE_THRESHOLD_ENV, "3")
        h = ig._HostHealth()
        h.record_failure("a", now=1.0)
        h.record_failure("b", now=2.0)
        h.record_success()
        assert h.state(now=3.0) is ig.HostState.HEALTHY
        assert h.snapshot()["consecutive_failures"] == 0


class TestTheTelemetryHandoff:
    @pytest.mark.asyncio
    async def test_remote_measurements_land_in_the_remote_ledger(
            self, monkeypatch):
        """The whole reason the hardware axis had to land first. Under the old
        `model@ctx` key these LAN numbers would have written the Mac's entry
        and the governor would have sized this queue from them."""
        monkeypatch.setenv(ig.REMOTE_ENDPOINT_ENV, REMOTE)
        g, made = _gw()
        await _run(g, route="background")
        key = made[REMOTE].profiler._ledger_key
        local_cfg = lid.LocalConfig.from_env()
        assert key != lid.physics_key(local_cfg)
        assert lid._physics_ledger_load().get(key)      # it really persisted

    @pytest.mark.asyncio
    async def test_the_governor_sizes_against_the_active_host(self,
                                                              monkeypatch):
        """The serialisation happens on the REMOTE device, so this Mac's own
        physics is irrelevant to how many lanes the queue may run."""
        from backend.core.ouroboros.governance import throughput_governor as tg
        monkeypatch.setenv(ig.REMOTE_ENDPOINT_ENV, REMOTE)
        ig.reset_for_tests()
        tg.reset_for_tests()
        _prof, cfg = tg.ThroughputGovernor._profiler_and_config()
        assert cfg is not None and cfg.base_url == REMOTE


class TestLifecycleAndConfig:
    def test_no_remote_configured_is_the_single_machine_case(self):
        t = ig.InferenceGateway().target_for()
        assert t.scope == "local"
        assert "no remote endpoint" in t.reason

    def test_the_endpoint_is_never_hardcoded(self, monkeypatch):
        monkeypatch.setenv(ig.REMOTE_ENDPOINT_ENV, "http://10.0.0.9:1234")
        assert ig.InferenceGateway().target_for().base_url == \
            "http://10.0.0.9:1234"

    def test_the_master_flag_off_is_local_only(self, monkeypatch):
        monkeypatch.setenv(ig.REMOTE_ENDPOINT_ENV, REMOTE)
        monkeypatch.setenv(ig.ENABLED_ENV, "0")
        assert ig.InferenceGateway().target_for().scope == "local"

    @pytest.mark.asyncio
    async def test_aclose_closes_every_client_it_owns(self, monkeypatch):
        """An un-closed aiohttp session is a leak this codebase already paid
        for once, in the cognition-lanes provider."""
        monkeypatch.setenv(ig.REMOTE_ENDPOINT_ENV, REMOTE)
        g, made = _gw()
        await _run(g, route="background")
        await g.aclose()
        assert all(c.closed for c in made.values())

    def test_snapshot_never_raises(self):
        assert isinstance(ig.InferenceGateway().snapshot(), dict)
