"""Trajectory recorder — O+V generations as canonical experience events.

Phase 1 of the Reactor-Core data flywheel. Every local-lane generation
already produces the two halves of a preference pair:

  * the PROMPT + the CANDIDATE the model actually emitted, and
  * the VERDICT the pipeline reached on it (applied / syntax-error /
    caged by governance).

They were never written down together, so the corpus that would let
Reactor-Core improve the 32B did not exist. This module joins them and
appends ONE canonical ``ExperienceEvent`` line per candidate.

## Why this schema

The line format is Reactor-Core's canonical ``ExperienceEvent``
(``reactor_core/schemas/experience_schema.py``) — *and* those same field
names are the first-choice keys that
``reactor_core/training/dpo_pair_generator.py::_ingest_telemetry`` reads
(``user_input`` / ``assistant_output`` / ``model_id`` / ``outcome`` /
``confidence`` / ``latency_ms`` / ``timestamp`` / ``event_id``).

One stream, two consumers, zero translation code: the
``TrinityExperienceReceiver`` watches this directory already, and the DPO
generator reads it by pointing ``DPO_TELEMETRY_DIR`` at the same path.
A cross-repo *import* is impossible here (jarvis and reactor-core are
separate repos in separate venvs), so the shared contract is the schema.

``metadata.should_train`` carries the SAME exclusion policy as
``reactor_core/ingestion/autonomy_classifier.py``: a governance denial is
INFRASTRUCTURE, not a model-quality signal, and must never become a
"rejected" sample. Training on the cage would teach the model that
correctly-refused work is bad output.

## Design constraints (load-bearing)

  * **Never in the hot path.** Emission is ``Queue.put_nowait`` — on a
    full queue the event is DROPPED and counted, never awaited. No
    generation ever waits on disk I/O.
  * **Fail-open.** Every path is swallowed. Recording failure must never
    cost an op.
  * **Default-off** (§33.1): ``JARVIS_TRAJECTORY_RECORDER_ENABLED``.
    Off ⇒ both emit calls are a flag read and a return.
  * **Bounded** — queue depth, pending-join map, per-field char caps.
  * **Reuses the canonical substrates**: candidate projection via
    ``provider_response_cache._trajectory_from_generation_result``,
    prompt identity via its ``_prefix_key``, and the append via
    ``cross_process_jsonl.async_flock_append_line``. No second hasher,
    no second locking discipline, no second JSON writer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

TRAJECTORY_RECORDER_SCHEMA_VERSION = "1.0"

_TRUTHY = ("1", "true", "yes", "on")

_ENV_MASTER = "JARVIS_TRAJECTORY_RECORDER_ENABLED"
_ENV_DIR = "JARVIS_TRAJECTORY_RECORDER_DIR"
_ENV_QUEUE_MAX = "JARVIS_TRAJECTORY_RECORDER_QUEUE_MAX"
_ENV_PENDING_MAX = "JARVIS_TRAJECTORY_RECORDER_PENDING_MAX"
_ENV_PENDING_TTL_S = "JARVIS_TRAJECTORY_RECORDER_PENDING_TTL_S"
_ENV_TICK_S = "JARVIS_TRAJECTORY_RECORDER_TICK_S"
_ENV_MAX_PROMPT_CHARS = "JARVIS_TRAJECTORY_RECORDER_MAX_PROMPT_CHARS"
_ENV_MAX_OUTPUT_CHARS = "JARVIS_TRAJECTORY_RECORDER_MAX_OUTPUT_CHARS"

_DEFAULT_QUEUE_MAX = 512
_DEFAULT_PENDING_MAX = 256
_DEFAULT_PENDING_TTL_S = 900.0
_DEFAULT_TICK_S = 60.0
_DEFAULT_MAX_PROMPT_CHARS = 24_000
_DEFAULT_MAX_OUTPUT_CHARS = 24_000

_TRUNC_MARKER = "\n...[truncated by trajectory_recorder]"


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def recorder_enabled() -> bool:
    """Master flag. Default FALSE per §33.1 (shadow-first)."""
    return _env_flag(_ENV_MASTER)


def events_dir() -> Path:
    """Directory the Trinity experience receiver already watches."""
    raw = os.getenv(_ENV_DIR, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".jarvis" / "trinity" / "events"


# ---------------------------------------------------------------------------
# Outcome policy — mirrors reactor_core/ingestion/autonomy_classifier.py
# ---------------------------------------------------------------------------

# (canonical_outcome, autonomy_event_type, should_train)
_OutcomePolicy = Tuple[str, str, bool]

_SUCCESS: _OutcomePolicy = ("success", "committed", True)
_FAILURE: _OutcomePolicy = ("failure", "failed", True)
_NOOP: _OutcomePolicy = ("partial", "committed", True)
_CAGED: _OutcomePolicy = ("unknown", "policy_denied", False)
_INFRA: _OutcomePolicy = ("unknown", "no_journal_lease", False)
_UNKNOWN: _OutcomePolicy = ("unknown", "intent_written", False)

# Terminal reason codes observed in the battle-test soaks. Governance
# denials and environmental faults are deliberately NOT trainable: the
# model's output was never the reason the op died.
_TERMINAL_REASON_POLICY: Dict[str, _OutcomePolicy] = {
    # --- model produced something the pipeline accepted ---
    "background_accepted": _SUCCESS,
    "applied": _SUCCESS,
    "completed": _SUCCESS,
    # --- a NO-OP verdict is an answer, not an absence ---
    "noop": _NOOP,
    "2b.1-noop": _NOOP,
    # --- the candidate itself was bad: the trainable failure ---
    "all_candidates_syntax_error": _FAILURE,
    "validation_failed": _FAILURE,
    "tests_failed": _FAILURE,
    # --- governance cage: correct refusals, never model-quality ---
    "self_modification_unsanctioned_source": _CAGED,
    "touches_kernel": _CAGED,
    "touches_supervisor": _CAGED,
    "touches_security": _CAGED,
    "target_out_of_scope": _CAGED,
    # --- environmental / lifecycle ---
    "l2_stopped": _INFRA,
    "session_exhausted": _INFRA,
    "unhandled_pipeline_exception": _INFRA,
    "wall_clock_cap": _INFRA,
}


def classify_terminal_reason(
    terminal_reason: str, terminal_phase: str = "",
) -> _OutcomePolicy:
    """Map an O+V terminal reason to ``(outcome, autonomy_type, train)``.

    Unknown reasons degrade to non-trainable UNKNOWN rather than being
    guessed into SUCCESS/FAILURE — a mislabelled pair is worse than a
    missing one.
    """
    reason = (terminal_reason or "").strip().lower()
    if reason in _TERMINAL_REASON_POLICY:
        return _TERMINAL_REASON_POLICY[reason]
    phase = (terminal_phase or "").strip().upper()
    if phase in ("COMPLETED", "APPLIED"):
        return _SUCCESS
    return _UNKNOWN


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNC_MARKER


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Queue payloads
# ---------------------------------------------------------------------------


@dataclass
class _PendingGeneration:
    """One generation awaiting its verdict."""

    op_id: str
    prompt: str
    prompt_key: str
    candidates: Tuple[Dict[str, Any], ...]
    model_id: str
    provider_name: str
    is_noop: bool
    latency_ms: float
    # Split, not summed. tok/s is completion_tokens over the generation
    # duration; a single `tokens_used` that folds the prompt in makes the
    # throughput term unrecoverable, and throughput is half of the
    # model A/B question (a smarter model that is 3x slower may lose).
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    task_type: str
    session_id: str
    created_monotonic: float = field(default_factory=time.monotonic)
    created_iso: str = field(default_factory=_utc_now_iso)


@dataclass
class _OutcomeEvent:
    op_id: str
    terminal_phase: str
    terminal_reason: str


class TrajectoryRecorder:
    """Async, bounded, fail-open recorder. One writer task per process."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path_override = path
        self._queue: Optional[asyncio.Queue] = None
        self._writer: Optional[asyncio.Task] = None
        # Wall-clock expiry watchdog. Separate from the drain loop because
        # expiry that only runs when a queue item arrives is not expiry at
        # all: on a sparse workload the pending generation waits for an
        # event that never comes, and its trajectory is never written. That
        # is exactly what produced 4 candidate sets and 0 recorded lines.
        self._watchdog: Optional[asyncio.Task] = None
        self._lock: Optional[asyncio.Lock] = None
        self._pending: "OrderedDict[str, _PendingGeneration]" = OrderedDict()
        self._stats: Dict[str, int] = {
            "generations_queued": 0,
            "outcomes_queued": 0,
            "events_written": 0,
            "dropped_queue_full": 0,
            "dropped_no_loop": 0,
            "pending_evicted": 0,
            "pending_expired": 0,
            "orphan_outcomes": 0,
            "write_failures": 0,
        }

    # -- paths ------------------------------------------------------------
    @property
    def path(self) -> Path:
        if self._path_override is not None:
            return self._path_override
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        return events_dir() / f"experience_{day}.jsonl"

    # -- lifecycle --------------------------------------------------------
    def _ensure_writer(self) -> bool:
        """Start the drain task if a loop is running. False => cannot record."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._stats["dropped_no_loop"] += 1
            return False
        if self._queue is None:
            self._queue = asyncio.Queue(
                maxsize=_env_int(
                    _ENV_QUEUE_MAX, _DEFAULT_QUEUE_MAX, 16, 65_536
                )
            )
        if self._lock is None:
            self._lock = asyncio.Lock()
        if self._writer is None or self._writer.done():
            self._writer = loop.create_task(
                self._drain_loop(), name="trajectory_recorder_drain"
            )
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = loop.create_task(
                self._expiry_watchdog(), name="trajectory_recorder_expiry"
            )
        return True

    async def _expiry_watchdog(self) -> None:
        """Flush overdue pendings on WALL-CLOCK time, not on queue traffic."""
        while True:
            tick = _env_float(_ENV_TICK_S, _DEFAULT_TICK_S, 1.0, 3_600.0)
            try:
                await asyncio.sleep(tick)
                await self._expire_pending()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- a watchdog must not die
                logger.debug(
                    "[TrajectoryRecorder] expiry tick failed", exc_info=True,
                )

    def _offer(self, item: Any) -> bool:
        if not recorder_enabled():
            return False
        try:
            if not self._ensure_writer():
                return False
            assert self._queue is not None
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            # Backpressure is a DROP, never a wait: the control loop must
            # not pay for a slow disk.
            self._stats["dropped_queue_full"] += 1
            return False
        except Exception:  # noqa: BLE001 — fail-open, always
            logger.debug("[TrajectoryRecorder] offer failed", exc_info=True)
            return False

    # -- public emit API --------------------------------------------------
    def record_generation(
        self,
        *,
        op_id: str,
        prompt: str,
        generation_result: Any,
        latency_ms: float = 0.0,
        task_type: str = "",
        session_id: str = "",
        route: str = "codegen",
    ) -> bool:
        """Queue one generation. Non-blocking. NEVER raises."""
        if not recorder_enabled() or not op_id:
            return False
        try:
            from backend.core.ouroboros.governance.provider_response_cache import (  # noqa: E501
                _prefix_key,
                _trajectory_from_generation_result,
            )
        except Exception:  # noqa: BLE001 — substrate optional
            return False

        model_id = str(getattr(generation_result, "model_id", "") or "")
        prompt_raw = str(prompt or "")
        prompt_text = _truncate(
            prompt_raw,
            _env_int(
                _ENV_MAX_PROMPT_CHARS,
                _DEFAULT_MAX_PROMPT_CHARS,
                256,
                2_000_000,
            ),
        )
        try:
            prompt_key = _prefix_key(prompt_raw, model_id, route)
        except Exception:  # noqa: BLE001
            prompt_key = ""

        # Candidate projection is the cache's already-hardened one — it
        # drops non-serializable tool objects and never raises.
        traj = _trajectory_from_generation_result(
            "", prompt_key, generation_result,
        )
        if traj is None or not traj.candidates:
            return False

        pending = _PendingGeneration(
            op_id=str(op_id),
            prompt=prompt_text,
            prompt_key=prompt_key,
            candidates=tuple(traj.candidates),
            model_id=traj.model_id,
            provider_name=traj.provider_name,
            is_noop=bool(traj.is_noop),
            latency_ms=max(0.0, float(latency_ms or 0.0)),
            prompt_tokens=int(traj.total_input_tokens or 0),
            completion_tokens=int(traj.total_output_tokens or 0),
            cost_usd=float(traj.original_cost_usd),
            task_type=str(task_type or ""),
            session_id=str(session_id or ""),
        )
        if self._offer(pending):
            self._stats["generations_queued"] += 1
            # Speak on SUCCESS too. A recorder that only logs failures
            # cannot distinguish "the generation hook never fired" from
            # "it fired and the verdict never joined" -- and those have
            # opposite fixes. Both halves must be visible by op_id or the
            # join is undiagnosable from a log.
            logger.info(
                "[TrajectoryRecorder] queued generation op=%s "
                "candidates=%d model=%s (awaiting verdict)",
                pending.op_id, len(pending.candidates), pending.model_id,
            )
            return True
        logger.info(
            "[TrajectoryRecorder] generation for op=%s was NOT queued "
            "(enabled=%s candidates=%d) — no trajectory will be written",
            str(op_id), recorder_enabled(), len(traj.candidates),
        )
        return False

    def record_outcome(
        self,
        *,
        op_id: str,
        terminal_phase: str = "",
        terminal_reason: str = "",
    ) -> bool:
        """Queue one op verdict. Non-blocking. NEVER raises."""
        if not recorder_enabled() or not op_id:
            return False
        evt = _OutcomeEvent(
            op_id=str(op_id),
            terminal_phase=str(terminal_phase or ""),
            terminal_reason=str(terminal_reason or ""),
        )
        if self._offer(evt):
            self._stats["outcomes_queued"] += 1
            return True
        return False

    # -- writer -----------------------------------------------------------
    async def _drain_loop(self) -> None:
        assert self._queue is not None
        while True:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                continue
            try:
                if isinstance(item, _PendingGeneration):
                    await self._admit_pending(item)
                elif isinstance(item, _OutcomeEvent):
                    await self._resolve(item)
                # Opportunistic sweep on activity; the watchdog is what
                # guarantees expiry when there is none.
                await self._expire_pending()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[TrajectoryRecorder] drain step failed", exc_info=True,
                )
            finally:
                try:
                    self._queue.task_done()
                except Exception:  # noqa: BLE001
                    pass

    async def _admit_pending(self, gen: _PendingGeneration) -> None:
        cap = _env_int(_ENV_PENDING_MAX, _DEFAULT_PENDING_MAX, 8, 100_000)
        async with self._guard():
            self._pending[gen.op_id] = gen
            self._pending.move_to_end(gen.op_id)
            while len(self._pending) > cap:
                dropped, _ = self._pending.popitem(last=False)
                self._stats["pending_evicted"] += 1
                logger.warning(
                    "[TrajectoryRecorder] evicted pending op=%s (cap=%d) — "
                    "its trajectory is lost; raise %s if this recurs",
                    dropped, cap, _ENV_PENDING_MAX,
                )

    def _guard(self) -> Any:
        """The pending-map lock, created lazily with the loop."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _resolve(self, evt: _OutcomeEvent) -> None:
        async with self._guard():
            gen = self._pending.pop(evt.op_id, None)
        if gen is None:
            # Two very different causes, and the distinction matters:
            #   * an op caged before GENERATE has no candidate text, so
            #     there is genuinely nothing to record; or
            #   * the op_id on the generation side does not MATCH the one
            #     on the terminal side, in which case the join is silently
            #     broken and every trajectory is being lost.
            # Only the log can tell them apart, so name the op.
            self._stats["orphan_outcomes"] += 1
            logger.info(
                "[TrajectoryRecorder] verdict for op=%s had no pending "
                "generation (reason=%s). Expected when the op was caged "
                "before GENERATE; if generations ARE happening, the "
                "generation/terminal op_ids disagree and the join is broken.",
                evt.op_id, evt.terminal_reason or "?",
            )
            return
        outcome, autonomy_type, should_train = classify_terminal_reason(
            evt.terminal_reason, evt.terminal_phase,
        )
        if gen.is_noop and outcome == "success":
            outcome, autonomy_type, should_train = _NOOP
        await self._write(gen, outcome, autonomy_type, should_train, evt)

    async def _expire_pending(self) -> None:
        """Flush generations whose op never reported a verdict.

        Collects under the lock and writes OUTSIDE it: the write awaits a
        cross-process file lock, and holding the pending-map lock across
        that would block every concurrent emit for the duration of disk
        I/O.
        """
        ttl = _env_float(
            _ENV_PENDING_TTL_S, _DEFAULT_PENDING_TTL_S, 30.0, 86_400.0
        )
        now = time.monotonic()
        expired: list = []
        async with self._guard():
            stale = [
                op_id
                for op_id, gen in self._pending.items()
                if (now - gen.created_monotonic) > ttl
            ]
            for op_id in stale:
                gen = self._pending.pop(op_id, None)
                if gen is not None:
                    expired.append((op_id, gen))

        for op_id, gen in expired:
            self._stats["pending_expired"] += 1
            # The breakpoint, named: this generation produced candidates
            # and its op never reported a terminal reason. Recorded as
            # non-trainable, because an outcome we never saw is not a
            # label -- but recorded, because the candidate text is still
            # evidence about the model.
            logger.warning(
                "[TrajectoryRecorder] op=%s expired after %.0fs with %d "
                "candidate(s) and NO verdict — writing outcome=unknown, "
                "should_train=false. If this is common, ops are outliving "
                "the TTL (%s) or never reaching a terminal phase.",
                op_id, ttl, len(gen.candidates), _ENV_PENDING_TTL_S,
            )
            await self._write(
                gen,
                "unknown",
                "intent_written",
                False,
                _OutcomeEvent(
                    op_id=op_id, terminal_phase="", terminal_reason="",
                ),
            )

    async def _write(
        self,
        gen: _PendingGeneration,
        outcome: str,
        autonomy_type: str,
        should_train: bool,
        evt: _OutcomeEvent,
    ) -> None:
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
                async_flock_append_line,
            )
        except Exception:  # noqa: BLE001
            self._stats["write_failures"] += 1
            return

        out_cap = _env_int(
            _ENV_MAX_OUTPUT_CHARS, _DEFAULT_MAX_OUTPUT_CHARS, 256, 2_000_000
        )
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._stats["write_failures"] += 1
            return

        confidence = (
            1.0 if outcome == "success"
            else (0.0 if outcome == "failure" else 0.5)
        )
        n_cands = len(gen.candidates)

        for idx, cand in enumerate(gen.candidates):
            if not isinstance(cand, dict):
                continue
            body = str(cand.get("full_content", "") or "")
            if not body:
                continue
            cand_hash = str(cand.get("candidate_hash", "") or "")
            line = json.dumps(
                {
                    "event_id": str(uuid.uuid4()),
                    "schema_version": TRAJECTORY_RECORDER_SCHEMA_VERSION,
                    "event_type": "interaction",
                    "source": "jarvis_body",
                    "timestamp": gen.created_iso,
                    "user_input": gen.prompt,
                    "assistant_output": _truncate(body, out_cap),
                    "system_context": "",
                    "outcome": outcome,
                    "confidence": confidence,
                    "model_id": gen.model_id,
                    "latency_ms": gen.latency_ms,
                    # Canonical ExperienceEvent field, kept for the reactor
                    # consumers that read it; the split values below are the
                    # ones throughput analysis uses.
                    "tokens_used": gen.prompt_tokens + gen.completion_tokens,
                    "prompt_tokens": gen.prompt_tokens,
                    "completion_tokens": gen.completion_tokens,
                    "tokens_per_second": (
                        round(
                            gen.completion_tokens / (gen.latency_ms / 1000.0),
                            2,
                        )
                        if gen.latency_ms > 0 and gen.completion_tokens
                        else 0.0
                    ),
                    "session_id": gen.session_id,
                    "task_type": gen.task_type,
                    "metadata": {
                        "op_id": gen.op_id,
                        "prompt_key": gen.prompt_key,
                        "candidate_id": str(
                            cand.get("candidate_id", "") or ""
                        ),
                        "candidate_hash": cand_hash,
                        "candidate_index": idx,
                        "n_candidates": n_cands,
                        "file_path": str(cand.get("file_path", "") or ""),
                        "source_path": str(cand.get("source_path", "") or ""),
                        "provider_name": gen.provider_name,
                        "is_noop": gen.is_noop,
                        "cost_usd": gen.cost_usd,
                        "terminal_phase": evt.terminal_phase,
                        "terminal_reason": evt.terminal_reason,
                        # Same exclusion policy as reactor-core's
                        # autonomy_classifier: infrastructure != quality.
                        "autonomy_event_type": autonomy_type,
                        "should_train": should_train,
                        "idempotency_key": f"{gen.op_id}:{cand_hash or idx}",
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            ok = await async_flock_append_line(path, line)
            if ok:
                self._stats["events_written"] += 1
            else:
                self._stats["write_failures"] += 1

    # -- shutdown / introspection ----------------------------------------
    async def drain(self, timeout_s: float = 5.0) -> bool:
        """Flush queued work. Returns True if fully drained in time."""
        if self._queue is None:
            return True
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False
        except Exception:  # noqa: BLE001
            return False

    async def aclose(self, timeout_s: float = 5.0) -> None:
        await self.drain(timeout_s)
        # Flush anything still awaiting a verdict BEFORE tearing down, or a
        # clean shutdown silently discards the trajectories of every op
        # that was still in flight.
        try:
            prev = os.environ.get(_ENV_PENDING_TTL_S)
            os.environ[_ENV_PENDING_TTL_S] = "30"
            for gen in list(self._pending.values()):
                gen.created_monotonic = 0.0
            await self._expire_pending()
            if prev is None:
                os.environ.pop(_ENV_PENDING_TTL_S, None)
            else:
                os.environ[_ENV_PENDING_TTL_S] = prev
        except Exception:  # noqa: BLE001
            logger.debug("[TrajectoryRecorder] final flush failed", exc_info=True)

        for task_attr in ("_watchdog", "_writer"):
            task = getattr(self, task_attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass
            setattr(self, task_attr, None)

    def stats(self) -> Dict[str, Any]:
        return {
            **self._stats,
            "pending_open": len(self._pending),
            "enabled": recorder_enabled(),
            "path": str(self.path),
        }


# ---------------------------------------------------------------------------
# Module-level singleton + thin emit helpers (the call-site surface)
# ---------------------------------------------------------------------------

_default_recorder: Optional[TrajectoryRecorder] = None


def get_recorder() -> TrajectoryRecorder:
    global _default_recorder
    if _default_recorder is None:
        _default_recorder = TrajectoryRecorder()
    return _default_recorder


def reset_recorder_for_tests(
    path: Optional[Path] = None,
) -> TrajectoryRecorder:
    global _default_recorder
    _default_recorder = TrajectoryRecorder(path=path)
    return _default_recorder


def record_generation(**kwargs: Any) -> bool:
    """Fire-and-forget generation emit. NEVER raises."""
    try:
        return get_recorder().record_generation(**kwargs)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[TrajectoryRecorder] record_generation failed", exc_info=True,
        )
        return False


def record_outcome(**kwargs: Any) -> bool:
    """Fire-and-forget verdict emit. NEVER raises."""
    try:
        return get_recorder().record_outcome(**kwargs)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[TrajectoryRecorder] record_outcome failed", exc_info=True,
        )
        return False


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[TrajectoryRecorder] register_flags degraded: %s", exc,
        )
        return 0
    tgt = (
        "backend/core/ouroboros/governance/observability/"
        "trajectory_recorder.py"
    )
    specs = [
        FlagSpec(
            name=_ENV_MASTER, type=FlagType.BOOL, default=False,
            category=Category.OBSERVABILITY, source_file=tgt,
            example=f"{_ENV_MASTER}=true",
            description=(
                "Master for the O+V trajectory recorder. OFF (default, "
                "§33.1) => both emit calls are a flag read and a return. "
                "ON => one canonical ExperienceEvent line per candidate "
                "is appended to the Trinity events dir for Reactor-Core."
            ),
        ),
        FlagSpec(
            name=_ENV_DIR, type=FlagType.STR, default="",
            category=Category.OBSERVABILITY, source_file=tgt,
            example=f"{_ENV_DIR}=/home/jarvis_svc/.jarvis/trinity/events",
            description=(
                "Override the events directory. Empty => "
                "~/.jarvis/trinity/events, which the Trinity experience "
                "receiver already watches."
            ),
        ),
        FlagSpec(
            name=_ENV_QUEUE_MAX, type=FlagType.INT,
            default=_DEFAULT_QUEUE_MAX, category=Category.CAPACITY,
            source_file=tgt, example=f"{_ENV_QUEUE_MAX}=1024",
            description=(
                "Emit queue depth. A full queue DROPS the event (counted "
                "in stats) rather than blocking the control loop."
            ),
        ),
        FlagSpec(
            name=_ENV_PENDING_MAX, type=FlagType.INT,
            default=_DEFAULT_PENDING_MAX, category=Category.CAPACITY,
            source_file=tgt, example=f"{_ENV_PENDING_MAX}=512",
            description=(
                "Max generations held awaiting a verdict (LRU "
                "drop-oldest). Bounds memory when ops never terminate."
            ),
        ),
        FlagSpec(
            name=_ENV_PENDING_TTL_S, type=FlagType.FLOAT,
            default=_DEFAULT_PENDING_TTL_S, category=Category.TIMING,
            source_file=tgt, example=f"{_ENV_PENDING_TTL_S}=1800",
            description=(
                "Seconds a generation waits for its verdict before being "
                "flushed with outcome=unknown/should_train=false."
            ),
        ),
        FlagSpec(
            name=_ENV_TICK_S, type=FlagType.FLOAT,
            default=_DEFAULT_TICK_S, category=Category.TIMING,
            source_file=tgt, example=f"{_ENV_TICK_S}=30",
            description=(
                "Seconds between wall-clock expiry sweeps. A dedicated "
                "watchdog task owns this because expiry driven by queue "
                "activity is not expiry: on a sparse workload the pending "
                "generation waits for an event that never arrives and its "
                "trajectory is never written."
            ),
        ),
        FlagSpec(
            name=_ENV_MAX_PROMPT_CHARS, type=FlagType.INT,
            default=_DEFAULT_MAX_PROMPT_CHARS, category=Category.CAPACITY,
            source_file=tgt, example=f"{_ENV_MAX_PROMPT_CHARS}=48000",
            description="Per-event prompt char cap (truncated, marked).",
        ),
        FlagSpec(
            name=_ENV_MAX_OUTPUT_CHARS, type=FlagType.INT,
            default=_DEFAULT_MAX_OUTPUT_CHARS, category=Category.CAPACITY,
            source_file=tgt, example=f"{_ENV_MAX_OUTPUT_CHARS}=48000",
            description="Per-event candidate char cap (truncated, marked).",
        ),
    ]
    n = 0
    for s in specs:
        try:
            registry.register(s)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[TrajectoryRecorder] seed %s skipped: %s", s.name, exc,
            )
    return n


__all__ = [
    "TRAJECTORY_RECORDER_SCHEMA_VERSION",
    "TrajectoryRecorder",
    "classify_terminal_reason",
    "events_dir",
    "get_recorder",
    "record_generation",
    "record_outcome",
    "recorder_enabled",
    "register_flags",
    "reset_recorder_for_tests",
]
