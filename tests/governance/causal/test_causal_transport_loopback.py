# -*- coding: utf-8 -*-
"""Domain-1 Staging-1 Task 3 -- the adversarial two-process loopback (MANDATE 4).

The load-bearing proof of the transport hop. Task 1 built the Body
``StructuralDeltaSensor`` (publishes ``causal.delta.<repo>``); Task 2 built the
Brain ``CausalDeltaSubscriber`` (reflective RepoType, fingerprint dedup, causal
order). This test drives them TOGETHER over a REAL localhost WS pair
(``DistributedEventBus`` client <-> server, TLS disabled via env) under
adversarial concurrent load: three sensors (jarvis/prime/reactor) fire N
DISTINCT deltas simultaneously via ``asyncio.gather``, with a fraction re-emitted
as EXACT duplicates, while a concurrent ticker proves the Brain loop never
starved.

MANDATE-2 IN-PAYLOAD IDENTITY (the design this test proves survives the hop):

* The RepoType origin rides as REFLECTIVE METADATA INSIDE the data payload
  (``payload["lineage"]["repo"]`` == ``delta["repo"]``), NOT typed on the
  transport. The generic ``TrinityBusBridge`` carries ``{topic, data, origin}``
  verbatim across the WS but re-mints ``TrinityEvent.source`` to the receiver
  bus's ``local_repo`` -- so ``event.source`` is NOT a reliable origin over a
  cross-host hop and is deliberately never read. The Brain
  ``CausalDeltaSubscriber`` reflects the origin from the surviving in-payload
  ``lineage.repo`` (``RepoType(...)``), so its ``observed()`` carries the TRUE
  per-repo identity end-to-end. This test asserts MANDATE-4 DIRECTLY against
  ``observed()`` -- no head_sha / event.source workaround.

The MANDATE-4 assertions (all condition-polled to a bounded deadline; NO bare
sleep-as-sync):

  1. ``observed_count() == 3*N`` -- every distinct delta crossed; duplicates
     dropped. ``set(observed())`` has exactly ``3*N`` unique
     ``(repo, emit_seq, head_sha)`` tuples with the CORRECT (in-payload) repo.
  2. The BODY bus's ``events_deduplicated`` advanced by EXACTLY the injected
     duplicate count (same topic+source+payload within the 60s fingerprint
     window collided and was dropped at publish -- the dups never left the Body).
  3. For EACH origin repo, the ``emit_seq`` subsequence of ``observed()``
     filtered to that repo is the strict ascending ``1..N`` -- causal
     chronological order within source, preserved end-to-end.
  4. NON-BLOCKING: a concurrent ~5ms ticker task kept ticking THROUGH the storm;
     its tick count sits within a band tied to the measured wall-clock -- the
     Brain ingest never starved the loop.

Run 3x consecutively for flake-immunity.

Isolation notes: ``TRINITY_MULTICAST_ENABLED=false`` suppresses the in-process
UDP shortcut (precedent: test_trinity_bus_bridge.py::_mk_bus). The disk file-sync
transport (``CrossRepoTransport._file_sync_loop``, 1s poll) would otherwise
double-deliver and bleed state across iterations, so each bus's ``_sync_dir`` is
repointed to a UNIQUE temp dir -- the WS is then the ONLY cross-bus path, which is
the hop under test.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

from backend.core.trinity_event_bus import RepoType, TrinityEventBus
from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEventBroker,
)
from backend.core.ouroboros.governance.transport.transport_config import (
    TransportConfig,
)
from backend.core.ouroboros.governance.transport.distributed_event_bus import (
    DistributedEventBus,
)
from backend.core.ouroboros.governance.transport.trinity_bus_bridge import (
    TrinityBusBridge,
)
from backend.core.ouroboros.governance.causal.causal_delta_subscriber import (
    CausalDeltaSubscriber,
)
from backend.core.ouroboros.governance.causal.structural_delta_sensor import (
    GitLineage,
    StructuralDeltaSensor,
)
from backend.core.ouroboros.governance.causal.structural_delta import EmitSequence

# --------------------------------------------------------------------------- #
# Adversarial parameters
# --------------------------------------------------------------------------- #
REPOS: Tuple[str, ...] = ("jarvis", "prime", "reactor")
N = 8                       # distinct deltas PER repo -> 3*N total
DUP_INDICES: Tuple[int, ...] = (0, 5, 10, 15, 20, 23)  # which captured events to
#                                                       re-emit as EXACT dups
DUP_COUNT = len(DUP_INDICES)
ITERATIONS = 3              # flake-immunity


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _before_after(repo: str, i: int) -> Tuple[str, str]:
    """A structurally-distinct before/after pair for (repo, i): the AFTER adds a
    uniquely-named function, so every delta has a distinct fingerprint."""
    before = "def base():\n    return 0\n"
    after = (
        "def base():\n    return 0\n\n"
        "def g_%s_%d():\n    return %d\n" % (repo, i, i)
    )
    return before, after


def _real_sync_dir() -> Path:
    """The bus's real sync dir (sandbox_fallback-resolved), so a pre-create
    clean removes any stale ``*_events.jsonl`` before the transport's first poll
    can replay it."""
    from backend.core.ouroboros.governance.sandbox_paths import sandbox_fallback
    return sandbox_fallback(Path.home() / ".jarvis" / "trinity" / "bus_sync")


def _clean_real_sync_dir() -> None:
    try:
        for stale in _real_sync_dir().glob("*_events.jsonl"):
            stale.unlink()
    except Exception:  # noqa: BLE001 -- best-effort isolation
        pass


async def _await_connected(client_bus: Any, timeout: float = 5.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        client = getattr(client_bus, "_client", None)
        if client is not None and getattr(client, "connected", False):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("client never reported connected")


async def _settle_count(sub: Any, expected: int, timeout: float = 20.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if sub.observed_count() >= expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        "brain never observed %d deltas (stuck at %d)"
        % (expected, sub.observed_count()))


class _Ticker:
    """A ~5ms heartbeat proving the event loop kept turning under the storm."""

    def __init__(self, interval: float = 0.005) -> None:
        self._interval = interval
        self.ticks = 0
        self._stop = False
        self._task: Optional[asyncio.Task] = None
        self._started_at = 0.0
        self._stopped_at = 0.0

    async def _loop(self) -> None:
        while not self._stop:
            self.ticks += 1
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        self._started_at = asyncio.get_event_loop().time()
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self._stop = True
        self._stopped_at = asyncio.get_event_loop().time()
        if self._task is not None:
            try:
                await self._task
            except Exception:  # noqa: BLE001
                pass

    @property
    def elapsed(self) -> float:
        return max(1e-6, self._stopped_at - self._started_at)


async def _one_iteration(iteration: int) -> str:
    """Build a REAL Body<->Brain WS pair, run the adversarial storm, assert the
    four MANDATE-4 invariants, tear everything down. Returns a one-line summary.
    """
    _clean_real_sync_dir()

    port = _free_port()
    os.environ["JARVIS_BRAIN_WS_TLS_ENABLED"] = "false"
    os.environ["JARVIS_BRAIN_WS_HOST"] = "127.0.0.1"
    os.environ["JARVIS_BRAIN_WS_PORT"] = str(port)
    os.environ["JARVIS_BRAIN_WS_HEARTBEAT_S"] = "1.0"
    os.environ["TRINITY_MULTICAST_ENABLED"] = "false"

    server_cfg = TransportConfig.from_env(role="server")
    client_cfg = TransportConfig.from_env(role="client")

    tmp_dirs: List[str] = []
    body_bus = None
    brain_bus = None
    body_bridge = None
    brain_bridge = None
    sub = None
    client_bus = None
    client_task = None
    runner = None
    ticker = _Ticker()

    try:
        # -- buses (repoint sync dirs to isolated temp dirs: WS-only crossing) -
        body_bus = await TrinityEventBus.create(local_repo=RepoType.JARVIS)
        brain_bus = await TrinityEventBus.create(local_repo=RepoType.JARVIS)
        for bus in (body_bus, brain_bus):
            d = tempfile.mkdtemp(prefix="causal_loopback_")
            tmp_dirs.append(d)
            bus._transport._sync_dir = Path(d)

        # -- brokers + Stage-0 distributed buses ---------------------------- #
        server_broker = StreamEventBroker()
        client_broker = StreamEventBroker()
        server_bus = DistributedEventBus(server_broker, server_cfg, role="server")
        client_bus = DistributedEventBus(client_broker, client_cfg, role="client")

        # -- bridges: Body mirrors causal.delta.* outbound; Brain drains inbound
        body_bridge = TrinityBusBridge(
            body_bus, client_broker,
            outbound_topics=["causal.delta.*"], source_id="mac-body")
        brain_bridge = TrinityBusBridge(
            brain_bus, server_broker,
            outbound_topics=[], source_id="brain")
        await body_bridge.start()
        await brain_bridge.start()

        # -- Brain subscriber (reads the in-payload lineage.repo identity) -- #
        sub = CausalDeltaSubscriber(brain_bus)
        await sub.start()

        # -- real localhost WS server + client ----------------------------- #
        app = web.Application()
        server_bus.register_server_routes(app)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="127.0.0.1", port=port)
        await site.start()
        url = "ws://127.0.0.1:%d%s" % (port, client_cfg.path)
        client_task = asyncio.ensure_future(client_bus.start_client(url))
        await _await_connected(client_bus)

        # -- three sensors, distinct durable emit sequences per repo -------- #
        sensors: Dict[str, StructuralDeltaSensor] = {}
        for repo in REPOS:
            seq_path = tempfile.mkdtemp(prefix="emit_seq_%s_" % repo)
            tmp_dirs.append(seq_path)
            seq = EmitSequence(path=os.path.join(seq_path, "seq.jsonl"))
            sensors[repo] = StructuralDeltaSensor(body_bus, repo=repo, emit_seq=seq)

        # -- capture the exact TrinityEvents the sensors publish (so a dup is a
        #    byte-identical re-publish -> same fingerprint). --------------- #
        published: List[Any] = []
        _orig_publish = body_bus.publish

        async def _spy_publish(event):
            published.append(event)
            return await _orig_publish(event)

        body_bus.publish = _spy_publish  # type: ignore[assignment]

        # -- THE STORM: all 3 repos x N deltas, fully concurrent ----------- #
        ticker.start()

        async def _emit(repo: str, i: int):
            before, after = _before_after(repo, i)
            return await sensors[repo].emit_file_change(
                "%s/f%d.py" % (repo, i), before, after,
                lineage=GitLineage(
                    head_sha="%ssha%d" % (repo, i),
                    parent_sha="%spar%d" % (repo, i),
                    merge_base="%smb" % repo,
                ),
            )

        storm = [_emit(repo, i) for repo in REPOS for i in range(1, N + 1)]
        emit_ids = await asyncio.gather(*storm)
        # every distinct emit produced a real publish (structural change present)
        assert all(eid for eid in emit_ids), (
            "iter %d: a sensor emit returned None (no publish)" % iteration)
        assert len(published) == 3 * N, (
            "iter %d: expected %d captured publishes, got %d"
            % (iteration, 3 * N, len(published)))

        # -- INJECT EXACT DUPLICATES: re-publish captured events (bypass spy so
        #    published[] stays the distinct set). Same topic+source+payload
        #    within the 60s window -> the Body bus fingerprint-dedups each. --
        dedup_before = body_bus._metrics.events_deduplicated
        dups = [_orig_publish(published[k]) for k in DUP_INDICES]
        await asyncio.gather(*dups)

        # -- settle: brain must observe exactly the 3*N distinct deltas ----- #
        await _settle_count(sub, 3 * N)
        await asyncio.sleep(0.3)  # drain window (NOT a sync): catch any straggler
        await ticker.stop()

        observed = sub.observed()
        dedup_after = body_bus._metrics.events_deduplicated

        # ================= MANDATE-4 ASSERTIONS ========================= #
        # (1) exactly 3*N distinct crossed; dups dropped. observed() carries the
        #     TRUE in-payload (repo, emit_seq, head_sha) identity.
        assert sub.observed_count() == 3 * N, (
            "iter %d: observed_count %d != %d"
            % (iteration, sub.observed_count(), 3 * N))
        expected = {
            (repo, i, "%ssha%d" % (repo, i))
            for repo in REPOS for i in range(1, N + 1)
        }
        assert set(observed) == expected, (
            "iter %d: observed() identity set mismatch (lost/extra deltas or "
            "wrong repo): %r" % (iteration, sorted(set(observed) ^ expected)))
        assert len(set(observed)) == 3 * N, (
            "iter %d: observed() has non-unique tuples: %r"
            % (iteration, observed))

        # (2) the BODY bus deduplicated EXACTLY the injected dup count.
        assert dedup_after - dedup_before == DUP_COUNT, (
            "iter %d: body dedup advanced by %d, expected %d"
            % (iteration, dedup_after - dedup_before, DUP_COUNT))
        # nothing was deduped on the Brain (every crossing delta was distinct).
        assert brain_bus._metrics.events_deduplicated == 0, (
            "iter %d: brain deduped %d (expected 0)"
            % (iteration, brain_bus._metrics.events_deduplicated))

        # (3) per-origin causal order preserved end-to-end: filter observed() to
        #     each origin repo (the TRUE in-payload identity) -> strict
        #     ascending 1..N.
        for repo in REPOS:
            seqs = [seq for (r, seq, _sha) in observed if r == repo]
            assert seqs == list(range(1, N + 1)), (
                "iter %d: repo %s emit_seq order %r != %r"
                % (iteration, repo, seqs, list(range(1, N + 1))))

        # (4) NON-BLOCKING: the ticker kept turning through the storm.
        expected_ticks = ticker.elapsed / 0.005
        floor = max(10, int(expected_ticks * 0.25))
        assert ticker.ticks >= floor, (
            "iter %d: ticker starved -- %d ticks over %.3fs (floor %d)"
            % (iteration, ticker.ticks, ticker.elapsed, floor))

        return (
            "iter %d OK: observed=%d dups_dropped=%d/%d "
            "ticks=%d/%.0f(elapsed=%.2fs) per_repo_order=strict[1..%d]"
            % (iteration, sub.observed_count(), dedup_after - dedup_before,
               DUP_COUNT, ticker.ticks, expected_ticks, ticker.elapsed, N))
    finally:
        # restore the patched method (defensive; bus is discarded anyway)
        if body_bus is not None and hasattr(body_bus, "publish"):
            try:
                del body_bus.publish  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                pass
        if ticker._task is not None and not ticker._stop:
            await ticker.stop()
        if sub is not None:
            try:
                await sub.stop()
            except Exception:  # noqa: BLE001
                pass
        for bridge in (body_bridge, brain_bridge):
            if bridge is not None:
                try:
                    await bridge.stop()
                except Exception:  # noqa: BLE001
                    pass
        if client_bus is not None:
            try:
                await client_bus.stop()
            except Exception:  # noqa: BLE001
                pass
        if client_task is not None:
            client_task.cancel()
            try:
                await client_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:  # noqa: BLE001
                pass
        for bus in (body_bus, brain_bus):
            if bus is not None:
                try:
                    await bus.stop()
                except Exception:  # noqa: BLE001
                    pass
        for d in tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)
        _clean_real_sync_dir()


# --------------------------------------------------------------------------- #
# The 3x flake-immunity driver.
# --------------------------------------------------------------------------- #
def test_adversarial_concurrent_transport_loopback():
    async def driver():
        summaries: List[str] = []
        for it in range(1, ITERATIONS + 1):
            summaries.append(await _one_iteration(it))
        return summaries

    summaries = _run(driver())
    # surface the per-run evidence (visible with -s / on failure).
    print("\n".join(summaries))
    assert len(summaries) == ITERATIONS
