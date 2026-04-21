"""
ScheduleRunner + REPL — Slice 4 of the Scheduled Wake-ups arc.
===============================================================

Two pieces bundled because they share the same introspection surface:

* :class:`ScheduleRunner` — an asyncio-native runner. On each tick
  (default 60s, env-tunable) it scans the :class:`JobRegistry` for due
  enabled jobs and fires them via their registered handlers. The
  :class:`WakeupController` manages its own in-process timers, so the
  runner doesn't need to poll wakeups — it just coexists with them.

* :func:`dispatch_schedule_command` — ``/schedule`` slash-command
  dispatcher for SerpentFlow. Lets operators list, inspect, add,
  remove, enable/disable, fire-now, and schedule dynamic wake-ups
  from the REPL.

Manifesto alignment
-------------------

* §1 — every operator-facing mutation (``add``, ``wakeup``, ``remove``,
  ``enable``, ``disable``, ``fire-now``) goes through code paths
  operators authored. The model cannot directly hit the registry.
* §5 — deterministic decisions (is this job due, has this wake-up
  deadline passed). Pure arithmetic over the tracked state.
* §7 — handler raising never brings down the runner. Lost clock
  (process suspend) replays the NEXT fire deadline, not the missed
  one (CC convention — don't fire a catch-up tsunami after a
  resume).
* §8 — every fire / skip / error emits a ``[ScheduleRunner]`` INFO log.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import textwrap
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from backend.core.ouroboros.governance.schedule_expression import (
    ScheduleExpression,
    ScheduleExpressionError,
)
from backend.core.ouroboros.governance.schedule_job import (
    JobRegistry,
    JobRegistryError,
    ScheduledJob,
    get_default_job_registry,
)
from backend.core.ouroboros.governance.schedule_wakeup import (
    WakeupController,
    WakeupError,
    get_default_wakeup_controller,
)

logger = logging.getLogger("Ouroboros.ScheduleRunner")


SCHEDULE_RUNNER_SCHEMA_VERSION: str = "schedule_runner.v1"


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def schedule_runner_enabled() -> bool:
    """Default **``false``** during arc rollout; Slice 5 graduates."""
    return os.environ.get(
        "JARVIS_SCHEDULE_RUNNER_ENABLED", "false",
    ).strip().lower() == "true"


def _tick_interval_s() -> float:
    try:
        return max(1.0, float(os.environ.get(
            "JARVIS_SCHEDULE_RUNNER_TICK_S", "60",
        )))
    except (TypeError, ValueError):
        return 60.0


# ---------------------------------------------------------------------------
# ScheduleRunner
# ---------------------------------------------------------------------------


class ScheduleRunner:
    """Async runner that polls :class:`JobRegistry` and fires due jobs.

    Wake-ups are fired by :class:`WakeupController`'s own per-request
    timers, not by this runner — the two cohabit but don't overlap.

    Usage::

        runner = ScheduleRunner(registry=reg)
        await runner.start()
        # ... later ...
        await runner.stop()
    """

    def __init__(
        self,
        *,
        registry: Optional[JobRegistry] = None,
        tick_interval_s: Optional[float] = None,
    ) -> None:
        self._registry = registry or get_default_job_registry()
        self._tick = tick_interval_s or _tick_interval_s()
        self._task: Optional[asyncio.Task[None]] = None
        self._stopping = asyncio.Event()
        self._last_tick_ts: Optional[float] = None
        self._fires_total = 0
        self._errors_total = 0

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(
            self._run(), name="schedule-runner",
        )
        logger.info(
            "[ScheduleRunner] started tick_interval_s=%.1f", self._tick,
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self._tick + 2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None
        logger.info("[ScheduleRunner] stopped")

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # --- run loop --------------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick(now=time.time())
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[ScheduleRunner] tick raised: %s", exc,
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._tick,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def tick(self, *, now: float) -> List[ScheduledJob]:
        """Scan the registry and fire every due enabled job.

        Returns the list of fired jobs (useful for tests).
        """
        self._last_tick_ts = now
        fired: List[ScheduledJob] = []
        due_jobs = [
            j for j in self._registry.list_jobs(enabled_only=True)
            if j.next_run_ts is not None and j.next_run_ts <= now
        ]
        for job in due_jobs:
            await self._fire_job(job, now=now)
            fired.append(job)
        return fired

    async def _fire_job(self, job: ScheduledJob, *, now: float) -> None:
        handler = self._registry.get_handler(job.handler_name)
        if handler is None:
            logger.warning(
                "[ScheduleRunner] skip job=%s — handler %r unregistered",
                job.job_id, job.handler_name,
            )
            self._errors_total += 1
            return
        try:
            coro = handler(job, dict(job.payload))
            if asyncio.iscoroutine(coro):
                await coro
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ScheduleRunner] job %s handler raised: %s",
                job.job_id, exc,
            )
            self._errors_total += 1
        else:
            logger.info(
                "[ScheduleRunner] fired job=%s handler=%s",
                job.job_id, job.handler_name,
            )
            self._fires_total += 1
        # Advance counters regardless of handler outcome — the intent
        # of the schedule was to wake at `now`, and the runner fulfilled
        # that. Handler-raise does not disable the job (policy: fail
        # forward; ops can disable manually if the handler's broken).
        self._registry.record_fire(job.job_id, fired_ts=now)

    # --- introspection ---------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEDULE_RUNNER_SCHEMA_VERSION,
            "is_running": self.is_running,
            "tick_interval_s": self._tick,
            "last_tick_ts": self._last_tick_ts,
            "fires_total": self._fires_total,
            "errors_total": self._errors_total,
        }


# ---------------------------------------------------------------------------
# REPL dispatcher
# ---------------------------------------------------------------------------


@dataclass
class ScheduleDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_SCHEDULE_HELP = textwrap.dedent(
    """
    Schedule + Wake-up commands
    ---------------------------
      /schedule                        — list active jobs + pending wakeups
      /schedule list                   — same as bare /schedule
      /schedule show <job-id>          — full detail of one job
      /schedule add <handler> <expr> [description]
                                       — add a recurring job (expr can be
                                         @hourly / @daily / @weekly / ...
                                         or 'every monday at 9am' or raw
                                         5-field cron)
      /schedule remove <job-id>        — remove a job
      /schedule enable <job-id>        — enable a disabled job
      /schedule disable <job-id>       — disable an active job
      /schedule fire-now <job-id>      — immediately record + advance a
                                         job's counters (does NOT run handler)
      /schedule handlers               — list registered handlers
      /schedule history                — recent runner stats
      /schedule help                   — this text

      /wakeup <handler> <delay-s> [reason]
                                       — schedule a one-shot wake-up
      /wakeup cancel <wakeup-id>       — cancel a pending wake-up
      /wakeup list                     — list pending wakeups
      /wakeup history                  — recent resolved outcomes
    """
).strip()


_COMMANDS = frozenset({"/schedule", "/wakeup"})


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def dispatch_schedule_command(
    line: str,
    *,
    registry: Optional[JobRegistry] = None,
    wakeup: Optional[WakeupController] = None,
) -> ScheduleDispatchResult:
    """Route one REPL line to a schedule/wakeup action.

    Injecting ``registry`` / ``wakeup`` makes tests deterministic;
    production callers pass ``None`` and get the module singletons.
    """
    if not _matches(line):
        return ScheduleDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ScheduleDispatchResult(
            ok=False, text=f"  /schedule parse error: {exc}",
        )
    if not tokens:
        return ScheduleDispatchResult(ok=False, text="", matched=False)

    reg = registry or get_default_job_registry()
    wak = wakeup or get_default_wakeup_controller()

    cmd = tokens[0]
    args = tokens[1:]
    if cmd == "/schedule":
        return _handle_schedule(reg, wak, args)
    if cmd == "/wakeup":
        return _handle_wakeup(reg, wak, args)
    return ScheduleDispatchResult(ok=False, text="", matched=False)


def _handle_schedule(
    reg: JobRegistry,
    wak: WakeupController,
    args: Sequence[str],
) -> ScheduleDispatchResult:
    if not args:
        return _schedule_list(reg, wak)
    head = args[0]
    if head == "list":
        return _schedule_list(reg, wak)
    if head == "help":
        return ScheduleDispatchResult(ok=True, text=_SCHEDULE_HELP)
    if head == "handlers":
        return _schedule_handlers(reg)
    if head == "show":
        if len(args) < 2:
            return ScheduleDispatchResult(
                ok=False, text="  /schedule show <job-id>",
            )
        return _schedule_show(reg, args[1])
    if head == "add":
        return _schedule_add(reg, args[1:])
    if head == "remove":
        if len(args) < 2:
            return ScheduleDispatchResult(
                ok=False, text="  /schedule remove <job-id>",
            )
        return _schedule_remove(reg, args[1])
    if head == "enable":
        if len(args) < 2:
            return ScheduleDispatchResult(
                ok=False, text="  /schedule enable <job-id>",
            )
        return _schedule_toggle(reg, args[1], enable=True)
    if head == "disable":
        if len(args) < 2:
            return ScheduleDispatchResult(
                ok=False, text="  /schedule disable <job-id>",
            )
        return _schedule_toggle(reg, args[1], enable=False)
    if head == "fire-now":
        if len(args) < 2:
            return ScheduleDispatchResult(
                ok=False, text="  /schedule fire-now <job-id>",
            )
        return _schedule_fire_now(reg, args[1])
    if head == "history":
        return _schedule_history(wak)
    # Shorthand: /schedule <job-id> → show
    return _schedule_show(reg, head)


def _schedule_list(
    reg: JobRegistry, wak: WakeupController,
) -> ScheduleDispatchResult:
    jobs = reg.list_jobs()
    pending_wakeups = wak.snapshot_all()
    pending_wakeups = [w for w in pending_wakeups if w["state"] == "pending"]
    lines: List[str] = []
    if jobs:
        lines.append(f"  Active jobs ({len(jobs)}):")
        for j in jobs:
            state = "enabled" if j.enabled else "disabled"
            nxt = (
                f"{j.next_run_ts:.0f}" if j.next_run_ts is not None else "-"
            )
            lines.append(
                f"  - {j.job_id}  {state:<8} handler={j.handler_name:<20} "
                f"cron={j.expression.canonical_cron}  next={nxt}  "
                f"runs={j.run_count}"
            )
    else:
        lines.append("  (no scheduled jobs)")
    if pending_wakeups:
        lines.append("")
        lines.append(f"  Pending wakeups ({len(pending_wakeups)}):")
        for w in pending_wakeups:
            lines.append(
                f"  - {w['wakeup_id']:<16} handler={w['handler_name']:<20} "
                f"delay_s={w['delay_seconds']:.0f}  fires={w['fires_at_iso']}"
            )
    return ScheduleDispatchResult(ok=True, text="\n".join(lines))


def _schedule_handlers(reg: JobRegistry) -> ScheduleDispatchResult:
    metas = reg.list_handlers()
    if not metas:
        return ScheduleDispatchResult(
            ok=True, text="  (no handlers registered)",
        )
    lines = [f"  Registered handlers ({len(metas)}):"]
    for m in metas:
        lines.append(
            f"  - {m.name:<24} source={m.source:<13} {m.description}"
        )
    return ScheduleDispatchResult(ok=True, text="\n".join(lines))


def _schedule_show(
    reg: JobRegistry, job_id: str,
) -> ScheduleDispatchResult:
    job = reg.get_job(job_id)
    if job is None:
        return ScheduleDispatchResult(
            ok=False, text=f"  /schedule: unknown job: {job_id}",
        )
    proj = JobRegistry.project_job(job)
    lines = [
        f"  Job {job_id}",
        f"    handler     : {proj['handler_name']}",
        f"    enabled     : {proj['enabled']}",
        f"    canonical   : {proj['canonical_cron']}",
        f"    original    : {proj['original_phrase']}",
        f"    created_at  : {proj['created_at_iso']}",
        f"    last_run_ts : {proj['last_run_ts']}",
        f"    next_run_ts : {proj['next_run_ts']}",
        f"    run_count   : {proj['run_count']}",
        f"    max_runs    : {proj['max_runs']}",
        f"    payload_keys: {proj['payload_keys']}",
        f"    description : {proj['description']}",
    ]
    return ScheduleDispatchResult(ok=True, text="\n".join(lines))


def _schedule_add(
    reg: JobRegistry, args: Sequence[str],
) -> ScheduleDispatchResult:
    if len(args) < 2:
        return ScheduleDispatchResult(
            ok=False,
            text='  /schedule add <handler> "<expr>" [description]',
        )
    handler_name = args[0]
    expr_phrase = args[1]
    description = " ".join(args[2:]).strip() if len(args) > 2 else ""
    try:
        expression = ScheduleExpression.from_phrase(expr_phrase)
    except ScheduleExpressionError as exc:
        return ScheduleDispatchResult(
            ok=False, text=f"  /schedule add: {exc}",
        )
    try:
        job = reg.add_job(
            handler_name=handler_name,
            expression=expression,
            description=description,
        )
    except JobRegistryError as exc:
        return ScheduleDispatchResult(
            ok=False, text=f"  /schedule add: {exc}",
        )
    return ScheduleDispatchResult(
        ok=True,
        text=(
            f"  added: {job.job_id} handler={handler_name} "
            f"cron={expression.canonical_cron}"
        ),
    )


def _schedule_remove(
    reg: JobRegistry, job_id: str,
) -> ScheduleDispatchResult:
    if reg.remove_job(job_id):
        return ScheduleDispatchResult(
            ok=True, text=f"  removed: {job_id}",
        )
    return ScheduleDispatchResult(
        ok=False, text=f"  /schedule remove: unknown job: {job_id}",
    )


def _schedule_toggle(
    reg: JobRegistry, job_id: str, *, enable: bool,
) -> ScheduleDispatchResult:
    updated = (
        reg.enable_job(job_id) if enable else reg.disable_job(job_id)
    )
    if updated is None:
        verb = "enable" if enable else "disable"
        return ScheduleDispatchResult(
            ok=False, text=f"  /schedule {verb}: unknown job: {job_id}",
        )
    state = "enabled" if enable else "disabled"
    return ScheduleDispatchResult(
        ok=True, text=f"  {state}: {job_id}",
    )


def _schedule_fire_now(
    reg: JobRegistry, job_id: str,
) -> ScheduleDispatchResult:
    updated = reg.record_fire(job_id, fired_ts=time.time())
    if updated is None:
        return ScheduleDispatchResult(
            ok=False, text=f"  /schedule fire-now: unknown job: {job_id}",
        )
    return ScheduleDispatchResult(
        ok=True,
        text=(
            f"  fire-now: {job_id} run_count={updated.run_count} "
            f"next={updated.next_run_ts}"
        ),
    )


def _schedule_history(wak: WakeupController) -> ScheduleDispatchResult:
    h = wak.history()[-10:]
    if not h:
        return ScheduleDispatchResult(ok=True, text="  (no history)")
    lines = [f"  Recent resolved wakeups ({len(h)}):"]
    for item in h:
        lines.append(
            f"  - {item['wakeup_id']:<16} state={item['state']:<10} "
            f"ok={item['ok']} actual_delay_s={item['actual_delay_s']:.1f}"
        )
    return ScheduleDispatchResult(ok=True, text="\n".join(lines))


def _handle_wakeup(
    reg: JobRegistry,
    wak: WakeupController,
    args: Sequence[str],
) -> ScheduleDispatchResult:
    if not args:
        return _wakeup_list(wak)
    head = args[0]
    if head == "list":
        return _wakeup_list(wak)
    if head == "history":
        return _schedule_history(wak)
    if head == "cancel":
        if len(args) < 2:
            return ScheduleDispatchResult(
                ok=False, text="  /wakeup cancel <wakeup-id>",
            )
        return _wakeup_cancel(wak, args[1])
    if head == "help":
        return ScheduleDispatchResult(ok=True, text=_SCHEDULE_HELP)
    # `/wakeup <handler> <delay> [reason]`
    if len(args) < 2:
        return ScheduleDispatchResult(
            ok=False,
            text="  /wakeup <handler> <delay-s> [reason]",
        )
    handler_name = args[0]
    try:
        delay = float(args[1])
    except ValueError:
        return ScheduleDispatchResult(
            ok=False, text=f"  /wakeup: invalid delay: {args[1]}",
        )
    reason = " ".join(args[2:]).strip()
    try:
        wak.schedule(
            handler_name=handler_name,
            delay_seconds=delay,
            reason=reason,
            source="operator",
        )
    except WakeupError as exc:
        return ScheduleDispatchResult(
            ok=False, text=f"  /wakeup: {exc}",
        )
    # Find the id we just created — last snapshot with state=pending
    snaps = [s for s in wak.snapshot_all() if s["state"] == "pending"]
    latest = snaps[-1] if snaps else {"wakeup_id": "?"}
    return ScheduleDispatchResult(
        ok=True,
        text=(
            f"  wakeup scheduled: {latest['wakeup_id']} "
            f"handler={handler_name} delay_s={delay}"
        ),
    )


def _wakeup_list(wak: WakeupController) -> ScheduleDispatchResult:
    snaps = [s for s in wak.snapshot_all() if s["state"] == "pending"]
    if not snaps:
        return ScheduleDispatchResult(
            ok=True, text="  (no pending wakeups)",
        )
    lines = [f"  Pending wakeups ({len(snaps)}):"]
    for s in snaps:
        lines.append(
            f"  - {s['wakeup_id']:<16} handler={s['handler_name']:<20} "
            f"delay_s={s['delay_seconds']:.0f}  fires={s['fires_at_iso']}"
        )
    return ScheduleDispatchResult(ok=True, text="\n".join(lines))


def _wakeup_cancel(
    wak: WakeupController, wakeup_id: str,
) -> ScheduleDispatchResult:
    outcome = wak.cancel(wakeup_id, reason="repl-cancel")
    if outcome is None:
        return ScheduleDispatchResult(
            ok=False, text=f"  /wakeup cancel: unknown id: {wakeup_id}",
        )
    return ScheduleDispatchResult(
        ok=True,
        text=f"  cancelled: {wakeup_id}",
    )


__all__ = [
    "SCHEDULE_RUNNER_SCHEMA_VERSION",
    "ScheduleDispatchResult",
    "ScheduleRunner",
    "dispatch_schedule_command",
    "schedule_runner_enabled",
]
