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

Stage-3 Task 4 -- durable degrade: the live default arms a ``DurableOutbound``
WAL on the broker (journal-at-publish; peer-republished events excluded via
``journal_filter``) and threads it plus ``url_resolver=discover_brain_endpoint``
into the ``DistributedEventBus`` client, so a partition never loses a signal
and reconnect re-races discovery per attempt. With the WAL armed, discovery
failure at start NO LONGER exits 2 -- the driver degrades: signals journal
durably, the client re-races forever with backoff, and the census surfaces
``connected=False queued=N``. ``--require-brain`` restores the strict Stage-2
exit-2 / exit-3 contract (acceptance runs). The "Brain offline" /
"Brain reconnected" lines are DETERMINISTIC edge transitions derived ONLY from
the bus client's ``connected`` property (set/cleared exactly at WS
establishment/teardown -- the transport closure), polled once per census tick.
No exception-driven state, no heartbeat-timeout heuristics (operator mandate).

Exit codes:
    0  acceptance-clean (duration elapsed, clean stop)
    2  discovery failed ("Brain offline") -- STRICT contract only
       (--require-brain, or no durable WAL armed)
    3  connect-gate timeout (WS client never established) -- strict contract

Env knobs (all resolved at call time -- zero baked assumptions):
    JARVIS_BRAIN_CONNECT_GATE_S     connect-gate budget (default 30)
    JARVIS_BODY_MODE_CENSUS_S       census log cadence (default 10)
    JARVIS_BRAIN_WS_*               Stage-0 transport client family
    JARVIS_BRAIN_MTLS_DIR           client mTLS material (Stage-1 conventions)
    JARVIS_BODY_WAL_*               DurableOutbound family (durable_outbound.py)
    JARVIS_BRAIN_RESURRECT_*        BrainKeeper family (brain_keeper.py, Stage-4)
    JARVIS_KEEPER_ID                keeper identity (default mac-body-keeper)

Usage::

    python3 scripts/run_body_mode.py                       # run until signal
    python3 scripts/run_body_mode.py --inject-test-signal 5 --duration-s 60
    python3 scripts/run_body_mode.py --require-brain       # strict exit 2/3
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


# The Body's bus-bridge identity -- MUST match the TrinityBusBridge
# source_id (``_do_bridge``): the durable journal_filter keys on it.
_SOURCE_ID = "mac-body"


def _journal_local_origin_only(event: Any) -> bool:
    """DurableOutbound journal_filter (live default): journal ONLY
    locally-originated bridgeable events.

    Peer-republished events -- imported off the wire and reflected onto
    the local broker -- carry client-local event ids the server has
    never seen; replaying them at reconnect defeats the server's
    qualified-id dedup and duplicates events on the far side.
    TrinityBusBridge stamps every outbound payload with
    ``origin=<source_id>``; peer imports carry the peer's id. An absent
    or empty origin journals anyway (fail OPEN: durability bias).
    """
    try:
        origin = (getattr(event, "payload", None) or {}).get("origin")
    except Exception:  # noqa: BLE001 -- fail OPEN
        return True
    return origin in (None, "", _SOURCE_ID)


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
        durable_factory    -> ``(broker) -> DurableOutbound`` (Stage-3 WAL)
        keeper_factory     -> ``() -> BrainKeeper`` (Stage-4 resurrection;
                              live default arms the real keeper with
                              provision_brain imported IN-PROCESS)
    """

    def __init__(
        self,
        *,
        inject_test_signals: int = 0,
        duration_s: Optional[float] = None,
        dry_run: bool = False,
        require_brain: bool = False,
        # Stage-4 IMPORTANT-3: keeper master. None -> consult
        # JARVIS_BRAIN_KEEPER_ENABLED (default false); True -> force on
        # (--keeper); False -> force off (--no-keeper, wins over everything).
        keeper_mode: Optional[bool] = None,
        # Injectable seams (live defaults resolved lazily inside run()).
        discover_fn: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
        bus_factory: Optional[Callable[[], Awaitable[Tuple[Any, Any, Any]]]] = None,
        bridge_factory: Optional[Callable[[Any, Any], Any]] = None,
        shim_factory: Optional[Callable[[Any], Any]] = None,
        sensor_factory: Optional[Callable[[Any], Any]] = None,
        watchdog_factory: Optional[Callable[[], Any]] = None,
        durable_factory: Optional[Callable[[Any], Any]] = None,
        keeper_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.inject_test_signals = max(0, int(inject_test_signals))
        self.duration_s = duration_s
        self.dry_run = dry_run
        self.require_brain = require_brain
        self.keeper_mode = keeper_mode

        self._discover = discover_fn
        self._bus_factory = bus_factory
        self._bridge_factory = bridge_factory
        self._shim_factory = shim_factory
        self._sensor_factory = sensor_factory
        self._watchdog_factory = watchdog_factory
        self._durable_factory = durable_factory
        self._keeper_factory = keeper_factory

        self._signals_sent = 0
        self._worst_lag_ms = 0.0
        self._durable: Optional[Any] = None
        self._durable_built = False
        # Stage-4 Task 3: the Brain KEEPER (sustained-absence resurrection).
        self._keeper: Optional[Any] = None
        self._exported_gen: Optional[int] = None
        # Deterministic degrade surfacing (operator mandate): the link
        # state derives ONLY from the client's `connected` property.
        # None = not yet observed; edge transitions log exactly once.
        self._link_up: Optional[bool] = None

    # -- lazy live default resolvers ---------------------------------------

    async def _do_discover(self) -> Optional[str]:
        """Discovery seam, WRAPPED so every result also feeds the keeper
        (Stage-4 Task 3): the initial discovery, every url_resolver
        reconnect re-race, and the keeper's own confirmation probe all
        flow through here -- one honest wiring, no second census path."""
        if self._discover is not None:
            url = await self._discover()
        else:
            from backend.core.ouroboros.governance.brain_discovery import (  # noqa: PLC0415
                discover_brain_endpoint,
            )
            url = await discover_brain_endpoint()
        if self._keeper is not None:
            try:
                self._keeper.note_discovery_result(url)
            except Exception:  # noqa: BLE001 -- keeper feed is fail-soft
                pass
        return url

    def _keeper_enabled(self) -> bool:
        """Stage-4 IMPORTANT-3 master resolution. ``--no-keeper`` (keeper_mode
        False) wins over everything; ``--keeper`` (True) forces on; None ->
        consult ``JARVIS_BRAIN_KEEPER_ENABLED`` (default FALSE -- SAFE default:
        the drill / live runs opt in explicitly, pre-Stage-4 degrade-and-wait
        is byte-identical)."""
        if self.keeper_mode is False:
            return False
        if self.keeper_mode is True:
            return True
        return (os.environ.get("JARVIS_BRAIN_KEEPER_ENABLED", "false")
                or "").strip().lower() in ("1", "true", "yes", "on")

    def _do_keeper(self) -> Optional[Any]:
        """Resolve the Brain-keeper seam (Stage-4 Task 3).

        Live default (no injected bus stack) ARMS the keeper:
        ``provision_fn`` is ``brain_lifecycle.provision_brain`` imported
        IN-PROCESS (the resource ledger and the provisioner share one
        process -- never a shell-out per resurrect), the manifest is the
        live ``ResourceManifest``, and the bucket is the persistent
        flock-journaled ``PersistentTokenBucket``. An injected bus stack
        WITHOUT a keeper seam stays keeper-less (the ``_build_durable``
        precedent: injected-seam tests must never touch the real repo
        ledger). Fail-soft: a keeper that cannot be built degrades to
        the keeper-less census rather than killing the driver."""
        # Stage-4 IMPORTANT-3: --no-keeper wins over an injected factory too.
        if self.keeper_mode is False:
            _log("brain keeper disabled (--no-keeper)")
            return None
        if self._keeper_factory is not None:
            try:
                return self._keeper_factory()
            except Exception as exc:  # noqa: BLE001
                _log("brain keeper unavailable (fail-soft): %s" % exc)
                return None
        if self._bus_factory is not None:
            return None
        if not self._keeper_enabled():
            _log("brain keeper disabled (JARVIS_BRAIN_KEEPER_ENABLED=false) "
                 "-- pre-Stage-4 degrade-and-wait")
            return None
        try:
            from backend.core.ouroboros.governance import brain_lifecycle  # noqa: PLC0415
            from backend.core.ouroboros.governance.brain_keeper import (  # noqa: PLC0415
                BrainKeeper,
                PersistentTokenBucket,
            )
            return BrainKeeper(
                discover_fn=self._do_discover,
                provision_fn=brain_lifecycle.provision_brain,
                manifest=brain_lifecycle.ResourceManifest(),
                bucket=PersistentTokenBucket(),
            )
        except Exception as exc:  # noqa: BLE001
            _log("brain keeper unavailable (fail-soft): %s" % exc)
            return None

    def _export_current_gen(self) -> int:
        """Export ``JARVIS_BRAIN_CURRENT_GEN`` for the discovery gen-filter
        (set at keeper construction + refreshed after each tick, which
        covers every resurrection). Gen 0 (no generation ever minted) is
        NOT exported -- exporting it would arm the filter and exclude a
        pre-Stage-4 unlabeled Brain before this keeper has minted
        anything. Returns the current gen (0 on any failure)."""
        if self._keeper is None:
            return 0
        try:
            gen = int(self._keeper.current_gen())
        except Exception:  # noqa: BLE001 -- fail-soft
            return self._exported_gen or 0
        if gen >= 1 and gen != self._exported_gen:
            os.environ["JARVIS_BRAIN_CURRENT_GEN"] = str(gen)
            self._exported_gen = gen
        return gen

    async def _keeper_tick(self) -> Optional[Tuple[str, int]]:
        """One keeper advance per census tick -> ``(state, gen)`` for the
        census line, or None (keeper-less / failed tick). Fail-soft."""
        if self._keeper is None:
            return None
        try:
            state = str(await self._keeper.tick())
        except Exception as exc:  # noqa: BLE001
            _log("keeper tick failed (fail-soft): %s" % exc)
            return None
        return (state, self._export_current_gen())

    async def _publish_keeper_heartbeat(
        self, trinity_bus: Any, keeper_info: Optional[Tuple[str, int]],
    ) -> None:
        """Stage-4 Task 4 (split-brain fence, Body side): publish the keeper's
        CURRENT generation as ``console.keeper_heartbeat`` on the local trinity
        bus every census tick. ``console.*`` is already on the mac-side
        TrinityBusBridge outbound allowlist (``_do_bridge``), so the beat
        transits the WS link onto the Brain organism's bus, where a superseded
        twin's GenerationFence observes a HIGHER gen and structurally fences
        itself. Gen 0 (nothing ever minted) is never published -- it would say
        nothing a fence could act on. ``persist=False``: a ~10s liveness beat
        must not accrete in the event store WAL. Fail-soft: a bus that cannot
        publish (or lacks publish_raw -- injected fakes) never kills the
        census."""
        if keeper_info is None:
            return
        _state, gen = keeper_info
        if gen < 1:
            return
        publish_raw = getattr(trinity_bus, "publish_raw", None)
        if publish_raw is None:
            return
        try:
            await publish_raw(
                "console.keeper_heartbeat", {"gen": int(gen)}, persist=False)
        except Exception as exc:  # noqa: BLE001 -- fail-soft
            _log("keeper heartbeat publish failed (fail-soft): %s" % exc)

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
        # Stage-3 Task 7 (cross-lifetime identity, live-fire finding A):
        # resolve the WAL path FIRST and seed the broker's event-id
        # sequence at the WAL high-water mark, so a restarted Body never
        # re-mints an id a previous lifetime already journaled (re-minted
        # ids were skipped as already-pending -- published-but-unjournaled
        # signals were silently lost during a partition, and the far
        # side's qualified-id dedup would drop the new events as
        # replays). Scan runs OFF-loop through the cooperative_fs_io
        # substrate; a broken offload falls back to the one-time inline
        # boot read (wal_high_water is itself fail-soft -> 0).
        broker = StreamEventBroker(
            initial_event_seq=await self._wal_high_water_seed())
        # Stage-3: build the durable WAL FIRST so the client constructor
        # receives it (WAL-seeded replay + on_ack trim, Task 3), and
        # thread the discovery re-race resolver (per-attempt).
        self._durable = self._build_durable(broker)
        self._durable_built = True
        cfg = TransportConfig.from_env(role="mac-body")
        dist_bus = DistributedEventBus(
            broker, cfg, role="client",
            durable_outbound=self._durable,
            url_resolver=self._do_discover,
        )
        return trinity_bus, broker, dist_bus

    async def _wal_high_water_seed(self) -> int:
        """Live-default broker seed (Stage-3 Task 7): the max event id
        EVER journaled into the durable outbound WAL -- tombstoned
        entries included. Resolves the SAME default WAL path the live
        DurableOutbound uses (``_default_wal_path``), so the seed and
        the journal always agree. Fail-soft: any failure degrades to 0
        (the legacy in-memory seed) rather than killing the driver."""
        try:
            from backend.core.ouroboros.governance.transport.durable_outbound import (  # noqa: PLC0415
                _default_wal_path,
                wal_high_water,
            )
        except Exception as exc:  # noqa: BLE001 -- fail-soft
            _log("WAL high-water seed unavailable (fail-soft): %s" % exc)
            return 0
        wal_path = _default_wal_path()
        try:
            from backend.core.ouroboros.governance.cooperative_fs_io import (  # noqa: PLC0415
                is_offload_error,
                offload,
            )
            result = await offload(wal_high_water, str(wal_path))
            seed = wal_high_water(wal_path) if is_offload_error(result) \
                else int(result)
        except Exception:  # noqa: BLE001 -- offload substrate unavailable:
            # one-time bounded boot read inline (wal_high_water never raises)
            seed = wal_high_water(wal_path)
        if seed:
            _log("broker event-seq seeded at %d (WAL high-water -- "
                 "cross-lifetime identity)" % seed)
        return seed

    def _wal_arming_intended(self) -> bool:
        """Structural arming intent, decided BEFORE anything is built:
        an injected durable_factory arms the WAL; the live default (no
        injected bus stack) always arms it; an injected bus stack
        WITHOUT a durable seam stays on the strict Stage-2 contract."""
        return self._durable_factory is not None or self._bus_factory is None

    def _build_durable(self, broker: Any) -> Optional[Any]:
        """Resolve the durable WAL seam. Fail-soft: a durable that
        cannot be built degrades to the strict (unarmed) behavior
        rather than killing the driver."""
        if self._durable_factory is not None:
            try:
                return self._durable_factory(broker)
            except Exception as exc:  # noqa: BLE001
                _log("durable outbound unavailable (fail-soft): %s" % exc)
                return None
        if self._bus_factory is not None:
            # Injected bus stack without a durable seam: the live
            # DurableOutbound needs a real broker -- stay unarmed.
            return None
        try:
            from backend.core.ouroboros.governance.transport.durable_outbound import (  # noqa: PLC0415
                DurableOutbound,
            )
            return DurableOutbound(
                broker, journal_filter=_journal_local_origin_only)
        except Exception as exc:  # noqa: BLE001
            _log("durable outbound unavailable (fail-soft): %s" % exc)
            return None

    def _do_bridge(self, trinity_bus: Any, broker: Any) -> Any:
        if self._bridge_factory is not None:
            return self._bridge_factory(trinity_bus, broker)
        from backend.core.ouroboros.governance.transport.trinity_bus_bridge import (  # noqa: PLC0415
            TrinityBusBridge,
        )
        return TrinityBusBridge(
            trinity_bus, broker,
            outbound_topics=["intake.remote_signal.*", "console.*", "causal.delta.*"],
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

    # -- census + deterministic degrade surfacing ----------------------------

    def _queued(self) -> int:
        """Durable queue depth (0 when the WAL is unarmed). Fail-soft."""
        if self._durable is None:
            return 0
        try:
            return int(self._durable.pending_count())
        except Exception:  # noqa: BLE001
            return 0

    def _update_link_state(self, connected: bool) -> None:
        """EDGE-triggered degrade surfacing (operator mandate): the UI
        state derives ONLY from the bus client's ``connected`` property
        -- set/cleared exactly at WS establishment/teardown inside
        ``_connect_once`` (the transport closure). Polled once per
        census tick; each episode transition logs exactly once. NO
        generalized try/except state, NO heartbeat-timeout heuristics.
        """
        if self._link_up is None:
            # First observation. A run that BEGINS dark is an offline
            # episode and must surface; a run that begins connected is
            # the quiet steady state.
            self._link_up = connected
            if not connected:
                _log("Brain offline -- %d signals queued (durable)"
                     % self._queued())
            return
        if connected == self._link_up:
            return  # steady state -- never repeat the episode line
        self._link_up = connected
        if connected:
            _log("Brain reconnected -- draining %d queued" % self._queued())
        else:
            _log("Brain offline -- %d signals queued (durable)"
                 % self._queued())

    def _census_tick(self, watchdog: Any, connected: bool,
                     keeper_info: Optional[Tuple[str, int]] = None) -> None:
        lag_events = int(getattr(watchdog, "lag_event_count", 0))
        try:
            records = watchdog.recent_lag_records()
        except Exception:  # noqa: BLE001
            records = []
        for r in records:
            self._worst_lag_ms = max(
                self._worst_lag_ms, float(getattr(r, "lag_ms", 0.0)))
        keeper_part = ""
        if keeper_info is not None:
            state, gen = keeper_info
            keeper_part = " gen=%d keeper=%s" % (gen, state)
        _log("lag_events=%d worst_ms=%.1f connected=%s queued=%d%s"
             % (lag_events, self._worst_lag_ms, connected, self._queued(),
                keeper_part))

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

        # 0. Brain KEEPER (Stage-4 Task 3) -- built BEFORE the first
        #    discovery so the initial result already feeds its absence
        #    window, and the discovery gen-filter env is exported from
        #    the persisted manifest at construction.
        self._keeper = self._do_keeper()
        self._export_current_gen()

        # 1. DISCOVER (stateless; fail-soft returns None). With the
        #    durable WAL armed (Stage 3), discovery failure DEGRADES
        #    instead of exiting: signals journal durably and the client
        #    re-races discovery per attempt. --require-brain (or an
        #    unarmed WAL) keeps the strict Stage-2 exit-2 contract.
        url = await self._do_discover()
        strict = self.require_brain or not self._wal_arming_intended()
        if not url:
            if strict:
                _log("Brain offline -- discovery returned no endpoint "
                     "(exit 2)")
                return 2
            _log("discovery returned no endpoint -- degrading durably "
                 "(WAL armed, reconnect re-racing)")
        else:
            _log("Brain discovered at %s" % url)

        # 2. Bus stack + durable WAL. The WAL arms BEFORE the client so
        #    journal-at-publish covers every event from the first
        #    publish (a partition can never lose an accepted signal).
        trinity_bus, broker, dist_bus = await self._do_bus_stack()
        if not self._durable_built:
            self._durable = self._build_durable(broker)
            self._durable_built = True
        if self._durable is not None:
            try:
                await self._durable.start()
            except Exception as exc:  # noqa: BLE001 -- fail-soft
                _log("durable WAL failed to arm (fail-soft): %s" % exc)
                self._durable = None
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
            # Connect gate (Stage-1 proven pattern). Strict contract:
            # never proceed on a dark link (exit 3). Degrade contract:
            # a dark link is an OFFLINE EPISODE, not an exit -- the WAL
            # is the sink and the client keeps re-racing. With no url at
            # all there is nothing to gate on; skip straight to the
            # degraded census.
            if url:
                if await _await_connected(
                        _env_float("JARVIS_BRAIN_CONNECT_GATE_S", 30.0)):
                    _log("bus client connected")
                elif strict:
                    _log("connect gate TIMEOUT -- WS client never "
                         "established (exit 3)")
                    return 3
                else:
                    _log("connect gate timeout -- degrading durably "
                         "(WAL armed, reconnect re-racing)")

            # Initial link-state observation: a run that begins dark
            # surfaces the canonical offline line exactly once here.
            self._update_link_state(_connected())

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
            #    Determinism contract: `connected` is polled EXACTLY once
            #    per tick; that single read feeds BOTH the edge machine
            #    and the census line (no re-read skew).
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
                connected = _connected()
                self._update_link_state(connected)
                keeper_info = await self._keeper_tick()
                await self._publish_keeper_heartbeat(trinity_bus, keeper_info)
                self._census_tick(watchdog, connected, keeper_info)

            # 6. Clean stop -> summary -> exit 0.
            connected = _connected()
            self._update_link_state(connected)
            keeper_info = await self._keeper_tick()
            await self._publish_keeper_heartbeat(trinity_bus, keeper_info)
            self._census_tick(watchdog, connected, keeper_info)
            return 0
        finally:
            lag_events = int(getattr(watchdog, "lag_event_count", 0)) \
                if watchdog is not None else 0
            queued_at_exit = self._queued()
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
            if self._durable is not None:
                try:
                    await self._durable.stop()
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
            _log("SUMMARY lag_events=%d worst_ms=%.1f signals_sent=%d "
                 "queued_at_exit=%d"
                 % (lag_events, self._worst_lag_ms, self._signals_sent,
                    queued_at_exit))


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
        "--require-brain", action="store_true",
        help="Strict Stage-2 contract: exit 2 when discovery fails and "
             "exit 3 on connect-gate timeout, even with the durable WAL "
             "armed (acceptance runs). Default: degrade durably -- journal "
             "signals to the WAL and keep re-racing discovery.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print the Body-mode plan and exit -- touches no network.",
    )
    # Stage-4 IMPORTANT-3: keeper master flag overrides. dest defaults to None
    # (neither given -> consult JARVIS_BRAIN_KEEPER_ENABLED, default false).
    p.add_argument(
        "--keeper", dest="keeper", action="store_true", default=None,
        help="Force-arm the Brain KEEPER (overrides "
             "JARVIS_BRAIN_KEEPER_ENABLED). Default: consult the env flag "
             "(off).",
    )
    p.add_argument(
        "--no-keeper", dest="keeper", action="store_false",
        help="Force the keeper OFF (wins over env + --keeper): pre-Stage-4 "
             "degrade-and-wait behavior.",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    driver = BodyModeDriver(
        inject_test_signals=args.inject_test_signal,
        duration_s=args.duration_s,
        dry_run=args.dry_run,
        require_brain=args.require_brain,
        keeper_mode=args.keeper,
    )
    try:
        return asyncio.run(driver.run())
    except KeyboardInterrupt:
        _log("interrupted -- clean stop")
        return 0


if __name__ == "__main__":
    sys.exit(main())
