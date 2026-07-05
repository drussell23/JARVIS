"""brain_keeper.py -- Stage-4 Task 3: the Body-side Brain KEEPER.

The keeper is the Body's answer to a dead Brain: it watches the driver's
existing discovery/census outcomes (``note_discovery_result``), detects
SUSTAINED absence (a continuous window with no healthy Brain endpoint), and
resurrects a new Brain GENERATION under a persistent token-bucket rate cap.

Components
----------
* ``PersistentTokenBucket``: a flock-journaled take ledger. The journal IS the
  state -- ``try_take`` replays the JSONL file and counts takes inside the
  rolling window, so the cap is deterministic across process restarts
  (Mandate 1). NO sleeps, NO retry/backoff logic lives here: the bucket only
  answers "may I spend a VM right now" from the on-disk record.
* ``BrainKeeper``: the absence-window state machine. States:
  ``healthy`` | ``absent`` | ``resurrecting`` | ``resurrected`` |
  ``cap_exhausted`` (TERMINAL). A refused take is a deterministic terminal
  state -- ``tick`` NEVER retries past it; only a NEW keeper process with
  restored bucket capacity can resurrect again (Mandate 1: no endless retry).

Wall-clock choice (documented, load-bearing)
--------------------------------------------
The bucket window uses ``time.time()`` (wall clock), NOT ``time.monotonic()``:
the window is a human-scale BILLING guard ("at most N VM creates per hour of
real time") and must survive process restarts -- monotonic clocks reset per
process, so a restart would forget every prior take. A wall-clock step
(NTP/DST) can widen or narrow one window edge; that is acceptable for a
billing guard and irreparable any other way without external state.

Failed attempts consume capacity BY DESIGN: the token is taken BEFORE the
provision call, and a failed provision does not refund it. The bucket guards
the VM FACTORY (attempts that can spend money), not successful births -- a
crash-looping provision path must exhaust the cap, not spin the factory.

Record-at-birth (closes Task-1 concern #3): the manifest ``record_create``
is appended BEFORE the provision await -- exactly the semantics
``ResourceManifest.record_create`` documents ("called AT BIRTH, right when
the resource is requested") -- so a keeper death mid-provision still leaves
the child on the manifest for the next teardown walk. A cleanly-failed
provision leaves the record in place (conservative: a partial create is
reaped by the walk; a never-created node's delete is a 404 no-op).

Env knobs (all resolved at call time -- zero baked assumptions):
    JARVIS_RESURRECT_BUCKET_PATH     bucket journal path
                                     (default <repo>/.jarvis/manifests/resurrect_bucket.jsonl)
    JARVIS_BRAIN_RESURRECT_WINDOW_S  rolling window seconds (default 3600.0)
    JARVIS_BRAIN_RESURRECT_MAX_PER_H takes allowed per window (default 2)
    JARVIS_BRAIN_RESURRECT_AFTER_S   sustained-absence threshold (default 900.0)
    JARVIS_KEEPER_ID                 keeper identity (default "mac-body-keeper")
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from backend.core.ouroboros.governance.brain_lifecycle import (
    LABEL_GEN,
    LABEL_OWNER,
    LABEL_PARENT,
    ResourceManifest,
    sanitize_label_value,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env knobs.
# ---------------------------------------------------------------------------

_ENV_BUCKET_PATH = "JARVIS_RESURRECT_BUCKET_PATH"
_ENV_WINDOW_S = "JARVIS_BRAIN_RESURRECT_WINDOW_S"
_DEFAULT_WINDOW_S = 3600.0
_ENV_CAPACITY = "JARVIS_BRAIN_RESURRECT_MAX_PER_H"
_DEFAULT_CAPACITY = 2
_ENV_RESURRECT_AFTER_S = "JARVIS_BRAIN_RESURRECT_AFTER_S"
_DEFAULT_RESURRECT_AFTER_S = 900.0
_ENV_KEEPER_ID = "JARVIS_KEEPER_ID"
_DEFAULT_KEEPER_ID = "mac-body-keeper"
# Stage-4 Task-4 (IMPORTANT-2): the bounded grace between DELIVERING the fence
# heartbeat to a superseded node (its deterministic self-fence + capture_inflight
# window) and REAPING it. Env-tunable; the keeper never blocks past this.
_ENV_FENCE_GRACE_S = "JARVIS_BRAIN_FENCE_GRACE_S"
_DEFAULT_FENCE_GRACE_S = 20.0
_ENV_FENCE_DELIVER_TIMEOUT_S = "JARVIS_BRAIN_FENCE_DELIVER_TIMEOUT_S"
_DEFAULT_FENCE_DELIVER_TIMEOUT_S = 15.0


def _default_bucket_path() -> Path:
    # backend/core/ouroboros/governance/brain_keeper.py -> repo root is 4 up.
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / ".jarvis" / "manifests" / "resurrect_bucket.jsonl"


def _env_float(name: str, default: float) -> float:
    try:
        v = float((os.environ.get(name, "") or "").strip())
        return v if v > 0 else default
    except (ValueError, AttributeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        return int(raw) if raw else default
    except (ValueError, AttributeError):
        return default


# ---------------------------------------------------------------------------
# PersistentTokenBucket -- the journal IS the state.
# ---------------------------------------------------------------------------


class PersistentTokenBucket:
    """Flock-journaled rate cap for Brain resurrections.

    Each successful take appends one JSONL record
    ``{"ts_utc": ..., "ts_wall": <time.time()>, "gen": N}``. ``try_take``
    replays the journal fresh on every call: takes whose ``ts_wall`` falls
    inside the rolling window count against the capacity; at/over capacity
    the take is REFUSED WITHOUT APPENDING. Deterministic across process
    restarts (Mandate 1) -- and deliberately free of sleeps/retries/backoff:
    rate POLICY lives in the keeper's terminal state, not here.

    Atomicity (review fix, IMPORTANT-1): the ENTIRE read-count-decide-append
    runs inside ONE ``flock_critical_section`` acquisition (the canonical
    ``cross_process_jsonl`` read-modify-write helper, the InvariantDriftStore
    precedent -- never hand-rolled locking). An unlocked decide followed by a
    locked append is a cross-process TOCTOU: two racing processes (a zombie
    driver overlapping a fresh one) could both count capacity-1 and both
    append, exceeding the cap by one. The append inside the section is
    caller-owned direct I/O per that helper's documented contract -- the lock
    is NON-reentrant (in-process ``threading.Lock`` + ``LOCK_NB`` poll), so
    nesting ``flock_append_line`` (the intake-WAL ``_write_line`` path) inside
    it would self-deadlock until timeout. Fail-CLOSED: a take that cannot
    acquire the lock is REFUSED -- a billing guard that cannot prove capacity
    must never spend.

    Wall clock (``time.time()``) is intentional -- see the module docstring.
    """

    def __init__(self, path: Optional[Any] = None) -> None:
        if path is not None:
            resolved = Path(path)
        else:
            raw = (os.environ.get(_ENV_BUCKET_PATH, "") or "").strip()
            resolved = Path(raw) if raw else _default_bucket_path()
        self._path = resolved
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # fail-soft: flock_critical_section re-mkdirs on the take path

    @property
    def path(self) -> Path:
        return self._path

    def _durable_path(self) -> Path:
        """The overlay-rerooted journal path (identity on plain hosts) --
        resolved at CALL time so replay reads and the locked append always
        target the SAME file ``flock_critical_section`` locks. Fail-soft to
        the raw path."""
        try:
            from backend.core.ouroboros.governance.workspace_resolver import (  # noqa: PLC0415
                resolve_durable_path,
            )

            return Path(resolve_durable_path(self._path))
        except Exception:  # noqa: BLE001
            return self._path

    def _replay(self) -> List[Dict[str, Any]]:
        """Tolerant journal replay (WAL semantics: corrupt lines skipped)."""
        records: List[Dict[str, Any]] = []
        journal = self._durable_path()
        if not journal.exists():
            return records
        try:
            with journal.open("r", encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(
                            "[ResurrectBucket] corrupt line %d in %s -- skipping",
                            line_no, self._path,
                        )
                        continue
                    if isinstance(rec, dict):
                        records.append(rec)
        except OSError as exc:
            logger.warning("[ResurrectBucket] read failed (%r) -- empty replay", exc)
        return records

    def taken_in_window(self, now_wall: Optional[float] = None) -> int:
        """Count of takes whose ``ts_wall`` is inside the rolling window."""
        now = time.time() if now_wall is None else float(now_wall)
        window_s = _env_float(_ENV_WINDOW_S, _DEFAULT_WINDOW_S)
        count = 0
        for rec in self._replay():
            try:
                ts_wall = float(rec.get("ts_wall"))
            except (TypeError, ValueError):
                continue
            if (now - ts_wall) <= window_s:
                count += 1
        return count

    def capacity(self) -> int:
        return _env_int(_ENV_CAPACITY, _DEFAULT_CAPACITY)

    def try_take(self, gen: Optional[int] = None) -> bool:
        """Spend one resurrection token, or refuse.

        The WHOLE read-count-decide-append runs under one
        ``flock_critical_section`` acquisition (no cross-process TOCTOU --
        see the class docstring). At/over capacity the refusal appends
        NOTHING (a refused caller must leave no trace); a lock that cannot
        be acquired REFUSES (fail-closed billing guard). On a grant, the
        take record is appended inside the held section (caller-owned
        direct I/O -- the lock is non-reentrant) and True is returned.
        No sleeps, no retries, no backoff -- ever (Mandate 1)."""
        record: Dict[str, Any] = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "ts_wall": time.time(),
            "gen": gen,
        }
        try:
            line = json.dumps(record, default=str)
        except (TypeError, ValueError):
            return False
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: PLC0415
                flock_critical_section,
            )
        except Exception as exc:  # noqa: BLE001 -- fail-CLOSED, loudly
            logger.error(
                "[ResurrectBucket] locking substrate unavailable (%r) -- "
                "REFUSING take (fail-closed billing guard)", exc,
            )
            return False
        with flock_critical_section(self._path) as locked:
            if not locked:
                logger.error(
                    "[ResurrectBucket] could not acquire the bucket lock for "
                    "%s -- REFUSING take (fail-closed: cannot prove capacity)",
                    self._path,
                )
                return False
            if self.taken_in_window(float(record["ts_wall"])) >= self.capacity():
                return False
            try:
                journal = self._durable_path()
                journal.parent.mkdir(parents=True, exist_ok=True)
                with journal.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                logger.error(
                    "[ResurrectBucket] take append failed (%r) -- REFUSING "
                    "(no journal record, no grant)", exc,
                )
                return False
        return True


# ---------------------------------------------------------------------------
# BrainKeeper -- the sustained-absence resurrection state machine.
# ---------------------------------------------------------------------------

_STATE_HEALTHY = "healthy"
_STATE_ABSENT = "absent"
_STATE_RESURRECTING = "resurrecting"
_STATE_RESURRECTED = "resurrected"
_STATE_CAP_EXHAUSTED = "cap_exhausted"  # TERMINAL


# ---------------------------------------------------------------------------
# Live-default fence delivery (IMPORTANT-2). The keeper ACTIVELY delivers the
# fence heartbeat to a known superseded node -- a single-bridge Body only binds
# the newest gen, so the old node would otherwise never receive the signal that
# triggers its deterministic self-fence + capture_inflight.
# ---------------------------------------------------------------------------


async def _fence_deliver_impl(node_name: str, gen: int) -> bool:
    """Open a SHORT-LIVED heartbeat-only client to the old node's bus and
    publish ``console.keeper_heartbeat{gen}`` -- the run_body_mode idiom
    (DistributedEventBus client role + TrinityBusBridge outbound ``console.*``)
    but pointed at ONE known endpoint. Returns True iff the publish was issued."""
    from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
        get_compute_rest,
    )
    from backend.core.ouroboros.governance.transport.transport_config import (  # noqa: PLC0415
        TransportConfig,
    )
    from backend.core.ouroboros.governance.transport.distributed_event_bus import (  # noqa: PLC0415
        DistributedEventBus,
    )
    from backend.core.ouroboros.governance.transport.trinity_bus_bridge import (  # noqa: PLC0415
        TrinityBusBridge,
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: PLC0415
        StreamEventBroker,
    )
    from backend.core.trinity_event_bus import get_trinity_event_bus  # noqa: PLC0415

    internal_ip, external_ip = await get_compute_rest().get_node_endpoints(
        node_name)
    host = external_ip or internal_ip
    if not host:
        return False
    cfg = TransportConfig.from_env(role="mac-body")
    scheme = "wss" if getattr(cfg, "tls_enabled", False) else "ws"
    url = "%s://%s:%d%s" % (scheme, host, cfg.port, cfg.path)
    trinity_bus = await get_trinity_event_bus()
    broker = StreamEventBroker()
    dist = DistributedEventBus(broker, cfg, role="client")
    bridge = TrinityBusBridge(
        trinity_bus, broker, outbound_topics=["console.*"],
        source_id="mac-body-keeper",
    )
    task = asyncio.ensure_future(dist.start_client(url))
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            client = getattr(dist, "_client", None)
            if client is not None and getattr(client, "connected", False):
                break
            await asyncio.sleep(0.1)
        await bridge.start()
        await trinity_bus.publish_raw(
            "console.keeper_heartbeat", {"gen": int(gen)}, persist=False)
        # Let the frame flush + the old node observe it and self-fence.
        await asyncio.sleep(1.0)
        return True
    finally:
        try:
            await bridge.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await dist.stop()
        except Exception:  # noqa: BLE001
            pass
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


async def _default_fence_deliver_fn(node_name: str, gen: int) -> bool:
    """Bounded, fail-soft wrapper: deliver the fence heartbeat under a hard
    timeout so an unreachable node can NEVER block the keeper. On any failure
    the caller falls through to the reap backstop after the grace."""
    import asyncio as _asyncio  # noqa: PLC0415
    budget = _env_float(_ENV_FENCE_DELIVER_TIMEOUT_S, _DEFAULT_FENCE_DELIVER_TIMEOUT_S)
    try:
        return bool(await _asyncio.wait_for(
            _fence_deliver_impl(node_name, gen), timeout=budget))
    except Exception as exc:  # noqa: BLE001 -- delivery is best-effort; reap backstops
        logger.warning(
            "[BrainKeeper] fence delivery to %s fail-soft (falling through to "
            "reap): %r", node_name, exc,
        )
        return False


class BrainKeeper:
    """Detect sustained Brain absence; resurrect a new generation, capped.

    The Body driver feeds every discovery outcome into
    ``note_discovery_result`` (truthy url resets the absence window; falsy
    starts/continues it) and awaits ``tick`` once per census tick. When the
    absence window has continuously exceeded ``resurrect_after_s`` and no
    resurrection is in flight, one bucket token is spent and ONE provision is
    issued (single-flight). A refused token is the deterministic TERMINAL
    ``cap_exhausted`` state: loud ``logger.error`` exactly once, then every
    subsequent ``tick`` returns the terminal state without retrying -- only a
    NEW keeper process with restored bucket capacity can resurrect (Mandate 1).

    ``discover_fn`` is used by ``tick`` ONLY in the ``resurrected`` state (no
    fresh discovery feed yet): one confirmation probe per tick closes the
    resurrected -> healthy loop; a falsy probe starts a fresh absence window
    for the NEW node (a still-dark resurrection re-arms the machine, bounded
    by the bucket).
    """

    def __init__(
        self,
        *,
        discover_fn: Callable[[], Awaitable[Optional[str]]],
        provision_fn: Callable[..., Awaitable[Any]],
        manifest: ResourceManifest,
        bucket: PersistentTokenBucket,
        resurrect_after_s: Optional[float] = None,
        keeper_id: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
        # Stage-4 Task-4 (IMPORTANT-2/4): fence-supersession seams. All None ->
        # the live defaults (real fence delivery + real GCP reap) resolve
        # lazily; tests inject recorders.
        fence_deliver_fn: Optional[Callable[[str, int], Awaitable[bool]]] = None,
        delete_instance_fn: Optional[Callable[..., Awaitable[Any]]] = None,
        delete_firewall_fn: Optional[Callable[..., Awaitable[Any]]] = None,
        label_query_fn: Optional[Callable[..., Awaitable[Any]]] = None,
        fence_grace_s: Optional[float] = None,
    ) -> None:
        self._discover_fn = discover_fn
        self._provision_fn = provision_fn
        self._manifest = manifest
        self._bucket = bucket
        self._fence_deliver_fn = fence_deliver_fn
        self._delete_instance_fn = delete_instance_fn
        self._delete_firewall_fn = delete_firewall_fn
        self._label_query_fn = label_query_fn
        self._fence_grace_s = (
            float(fence_grace_s) if fence_grace_s is not None
            else _env_float(_ENV_FENCE_GRACE_S, _DEFAULT_FENCE_GRACE_S)
        )
        self._resurrect_after_s = (
            float(resurrect_after_s) if resurrect_after_s is not None
            else _env_float(_ENV_RESURRECT_AFTER_S, _DEFAULT_RESURRECT_AFTER_S)
        )
        if keeper_id is not None:
            self._keeper_id = str(keeper_id)
        else:
            raw = (os.environ.get(_ENV_KEEPER_ID, "") or "").strip()
            self._keeper_id = raw or _DEFAULT_KEEPER_ID
        self._clock = clock

        self._state = _STATE_HEALTHY
        self._absence_started: Optional[float] = None
        self._inflight = False

    # -- read surface ---------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def current_gen(self) -> int:
        """The highest generation ever minted (manifest replay)."""
        return self._manifest.max_gen()

    # -- discovery feed -------------------------------------------------------

    def note_discovery_result(self, url: Optional[str]) -> None:
        """The Body driver calls this from its existing discovery/census path.

        Truthy url: the Brain answered -- the absence window resets; a
        non-terminal, non-inflight keeper returns to ``healthy``. Falsy: the
        window starts (first miss) or continues (already running -- the start
        timestamp is NEVER refreshed by later misses: 'continuously absent').

        ``cap_exhausted`` is terminal BY MANDATE: even a recovered Brain never
        un-exhausts this keeper (the state persists as the audit trail)."""
        if url:
            self._absence_started = None
            if self._state != _STATE_CAP_EXHAUSTED and not self._inflight:
                self._state = _STATE_HEALTHY
            return
        if self._absence_started is None:
            self._absence_started = self._clock()

    # -- the tick FSM ---------------------------------------------------------

    async def tick(self) -> str:
        """Advance the state machine once; returns the current state."""
        if self._state == _STATE_CAP_EXHAUSTED:
            return self._state  # TERMINAL -- never retries, never re-logs
        if self._inflight:
            return _STATE_RESURRECTING  # single-flight: one provision at a time

        if self._state == _STATE_RESURRECTED and self._absence_started is None:
            # Confirmation probe for the freshly-minted node (fail-soft).
            url: Optional[str] = None
            try:
                url = await self._discover_fn()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[BrainKeeper] confirm probe fail-soft err=%r", exc)
            self.note_discovery_result(url)
            if url:
                return self._state  # -> healthy via note_discovery_result

        if self._absence_started is None:
            return self._state  # healthy / resurrected steady state

        elapsed = self._clock() - self._absence_started
        if elapsed <= self._resurrect_after_s:
            self._state = _STATE_ABSENT
            return self._state

        # Sustained absence -> spend a token, then single-flight resurrect.
        gen = self._manifest.max_gen() + 1
        if not self._bucket.try_take(gen=gen):
            self._state = _STATE_CAP_EXHAUSTED
            logger.error(
                "[BrainKeeper] resurrection cap EXHAUSTED (%d takes / %.0fs "
                "window) -- TERMINAL: this keeper will NEVER retry; a new "
                "keeper process with restored bucket capacity is required "
                "(keeper_id=%s absence_s=%.0f)",
                self._bucket.capacity(),
                _env_float(_ENV_WINDOW_S, _DEFAULT_WINDOW_S),
                self._keeper_id, elapsed,
            )
            return self._state

        self._inflight = True
        self._state = _STATE_RESURRECTING
        try:
            return await self._resurrect(gen)
        finally:
            self._inflight = False

    async def _resurrect(self, gen: int) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        node_name = "jarvis-brain-gen%d-%s" % (gen, stamp)
        labels = {
            LABEL_OWNER: self._keeper_id,
            LABEL_PARENT: sanitize_label_value(self._keeper_id),
            LABEL_GEN: str(gen),
        }
        extra_env = {
            "JARVIS_BRAIN_GENERATION": str(gen),
            "JARVIS_BRAIN_PARENT_NODE": node_name,
            # Stage-4 CRITICAL-1: ship the keeper id onto the Brain so any
            # node ITS failover path awakens (the grandchild) can stamp the
            # LABEL_OWNER=keeper_id family label + record-at-birth into the
            # manifest -- otherwise the grandchild is invisible to both the
            # family walk (teardown_family) AND the keeper-keyed drift
            # detector (the orphan class Stage 4 exists to kill).
            "JARVIS_KEEPER_ID": self._keeper_id,
        }
        # RECORD-AT-BIRTH (Task-1 concern #3): append BEFORE the provision
        # await, so a keeper death mid-provision still leaves the child on the
        # manifest for the next teardown walk. A cleanly-failed provision
        # leaves the record in place (conservative -- see module docstring).
        self._manifest.record_create(
            kind="instance", name=node_name, labels=dict(labels),
            parent=self._keeper_id, gen=gen, keeper_id=self._keeper_id,
        )
        logger.info(
            "[BrainKeeper] resurrecting gen=%d node=%s keeper=%s",
            gen, node_name, self._keeper_id,
        )
        try:
            result = await self._provision_fn(
                node_name=node_name, labels=labels, extra_env=extra_env,
            )
            ok = bool(result[0]) if isinstance(result, tuple) else bool(result)
            detail = str(result[1]) if isinstance(result, tuple) and len(result) > 1 else ""
        except Exception as exc:  # noqa: BLE001 -- fail-soft; token stays SPENT
            ok, detail = False, repr(exc)
        if ok:
            self._state = _STATE_RESURRECTED
            self._absence_started = None  # window resets; probe re-arms it
            logger.info(
                "[BrainKeeper] resurrection SUCCEEDED gen=%d node=%s",
                gen, node_name,
            )
            # Stage-4 IMPORTANT-2: a NEW generation is now live -- actively
            # deliver the fence signal to every OLDER live node (its
            # self-fence + capture_inflight window), then reap it. Fail-soft:
            # supersession must never undo a successful resurrection.
            try:
                await self.supersede_old_generations(gen)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[BrainKeeper] supersession fail-soft (resurrection stands): "
                    "%r", exc,
                )
        else:
            # The token is SPENT by design (VM-factory guard: attempts that
            # can spend money count against the cap, refunds would let a
            # crash-looping provision path spin the factory). The absence
            # window keeps running -> the next tick may retry until the
            # bucket refuses -> deterministic cap_exhausted.
            self._state = _STATE_ABSENT
            logger.warning(
                "[BrainKeeper] resurrection FAILED gen=%d node=%s detail=%s "
                "(token spent by design)", gen, node_name, detail,
            )
        return self._state

    # -- fence supersession (Stage-4 Task-4, IMPORTANT-2/4) -------------------

    def _resolve_fence_deliver(self) -> Callable[[str, int], Awaitable[bool]]:
        return self._fence_deliver_fn or _default_fence_deliver_fn

    def _resolve_delete_instance(self) -> Callable[..., Awaitable[Any]]:
        if self._delete_instance_fn is not None:
            return self._delete_instance_fn

        async def _live(name: str, *, zone: Optional[str] = None) -> Any:
            from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
                get_compute_rest,
            )
            return await get_compute_rest().delete_instance(name, zone=zone)

        return _live

    def _resolve_delete_firewall(self) -> Callable[..., Awaitable[Any]]:
        if self._delete_firewall_fn is not None:
            return self._delete_firewall_fn

        async def _live(*, rule_name: str) -> Any:
            from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
                get_compute_rest,
            )
            return await get_compute_rest().delete_firewall_rule(rule_name)

        return _live

    def _resolve_label_query(self) -> Callable[..., Awaitable[Any]]:
        if self._label_query_fn is not None:
            return self._label_query_fn

        async def _live(*, label_key: str, label_value: str) -> Any:
            from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
                get_compute_rest,
            )
            return await get_compute_rest().list_instances_by_label(
                label_key=label_key, label_value=label_value)

        return _live

    async def supersede_old_generations(self, current_gen: int) -> Dict[str, Any]:
        """Deliver the fence signal to every LIVE node older than
        ``current_gen``, then (after the bounded grace) reap it.

        The keeper OWNS the family (every resurrected node is recorded with
        ``parent=keeper_id``), so ``live_family(keeper_id)`` is the complete
        live set. For each ``gen < current_gen`` node: deliver
        ``console.keeper_heartbeat{gen:current_gen}`` to its endpoint (its
        deterministic self-fence + capture_inflight window). After ALL
        deliveries, sleep the bounded grace ONCE, then reap each old node via
        :meth:`reap_generation`. Delivery is best-effort -- an unreachable node
        falls through to the reap backstop (never blocks). Fail-soft
        throughout: returns a structured summary, never raises."""
        try:
            live = self._manifest.live_family(self._keeper_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BrainKeeper] supersede: live_family failed: %r", exc)
            return {"superseded": [], "delivered": {}, "reaped": {}}

        old_nodes: List[str] = []
        for rec in live:
            if str(rec.get("kind") or "instance") != "instance":
                continue
            g = rec.get("gen")
            try:
                g_int = int(g)
            except (TypeError, ValueError):
                continue
            if g_int >= int(current_gen):
                continue  # the new gen (and any equal/higher) is not superseded
            name = str(rec.get("name") or "")
            if name:
                old_nodes.append(name)

        if not old_nodes:
            return {"superseded": [], "delivered": {}, "reaped": {}}

        logger.warning(
            "[BrainKeeper] SUPERSEDING %d old generation(s) after gen=%d: %s",
            len(old_nodes), current_gen, ", ".join(old_nodes),
        )
        deliver = self._resolve_fence_deliver()
        delivered: Dict[str, bool] = {}
        for name in old_nodes:
            try:
                delivered[name] = bool(await deliver(name, int(current_gen)))
            except Exception as exc:  # noqa: BLE001 -- delivery never blocks reap
                logger.warning(
                    "[BrainKeeper] fence delivery raised for %s (reap backstops): "
                    "%r", name, exc,
                )
                delivered[name] = False

        # ONE bounded grace: each delivered node gets its self-fence +
        # capture_inflight window before the reap. Then reap regardless of
        # delivery outcome (reap is the backstop).
        if self._fence_grace_s > 0:
            await asyncio.sleep(self._fence_grace_s)

        reaped: Dict[str, Any] = {}
        for name in old_nodes:
            reaped[name] = await self.reap_generation(name)

        return {
            "superseded": old_nodes,
            "delivered": delivered,
            "reaped": reaped,
        }

    async def reap_generation(self, node_name: str) -> Dict[str, Any]:
        """Reap a superseded node's family via the manifest walk. Passes
        ``owner_id=keeper_id`` so the drift detector queries the label the
        keeper actually stamps (``LABEL_OWNER=keeper_id``) -- IMPORTANT-4.
        Fail-soft: any failure returns an ``error`` dict, never raises."""
        try:
            from backend.core.ouroboros.governance.brain_lifecycle import (  # noqa: PLC0415
                teardown_family,
            )
            return await teardown_family(
                node_name,
                manifest=self._manifest,
                owner_id=self._keeper_id,
                delete_instance_fn=self._resolve_delete_instance(),
                delete_firewall_fn=self._resolve_delete_firewall(),
                label_query_fn=self._resolve_label_query(),
            )
        except Exception as exc:  # noqa: BLE001 -- reap is fail-soft
            logger.warning(
                "[BrainKeeper] reap_generation(%s) fail-soft: %r", node_name, exc)
            return {"root": node_name, "error": repr(exc)}
