"""Antivenom observer -- runs the SemanticGuardian self-immunization audit.

``antivenom_self_immunization.audit_self_immunization`` mutates the canonical
adversarial corpus (whitespace drift, comment insertion, rename, string
split, paren wrap) and checks that SemanticGuardian still catches every
variant. It was complete, tested -- and had no production caller: turning its
flag on did nothing (found 2026-10-08). This is that caller.

* Runs once at boot and then on an interval (``JARVIS_ANTIVENOM_INTERVAL_S``):
  the probe is deterministic, so a new result can only come from a change to
  the guardian or the corpus -- frequent re-runs would re-measure the same.
* Off the event loop: the audit is synchronous CPU work (25 entries x 5
  mutations through the guardian).
* The audit already persists its report (flock'd JSONL, sub-flag) and
  publishes its SSE event. The observer adds the one thing missing: the
  verdict reaches the capability-health ledger, so a guardian that MISSES a
  mutated pattern raises the same HEALTH alarm (and the same "recovered"
  line) every other failing capability does -- not a row nobody reads.

Gated by the audit's own master flag (``JARVIS_ANTIVENOM_IMMUNIZATION_ENABLED``).
NEVER raises out of its loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger("Ouroboros.AntivenomObserver")

_ENV_INTERVAL = "JARVIS_ANTIVENOM_INTERVAL_S"
#: The capability name the health ledger tracks the guardian's immunity under.
CAPABILITY = "antivenom_immunization"


def interval_s() -> float:
    try:
        return max(60.0, float(os.environ.get(_ENV_INTERVAL, "") or 6 * 3600))
    except ValueError:
        return 6 * 3600.0


class AntivenomObserver:
    """Boot + interval runner for the self-immunization audit."""

    def __init__(self, *, audit: Optional[Callable[[], Any]] = None, ledger: Any = None,
                 interval: Optional[Callable[[], float]] = None) -> None:
        self._audit = audit
        self._ledger = ledger
        self._interval = interval or interval_s
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.last_report: Any = None

    def _resolve(self):
        audit = self._audit
        if audit is None:
            from backend.core.ouroboros.governance.antivenom_self_immunization import (  # noqa: PLC0415
                audit_self_immunization,
            )
            audit = audit_self_immunization
        ledger = self._ledger
        if ledger is None:
            from backend.core.ouroboros.governance.reachability_ledger import default_ledger  # noqa: PLC0415
            ledger = default_ledger()
        return audit, ledger

    async def run_once(self) -> Any:
        """One audit, its verdict settled into the health ledger. NEVER raises."""
        try:
            audit, ledger = self._resolve()
            report = await asyncio.to_thread(audit)
            self.last_report = report
            finding = str(getattr(getattr(report, "finding", ""), "value", getattr(report, "finding", "")))
            if finding == "disabled":
                return report
            gaps = int(getattr(report, "gap_count", 0) or 0)
            await ledger.settle(CAPABILITY, ok=gaps == 0, detail=str(getattr(report, "diagnostic", ""))[:300])
            log = logger.warning if gaps else logger.info
            log("[AntivenomObserver] guardian immunity: %s (%s)", finding, getattr(report, "diagnostic", ""))
            from backend.core.ouroboros.governance.proof_of_work import emit_evidence
            emit_evidence("antivenom", finding, detail=str(getattr(report, "diagnostic", ""))[:200])
            return report
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- an observer never takes the organism down
            logger.warning("[AntivenomObserver] audit failed", exc_info=True)
            return None

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval())
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.get_running_loop().create_task(self._run(), name="antivenom-observer")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


__all__ = ["AntivenomObserver", "CAPABILITY", "interval_s"]
