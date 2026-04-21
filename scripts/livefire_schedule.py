#!/usr/bin/env python3
"""Live-fire battle test — Scheduled Wake-ups arc.

Resolves the gap-writeup quote:
  "O+V has sensors-on-poll, not true cron. For 'check this file every
   Monday morning,' no primitive."

Scenarios
---------
 1. The exact gap-quote phrase parses to canonical cron.
 2. Cron jobs fire through a real ScheduleRunner.tick().
 3. Runner skips disabled / non-due jobs.
 4. Handler raising doesn't crash runner; other jobs still fire.
 5. WakeupController schedules + fires a one-shot via its own timer.
 6. WakeupController cancel works before fire.
 7. WakeupController fail-closed on missing handler.
 8. /schedule add + remove + enable/disable round-trip via REPL.
 9. /wakeup schedule + cancel round-trip via REPL.
10. §1 authority: model source rejected.
11. Delay bounds + capacity bounds enforced post-arc.
12. Authority invariant grep on all 4 arc modules.

Run::
    python3 scripts/livefire_schedule.py
"""
from __future__ import annotations

import asyncio
import re as _re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.core.ouroboros.governance.schedule_expression import (  # noqa: E402
    ScheduleExpression,
    ScheduleExpressionError,
)
from backend.core.ouroboros.governance.schedule_job import (  # noqa: E402
    HandlerAuthorityError,
    HandlerSource,
    JobRegistry,
    JobRegistryError,
)
from backend.core.ouroboros.governance.schedule_runner import (  # noqa: E402
    ScheduleRunner,
    dispatch_schedule_command,
)
from backend.core.ouroboros.governance.schedule_wakeup import (  # noqa: E402
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_FIRED,
    WakeupCapacityError,
    WakeupController,
    WakeupDelayError,
)


C_PASS, C_FAIL, C_BOLD, C_DIM, C_END = (
    "\033[92m", "\033[91m", "\033[1m", "\033[2m", "\033[0m",
)


def _banner(text: str) -> None:
    print(f"\n{C_BOLD}{'━' * 72}{C_END}\n{C_BOLD}▶ {text}{C_END}\n{C_BOLD}{'━' * 72}{C_END}")


def _pass(t: str) -> None:
    print(f"  {C_PASS}✓ {t}{C_END}")


def _fail(t: str) -> None:
    print(f"  {C_FAIL}✗ {t}{C_END}")


class Scenario:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed: List[str] = []
        self.failed: List[str] = []

    def check(self, desc: str, ok: bool) -> None:
        (self.passed if ok else self.failed).append(desc)
        (_pass if ok else _fail)(desc)

    @property
    def ok(self) -> bool:
        return not self.failed


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_gap_quote_parses() -> Scenario:
    """The exact gap-writeup phrase parses to canonical cron."""
    s = Scenario("Gap quote: 'every monday' parses to cron")
    expr = ScheduleExpression.from_phrase("every monday")
    s.check(
        f"'every monday' → {expr.canonical_cron}",
        expr.canonical_cron == "0 9 * * 1",
    )
    expr2 = ScheduleExpression.from_phrase("every monday at 9am")
    s.check(
        f"'every monday at 9am' → {expr2.canonical_cron}",
        expr2.canonical_cron == "0 9 * * 1",
    )
    # Also "every weekday at 9am"
    expr3 = ScheduleExpression.from_phrase("every weekday at 9am")
    s.check(
        f"'every weekday at 9am' → {expr3.canonical_cron}",
        expr3.canonical_cron == "0 9 * * 1-5",
    )
    return s


async def scenario_runner_fires_due_jobs() -> Scenario:
    """ScheduleRunner.tick fires jobs whose next_run_ts is due."""
    s = Scenario("Runner fires due jobs through tick()")
    reg = JobRegistry()
    fired: List[str] = []

    async def _handler(job, payload):
        fired.append(job.job_id)

    reg.register_handler("tick", _handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    past = time.time() - 7200
    job = reg.add_job(handler_name="tick", expression=expr, now_ts=past)
    runner = ScheduleRunner(registry=reg)
    fired_list = await runner.tick(now=time.time())
    s.check(f"1 job fired (got {len(fired_list)})", len(fired_list) == 1)
    s.check(f"handler actually invoked (fired {len(fired)}x)", len(fired) == 1)
    updated = reg.get_job(job.job_id)
    s.check(f"run_count advanced to 1 (got {updated.run_count})",
            updated.run_count == 1)
    s.check("next_run_ts recalculated", updated.next_run_ts > past)
    return s


async def scenario_runner_skips_disabled_and_future() -> Scenario:
    """Disabled jobs and not-yet-due jobs are skipped."""
    s = Scenario("Runner skips disabled + future jobs")
    reg = JobRegistry()
    fired: List[str] = []

    async def _h(job, payload):
        fired.append(job.job_id)

    reg.register_handler("tick", _h, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    past = time.time() - 7200
    future = time.time() + 7200

    j_disabled = reg.add_job(handler_name="tick", expression=expr, now_ts=past)
    reg.disable_job(j_disabled.job_id)
    j_future = reg.add_job(handler_name="tick", expression=expr, now_ts=future)
    j_due = reg.add_job(handler_name="tick", expression=expr, now_ts=past)

    runner = ScheduleRunner(registry=reg)
    await runner.tick(now=time.time())
    s.check("only the enabled + due job fired", fired == [j_due.job_id])
    s.check("disabled job still has run_count=0",
            reg.get_job(j_disabled.job_id).run_count == 0)
    s.check("future job still has run_count=0",
            reg.get_job(j_future.job_id).run_count == 0)
    return s


async def scenario_runner_handler_raise_isolation() -> Scenario:
    """One handler raising doesn't prevent others from firing."""
    s = Scenario("Handler raise isolated; other jobs still fire")
    reg = JobRegistry()
    fired: List[str] = []

    async def _good(job, payload):
        fired.append("good")

    async def _bad(job, payload):
        raise RuntimeError("boom")

    reg.register_handler("good", _good, source=HandlerSource.OPERATOR)
    reg.register_handler("bad", _bad, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    past = time.time() - 7200
    reg.add_job(handler_name="bad", expression=expr, now_ts=past)
    reg.add_job(handler_name="good", expression=expr, now_ts=past)
    runner = ScheduleRunner(registry=reg)
    await runner.tick(now=time.time())
    s.check("good handler ran despite bad handler raising",
            "good" in fired)
    s.check("runner errors_total advanced to 1",
            runner.stats()["errors_total"] == 1)
    return s


async def scenario_wakeup_schedule_and_fire() -> Scenario:
    """WakeupController schedules a one-shot; timer fires handler."""
    s = Scenario("Wakeup schedules + fires via timer")
    fired: List[Dict[str, Any]] = []

    async def _handler(req, payload):
        fired.append({"wakeup_id": req.wakeup_id, "payload": dict(payload)})
        return "ok"

    ctl = WakeupController(
        handler_resolver=lambda _n: _handler,
        min_delay_s=0.0, max_delay_s=10.0,
    )
    fut = ctl.schedule(
        handler_name="h", delay_seconds=0.1,
        reason="live-fire test",
        payload={"key": "val"},
    )
    outcome = await asyncio.wait_for(fut, timeout=3.0)
    s.check(f"outcome.state == {STATE_FIRED}", outcome.state == STATE_FIRED)
    s.check("outcome.ok", outcome.ok)
    s.check("handler actually invoked", len(fired) == 1)
    s.check("payload delivered", fired[0]["payload"] == {"key": "val"})
    s.check("handler_result captured", outcome.handler_result == "ok")
    return s


async def scenario_wakeup_cancel_before_fire() -> Scenario:
    """Cancelling a pending wakeup prevents the fire."""
    s = Scenario("Wakeup cancel before fire")
    fired: List[str] = []

    async def _handler(req, payload):
        fired.append(req.wakeup_id)

    ctl = WakeupController(
        handler_resolver=lambda _n: _handler,
        min_delay_s=0.0, max_delay_s=100.0,
    )
    fut = ctl.schedule(handler_name="h", delay_seconds=50.0)
    wid = ctl.pending_ids()[0]
    cancel_outcome = ctl.cancel(wid, reason="test")
    s.check(f"cancel state == {STATE_CANCELLED}",
            cancel_outcome.state == STATE_CANCELLED)
    future_outcome = await asyncio.wait_for(fut, timeout=1.0)
    s.check(f"future resolved to {STATE_CANCELLED}",
            future_outcome.state == STATE_CANCELLED)
    s.check("handler was NOT invoked", fired == [])
    return s


async def scenario_wakeup_missing_handler_fail_closed() -> Scenario:
    """Resolver returning None resolves to STATE_FAILED."""
    s = Scenario("Wakeup fail-closed on missing handler")
    ctl = WakeupController(
        handler_resolver=lambda _n: None,
        min_delay_s=0.0, max_delay_s=10.0,
    )
    fut = ctl.schedule(handler_name="missing", delay_seconds=0.1)
    outcome = await asyncio.wait_for(fut, timeout=3.0)
    s.check(f"state == {STATE_FAILED}", outcome.state == STATE_FAILED)
    s.check("error message references handler", "no_handler" in (outcome.error or ""))
    return s


async def scenario_schedule_repl_round_trip() -> Scenario:
    """/schedule add → list → disable → enable → remove."""
    s = Scenario("/schedule REPL add/list/toggle/remove round trip")
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)

    async def _h(job, payload):
        pass

    reg.register_handler("check", _h, source=HandlerSource.OPERATOR)

    r_add = dispatch_schedule_command(
        '/schedule add check "every monday at 9am" weekly-checkin',
        registry=reg, wakeup=wak,
    )
    s.check("/schedule add ok", r_add.ok)
    s.check("registry has 1 job", len(reg.list_jobs()) == 1)

    job = reg.list_jobs()[0]
    s.check(
        f"cron canonicalised to '0 9 * * 1' (got {job.expression.canonical_cron})",
        job.expression.canonical_cron == "0 9 * * 1",
    )

    r_list = dispatch_schedule_command(
        "/schedule", registry=reg, wakeup=wak,
    )
    s.check("/schedule list shows the job",
            job.job_id in r_list.text and "check" in r_list.text)

    r_disable = dispatch_schedule_command(
        f"/schedule disable {job.job_id}", registry=reg, wakeup=wak,
    )
    s.check("/schedule disable ok", r_disable.ok)
    s.check("job now disabled", reg.get_job(job.job_id).enabled is False)

    r_enable = dispatch_schedule_command(
        f"/schedule enable {job.job_id}", registry=reg, wakeup=wak,
    )
    s.check("/schedule enable ok", r_enable.ok)
    s.check("job re-enabled", reg.get_job(job.job_id).enabled is True)

    r_remove = dispatch_schedule_command(
        f"/schedule remove {job.job_id}", registry=reg, wakeup=wak,
    )
    s.check("/schedule remove ok", r_remove.ok)
    s.check("registry empty", len(reg.list_jobs()) == 0)
    return s


async def scenario_wakeup_repl_round_trip() -> Scenario:
    """/wakeup <handler> <delay> + cancel."""
    s = Scenario("/wakeup REPL schedule + cancel round trip")
    reg = JobRegistry()
    wak = WakeupController(
        handler_resolver=lambda _n: lambda r, p: None,
        min_delay_s=0.0, max_delay_s=100.0,
    )
    r_sched = dispatch_schedule_command(
        '/wakeup alert 30 "check test output"',
        registry=reg, wakeup=wak,
    )
    s.check("/wakeup schedule ok", r_sched.ok)
    s.check("wakeup pending count=1", wak.pending_count() == 1)

    wid = wak.pending_ids()[0]
    r_cancel = dispatch_schedule_command(
        f"/wakeup cancel {wid}", registry=reg, wakeup=wak,
    )
    s.check("/wakeup cancel ok", r_cancel.ok)
    s.check("wakeup pending count=0", wak.pending_count() == 0)
    return s


async def scenario_authority_model_rejected() -> Scenario:
    """§1: model source cannot register handlers."""
    s = Scenario("Authority: model source rejected")
    reg = JobRegistry()

    class FakeSource(str):
        pass

    try:
        reg.register_handler(
            "evil", lambda *a: None,
            source=FakeSource("model"),  # type: ignore[arg-type]
        )
        s.check("model source refused (didn't raise)", False)
    except HandlerAuthorityError:
        s.check("model source → HandlerAuthorityError", True)
    return s


async def scenario_bounds_enforced() -> Scenario:
    """Delay + capacity caps still hold post-arc."""
    s = Scenario("Delay + capacity bounds enforced")
    ctl = WakeupController(
        min_delay_s=60.0, max_delay_s=3600.0, max_pending=2,
    )
    try:
        ctl.schedule(handler_name="h", delay_seconds=0.0)
        s.check("delay < min refused", False)
    except WakeupDelayError:
        s.check("delay < min → WakeupDelayError", True)
    try:
        ctl.schedule(handler_name="h", delay_seconds=9999.0)
        s.check("delay > max refused", False)
    except WakeupDelayError:
        s.check("delay > max → WakeupDelayError", True)
    ctl.schedule(handler_name="h", delay_seconds=100.0)
    ctl.schedule(handler_name="h", delay_seconds=100.0)
    try:
        ctl.schedule(handler_name="h", delay_seconds=100.0)
        s.check("capacity cap refused", False)
    except WakeupCapacityError:
        s.check("capacity cap → WakeupCapacityError", True)
    return s


async def scenario_authority_invariant_grep() -> Scenario:
    """All 4 arc modules import no gate/execution code."""
    s = Scenario("Authority invariant grep")
    forbidden = [
        "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "tool_executor", "candidate_generator",
        "change_engine",
    ]
    modules = [
        "backend/core/ouroboros/governance/schedule_expression.py",
        "backend/core/ouroboros/governance/schedule_job.py",
        "backend/core/ouroboros/governance/schedule_wakeup.py",
        "backend/core/ouroboros/governance/schedule_runner.py",
    ]
    for path in modules:
        src = Path(path).read_text()
        violations = []
        for mod in forbidden:
            if _re.search(
                rf"^\s*(from|import)\s+[^#\n]*{_re.escape(mod)}",
                src, _re.MULTILINE,
            ):
                violations.append(mod)
        s.check(
            f"{Path(path).name}: zero forbidden imports",
            not violations,
        )
    return s


ALL_SCENARIOS = [
    scenario_gap_quote_parses,
    scenario_runner_fires_due_jobs,
    scenario_runner_skips_disabled_and_future,
    scenario_runner_handler_raise_isolation,
    scenario_wakeup_schedule_and_fire,
    scenario_wakeup_cancel_before_fire,
    scenario_wakeup_missing_handler_fail_closed,
    scenario_schedule_repl_round_trip,
    scenario_wakeup_repl_round_trip,
    scenario_authority_model_rejected,
    scenario_bounds_enforced,
    scenario_authority_invariant_grep,
]


async def main() -> int:
    print(f"{C_BOLD}Scheduled Wake-ups — live-fire battle test{C_END}")
    print(f"{C_DIM}Slices 1–5 end-to-end + gap-quote proof{C_END}")
    t0 = time.monotonic()
    results: List[Scenario] = []
    for fn in ALL_SCENARIOS:
        title = fn.__doc__.splitlines()[0] if fn.__doc__ else fn.__name__
        _banner(title)
        try:
            results.append(await fn())
        except Exception as exc:
            sc = Scenario(fn.__name__)
            sc.failed.append(f"raised: {type(exc).__name__}: {exc}")
            _fail(f"raised: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
            results.append(sc)
    elapsed = time.monotonic() - t0
    _banner("SUMMARY")
    total_pass = sum(len(s.passed) for s in results)
    total_fail = sum(len(s.failed) for s in results)
    ok = sum(1 for s in results if s.ok)
    for s in results:
        status = f"{C_PASS}PASS{C_END}" if s.ok else f"{C_FAIL}FAIL{C_END}"
        print(f"  {status} {s.title}  ({len(s.passed)} ✓, {len(s.failed)} ✗)")
    print()
    print(
        f"  {C_BOLD}Total:{C_END} {total_pass} checks passed, "
        f"{total_fail} failed — {ok}/{len(results)} scenarios OK"
    )
    print(f"  {C_DIM}elapsed: {elapsed:.2f}s{C_END}")
    print()
    if total_fail == 0:
        print(
            f"  {C_PASS}{C_BOLD}"
            f"CRON / SCHEDULED WAKE-UP GAP: CLOSED"
            f"{C_END}"
        )
        return 0
    print(
        f"  {C_FAIL}{C_BOLD}{total_fail} check(s) failed{C_END}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
