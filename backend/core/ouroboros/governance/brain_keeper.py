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
    ``{"ts_utc": ..., "ts_wall": <time.time()>, "gen": N}`` through the SAME
    intake-WAL ``_write_line`` substrate ``ResourceManifest`` reuses (flock
    cross-process append -- imported, never copied). ``try_take`` replays the
    journal fresh on every call: takes whose ``ts_wall`` falls inside the
    rolling window count against the capacity; at/over capacity the take is
    REFUSED WITHOUT APPENDING. Deterministic across process restarts
    (Mandate 1) -- and deliberately free of sleeps/retries/backoff: rate
    POLICY lives in the keeper's terminal state, not here.

    Wall clock (``time.time()``) is intentional -- see the module docstring.
    """

    def __init__(self, path: Optional[Any] = None) -> None:
        if path is not None:
            resolved = Path(path)
        else:
            raw = (os.environ.get(_ENV_BUCKET_PATH, "") or "").strip()
            resolved = Path(raw) if raw else _default_bucket_path()
        self._path = resolved
        # REUSE the intake-WAL primitives exactly as ResourceManifest does:
        # WAL.__init__ mkdirs parents; WAL._write_line is the flock append.
        from backend.core.ouroboros.governance.intake.wal import WAL  # noqa: PLC0415

        self._wal = WAL(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def _replay(self) -> List[Dict[str, Any]]:
        """Tolerant journal replay (WAL semantics: corrupt lines skipped)."""
        records: List[Dict[str, Any]] = []
        if not self._path.exists():
            return records
        try:
            with self._path.open("r", encoding="utf-8") as fh:
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

        Replays the journal, counts in-window takes; at/over capacity the
        refusal appends NOTHING (a refused caller must leave no trace). On a
        grant, appends the take record and returns True. No sleeps, no
        retries, no backoff -- ever (Mandate 1)."""
        now = time.time()
        if self.taken_in_window(now) >= self.capacity():
            return False
        record: Dict[str, Any] = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "ts_wall": now,
            "gen": gen,
        }
        self._wal._write_line(record)  # noqa: SLF001 -- private-but-stable WAL substrate
        return True


# ---------------------------------------------------------------------------
# BrainKeeper -- the sustained-absence resurrection state machine.
# ---------------------------------------------------------------------------

_STATE_HEALTHY = "healthy"
_STATE_ABSENT = "absent"
_STATE_RESURRECTING = "resurrecting"
_STATE_RESURRECTED = "resurrected"
_STATE_CAP_EXHAUSTED = "cap_exhausted"  # TERMINAL


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
    ) -> None:
        self._discover_fn = discover_fn
        self._provision_fn = provision_fn
        self._manifest = manifest
        self._bucket = bucket
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
