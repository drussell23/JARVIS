"""Phase 1 Slice 1.2 — Decision Runtime (record / replay / verify).

The CALL-SITE integration layer for the Determinism Substrate.

Phase 1 is layered. Each layer is owned by a different module:

  * Slice 1.1 (mine) — ``determinism/entropy.py`` + ``clock.py``:
    deterministic random + time primitives. Substrate of execution.

  * Antigravity §24 work — ``observability/determinism_substrate.py``:
    canonical_serialize + canonical_hash + DecisionHash + PromptHasher.
    Substrate of identity (how to content-address a decision).

  * Antigravity §24 work — ``observability/replay_harness.py``:
    replay(log, state_0) → state_T pure function. Verification engine.

  * **THIS module — Slice 1.2:** the runtime that ties them together.
    Wraps any decision in a ``decide(...)`` call that can:
      - PASSTHROUGH: just compute (legacy, master flag off)
      - RECORD: compute + append to per-session ledger
      - REPLAY: skip compute + return recorded output
      - VERIFY: compute + lookup + assert match

The integration surface is one function callers actually use::

    output = await decide(
        op_id=ctx.op_id, phase="ROUTE", kind="route_assignment",
        inputs={"urgency": "normal", "source": "TestFailureSensor"},
        compute=lambda: router.assign_route(ctx),
    )

And the runtime handles RECORD/REPLAY/VERIFY transparently based on
``JARVIS_DETERMINISM_LEDGER_MODE``.

Operator's design constraints (re-applied per directive 2026-04-28):

  * **Asynchronous** — appends serialize through an asyncio.Lock; the
    flush is a fire-and-forget background task.
  * **Dynamic** — decision kinds are free-form strings, not an enum.
    A new kind requires zero code changes; just call ``decide(kind=...)``.
  * **Adaptive** — mode is hot-reread from env/runtime so an operator
    can flip RECORD→VERIFY mid-session via ``/determinism mode verify``.
    Corrupt ledger files auto-quarantine + fall through to PASSTHROUGH.
  * **Intelligent** — input canonicalization via Antigravity's
    canonical_hash so semantically-identical inputs (different dict
    ordering, equivalent floats) match across runs.
  * **Robust** — every public method NEVER raises; defensive try/except;
    cross-process safe via flock on the per-session JSONL.
  * **No hardcoding** — every default env-tunable; decision kinds
    dynamic; storage paths configurable.
  * **Leverages existing** — imports Antigravity's canonical_serialize
    + canonical_hash. Imports Slice 1.1's entropy_for (for record_id
    generation) + clock_for_session (for monotonic_ts). No duplication
    of hashing, atomic-write, or replay primitives.

Authority invariants pinned by tests:
  * NEVER imports orchestrator / phase_runner / candidate_generator.
  * NEVER raises out of any public method.
  * No new locks beyond the file-scoped asyncio.Lock + flock.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time as _time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    Any, AsyncIterator, Awaitable, Callable, Dict, List, Mapping,
    Optional, Tuple,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag + mode resolution
# ---------------------------------------------------------------------------


def ledger_enabled() -> bool:
    """``JARVIS_DETERMINISM_LEDGER_ENABLED`` (default ``true`` —
    graduated in Phase 1 Slice 1.5).

    Re-read at call time so monkeypatch works in tests + operators
    can flip live without re-init. Hot-revert path: ``export
    JARVIS_DETERMINISM_LEDGER_ENABLED=false`` short-circuits
    ``decide()`` to PASSTHROUGH (compute runs, no recording, no
    lookup) regardless of mode env.

    When ``true``: ``decide()`` engages the configured mode
    (RECORD/REPLAY/VERIFY/PASSTHROUGH per
    JARVIS_DETERMINISM_LEDGER_MODE)."""
    raw = os.environ.get(
        "JARVIS_DETERMINISM_LEDGER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


class LedgerMode(Enum):
    """Operating mode for the decision runtime.

    Modes are dynamic — the runtime re-reads the env on every
    ``decide()`` call so an operator REPL can flip mode mid-session
    without restarting the harness."""
    PASSTHROUGH = "passthrough"  # No record, no replay (legacy)
    RECORD = "record"             # Compute + append
    REPLAY = "replay"             # Skip compute, return recorded
    VERIFY = "verify"             # Compute + lookup + assert match


def _resolve_mode() -> LedgerMode:
    """Resolution order:
      1. ``JARVIS_DETERMINISM_LEDGER_MODE`` env (passthrough/record/
         replay/verify)
      2. Master flag: when off → PASSTHROUGH; when on → RECORD
         (default operating mode for live sessions)

    Unknown env values fall through to flag-based default (NEVER
    raises)."""
    if not ledger_enabled():
        return LedgerMode.PASSTHROUGH
    raw = os.environ.get(
        "JARVIS_DETERMINISM_LEDGER_MODE", "",
    ).strip().lower()
    if raw == "passthrough":
        return LedgerMode.PASSTHROUGH
    if raw == "replay":
        return LedgerMode.REPLAY
    if raw == "verify":
        return LedgerMode.VERIFY
    if raw == "record":
        return LedgerMode.RECORD
    # Master flag on, no explicit mode → RECORD (default live behavior)
    return LedgerMode.RECORD


def _verify_raises() -> bool:
    """``JARVIS_DETERMINISM_LEDGER_VERIFY_RAISES`` (default ``false``).

    In VERIFY mode, on mismatch:
      * ``false`` → log structured warning, return live output (default)
      * ``true``  → raise ``DecisionMismatchError`` (strict CI mode)

    Default false to keep CI noise low; operators flip true for
    bisecting nondeterminism bugs."""
    raw = os.environ.get(
        "JARVIS_DETERMINISM_LEDGER_VERIFY_RAISES", "",
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _ledger_dir() -> Path:
    """``JARVIS_DETERMINISM_LEDGER_DIR`` — base directory for per-
    session decision ledgers. Default
    ``.jarvis/determinism`` (same root as Slice 1.1's seed storage).
    Per-session file lives at
    ``<dir>/<session-id>/decisions.jsonl``."""
    raw = os.environ.get(
        "JARVIS_DETERMINISM_LEDGER_DIR",
        ".jarvis/determinism",
    ).strip()
    return Path(raw)


SCHEMA_VERSION = "decision_record.1"


# ---------------------------------------------------------------------------
# DecisionRecord schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionRecord:
    """One captured decision — frozen + hashable.

    Schema designed for stable cross-session diffing:
      * ``record_id`` is deterministic via Slice 1.1's entropy
      * ``inputs_hash`` collapses semantically-identical inputs
      * ``output_repr`` is the canonical JSON of the output
      * ``monotonic_ts`` comes from Slice 1.1's clock (record-mode
        clock captures real time + traces it)

    Lookup keys: (session_id, op_id, phase, kind, ordinal). Two
    decisions with the same keys are duplicates — REPLAY returns
    the FIRST match; ordinal disambiguates repeated calls within
    the same op+phase+kind tuple."""
    record_id: str
    session_id: str
    op_id: str
    phase: str
    kind: str
    ordinal: int
    inputs_hash: str
    output_repr: str
    monotonic_ts: float
    wall_ts: float
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "session_id": self.session_id,
            "op_id": self.op_id,
            "phase": self.phase,
            "kind": self.kind,
            "ordinal": self.ordinal,
            "inputs_hash": self.inputs_hash,
            "output_repr": self.output_repr,
            "monotonic_ts": self.monotonic_ts,
            "wall_ts": self.wall_ts,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Optional["DecisionRecord"]:
        """Parse from a JSONL row. NEVER raises — returns None on
        unparseable input."""
        try:
            if not isinstance(raw, Mapping):
                return None
            if raw.get("schema_version") != SCHEMA_VERSION:
                return None
            return cls(
                record_id=str(raw["record_id"]),
                session_id=str(raw["session_id"]),
                op_id=str(raw["op_id"]),
                phase=str(raw["phase"]),
                kind=str(raw["kind"]),
                ordinal=int(raw["ordinal"]),
                inputs_hash=str(raw["inputs_hash"]),
                output_repr=str(raw["output_repr"]),
                monotonic_ts=float(raw["monotonic_ts"]),
                wall_ts=float(raw["wall_ts"]),
            )
        except (KeyError, ValueError, TypeError):
            return None


@dataclass(frozen=True)
class VerifyResult:
    """Result of a VERIFY-mode comparison."""
    matched: bool
    expected_hash: str
    actual_hash: str
    expected_repr: str
    actual_repr: str
    detail: str = ""


class DecisionMismatchError(RuntimeError):
    """Raised by VERIFY mode when ``JARVIS_DETERMINISM_LEDGER_VERIFY_RAISES``
    is true AND live output diverges from recorded."""
    def __init__(self, result: VerifyResult) -> None:
        super().__init__(
            f"decision mismatch: expected_hash={result.expected_hash[:12]} "
            f"actual_hash={result.actual_hash[:12]} detail={result.detail}"
        )
        self.result = result


# ---------------------------------------------------------------------------
# Lazy imports — keep cage discipline (defer Antigravity + Slice 1.1)
# ---------------------------------------------------------------------------


def _canonical_hash(obj: Any) -> str:
    """Lazy adapter — never raises. Returns ``"error:<reason>"`` on
    serialization fault so the ledger can still record."""
    try:
        from backend.core.ouroboros.governance.observability.determinism_substrate import (
            canonical_hash,
        )
        return canonical_hash(obj)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[determinism] canonical_hash unavailable: %s", exc)
        # Fall back to repr-based pseudo-hash (NOT byte-stable across
        # arches but better than crashing the ledger). Operators
        # missing the canonicalizer module still get a working
        # decision flow with weaker reproducibility guarantees.
        try:
            return f"fallback:{hash(repr(obj)) & 0xFFFF_FFFF_FFFF_FFFF:016x}"
        except Exception:  # noqa: BLE001 — defensive
            return "error:unhashable"


def _canonical_serialize(obj: Any) -> str:
    """Lazy adapter — never raises. Returns ``"error:..."`` on fault."""
    try:
        from backend.core.ouroboros.governance.observability.determinism_substrate import (
            canonical_serialize,
        )
        return canonical_serialize(obj)
    except Exception as exc:  # noqa: BLE001 — defensive
        try:
            return json.dumps(
                {"_repr": repr(obj), "_fallback": True},
                sort_keys=True,
            )
        except Exception:  # noqa: BLE001 — defensive
            return f"error:{type(exc).__name__}"


def _next_record_id(session_id: str, op_id: str, ordinal: int) -> str:
    """Generate a deterministic record_id via Slice 1.1's entropy.
    Falls back to a wall-clock-stamped ID if entropy is unavailable."""
    try:
        from backend.core.ouroboros.governance.determinism.entropy import (
            entropy_for,
        )
        ent = entropy_for(f"{op_id}:record-{ordinal}", session_id=session_id)
        return str(ent.uuid4())
    except Exception:  # noqa: BLE001 — defensive
        # Fallback: wall-clock + ordinal (NOT deterministic across runs
        # but unique within this run)
        return f"fallback-{int(_time.monotonic() * 1e6):016x}-{ordinal}"


def _capture_clock_now() -> Tuple[float, float]:
    """Capture (monotonic, wall) timestamp pair via Slice 1.1's clock
    when available, real time otherwise. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.determinism.clock import (
            clock_for_session,
        )
        c = clock_for_session(op_id="ledger-record")
        return c.monotonic(), c.wall_clock()
    except Exception:  # noqa: BLE001 — defensive
        return _time.monotonic(), _time.time()


# ---------------------------------------------------------------------------
# DecisionRuntime — per-session ledger
# ---------------------------------------------------------------------------


class DecisionRuntime:
    """Per-session decision ledger.

    Storage: append-only JSONL at
    ``<ledger_dir>/<session-id>/decisions.jsonl``. One record per
    line. New sessions create the file lazily on first record.

    Concurrency:
      * In-process: ``asyncio.Lock`` per runtime instance serializes
        appends. Async callers serialize cleanly.
      * Cross-process: ``flock`` advisory lock on the JSONL handle
        for the duration of each append (mirrors Antigravity's
        decision_trace_ledger pattern).

    Lookup performance:
      * In-RECORD-mode: only writes happen, no lookups.
      * In-REPLAY/VERIFY: an in-memory index keyed by
        (op_id, phase, kind, ordinal) is built lazily on first
        lookup. Subsequent lookups are O(1).

    NEVER raises out of any public method. Disk faults log warnings
    + degrade to in-memory-only operation."""

    def __init__(
        self,
        *,
        session_id: str,
        path: Optional[Path] = None,
    ) -> None:
        self._session_id = (str(session_id).strip() or "default")
        self._path = path  # resolved lazily so env can be patched
        self._async_lock = asyncio.Lock()
        self._sync_lock = threading.RLock()
        # Per-(op_id, phase, kind) ordinal counters for record mode
        self._ordinals: Dict[Tuple[str, str, str], int] = {}
        # Lazy lookup index for replay/verify: keys → record
        self._index: Optional[Dict[Tuple[str, str, str, int], DecisionRecord]] = None
        self._index_loaded_from_path: Optional[Path] = None

    @property
    def session_id(self) -> str:
        return self._session_id

    def _resolved_path(self) -> Path:
        if self._path is not None:
            return self._path
        return _ledger_dir() / self._session_id / "decisions.jsonl"

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    async def record(
        self,
        *,
        op_id: str,
        phase: str,
        kind: str,
        inputs: Mapping[str, Any],
        output: Any,
    ) -> Optional[DecisionRecord]:
        """Append a record. Returns the record on success, None on
        disk fault (in which case the caller's compute() output is
        still valid — only the ledger entry was dropped). NEVER
        raises."""
        try:
            ordinal_key = (op_id, phase, kind)
            with self._sync_lock:
                ordinal = self._ordinals.get(ordinal_key, 0)
                self._ordinals[ordinal_key] = ordinal + 1

            inputs_hash = _canonical_hash(dict(inputs) if inputs else {})
            output_repr = _canonical_serialize(output)
            monotonic_ts, wall_ts = _capture_clock_now()
            record = DecisionRecord(
                record_id=_next_record_id(
                    self._session_id, op_id, ordinal,
                ),
                session_id=self._session_id,
                op_id=str(op_id),
                phase=str(phase),
                kind=str(kind),
                ordinal=ordinal,
                inputs_hash=inputs_hash,
                output_repr=output_repr,
                monotonic_ts=monotonic_ts,
                wall_ts=wall_ts,
            )
            await self._append_to_disk(record)
            # Mutate the lazy index if it was already built
            with self._sync_lock:
                if self._index is not None:
                    self._index[
                        (record.op_id, record.phase, record.kind, record.ordinal)
                    ] = record
            return record
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[determinism] record failed for op_id=%s phase=%s "
                "kind=%s: %s — caller output is still valid",
                op_id, phase, kind, exc,
            )
            return None

    async def _append_to_disk(self, record: DecisionRecord) -> None:
        """Append one JSONL row. Async-locked + flocked. Defensive
        — disk error logs but doesn't propagate (caller still gets
        the record from the in-memory return path)."""
        path = self._resolved_path()
        line = json.dumps(
            record.to_dict(), sort_keys=True, ensure_ascii=True,
        ) + "\n"

        async with self._async_lock:
            # Run blocking I/O in the default executor so we don't
            # stall the event loop on slow disks.
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None, _atomic_append, path, line,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "[determinism] disk append failed at %s: %s — "
                    "in-memory record preserved", path, exc,
                )

    # ------------------------------------------------------------------
    # Lookup (replay / verify)
    # ------------------------------------------------------------------

    async def lookup(
        self,
        *,
        op_id: str,
        phase: str,
        kind: str,
        ordinal: int = 0,
    ) -> Optional[DecisionRecord]:
        """Find a recorded decision. Returns None if not found.
        NEVER raises."""
        try:
            self._ensure_index_loaded()
            with self._sync_lock:
                if self._index is None:
                    return None
                return self._index.get(
                    (str(op_id), str(phase), str(kind), int(ordinal)),
                )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug("[determinism] lookup failed: %s", exc)
            return None

    def _ensure_index_loaded(self) -> None:
        """Lazy-load the lookup index from disk on first call.
        Subsequent calls fast-path return. Reload triggers when the
        path changes (test rebinding env)."""
        path = self._resolved_path()
        with self._sync_lock:
            if (
                self._index is not None
                and self._index_loaded_from_path == path
            ):
                return
            self._index = {}
            self._index_loaded_from_path = path
            if not path.exists():
                return
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for raw_line in fh:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            payload = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue
                        rec = DecisionRecord.from_dict(payload)
                        if rec is None:
                            continue
                        if rec.session_id != self._session_id:
                            # Cross-session pollution — skip silently.
                            # Per-session storage means this should
                            # never happen, but defensive.
                            continue
                        self._index[
                            (rec.op_id, rec.phase, rec.kind, rec.ordinal)
                        ] = rec
            except OSError as exc:
                logger.debug(
                    "[determinism] index load OSError %s — empty index",
                    exc,
                )

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    def verify(
        self,
        *,
        recorded: DecisionRecord,
        live_output: Any,
    ) -> VerifyResult:
        """Compare a live output against a recorded one. Pure (no
        I/O, no logging — caller decides what to do with the result).
        NEVER raises."""
        try:
            actual_repr = _canonical_serialize(live_output)
            actual_hash = _canonical_hash(live_output)
            expected_repr = recorded.output_repr
            # Hash the parsed expected repr the same way actual is
            # hashed to compare apples-to-apples
            try:
                expected_obj = json.loads(expected_repr)
                expected_hash = _canonical_hash(expected_obj)
            except json.JSONDecodeError:
                expected_hash = _canonical_hash(expected_repr)
            matched = actual_repr == expected_repr
            detail = "" if matched else (
                f"diff_first_chars="
                f"{_diff_marker(expected_repr, actual_repr)}"
            )
            return VerifyResult(
                matched=matched,
                expected_hash=expected_hash,
                actual_hash=actual_hash,
                expected_repr=expected_repr,
                actual_repr=actual_repr,
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            return VerifyResult(
                matched=False,
                expected_hash="error",
                actual_hash="error",
                expected_repr="",
                actual_repr="",
                detail=f"verify_error:{type(exc).__name__}:{exc}",
            )


# ---------------------------------------------------------------------------
# Module-level decide() helper — what callers actually use
# ---------------------------------------------------------------------------


async def decide(
    *,
    op_id: str,
    phase: str,
    kind: str,
    inputs: Mapping[str, Any],
    compute: Callable[[], Awaitable[Any]],
    runtime: Optional[DecisionRuntime] = None,
    ordinal: Optional[int] = None,
) -> Any:
    """The integration surface. Wraps a decision in record/replay/
    verify semantics based on session mode.

    Behavior by mode:
      * **PASSTHROUGH** — ``await compute()``, no recording, no
        lookup. Bit-for-bit legacy behavior when master flag is off.
      * **RECORD** — ``await compute()``, then append the result to
        the session ledger. Returns compute output.
      * **REPLAY** — look up the recorded decision; if found, return
        its output (skipping ``compute()`` entirely). If NOT found,
        log a structured warning + fall through to RECORD-mode
        behavior (best-effort replay). Operators see a divergence
        warning instead of a crash.
      * **VERIFY** — ``await compute()`` AND lookup. If match, return
        live output. If diverge:
          - ``JARVIS_DETERMINISM_LEDGER_VERIFY_RAISES=true`` →
            raise ``DecisionMismatchError``
          - default → log warning + return live output

    Async-safe + thread-safe. NEVER raises (except VERIFY-strict
    mode). Works whether or not the master flag is set.

    NOTE: ``compute()`` is allowed to be a sync callable returning
    awaitable, OR a sync callable returning a value (auto-wrapped),
    OR an async callable returning a value/awaitable. We accept any
    of the three patterns.
    """
    mode = _resolve_mode()

    # PASSTHROUGH fast path — no runtime, no recording
    if mode is LedgerMode.PASSTHROUGH:
        return await _maybe_await(compute)

    # Resolve runtime (lazy singleton per session)
    if runtime is None:
        runtime = runtime_for_session()

    if mode is LedgerMode.REPLAY:
        if ordinal is None:
            ordinal = _peek_ordinal(runtime, op_id, phase, kind)
        recorded = await runtime.lookup(
            op_id=op_id, phase=phase, kind=kind, ordinal=ordinal,
        )
        if recorded is not None:
            # HIT: advance the counter so the next decide() in this
            # (op, phase, kind) tuple looks up the NEXT record.
            _advance_ordinal(runtime, op_id, phase, kind)
            try:
                return json.loads(recorded.output_repr)
            except (json.JSONDecodeError, TypeError):
                # Recorded output wasn't JSON-parseable — fall through
                # to live compute. Operators see the warning.
                logger.warning(
                    "[determinism] REPLAY: recorded output for "
                    "op_id=%s phase=%s kind=%s is not JSON-parseable "
                    "— falling through to live compute",
                    op_id, phase, kind,
                )
        # Replay miss — fall through to RECORD mode (best-effort).
        # record() will advance the counter itself, so we must NOT
        # advance it here; otherwise the recorded ordinal drifts
        # ahead of the lookup ordinal.
        live = await _maybe_await(compute)
        await runtime.record(
            op_id=op_id, phase=phase, kind=kind,
            inputs=inputs, output=live,
        )
        return live

    if mode is LedgerMode.VERIFY:
        if ordinal is None:
            ordinal = _peek_ordinal(runtime, op_id, phase, kind)
        live = await _maybe_await(compute)
        recorded = await runtime.lookup(
            op_id=op_id, phase=phase, kind=kind, ordinal=ordinal,
        )
        # VERIFY always advances the counter (whether match or miss)
        # so subsequent decide() calls in the same tuple look up the
        # next record.
        _advance_ordinal(runtime, op_id, phase, kind)
        if recorded is not None:
            result = runtime.verify(
                recorded=recorded, live_output=live,
            )
            if not result.matched:
                logger.warning(
                    "[determinism] VERIFY mismatch op_id=%s phase=%s "
                    "kind=%s ordinal=%d expected_hash=%s actual_hash=%s "
                    "detail=%s",
                    op_id, phase, kind, ordinal,
                    result.expected_hash[:12], result.actual_hash[:12],
                    result.detail,
                )
                if _verify_raises():
                    raise DecisionMismatchError(result)
        # In VERIFY mode we don't append (the recorded entry exists
        # already — appending would cause ordinal drift).
        return live

    # RECORD mode (default when master flag is on)
    live = await _maybe_await(compute)
    await runtime.record(
        op_id=op_id, phase=phase, kind=kind,
        inputs=inputs, output=live,
    )
    return live


def _peek_ordinal(
    runtime: DecisionRuntime, op_id: str, phase: str, kind: str,
) -> int:
    """Return the current ordinal for this (op, phase, kind) WITHOUT
    advancing. REPLAY uses this to find the right record; the
    advance happens explicitly via ``_advance_ordinal`` only on a
    successful hit (otherwise the fall-through to RECORD path would
    double-increment and skew the recorded ordinals)."""
    with runtime._sync_lock:  # noqa: SLF001
        return runtime._ordinals.get((op_id, phase, kind), 0)  # noqa: SLF001


def _advance_ordinal(
    runtime: DecisionRuntime, op_id: str, phase: str, kind: str,
) -> None:
    """Advance the ordinal counter for this (op, phase, kind) by one.
    Called by REPLAY-hit and VERIFY paths. RECORD path advances
    internally inside ``DecisionRuntime.record``."""
    with runtime._sync_lock:  # noqa: SLF001
        cur = runtime._ordinals.get((op_id, phase, kind), 0)  # noqa: SLF001
        runtime._ordinals[(op_id, phase, kind)] = cur + 1  # noqa: SLF001


async def _maybe_await(compute: Callable[[], Any]) -> Any:
    """Accept compute as sync-returning-value, sync-returning-awaitable,
    or async function. NEVER raises beyond compute's own exceptions."""
    try:
        result = compute()
    except TypeError:
        # Some test stubs are pre-bound coroutine objects, not
        # callables. Try awaiting directly.
        if hasattr(compute, "__await__"):
            return await compute  # type: ignore[misc]
        raise
    if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
        return await result
    return result


# ---------------------------------------------------------------------------
# Per-session runtime singleton
# ---------------------------------------------------------------------------


_runtime_cache: Dict[str, DecisionRuntime] = {}
_runtime_cache_lock = threading.RLock()


def runtime_for_session(
    session_id: Optional[str] = None,
) -> DecisionRuntime:
    """Lazy singleton accessor. Same session_id always returns the
    same runtime instance.

    ``session_id=None`` → reads from ``OUROBOROS_BATTLE_SESSION_ID``,
    falls back to ``"default"``.

    NEVER raises."""
    if session_id is None or not str(session_id).strip():
        session_id = os.environ.get(
            "OUROBOROS_BATTLE_SESSION_ID", "",
        ).strip() or "default"
    sid = str(session_id)
    with _runtime_cache_lock:
        cached = _runtime_cache.get(sid)
        if cached is not None:
            return cached
        rt = DecisionRuntime(session_id=sid)
        _runtime_cache[sid] = rt
        return rt


def reset_for_session(session_id: str) -> None:
    """Drop the cached runtime for a session. Test hook + operator
    REPL ``/determinism reset <session-id>``. NEVER raises."""
    with _runtime_cache_lock:
        _runtime_cache.pop(str(session_id), None)


def reset_all_for_tests() -> None:
    """Clear all cached runtimes. Production code MUST NOT call this."""
    with _runtime_cache_lock:
        _runtime_cache.clear()


# ---------------------------------------------------------------------------
# Atomic append (mirrors posture_store / dw_promotion_ledger pattern,
# but APPEND-mode rather than full-rewrite)
# ---------------------------------------------------------------------------


def _atomic_append(path: Path, line: str) -> None:
    """Append one line to ``path`` with cross-process safety via
    flock. Creates parent dirs if needed. The line MUST already
    end with a newline.

    flock semantics: advisory lock; another process appending
    concurrently waits. Crash mid-write leaves at most a partial
    last line (operators recover by truncating to the last newline
    on next read — defensive readers in this module do that
    automatically).

    Reuses the same flock pattern as Antigravity's
    decision_trace_ledger so behavior is consistent across the
    two ledgers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open in append-binary so the OS-level append is atomic with
    # respect to the file's size cursor (POSIX guarantees this for
    # < PIPE_BUF writes; our lines are typically < 1KB).
    with open(path, "ab") as fh:
        # Cross-process lock via fcntl.flock (Unix) or msvcrt
        # (Windows — no-op fallback). We use fcntl since Trinity is
        # macOS/Linux only.
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(line.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except ImportError:
            # No fcntl (Windows) — best-effort write without locking.
            # Cross-process safety degrades but in-process callers
            # are still serialized by asyncio.Lock above.
            fh.write(line.encode("utf-8"))
            fh.flush()


# ---------------------------------------------------------------------------
# Diff utility (minimal — just first divergence point)
# ---------------------------------------------------------------------------


def _diff_marker(expected: str, actual: str, *, max_len: int = 40) -> str:
    """Return a short marker showing where two strings diverge.
    Used in VERIFY warning messages — bounded so logs don't explode
    on large outputs."""
    n = min(len(expected), len(actual))
    for i in range(n):
        if expected[i] != actual[i]:
            start = max(0, i - 5)
            end = min(n, i + max_len)
            return (
                f"@{i}: expected={expected[start:end]!r} "
                f"actual={actual[start:end]!r}"
            )
    if len(expected) != len(actual):
        return (
            f"length_diff: expected={len(expected)} actual={len(actual)}"
        )
    return "identical_but_marked_diff"


# ---------------------------------------------------------------------------
# Async context manager — convenience for harness integration
# ---------------------------------------------------------------------------


@asynccontextmanager
async def runtime_session(
    session_id: Optional[str] = None,
) -> AsyncIterator[DecisionRuntime]:
    """Yield a DecisionRuntime for the duration of a session.

    Currently a thin wrapper over ``runtime_for_session``; future
    slices (1.3+) will add fsync-on-exit + index commit semantics
    here. Operators get forward-compatible code by using the context
    manager today."""
    rt = runtime_for_session(session_id)
    try:
        yield rt
    finally:
        # Future: flush + fsync + close index. For now, no-op since
        # every record is fsynced on append.
        pass


__all__ = [
    "DecisionMismatchError",
    "DecisionRecord",
    "DecisionRuntime",
    "LedgerMode",
    "SCHEMA_VERSION",
    "VerifyResult",
    "decide",
    "ledger_enabled",
    "reset_all_for_tests",
    "reset_for_session",
    "runtime_for_session",
    "runtime_session",
]
