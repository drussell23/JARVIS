"""Wave 2 (4) Curiosity engine primitive + per-op budget tracker.

Slice 1 (2026-04-25) — primitive + ContextVar + JSONL ledger + 4 env knobs.
Slice 2 (2026-04-25) — Rule 14 widening + GENERATE phase budget binding.
Slice 3 (2026-04-25) — SSE bridge + IDE GET routes.
Slice 4 (2026-04-25) — GRADUATION: ``JARVIS_CURIOSITY_ENABLED`` default
flipped ``false → true``. SSE sub-flag stays default ``false`` (operator
opt-in). Per-session caps unchanged: 3 questions / $0.05 each / posture
must be EXPLORE or CONSOLIDATE.

Authority posture (preserved across all 4 slices):

* §1 additive only — ``ask_human`` was already authority-free; this
  primitive only tracks budget for *when* it can fire.
* §5 Tier −1 — question-text sanitization at the policy gate (Slice 2).
* §6 Iron Gate unchanged.
* §7 Approval surface untouched.
* §8 Observability — JSONL ledger + SSE bridge + IDE GET routes.

Hot-revert (graduation): set ``JARVIS_CURIOSITY_ENABLED=false`` — that
single env knob force-disables every sub-flag and restores byte-for-byte
pre-W2(4) behavior (Rule 14 falls through to the legacy
``tool.denied.ask_human_low_risk`` reject at SAFE_AUTO). Pinned by
``tests/governance/test_w2_4_graduation_pins_slice4.py`` on every commit.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger("Ouroboros.CuriosityEngine")


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    """Standard JARVIS env-bool parse — true/1/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def curiosity_enabled() -> bool:
    """Master flag — `JARVIS_CURIOSITY_ENABLED`.

    **Default**: ``true`` (graduated 2026-04-25 via Slice 4 after Slices
    1-3 shipped the primitive + Rule 14 widening + SSE/IDE surfaces with
    158/158 combined regression green and the structural live-fire smoke
    pinning the GENERATE → ContextVar → Rule 14 → ledger → SSE chain).

    Master-off is THE single hot-revert env knob — when ``false``, every
    sub-flag below force-disables (composition pattern, same as W3(7)
    cancel master-off). Setting ``JARVIS_CURIOSITY_ENABLED=false``
    restores byte-for-byte pre-W2(4) behavior.
    """
    return _env_bool("JARVIS_CURIOSITY_ENABLED", True)


def questions_per_session() -> int:
    """`JARVIS_CURIOSITY_QUESTIONS_PER_SESSION` — default ``3``.

    Hard ceiling on the number of curiosity questions the model can ask
    in a single session. Each ``try_charge`` Allowed result decrements the
    remaining quota; once it hits 0, every subsequent ``try_charge``
    returns Denied(questions_exhausted). Master-off → effective cap is 0.
    """
    if not curiosity_enabled():
        return 0
    raw = os.environ.get("JARVIS_CURIOSITY_QUESTIONS_PER_SESSION", "3")
    try:
        n = int(raw)
        return max(0, n)
    except (TypeError, ValueError):
        return 3


def cost_cap_usd() -> float:
    """`JARVIS_CURIOSITY_COST_CAP_USD` — default ``0.05`` per question.

    Per-question soft cap on the estimated LLM cost of generating the
    question text. Each ``try_charge`` rejects if ``est_cost_usd`` exceeds
    this cap. Operator-binding default 0.05. Master-off → cap is 0.0
    (rejects everything).
    """
    if not curiosity_enabled():
        return 0.0
    raw = os.environ.get("JARVIS_CURIOSITY_COST_CAP_USD", "0.05")
    try:
        v = float(raw)
        return max(0.0, v)
    except (TypeError, ValueError):
        return 0.05


# Posture allowlist v1 per operator binding 2026-04-25 — EXPLORE +
# CONSOLIDATE only. HARDEN excluded by design (focus stabilization, not
# new questions). MAINTAIN excluded for Slice 1 — operator can revisit.
_DEFAULT_POSTURE_ALLOWLIST = "EXPLORE,CONSOLIDATE"


def posture_allowlist() -> frozenset:
    """`JARVIS_CURIOSITY_POSTURE_ALLOWLIST` — default ``"EXPLORE,CONSOLIDATE"``.

    Comma-separated list of posture values that allow curiosity to fire.
    Master-off → empty set. Whitespace + case tolerated.
    """
    if not curiosity_enabled():
        return frozenset()
    raw = os.environ.get(
        "JARVIS_CURIOSITY_POSTURE_ALLOWLIST", _DEFAULT_POSTURE_ALLOWLIST,
    )
    return frozenset(
        part.strip().upper()
        for part in raw.split(",")
        if part.strip()
    )


def sse_enabled() -> bool:
    """`JARVIS_CURIOSITY_SSE_ENABLED` — default ``false`` (operator opt-in).

    Slice 3 sub-flag. Mirrors W3(7) ``cancel_token.sse_enabled()``: even
    when curiosity master is on + persistence on, the SSE publish stays
    off until the operator explicitly opts in. Composition: master-off
    forces SSE-off (no event ever leaks past the gate). The IDE stream's
    own master flag (`JARVIS_IDE_STREAM_ENABLED`) is consulted at publish
    time too — both gates must be on for an event to land on the broker.
    """
    if not curiosity_enabled():
        return False
    return _env_bool("JARVIS_CURIOSITY_SSE_ENABLED", False)


def ledger_persist_enabled() -> bool:
    """`JARVIS_CURIOSITY_LEDGER_PERSIST_ENABLED` — default ``true``
    when master is on. Master-off → always False."""
    if not curiosity_enabled():
        return False
    return _env_bool("JARVIS_CURIOSITY_LEDGER_PERSIST_ENABLED", True)


# ---------------------------------------------------------------------------
# Decision result types
# ---------------------------------------------------------------------------


class DenyReason(str, Enum):
    """Stable, grep-friendly deny-reason vocabulary for telemetry."""

    MASTER_OFF = "master_off"
    POSTURE_DISALLOWED = "posture_disallowed"
    QUESTIONS_EXHAUSTED = "questions_exhausted"
    COST_EXCEEDED = "cost_exceeded"
    INVALID_QUESTION = "invalid_question"  # empty / non-string text


@dataclass(frozen=True)
class ChargeResult:
    """Immutable result of :meth:`CuriosityBudget.try_charge`.

    ``allowed=True`` → ``question_id`` is populated with the assigned UUID.
    ``allowed=False`` → ``deny_reason`` carries the structured reason; the
    caller should NOT proceed with the question.
    """

    allowed: bool
    question_id: Optional[str] = None
    deny_reason: Optional[DenyReason] = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Ledger record (schema curiosity.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CuriosityRecord:
    """Immutable record of one curiosity decision (schema ``curiosity.1``).

    Frozen because once persisted it's the system of record for the
    operator-facing audit. Mutating it would break the deterministic
    ledger contract.
    """

    schema_version: str
    question_id: str
    op_id: str
    posture_at_charge: str
    question_text: str
    est_cost_usd: float
    issued_at_monotonic: float
    issued_at_iso: str
    result: str  # "allowed" | "denied:<reason>"

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":")) + "\n"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_question_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# CuriosityBudget — per-op tracker
# ---------------------------------------------------------------------------


@dataclass
class CuriosityBudget:
    """Per-op budget tracker.

    Construct one per op (typically inside the GENERATE runner before the
    Venom tool loop starts). Slice 2 will look this up via the
    :data:`current_curiosity_budget_var` ContextVar from
    ``tool_executor.py`` Rule 14.

    Lifecycle:
      1. ``CuriosityBudget(op_id, posture)`` — captures posture_at_arm.
      2. ``try_charge(question_text, est_cost_usd)`` — call once per
         model-issued question. Returns Allowed (and increments the
         counter) or Denied(reason).
      3. ``snapshot()`` — read-only view (questions_used, cost_burn,
         remaining quota) for postmortem + summary.json.

    Thread-safety: Slice 1 is single-threaded inside one op (Venom tool
    loop is async-serial per op). If a future slice needs concurrent
    access, add a lock. Don't add prematurely.

    Master-off contract: when ``curiosity_enabled()`` is False, every
    ``try_charge`` returns ``Denied(MASTER_OFF)`` without touching the
    counter. The counter never increments past 0 in that mode.
    """

    op_id: str
    posture_at_arm: str
    session_dir: Optional[Path] = None
    # Internal mutable state (not constructor args)
    _questions_used: int = field(default=0, init=False)
    _cost_burn_usd: float = field(default=0.0, init=False)

    @property
    def questions_used(self) -> int:
        return self._questions_used

    @property
    def cost_burn_usd(self) -> float:
        return self._cost_burn_usd

    @property
    def questions_remaining(self) -> int:
        """Quota left for this session. Returns 0 when master-off or used up."""
        cap = questions_per_session()
        return max(0, cap - self._questions_used)

    def try_charge(
        self,
        question_text: str,
        est_cost_usd: float,
    ) -> ChargeResult:
        """Atomic decision + commit.

        Composition order (first deny wins):
          1. master flag — gates everything
          2. invalid question (empty / non-string)
          3. posture allowlist
          4. questions-per-session quota
          5. per-question cost cap

        On Allowed: increments counter, accumulates cost, persists ledger
        record (if persist sub-flag on + session_dir set), returns the
        ``question_id``. On Denied: persists a denied record (so operators
        can see WHY curiosity didn't fire), returns the reason.
        """
        # 1. Master flag
        if not curiosity_enabled():
            return self._record_decision(
                question_text=question_text,
                est_cost_usd=est_cost_usd,
                allowed=False,
                deny_reason=DenyReason.MASTER_OFF,
            )
        # 2. Invalid question
        if not isinstance(question_text, str) or not question_text.strip():
            return self._record_decision(
                question_text=str(question_text)[:200],
                est_cost_usd=est_cost_usd,
                allowed=False,
                deny_reason=DenyReason.INVALID_QUESTION,
                detail="empty or non-string question_text",
            )
        # 3. Posture allowlist
        allowlist = posture_allowlist()
        normalized_posture = (self.posture_at_arm or "").strip().upper()
        if normalized_posture not in allowlist:
            return self._record_decision(
                question_text=question_text,
                est_cost_usd=est_cost_usd,
                allowed=False,
                deny_reason=DenyReason.POSTURE_DISALLOWED,
                detail=(
                    f"posture={normalized_posture!r} not in "
                    f"allowlist={sorted(allowlist)}"
                ),
            )
        # 4. Questions quota
        cap = questions_per_session()
        if self._questions_used >= cap:
            return self._record_decision(
                question_text=question_text,
                est_cost_usd=est_cost_usd,
                allowed=False,
                deny_reason=DenyReason.QUESTIONS_EXHAUSTED,
                detail=f"used={self._questions_used}/cap={cap}",
            )
        # 5. Per-question cost cap
        per_q_cap = cost_cap_usd()
        if est_cost_usd > per_q_cap:
            return self._record_decision(
                question_text=question_text,
                est_cost_usd=est_cost_usd,
                allowed=False,
                deny_reason=DenyReason.COST_EXCEEDED,
                detail=f"est_cost=${est_cost_usd:.4f} > cap=${per_q_cap:.4f}",
            )
        # All gates passed — commit
        self._questions_used += 1
        self._cost_burn_usd += float(est_cost_usd)
        return self._record_decision(
            question_text=question_text,
            est_cost_usd=est_cost_usd,
            allowed=True,
        )

    def _record_decision(
        self,
        *,
        question_text: str,
        est_cost_usd: float,
        allowed: bool,
        deny_reason: Optional[DenyReason] = None,
        detail: str = "",
    ) -> ChargeResult:
        """Build the ChargeResult, log, and persist."""
        question_id = _new_question_id()
        record = CuriosityRecord(
            schema_version="curiosity.1",
            question_id=question_id,
            op_id=self.op_id,
            posture_at_charge=(self.posture_at_arm or "").strip().upper(),
            question_text=question_text,
            est_cost_usd=float(est_cost_usd),
            issued_at_monotonic=time.monotonic(),
            issued_at_iso=_now_iso(),
            result=("allowed" if allowed else f"denied:{deny_reason.value}"
                    if deny_reason else "denied:unknown"),
        )
        # Single-line INFO log line (operator-facing audit).
        if allowed:
            logger.info(
                "[Curiosity] op=%s ALLOWED question_id=%s "
                "posture=%s est_cost=$%.4f used=%d/%d",
                self.op_id[:16], question_id,
                record.posture_at_charge, record.est_cost_usd,
                self._questions_used, questions_per_session(),
            )
        else:
            logger.info(
                "[Curiosity] op=%s DENIED reason=%s detail=%r question_text=%r",
                self.op_id[:16],
                deny_reason.value if deny_reason else "unknown",
                detail,
                question_text[:80],
            )
        # Persist to ledger (best-effort).
        if ledger_persist_enabled() and self.session_dir is not None:
            self._persist(record)
        # Slice 3 — bridge to SSE (best-effort, gated by master + sub-flag
        # + IDE stream master). Both allowed and denied records are
        # surfaced so operators can see the full curiosity audit trail
        # in the live stream — same shape as cancel_origin_emitted.
        bridge_curiosity_to_sse(record)
        return ChargeResult(
            allowed=allowed,
            question_id=question_id if allowed else None,
            deny_reason=deny_reason,
            detail=detail,
        )

    def _persist(self, record: CuriosityRecord) -> None:
        """Append the record to ``curiosity_ledger.jsonl``. Best-effort."""
        try:
            artifact = self.session_dir / "curiosity_ledger.jsonl"  # type: ignore[union-attr]
            artifact.parent.mkdir(parents=True, exist_ok=True)
            with artifact.open("a", encoding="utf-8") as f:
                f.write(record.to_jsonl())
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            logger.warning(
                "[Curiosity] persist failed op=%s question_id=%s err=%s",
                self.op_id[:16], record.question_id,
                f"{type(exc).__name__}: {exc}",
            )

    def snapshot(self) -> dict:
        """Read-only view for postmortem + summary.json composition."""
        return {
            "op_id": self.op_id,
            "posture_at_arm": (self.posture_at_arm or "").strip().upper(),
            "questions_used": self._questions_used,
            "questions_remaining": self.questions_remaining,
            "cost_burn_usd": round(self._cost_burn_usd, 4),
        }


# ---------------------------------------------------------------------------
# ContextVar — Slice 2 transport
# ---------------------------------------------------------------------------


curiosity_budget_var: contextvars.ContextVar[Optional[CuriosityBudget]] = (
    contextvars.ContextVar("ouroboros.curiosity_budget", default=None)
)


def current_curiosity_budget() -> Optional[CuriosityBudget]:
    """Read the ambient :class:`CuriosityBudget` for this asyncio task chain.

    Returns ``None`` when no budget has been bound (default — Slice 1
    callers, unit tests, pre-W2(4) call paths). Slice 2's tool_executor
    Rule 14 reads this to decide whether to allow ``ask_human`` at
    SAFE_AUTO risk tier.
    """
    return curiosity_budget_var.get()


# ---------------------------------------------------------------------------
# Slice 3 — SSE bridge
# ---------------------------------------------------------------------------


def bridge_curiosity_to_sse(record: "CuriosityRecord") -> None:
    """Publish a ``curiosity_question_emitted`` SSE event for ``record``.

    Slice 3 (W2(4)). Best-effort, never raises. Gated by:

    1. :func:`sse_enabled` — curiosity master + curiosity SSE sub-flag.
    2. :func:`ide_observability_stream.stream_enabled` — IDE stream master.

    Both must be on for the event to land on the broker. The payload is
    a summary form (question_id, op_id, posture, result, question text
    truncated to 80 chars). Full record stays in
    ``curiosity_ledger.jsonl`` and at the
    ``/observability/curiosity/<question_id>`` GET endpoint.

    Mirrors ``cancel_token.bridge_cancel_origin_to_sse`` byte-for-byte
    in error handling: never raises, swallows ImportError /
    AttributeError / broker-publish exceptions silently.
    """
    if not sse_enabled():
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_CURIOSITY_QUESTION_EMITTED as _EV_TYPE,
            get_default_broker as _get_default_broker,
            stream_enabled as _stream_enabled,
        )
        if not _stream_enabled():
            return
        _broker = _get_default_broker()
        if _broker is None:
            return
        _broker.publish(
            event_type=_EV_TYPE,
            op_id=record.op_id,
            payload={
                "question_id": record.question_id,
                "posture": record.posture_at_charge,
                "result": record.result,
                # Truncate model-supplied text — full text in ledger.
                "question_text": record.question_text[:80],
            },
        )
    except Exception:  # noqa: BLE001 — SSE publish is best-effort
        pass


__all__ = [
    "CuriosityBudget",
    "CuriosityRecord",
    "ChargeResult",
    "DenyReason",
    "curiosity_budget_var",
    "current_curiosity_budget",
    "curiosity_enabled",
    "questions_per_session",
    "cost_cap_usd",
    "posture_allowlist",
    "ledger_persist_enabled",
    "sse_enabled",
    "bridge_curiosity_to_sse",
]
