"""Single REM epoch: one exploration cycle driven by the REM Sleep daemon.

A RemEpoch is a bounded unit of work: it receives a CancellationToken from
the daemon, explores the codebase via ExplorationSubagents, ranks findings,
converts them to IntentEnvelopes, and returns an EpochResult.

The daemon creates a fresh RemEpoch for every idle window.  Epochs do not
share state — all intermediate data lives inside the epoch instance.

Usage::

    epoch = RemEpoch(
        epoch_id=1,
        oracle=oracle,
        fleet=fleet,
        spinal_cord=spinal_cord,
        intake_router=intake_router,
        proactive_drive=proactive_drive,
        doubleword=doubleword,
        config=config,
        token=token,
    )
    result = await epoch.run()
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from backend.core.ouroboros.cancellation_token import CancellationToken
from backend.core.ouroboros.finding_ranker import RankedFinding

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EpochResult
# ---------------------------------------------------------------------------


@dataclass
class EpochResult:
    """Outcome of a single REM epoch.

    Attributes
    ----------
    epoch_id:
        Monotonically increasing identifier for the epoch.
    findings:
        Ranked list of findings discovered during the epoch.
    envelopes_submitted:
        Number of IntentEnvelopes successfully submitted to the intake router.
    duration_s:
        Wall-clock seconds the epoch ran for.
    cancelled:
        True if the epoch was cancelled before it could complete normally.
    error:
        Exception message if the epoch failed unexpectedly, else None.
    """

    epoch_id: int
    findings: List[RankedFinding] = field(default_factory=list)
    envelopes_submitted: int = 0
    duration_s: float = 0.0
    cancelled: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# RemEpoch
# ---------------------------------------------------------------------------


class RemEpoch:
    """Single exploration epoch for the Ouroboros REM Sleep daemon.

    Parameters
    ----------
    epoch_id:
        Unique monotonic identifier assigned by RemSleepDaemon.
    oracle:
        TheOracle instance for codebase graph queries.
    fleet:
        Agent fleet providing ExplorationSubagents.
    spinal_cord:
        SpinalCord for streaming findings upward.
    intake_router:
        Governance intake router for submitting IntentEnvelopes.
    proactive_drive:
        ProactiveDrive for observability / future eligibility signalling.
    doubleword:
        Optional Doubleword provider for deep analysis.
    config:
        DaemonConfig controlling epoch timeouts and limits.
    token:
        CancellationToken for cooperative cancellation by the daemon.
    """

    def __init__(
        self,
        epoch_id: int,
        oracle: Any,
        fleet: Any,
        spinal_cord: Any,
        intake_router: Any,
        proactive_drive: Any,
        doubleword: Any,
        config: Any,
        token: CancellationToken,
    ) -> None:
        self._epoch_id = epoch_id
        self._oracle = oracle
        self._fleet = fleet
        self._spinal_cord = spinal_cord
        self._intake_router = intake_router
        self._proactive_drive = proactive_drive
        self._doubleword = doubleword
        self._config = config
        self._token = token

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> EpochResult:
        """Execute the epoch and return an EpochResult.

        The epoch honours ``token.is_cancelled`` between work units and returns
        a result with ``cancelled=True`` if interrupted.
        """
        start = time.monotonic()
        findings: List[RankedFinding] = []
        envelopes_submitted = 0

        try:
            result = await asyncio.wait_for(
                self._execute(findings),
                timeout=self._config.rem_epoch_timeout_s,
            )
            envelopes_submitted = result
        except asyncio.TimeoutError:
            logger.warning(
                "RemEpoch %d timed out after %.1fs",
                self._epoch_id,
                self._config.rem_epoch_timeout_s,
            )
        except asyncio.CancelledError:
            logger.info("RemEpoch %d cancelled", self._epoch_id)
            return EpochResult(
                epoch_id=self._epoch_id,
                findings=findings,
                envelopes_submitted=envelopes_submitted,
                duration_s=time.monotonic() - start,
                cancelled=True,
            )
        except Exception as exc:
            logger.exception("RemEpoch %d failed: %s", self._epoch_id, exc)
            return EpochResult(
                epoch_id=self._epoch_id,
                findings=findings,
                envelopes_submitted=envelopes_submitted,
                duration_s=time.monotonic() - start,
                error=str(exc),
            )

        cancelled = self._token.is_cancelled
        return EpochResult(
            epoch_id=self._epoch_id,
            findings=findings,
            envelopes_submitted=envelopes_submitted,
            duration_s=time.monotonic() - start,
            cancelled=cancelled,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _execute(self, findings: List[RankedFinding]) -> int:
        """Inner execution body.  Returns the number of envelopes submitted."""
        if self._token.is_cancelled:
            return 0

        # Delegate to fleet for exploration (fleet is injected — no hardcoding)
        try:
            raw = await self._fleet.explore(
                epoch_id=self._epoch_id,
                token=self._token,
                max_findings=self._config.rem_max_findings_per_epoch,
            )
            findings.extend(raw)
        except Exception as exc:
            logger.warning("RemEpoch %d fleet explore failed: %s", self._epoch_id, exc)

        if self._token.is_cancelled or not findings:
            return 0

        # Stream findings up via spinal cord
        try:
            await self._spinal_cord.stream_up(
                "rem_findings",
                {
                    "epoch_id": self._epoch_id,
                    "count": len(findings),
                    "top_score": findings[0].score if findings else 0.0,
                },
            )
        except Exception as exc:
            logger.warning(
                "RemEpoch %d spinal stream_up failed: %s", self._epoch_id, exc
            )

        if self._token.is_cancelled:
            return 0

        # Submit envelopes to intake router
        envelopes_submitted = 0
        try:
            from backend.core.ouroboros.exploration_envelope_factory import (
                findings_to_envelopes,
            )

            envelopes = findings_to_envelopes(findings, epoch_id=self._epoch_id)
            for env in envelopes:
                if self._token.is_cancelled:
                    break
                try:
                    await self._intake_router.submit(env)
                    envelopes_submitted += 1
                except Exception as exc:
                    logger.warning(
                        "RemEpoch %d envelope submit failed: %s", self._epoch_id, exc
                    )
        except Exception as exc:
            logger.warning(
                "RemEpoch %d envelope factory failed: %s", self._epoch_id, exc
            )

        return envelopes_submitted
