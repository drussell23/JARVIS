"""Notification adapter for the email triage system.

Thin adapter between triage decisions and the notification bridge.
Handles bounded async delivery for immediate (tier 1-2) and summary
(tier 0 / digest) notifications.

Core invariant: notification delivery failure NEVER changes triage
score, tier, or label outcome.  This module is called AFTER all
scoring/labeling decisions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, List, Optional

from autonomy.email_triage.events import (
    EVENT_NOTIFICATION_DELIVERY_RESULT,
    emit_triage_event,
)
from autonomy.email_triage.schemas import (
    NotificationDeliveryResult,
    TriagedEmail,
)

logger = logging.getLogger("jarvis.email_triage.notifications")

# ---------------------------------------------------------------------------
# Tier-to-urgency mapping
# ---------------------------------------------------------------------------

# Maps triage tier -> NotificationUrgency integer value.
# Only tier 1 (URGENT=4) and tier 2 (HIGH=3) have elevated urgency.
# Everything else (summary tier 0, tier 3, tier 4, unknown) maps to NORMAL (2).
_TIER_URGENCY_MAP = {
    1: 4,  # URGENT
    2: 3,  # HIGH
}

_DEFAULT_URGENCY = 2  # NORMAL


def tier_to_urgency(tier: int) -> int:
    """Map a triage tier to a NotificationUrgency integer.

    Args:
        tier: Triage tier (1-4, or 0 for summary).

    Returns:
        Urgency integer: 4 (URGENT), 3 (HIGH), or 2 (NORMAL fallback).
    """
    return _TIER_URGENCY_MAP.get(tier, _DEFAULT_URGENCY)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _invoke_notifier(
    notifier: Callable[..., Any],
    **kwargs: Any,
) -> bool:
    """Call notifier regardless of whether it is sync or async.

    Returns the boolean result of the notifier call.
    """
    if asyncio.iscoroutinefunction(notifier) or (
        hasattr(notifier, "__call__")
        and asyncio.iscoroutinefunction(notifier.__call__)
    ):
        return await notifier(**kwargs)
    else:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: notifier(**kwargs))


async def _deliver_one(
    triaged: TriagedEmail,
    notifier: Callable[..., Any],
    urgency: int,
) -> NotificationDeliveryResult:
    """Deliver a single notification for a triaged email.

    Constructs title and message from the TriagedEmail fields, calls the
    notifier, and measures latency.  All exceptions are caught and
    converted into a failure result -- delivery failure must never
    propagate.

    Args:
        triaged: The triaged email to notify about.
        notifier: Callable that sends the notification.
        urgency: NotificationUrgency integer value.

    Returns:
        NotificationDeliveryResult with success/failure and latency.
    """
    message_id = triaged.features.message_id
    title = f"Email from {triaged.features.sender}"
    message = (
        f"[{triaged.scoring.tier_label}] {triaged.features.subject}\n"
        f"From: {triaged.features.sender}"
    )

    t0 = time.monotonic()
    try:
        result = await _invoke_notifier(
            notifier,
            message=message,
            urgency=urgency,
            title=title,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        success = bool(result)
        error: Optional[str] = None if success else "notifier returned False"
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        success = False
        error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Notification delivery failed for %s: %s",
            message_id,
            error,
        )

    delivery = NotificationDeliveryResult(
        message_id=message_id,
        channel="bridge",
        success=success,
        latency_ms=latency_ms,
        error=error,
    )

    emit_triage_event(
        EVENT_NOTIFICATION_DELIVERY_RESULT,
        {
            "message_id": delivery.message_id,
            "channel": delivery.channel,
            "success": delivery.success,
            "latency_ms": delivery.latency_ms,
            "error": delivery.error,
        },
    )

    return delivery


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def deliver_immediate(
    emails: List[TriagedEmail],
    notifier: Callable[..., Any],
    timeout_s: float,
) -> List[NotificationDeliveryResult]:
    """Deliver individual notifications for tier 1-2 emails.

    All emails are dispatched in parallel via ``asyncio.gather()`` inside
    an ``asyncio.wait_for()`` timeout envelope.  On timeout, every email
    that did not complete receives a failure result.

    Args:
        emails: List of triaged emails to notify about.
        notifier: Callable (sync or async) that sends notifications.
        timeout_s: Maximum wall-clock seconds for the entire batch.

    Returns:
        One NotificationDeliveryResult per email, in the same order.
    """
    if not emails:
        return []

    tasks = [
        _deliver_one(email, notifier, tier_to_urgency(email.scoring.tier))
        for email in emails
    ]

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        # Entire batch timed out -- return failure for every email.
        results_list: List[NotificationDeliveryResult] = []
        for email in emails:
            failure = NotificationDeliveryResult(
                message_id=email.features.message_id,
                channel="bridge",
                success=False,
                latency_ms=int(timeout_s * 1000),
                error=f"Batch delivery timed out after {timeout_s}s",
            )
            emit_triage_event(
                EVENT_NOTIFICATION_DELIVERY_RESULT,
                {
                    "message_id": failure.message_id,
                    "channel": failure.channel,
                    "success": failure.success,
                    "latency_ms": failure.latency_ms,
                    "error": failure.error,
                },
            )
            results_list.append(failure)
        return results_list

    # Process gather results -- some may be exceptions if individual
    # tasks raised after gather resolved (shouldn't happen since
    # _deliver_one catches all, but be defensive).
    final: List[NotificationDeliveryResult] = []
    for i, result in enumerate(results):
        if isinstance(result, NotificationDeliveryResult):
            final.append(result)
        elif isinstance(result, BaseException):
            email = emails[i]
            error_msg = f"{type(result).__name__}: {result}"
            failure = NotificationDeliveryResult(
                message_id=email.features.message_id,
                channel="bridge",
                success=False,
                latency_ms=0,
                error=error_msg,
            )
            emit_triage_event(
                EVENT_NOTIFICATION_DELIVERY_RESULT,
                {
                    "message_id": failure.message_id,
                    "channel": failure.channel,
                    "success": failure.success,
                    "latency_ms": failure.latency_ms,
                    "error": failure.error,
                },
            )
            final.append(failure)
        else:
            # Unexpected type -- treat as failure.
            email = emails[i]
            failure = NotificationDeliveryResult(
                message_id=email.features.message_id,
                channel="bridge",
                success=False,
                latency_ms=0,
                error=f"Unexpected result type: {type(result).__name__}",
            )
            emit_triage_event(
                EVENT_NOTIFICATION_DELIVERY_RESULT,
                {
                    "message_id": failure.message_id,
                    "channel": failure.channel,
                    "success": failure.success,
                    "latency_ms": failure.latency_ms,
                    "error": failure.error,
                },
            )
            final.append(failure)

    return final


async def deliver_summary(
    emails: List[TriagedEmail],
    notifier: Callable[..., Any],
    timeout_s: float,
) -> NotificationDeliveryResult:
    """Deliver a single summary/digest notification.

    An empty buffer is an immediate success (no notification needed).

    Args:
        emails: List of triaged emails to summarize.
        notifier: Callable (sync or async) that sends the notification.
        timeout_s: Maximum wall-clock seconds for delivery.

    Returns:
        A single NotificationDeliveryResult.
    """
    if not emails:
        return NotificationDeliveryResult(
            message_id="summary_empty",
            channel="summary",
            success=True,
            latency_ms=0,
        )

    count = len(emails)
    title = "Email Summary"
    lines = [f"{count} new email{'s' if count != 1 else ''} triaged:"]
    for email in emails:
        lines.append(
            f"  - [{email.scoring.tier_label}] {email.features.subject} "
            f"(from {email.features.sender})"
        )
    message = "\n".join(lines)

    urgency = _DEFAULT_URGENCY  # NORMAL for summaries

    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(
            _invoke_notifier(
                notifier,
                message=message,
                urgency=urgency,
                title=title,
            ),
            timeout=timeout_s,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        success = bool(result)
        error: Optional[str] = None if success else "notifier returned False"
    except asyncio.TimeoutError:
        latency_ms = int((time.monotonic() - t0) * 1000)
        success = False
        error = f"Summary delivery timed out after {timeout_s}s"
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        success = False
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("Summary delivery failed: %s", error)

    # Use the first email's message_id as the summary identifier,
    # or a generated one.
    summary_id = f"summary_{emails[0].features.message_id}"

    delivery = NotificationDeliveryResult(
        message_id=summary_id,
        channel="summary",
        success=success,
        latency_ms=latency_ms,
        error=error,
    )

    emit_triage_event(
        EVENT_NOTIFICATION_DELIVERY_RESULT,
        {
            "message_id": delivery.message_id,
            "channel": delivery.channel,
            "success": delivery.success,
            "latency_ms": delivery.latency_ms,
            "error": delivery.error,
        },
    )

    return delivery
