"""run_body_mode.py -- Stage-2 Body-mode driver (the Mac side of the relocation).

A thin, composition-only process: discovers the Brain's WS endpoint, connects a
client ``DistributedEventBus`` over mTLS, bridges the local ``TrinityEventBus``
onto the wire (``TrinityBusBridge``), points the Body sensors at the
``RemoteIntakeRouter`` shim (which publishes IntentEnvelopes to the bus instead
of a local orchestrator), and runs a starvation census against the
``ControlPlaneWatchdog`` -- the Stage-2 payoff proof that with the Brain
relocated off-box, the Mac's control-plane starvation is ~0.

Every collaborator is an injectable seam (the ``BrainIgnitionDriver`` pattern,
scripts/ignite_brain_vm.py); the live defaults are resolved lazily inside
``run()`` so the unit tests (tests/infra/test_run_body_mode.py) stay pure-logic
with zero sockets.

Exit codes:
    0  acceptance-clean (duration elapsed, clean stop)
    2  discovery failed ("Brain offline")
    3  connect-gate timeout (WS client never established)

Env knobs (all resolved at call time -- zero baked assumptions):
    JARVIS_BRAIN_CONNECT_GATE_S     connect-gate budget (default 30)
    JARVIS_BODY_MODE_CENSUS_S       census log cadence (default 10)
    JARVIS_BRAIN_WS_*               Stage-0 transport client family
    JARVIS_BRAIN_MTLS_DIR           client mTLS material (Stage-1 conventions)

Usage::

    python3 scripts/run_body_mode.py                       # run until signal
    python3 scripts/run_body_mode.py --inject-test-signal 5 --duration-s 60
    python3 scripts/run_body_mode.py --dry-run             # plan only, no network
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any, Awaitable, Callable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths -- derived from this file's location; backend importable regardless of cwd.
# ---------------------------------------------------------------------------
_SCRIPTS_DIR: str = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT: str = os.path.dirname(_SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _log(msg: str) -> None:
    print("[BodyMode] %s" % (msg,), flush=True)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# The Body-mode driver.
# ---------------------------------------------------------------------------


class BodyModeDriver:
    """Discover -> connect -> bridge -> shim sensors -> starvation census.

    Every collaborator is an injectable seam; live defaults resolve lazily
    inside ``run()`` (Mandate: fakes only in tests, no simulation on the live
    path).

    Seams:
        discover_fn        -> ``async () -> Optional[str]`` (Brain WS URL)
        bus_factory        -> ``async () -> (trinity_bus, broker, dist_bus)``
        bridge_factory     -> ``(trinity_bus, broker) -> TrinityBusBridge``
        shim_factory       -> ``(trinity_bus) -> RemoteIntakeRouter``
        sensor_factory     -> ``(shim) -> Optional[sensor]`` (fail-soft)
        watchdog_factory   -> ``() -> ControlPlaneWatchdog``
    """

    def __init__(
        self,
        *,
        inject_test_signals: int = 0,
        duration_s: Optional[float] = None,
        dry_run: bool = False,
        # Injectable seams (live defaults resolved lazily inside run()).
        discover_fn: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
        bus_factory: Optional[Callable[[], Awaitable[Tuple[Any, Any, Any]]]] = None,
        bridge_factory: Optional[Callable[[Any, Any], Any]] = None,
        shim_factory: Optional[Callable[[Any], Any]] = None,
        sensor_factory: Optional[Callable[[Any], Any]] = None,
        watchdog_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.inject_test_signals = max(0, int(inject_test_signals))
        self.duration_s = duration_s
        self.dry_run = dry_run

        self._discover = discover_fn
        self._bus_factory = bus_factory
        self._bridge_factory = bridge_factory
        self._shim_factory = shim_factory
        self._sensor_factory = sensor_factory
        self._watchdog_factory = watchdog_factory

        self._signals_sent = 0
        self._worst_lag_ms = 0.0

    # -- lazy live default resolvers ---------------------------------------

    async def _do_discover(self) -> Optional[str]:
        if self._discover is not None:
            return await self._discover()
        from backend.core.ouroboros.governance.brain_discovery import (  # noqa: PLC0415
            discover_brain_endpoint,
        )
        return await discover_brain_endpoint()

    async def _do_bus_stack(self) -> Tuple[Any, Any, Any]:
        if self._bus_factory is not None:
            return await self._bus_factory()
        from backend.core.trinity_event_bus import get_trinity_event_bus  # noqa: PLC0415
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: PLC0415
            StreamEventBroker,
        )
        from backend.core.ouroboros.governance.transport.distributed_event_bus import (  # noqa: PLC0415
            DistributedEventBus,
        )
        from backend.core.ouroboros.governance.transport.transport_config import (  # noqa: PLC0415
            TransportConfig,
        )
        trinity_bus = await get_trinity_event_bus()
        broker = StreamEventBroker()
        cfg = TransportConfig.from_env(role="mac-body")
        dist_bus = DistributedEventBus(broker, cfg, role="client")
        return trinity_bus, broker, dist_bus

    def _do_bridge(self, trinity_bus: Any, broker: Any) -> Any:
        if self._bridge_factory is not None:
            return self._bridge_factory(trinity_bus, broker)
        from backend.core.ouroboros.governance.transport.trinity_bus_bridge import (  # noqa: PLC0415
            TrinityBusBridge,
        )
        return TrinityBusBridge(
            trinity_bus, broker,
            outbound_topics=["intake.remote_signal.*", "console.*"],
            source_id="mac-body",
        )

    def _do_shim(self, trinity_bus: Any) -> Any:
        if self._shim_factory is not None:
            return self._shim_factory(trinity_bus)
        from backend.core.ouroboros.governance.intake.remote_intake import (  # noqa: PLC0415
            RemoteIntakeRouter,
        )
        return RemoteIntakeRouter(trinity_bus)

    def _do_sensor(self, shim: Any) -> Optional[Any]:
        """Fail-soft: the Mac test env may lack voice infra entirely --
        ``--inject-test-signal`` is the deterministic acceptance path."""
        if self._sensor_factory is not None:
            try:
                return self._sensor_factory(shim)
            except Exception as exc:  # noqa: BLE001
                _log("voice sensor unavailable (fail-soft): %s" % exc)
                return None
        try:
            from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (  # noqa: PLC0415
                VoiceCommandSensor,
            )
            # Construction pattern: intake_layer_service.py:578-583 --
            # event-driven, no start/stop lifecycle; store as attribute only.
            return VoiceCommandSensor(router=shim, repo="jarvis")
        except Exception as exc:  # noqa: BLE001
            _log("voice sensor unavailable (fail-soft): %s" % exc)
            return None

    def _do_watchdog(self) -> Any:
        if self._watchdog_factory is not None:
            return self._watchdog_factory()
        from backend.core.ouroboros.governance.control_plane_watchdog import (  # noqa: PLC0415
            get_default_watchdog,
        )
        return get_default_watchdog()

    # -- test-signal injection (the deterministic acceptance path) ----------

    @staticmethod
    def _build_test_envelopes(n: int) -> List[Any]:
        from backend.core.ouroboros.governance.intake.intent_envelope import (  # noqa: PLC0415
            make_envelope,
        )
        out: List[Any] = []
        for i in range(n):
            # dedup_key = sha256(source|files|evidence["signature"]) -- the
            # per-i signature makes every envelope's dedup_key DISTINCT
            # (description alone would NOT: it is not part of the key).
            #
            # source: ``cadence_synthetic`` is the whitelisted honest-source
            # token for synthetic test-workload injection (intent_envelope.py
            # _VALID_SOURCES, 2026-05-05 precedent -- synthetic traffic MUST
            # NOT masquerade as a production source). Routing truth: the
            # token is in NEITHER _BACKGROUND_SOURCES nor _SPECULATIVE_SOURCES
            # (urgency_router.py:134-146), so with urgency="low" and default
            # moderate complexity these envelopes fall through to the
            # Priority-5 default = ProviderRoute.STANDARD
            # (urgency_router.py:731-734): DW primary WITH Claude fallback --
            # a small residual Claude-fallback cost is possible if DW is
            # degraded during acceptance, bounded by the run's cost cap. The
            # Body-mode identity travels in ``evidence.origin`` for
            # downstream filtering.
            out.append(make_envelope(
                source="cadence_synthetic",
                description="stage2 acceptance signal %d" % i,
                # Non-empty per envelope validation; "." is the established
                # cadence_synthetic placeholder (phase_9_synthetic_workload.py).
                target_files=(".",),
                repo="jarvis",
                confidence=0.5,
                urgency="low",
                evidence={
                    "signature": "body_test_stage2:%d" % i,
                    "origin": "body_test",
                },
                requires_human_ack=False,
            ))
        return out

    async def _inject_signals(self, shim: Any) -> None:
        for envelope in self._build_test_envelopes(self.inject_test_signals):
            try:
                verdict = await shim.ingest(envelope)
            except Exception as exc:  # noqa: BLE001 -- fail-soft, keep injecting
                _log("inject FAILED dedup_key=%s: %s"
                     % (envelope.dedup_key, exc))
                continue
            self._signals_sent += 1
            _log("injected dedup_key=%s verdict=%s"
                 % (envelope.dedup_key, verdict))

    # -- census -------------------------------------------------------------

    def _census_tick(self, watchdog: Any, connected: bool) -> None:
        lag_events = int(getattr(watchdog, "lag_event_count", 0))
        try:
            records = watchdog.recent_lag_records()
        except Exception:  # noqa: BLE001
            records = []
        for r in records:
            self._worst_lag_ms = max(
                self._worst_lag_ms, float(getattr(r, "lag_ms", 0.0)))
        _log("lag_events=%d worst_ms=%.1f connected=%s"
             % (lag_events, self._worst_lag_ms, connected))

    # -- the run FSM ---------------------------------------------------------

    async def run(self) -> int:
        if self.dry_run:
            _log("[dry-run] would discover the Brain WS endpoint, connect the "
                 "mTLS bus client (role=mac-body), bridge "
                 "['intake.remote_signal.*', 'console.*'], point sensors at "
                 "the RemoteIntakeRouter shim, inject %d test signal(s), and "
                 "run the starvation census -- no network touched"
                 % self.inject_test_signals)
            return 0

        # 1. DISCOVER (stateless; fail-soft returns None).
        url = await self._do_discover()
        if not url:
            _log("Brain offline -- discovery returned no endpoint (exit 2)")
            return 2
        _log("Brain discovered at %s" % url)

        # 2. Bus stack + connect gate (Stage-1 proven pattern: events published
        #    before the first successful connect are live-only-lost -- never
        #    proceed on a dark link).
        trinity_bus, broker, dist_bus = await self._do_bus_stack()
        client_task = asyncio.ensure_future(dist_bus.start_client(url))

        def _client_getter() -> Any:
            return getattr(dist_bus, "_client", None)

        def _connected() -> bool:
            client = _client_getter()
            return bool(client is not None
                        and getattr(client, "connected", False))

        async def _await_connected(budget_s: float) -> bool:
            deadline = time.monotonic() + budget_s
            while time.monotonic() < deadline:
                if _connected():
                    return True
                await asyncio.sleep(0.1)
            return False

        bridge = None
        watchdog = None
        try:
            if not await _await_connected(
                    _env_float("JARVIS_BRAIN_CONNECT_GATE_S", 30.0)):
                _log("connect gate TIMEOUT -- WS client never established "
                     "(exit 3)")
                return 3
            _log("bus client connected")

            # 3. Bridge the local trinity bus onto the wire.
            bridge = self._do_bridge(trinity_bus, broker)
            await bridge.start()

            # 4. Sensors against the remote-intake shim.
            shim = self._do_shim(trinity_bus)
            sensor = self._do_sensor(shim)  # noqa: F841 -- event-driven, held alive
            if sensor is not None:
                _log("voice sensor armed against the remote-intake shim")
            if self.inject_test_signals:
                await self._inject_signals(shim)

            # 5. Starvation census (the Stage-2 payoff measurement).
            watchdog = self._do_watchdog()
            watchdog.start()
            census_s = _env_float("JARVIS_BODY_MODE_CENSUS_S", 10.0)
            deadline = (time.monotonic() + self.duration_s
                        if self.duration_s is not None else None)
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(census_s, remaining))
                else:
                    await asyncio.sleep(census_s)
                self._census_tick(watchdog, _connected())

            # 6. Clean stop -> summary -> exit 0.
            self._census_tick(watchdog, _connected())
            return 0
        finally:
            lag_events = int(getattr(watchdog, "lag_event_count", 0)) \
                if watchdog is not None else 0
            if watchdog is not None:
                try:
                    await watchdog.stop()
                except Exception:  # noqa: BLE001 -- fail-soft teardown
                    pass
            if bridge is not None:
                try:
                    await bridge.stop()
                except Exception:  # noqa: BLE001
                    pass
            try:
                await dist_bus.stop()
            except Exception:  # noqa: BLE001
                pass
            client_task.cancel()
            try:
                await client_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            _log("SUMMARY lag_events=%d worst_ms=%.1f signals_sent=%d"
                 % (lag_events, self._worst_lag_ms, self._signals_sent))


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_body_mode.py",
        description=(
            "Stage-2 Body-mode driver. Discovers the relocated Brain, connects "
            "the mTLS WS bus, runs Body sensors against the RemoteIntakeRouter "
            "shim, and proves the Mac's control-plane starvation is ~0."
        ),
    )
    p.add_argument(
        "--inject-test-signal", type=int, default=0, metavar="N",
        help="Inject N deterministic body_test IntentEnvelopes through the "
             "shim (the acceptance path; default 0).",
    )
    p.add_argument(
        "--duration-s", type=float, default=None, metavar="S",
        help="Run for S seconds then stop cleanly (default: until signal).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print the Body-mode plan and exit -- touches no network.",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    driver = BodyModeDriver(
        inject_test_signals=args.inject_test_signal,
        duration_s=args.duration_s,
        dry_run=args.dry_run,
    )
    try:
        return asyncio.run(driver.run())
    except KeyboardInterrupt:
        _log("interrupted -- clean stop")
        return 0


if __name__ == "__main__":
    sys.exit(main())
