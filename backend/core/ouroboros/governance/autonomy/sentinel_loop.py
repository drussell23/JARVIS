"""The Sentinel loop — discover, sanction, dispatch, learn, cool down, repeat.

The three parts of autonomous operation already exist separately: the sensor
that finds work (:mod:`goal_discovery`), the judgement that approves it
(:mod:`sentinel`), and the brake that stops it spinning
(:mod:`sentinel_cooldown`). This is the driver that turns them into a loop the
operator can watch instead of drive.

## One op at a time, deliberately

The loop dispatches a single goal and waits for its outcome before discovering
again. It would be easy to fan out — the background pool has six workers — and
it would be wrong: the sensor ranks by evidence, and evidence changes as work
lands. Six simultaneous self-authored goals means five of them were chosen
against a repository state that no longer exists, and any two touching the same
module race each other's validation. Throughput here is not the objective;
*correct sequencing* is, and the pool remains free for the operator's own work.

## Every exit records something

A pass ends in exactly one of four states, and none of them is silent:

* **landed** — the cooldown for that target is CLEARED (backoff measures
  consecutive failure, and this target just proved it can be fixed);
* **rejected / failed** — a lesson to :mod:`lesson_memory` and an escalating
  cooldown on that target;
* **timed out** — same treatment as a failure. An op that never reached a
  terminal state is not evidence of anything except that this target is
  expensive, which is exactly what a cooldown encodes;
* **nothing to do** — the sensor found no eligible work. The loop idles for one
  interval rather than exiting, because "everything is cooling" is a temporary
  state by construction.

The failure paths are what stop a runaway: a target that keeps failing gets
geometrically rarer without anyone choosing a retry limit, and the loop moves
to the next-best evidence instead of grinding.

## Nothing here waits on a human

There is no stdin read anywhere in this module. The approval question is
answered by :func:`sentinel.auto_approval_verdict`, and if that says no, the op
escalates through the normal Iron Gate — which is bounded by its own approval
deadline and sheds itself. The loop never blocks on a TTY, so an unattended
session cannot wedge on one.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.SentinelLoop")

__all__ = ["PassOutcome", "SentinelLoop", "loop_interval_s"]

_ENV_INTERVAL = "JARVIS_SENTINEL_INTERVAL_S"
_ENV_MAX_PASSES = "JARVIS_SENTINEL_MAX_PASSES"


def _env_float(name: str, default: float) -> float:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def loop_interval_s() -> float:
    """Idle interval between passes, derived from the pipeline budget.

    Not a constant: a session whose ops take an hour should not re-scan every
    thirty seconds, and one whose ops take a minute should not sleep for ten.
    A quarter of the pipeline budget keeps the loop responsive without the
    census dominating the machine.
    """
    explicit = _env_float(_ENV_INTERVAL, 0.0)
    if explicit > 0:
        return max(5.0, explicit)
    pipeline = _env_float("JARVIS_PIPELINE_TIMEOUT_S", 0.0)
    if pipeline > 0:
        return max(15.0, pipeline / 4.0)
    return 60.0


@dataclass(frozen=True)
class PassOutcome:
    """What one pass of the loop did — the operator's telemetry row."""

    state: str                      # landed | failed | timed_out | idle | refused
    target: str = ""
    goal_id: str = ""
    op_id: str = ""
    detail: str = ""
    duration_s: float = 0.0

    def render(self) -> str:
        head = f"[Sentinel] {self.state}"
        if self.target:
            head += f" {self.target}"
        if self.goal_id:
            head += f" ({self.goal_id})"
        if self.detail:
            head += f" — {self.detail}"
        return head


class SentinelLoop:
    """Drives autonomous work. Every dependency is injected, so the loop is
    testable without a live organism and cannot reach past its seams."""

    def __init__(
        self,
        *,
        repo_root: Path,
        dispatch: Callable[..., Any],
        watcher: Any = None,
        cooldown: Any = None,
        outcome_fn: Optional[Callable[[str, float], Awaitable[Tuple[str, str]]]] = None,
        observer: Optional[Callable[[PassOutcome], None]] = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._dispatch = dispatch
        self._watcher = watcher
        self._observer = observer
        self._outcome_fn = outcome_fn
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self.passes: int = 0
        if cooldown is not None:
            self._cooldown = cooldown
        else:
            from backend.core.ouroboros.governance.autonomy.sentinel_cooldown import (
                get_default_ledger,
            )
            self._cooldown = get_default_ledger()

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="sentinel_loop")
        logger.warning(
            "[Sentinel] autonomous loop STARTED — interval=%.0fs; the organism "
            "now selects, sanctions and dispatches its own work",
            loop_interval_s(),
        )

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait({task}, timeout=5.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
        logger.warning("[Sentinel] autonomous loop STOPPED after %d pass(es)", self.passes)

    async def _run(self) -> None:
        max_passes = int(_env_float(_ENV_MAX_PASSES, 0.0)) or 0
        try:
            while not self._stopping.is_set():
                if max_passes and self.passes >= max_passes:
                    logger.warning(
                        "[Sentinel] pass ceiling %d reached — idling", max_passes,
                    )
                    return
                try:
                    outcome = await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — a pass must never kill the loop
                    logger.warning("[Sentinel] pass failed: %r", exc, exc_info=True)
                    outcome = PassOutcome("failed", detail=f"{type(exc).__name__}: {exc}")
                self.passes += 1
                self._emit(outcome)
                # Idle only when there was nothing to do; otherwise go straight
                # to the next-best evidence.
                if outcome.state in ("idle", "refused"):
                    try:
                        await asyncio.wait_for(
                            self._stopping.wait(), timeout=loop_interval_s(),
                        )
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise

    # -- one pass ---------------------------------------------------------

    async def run_once(self) -> PassOutcome:
        """Discover → sanction → dispatch → outcome. NEVER raises."""
        started = time.monotonic()
        from backend.core.ouroboros.governance.autonomy import goal_discovery as gd

        candidates = await gd.discover(
            repo_root=self._repo_root,
            watcher=self._watcher,
            cooldown=self._cooldown,
        )
        if not candidates:
            return PassOutcome("idle", detail="no eligible work", duration_s=0.0)

        work = candidates[0]
        result = gd.synthesize_and_sign(work)
        if result is None or not getattr(result, "ok", False):
            reason = getattr(result, "reason", "unknown") if result else "unknown"
            # A duplicate id means this exact work is ALREADY on the roadmap —
            # not a fault, and not something a cooldown should punish.
            if "duplicate" in str(reason).lower():
                self._cooldown.record_failure(
                    work.target_file, reason="already sanctioned; deferring",
                )
                return PassOutcome(
                    "refused", work.target_file, work.goal_id,
                    detail=f"already on the roadmap ({reason})",
                    duration_s=time.monotonic() - started,
                )
            await self._record_lesson(
                work, phase="SANCTION", failure_class="sanction_refused",
                error_text=str(reason),
            )
            self._cooldown.record_failure(work.target_file, reason=str(reason))
            return PassOutcome(
                "failed", work.target_file, work.goal_id,
                detail=f"sanction refused: {reason}",
                duration_s=time.monotonic() - started,
            )

        op_id = ""
        try:
            dispatched = self._dispatch(
                goal_id=result.goal_id,
                description=work.describe(),
                target_files=(work.target_file,),
            )
            if asyncio.iscoroutine(dispatched):
                dispatched = await dispatched
            op_id = str(dispatched or "")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            op_id = ""
            logger.warning("[Sentinel] dispatch failed: %r", exc)

        if not op_id:
            await self._record_lesson(
                work, phase="INTAKE", failure_class="dispatch_refused",
                error_text="the cage refused the claim, or intake was unreachable",
            )
            self._cooldown.record_failure(work.target_file, reason="not dispatched")
            return PassOutcome(
                "failed", work.target_file, result.goal_id,
                detail="not dispatched", duration_s=time.monotonic() - started,
            )

        state, detail = await self._await_outcome(op_id)
        elapsed = time.monotonic() - started

        if state == "landed":
            self._cooldown.record_success(work.target_file)
            return PassOutcome("landed", work.target_file, result.goal_id, op_id,
                               detail, elapsed)

        await self._record_lesson(
            work, phase="TERMINAL", failure_class=state,
            error_text=detail or state, op_id=op_id,
        )
        self._cooldown.record_failure(work.target_file, reason=f"{state}: {detail}")
        return PassOutcome(state, work.target_file, result.goal_id, op_id,
                           detail, elapsed)

    # -- seams ------------------------------------------------------------

    async def _await_outcome(self, op_id: str) -> Tuple[str, str]:
        """Terminal state for *op_id*, bounded. ('landed'|'failed'|'timed_out', detail)."""
        if self._outcome_fn is not None:
            try:
                return await self._outcome_fn(op_id, self._outcome_deadline_s())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                return "failed", f"outcome probe failed: {type(exc).__name__}"
        return await self._await_via_ledger(op_id)

    def _outcome_deadline_s(self) -> float:
        """An op gets its own pipeline budget, plus slack for teardown."""
        pipeline = _env_float("JARVIS_PIPELINE_TIMEOUT_S", 0.0)
        return (pipeline if pipeline > 0 else 900.0) * 1.2

    async def _await_via_ledger(self, op_id: str) -> Tuple[str, str]:
        """Poll the op ledger the rest of the pipeline already writes to.

        Reading the same rows the operator reads means the loop's idea of
        "this op finished" cannot drift from the record they inspect when they
        ask why it did what it did.
        """
        from backend.core.ouroboros.governance.ledger import OperationState

        terminal_ok = {OperationState.APPLIED.value}
        terminal_bad = {
            OperationState.FAILED.value,
            OperationState.ROLLED_BACK.value,
            OperationState.BLOCKED.value,
        }
        deadline = time.monotonic() + self._outcome_deadline_s()
        poll = max(2.0, loop_interval_s() / 10.0)
        while time.monotonic() < deadline:
            if self._stopping.is_set():
                return "failed", "loop stopping"
            state = await asyncio.to_thread(self._last_ledger_state, op_id)
            if state in terminal_ok:
                return "landed", state
            if state in terminal_bad:
                return "failed", state
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=poll)
                return "failed", "loop stopping"
            except asyncio.TimeoutError:
                continue
        return "timed_out", f"no terminal state within {self._outcome_deadline_s():.0f}s"

    @staticmethod
    def _last_ledger_state(op_id: str) -> str:
        """The most recent state recorded for *op_id*, or "". NEVER raises."""
        try:
            import json

            base = Path(os.environ.get(
                "OUROBOROS_LEDGER_DIR",
                str(Path.home() / ".jarvis" / "ouroboros" / "ledger"),
            ))
            state = ""
            for path in base.glob(f"*{op_id}*.jsonl"):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        state = str(json.loads(line).get("state", "")) or state
                    except ValueError:
                        continue
            return state
        except Exception:  # noqa: BLE001
            return ""

    async def _record_lesson(
        self, work: Any, *, phase: str, failure_class: str,
        error_text: str, op_id: str = "",
    ) -> None:
        """Route a failed pass into LessonMemory. NEVER raises."""
        try:
            from backend.core.ouroboros.governance.lesson_memory import record_lesson

            await record_lesson(
                op_id=op_id or f"sentinel:{work.goal_id}",
                target_files=(work.target_file,),
                phase=phase,
                failure_class=failure_class,
                error_text=str(error_text)[:600],
                summary=f"autonomous goal {work.goal_id} did not land",
                error_class="sentinel_pass_failed",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Sentinel] lesson recording degraded: %r", exc)

    def _emit(self, outcome: PassOutcome) -> None:
        logger.warning("%s", outcome.render())
        if self._observer is None:
            return
        try:
            self._observer(outcome)
        except Exception:  # noqa: BLE001 — the TUI must never break the loop
            logger.debug("[Sentinel] observer degraded", exc_info=True)
