"""CLASSIFY-phase clarification — one operator question at ROUTE boundary.

The ``ask_human`` tool inside Venom is gated to NOTIFY_APPLY+ risk tiers
(post-generation). But clarification matters most EARLIER — when the
intake description itself is ambiguous. Without this layer, the model
silently picks a meaning and O+V runs the whole pipeline on a guess.

This module adds **one** clarifying question at the CLASSIFY→ROUTE
boundary, gated by a narrow ambiguity heuristic and bounded by a
timeout + per-session cap.

Manifesto alignment:
  * §1 Boundary Principle — the operator's answer enriches
    ``ctx.description`` / ``ctx.evidence`` only. It NEVER overrides
    risk classification, routing law, SemanticGuardian findings, or
    any deterministic engine input.
  * §3 structured concurrency — ``asyncio.wait_for`` with a bounded
    timeout; cancellation propagates cleanly.
  * §5 untrusted input — answer is treated as Tier -1: sanitized via
    ``secure_logging.sanitize_for_log`` + capped length + secret-shape
    redaction. Same discipline as ConversationBridge.
  * §8 observability — single ``[ClassifyClarify]`` INFO line per
    firing with ``why_triggered=<reason_code>`` so operators can tune
    false positives without guessing.

Default **OFF** (``JARVIS_CLASSIFY_CLARIFY_ENABLED=false``) — narrow
trigger set means the feature needs operator opt-in before it can
interrupt a run.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ClassifyClarify")

_ENV_ENABLED = "JARVIS_CLASSIFY_CLARIFY_ENABLED"
_ENV_TIMEOUT_S = "JARVIS_CLASSIFY_CLARIFY_TIMEOUT_S"
_ENV_MAX_PER_SESSION = "JARVIS_CLASSIFY_CLARIFY_MAX_PER_SESSION"
_ENV_MIN_DESC_CHARS = "JARVIS_CLASSIFY_CLARIFY_MIN_DESC_CHARS"
_ENV_ANSWER_MAX_CHARS = "JARVIS_CLASSIFY_CLARIFY_ANSWER_MAX_CHARS"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Sentinel markers that suggest the intake description is a generic
# placeholder rather than an actionable goal.
_GENERIC_MARKERS = frozenset({
    "various", "misc", "tbd", "n/a", "todo", "(placeholder)", "placeholder",
})


def clarify_enabled() -> bool:
    """Master switch. Default OFF — opt-in, not opt-out."""
    return os.environ.get(_ENV_ENABLED, "0").strip().lower() in _TRUTHY


def clarify_timeout_s() -> float:
    """Operator question timeout. Default 30s — tuned for real humans,
    not impatient scripts."""
    try:
        return max(5.0, min(300.0, float(
            os.environ.get(_ENV_TIMEOUT_S, "30"),
        )))
    except (TypeError, ValueError):
        return 30.0


def max_per_session() -> int:
    """Upper bound on clarifications per harness session. Prevents
    question fatigue even with a noisy trigger."""
    try:
        return max(0, min(20, int(
            os.environ.get(_ENV_MAX_PER_SESSION, "3"),
        )))
    except (TypeError, ValueError):
        return 3


def min_desc_chars() -> int:
    """Descriptions shorter than this are candidates for clarification."""
    try:
        return max(10, int(
            os.environ.get(_ENV_MIN_DESC_CHARS, "40"),
        ))
    except (TypeError, ValueError):
        return 40


def answer_max_chars() -> int:
    """Hard cap on the length of the clarification we accept into ctx.
    Even when sanitized, untrusted text shouldn't be unbounded."""
    try:
        return max(64, min(4096, int(
            os.environ.get(_ENV_ANSWER_MAX_CHARS, "512"),
        )))
    except (TypeError, ValueError):
        return 512


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClarifyRequest:
    """The single question we'd ask the operator for this op."""

    op_id: str
    question: str                   # ≤ 256 chars, phrased concisely
    why_triggered: str               # reason_code (tunable via telemetry)


@dataclass(frozen=True)
class ClarifyResponse:
    """Result of an ask — either a sanitized answer, a timeout, or an
    explicit skip. Treated as Tier-1 untrusted text downstream.

    outcome ∈ {"answered", "timeout", "declined", "skipped_disabled",
               "skipped_cap", "skipped_no_channel"}
    """

    outcome: str
    answer_raw: str = ""             # exact bytes the operator typed
    answer_sanitized: str = ""       # safe for inclusion in prompts/ledger
    duration_ms: int = 0
    why_triggered: str = ""
    question: str = ""


# ---------------------------------------------------------------------------
# Sanitizer — Tier -1 treatment of operator-typed text
# ---------------------------------------------------------------------------


_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"gho_[A-Za-z0-9]{30,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----"),
)


def _redact_secrets(text: str) -> str:
    """Mask credential-shaped tokens in the operator's answer before
    it lands in prompt or ledger. Paranoid by design — FPs are cheap,
    a leaked key is disastrous.
    """
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED_SECRET]", text)
    return text


def sanitize_answer(raw: str, *, max_chars: Optional[int] = None) -> str:
    """Full Tier-1 sanitizer chain for the clarification answer.

    Order:
      1. strip / normalize whitespace
      2. drop control chars (keep \\n, \\t for readability)
      3. secret-shape redaction
      4. length cap with ellipsis
      5. delegate to the shared ``secure_logging.sanitize_for_log`` when
         importable (adds logfile-safe escaping)

    Never raises: returns the empty string on any error.
    """
    if not raw:
        return ""
    try:
        if max_chars is None:
            max_chars = answer_max_chars()
        # Normalise whitespace but keep newlines + tabs.
        cleaned = "".join(
            c for c in raw
            if c in ("\n", "\t") or (ord(c) >= 32 and ord(c) < 127)
            or ord(c) >= 160  # unicode pass-through above C1
        ).strip()
        cleaned = _redact_secrets(cleaned)
        if len(cleaned) > max_chars:
            cleaned = cleaned[: max_chars - 1].rstrip() + "…"
        try:
            from backend.core.secure_logging import sanitize_for_log
            cleaned = sanitize_for_log(cleaned)
        except Exception:  # noqa: BLE001
            pass
        return cleaned
    except Exception:  # noqa: BLE001
        logger.debug("[ClassifyClarify] sanitize failed", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Ambiguity heuristic — narrow by design
# ---------------------------------------------------------------------------


def _has_generic_target_files(target_files: Tuple[str, ...]) -> bool:
    """True when target_files is empty or contains only sentinel markers."""
    if not target_files:
        return True
    for f in target_files:
        norm = (f or "").strip().lower()
        if norm and norm not in _GENERIC_MARKERS:
            return False
    return True


def _keyword_coverage(description: str, goal_keywords: Tuple[str, ...]) -> int:
    """Count of goal keywords that appear (as whole words) in description.

    Used as a soft signal: a description that matches no active goal is
    more likely to be a garbled / placeholder intake.
    """
    if not description or not goal_keywords:
        return 0
    text = description.lower()
    hits = 0
    for kw in goal_keywords:
        kw_norm = kw.strip().lower()
        if not kw_norm or len(kw_norm) < 3:
            continue
        # Whole-word match to avoid false positives on substrings.
        if re.search(rf"\b{re.escape(kw_norm)}\b", text):
            hits += 1
    return hits


def should_ask(
    *,
    description: str,
    target_files: Tuple[str, ...],
    goal_keywords: Tuple[str, ...] = (),
) -> Optional[str]:
    """Return a reason_code describing *why* the signal is ambiguous,
    or ``None`` when the intake looks actionable.

    Narrow by design: a trigger must match AT LEAST ONE of:

      * ``short_description_no_target_files`` — desc < min_desc_chars
        and target_files is empty/generic
      * ``generic_target_file_list`` — target_files contains only the
        ``_GENERIC_MARKERS`` sentinel set
      * ``no_goal_keyword_match`` — desc is short-ish AND no goal
        keyword appears

    False positives are explicit in the reason_code so operators can
    tune which triggers fire via env knobs without guessing.
    """
    desc = (description or "").strip()
    min_chars = min_desc_chars()

    # Generic target-file sentinel fires on explicit placeholders even
    # with a long description (the model still has to guess targets).
    if target_files and all(
        (f or "").strip().lower() in _GENERIC_MARKERS for f in target_files
    ):
        return "generic_target_file_list"

    if len(desc) < min_chars and not target_files:
        return "short_description_no_target_files"

    if (
        len(desc) < (min_chars * 2)
        and goal_keywords
        and _keyword_coverage(desc, goal_keywords) == 0
    ):
        return "no_goal_keyword_match"

    return None


# ---------------------------------------------------------------------------
# Per-session counter — prevents question fatigue
# ---------------------------------------------------------------------------


_SESSION_COUNT: int = 0


def reset_session_count() -> None:
    """Harness calls this at session boot + on session end for clean
    test isolation."""
    global _SESSION_COUNT
    _SESSION_COUNT = 0


def _session_count() -> int:
    return _SESSION_COUNT


def _increment_session_count() -> None:
    global _SESSION_COUNT
    _SESSION_COUNT += 1


# ---------------------------------------------------------------------------
# Channel hook — how we actually ask the operator
# ---------------------------------------------------------------------------


class _NoChannel:
    """Sentinel returned when no interactive channel is registered —
    the clarifier degrades to a no-op rather than blocking."""

    async def ask(self, question: str, *, timeout_s: float) -> Optional[str]:
        return None

    @property
    def available(self) -> bool:
        return False


_DEFAULT_CHANNEL: Any = _NoChannel()


def register_clarify_channel(channel: Any) -> None:
    """Harness registers a real channel at boot (SerpentFlow prompt,
    etc.). The channel must expose:

        async def ask(question: str, *, timeout_s: float) -> Optional[str]
        @property
        def available(self) -> bool

    ``ask`` should return ``None`` when the operator declined or the
    channel is unavailable, and the raw answer string otherwise.
    """
    global _DEFAULT_CHANNEL
    _DEFAULT_CHANNEL = channel if channel is not None else _NoChannel()


def get_clarify_channel() -> Any:
    return _DEFAULT_CHANNEL


def reset_clarify_channel() -> None:
    global _DEFAULT_CHANNEL
    _DEFAULT_CHANNEL = _NoChannel()


# ---------------------------------------------------------------------------
# Ask — composes gate + heuristic + channel + timeout
# ---------------------------------------------------------------------------


async def ask_operator(
    *,
    op_id: str,
    description: str,
    target_files: Tuple[str, ...],
    goal_keywords: Tuple[str, ...] = (),
    channel: Any = None,
) -> ClarifyResponse:
    """Main entry point. Decides whether to ask, asks if warranted,
    returns a :class:`ClarifyResponse` with the outcome.

    Never raises: any failure in the channel, sanitizer, or heuristic
    returns a ``skipped_*`` / ``timeout`` response so the orchestrator
    can always proceed.
    """
    import time

    # Master gate.
    if not clarify_enabled():
        logger.debug(
            "[ClassifyClarify] op=%s gate=disabled", op_id,
        )
        return ClarifyResponse(
            outcome="skipped_disabled", why_triggered="",
        )

    # Session cap.
    if _session_count() >= max_per_session():
        logger.debug(
            "[ClassifyClarify] op=%s cap_reached=%d", op_id, _session_count(),
        )
        return ClarifyResponse(
            outcome="skipped_cap", why_triggered="session_cap_exhausted",
        )

    # Heuristic.
    reason = should_ask(
        description=description,
        target_files=target_files,
        goal_keywords=goal_keywords,
    )
    if reason is None:
        return ClarifyResponse(
            outcome="skipped_disabled", why_triggered="no_trigger_match",
        )

    # Channel.
    ch = channel if channel is not None else get_clarify_channel()
    if not getattr(ch, "available", False):
        logger.info(
            "[ClassifyClarify] op=%s why_triggered=%s outcome=skipped_no_channel",
            op_id, reason,
        )
        return ClarifyResponse(
            outcome="skipped_no_channel", why_triggered=reason,
        )

    # Compose the question (concise; operator needs to answer in ≤ one line).
    question = _compose_question(
        description=description, reason=reason,
    )

    # Ask with a bounded timeout. asyncio.wait_for cancels the
    # underlying coroutine on timeout — the channel must honor that.
    _t0 = time.monotonic()
    timeout = clarify_timeout_s()
    try:
        raw = await asyncio.wait_for(
            ch.ask(question, timeout_s=timeout),
            timeout=timeout + 2.0,  # small guard over the channel's own timeout
        )
    except asyncio.TimeoutError:
        dur = int((time.monotonic() - _t0) * 1000)
        _increment_session_count()
        logger.info(
            "[ClassifyClarify] op=%s why_triggered=%s outcome=timeout "
            "duration_ms=%d",
            op_id, reason, dur,
        )
        return ClarifyResponse(
            outcome="timeout",
            duration_ms=dur,
            why_triggered=reason,
            question=question,
        )
    except asyncio.CancelledError:
        # Propagate — don't swallow. Upstream wait_for already times
        # things out; a cancel here is genuine shutdown.
        raise
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ClassifyClarify] op=%s channel raised — treating as declined",
            op_id, exc_info=True,
        )
        _increment_session_count()
        return ClarifyResponse(
            outcome="declined", why_triggered=reason,
            duration_ms=int((time.monotonic() - _t0) * 1000),
            question=question,
        )

    dur = int((time.monotonic() - _t0) * 1000)
    _increment_session_count()

    if raw is None or not str(raw).strip():
        logger.info(
            "[ClassifyClarify] op=%s why_triggered=%s outcome=declined "
            "duration_ms=%d",
            op_id, reason, dur,
        )
        return ClarifyResponse(
            outcome="declined",
            duration_ms=dur,
            why_triggered=reason,
            question=question,
        )

    sanitized = sanitize_answer(str(raw))
    logger.info(
        "[ClassifyClarify] op=%s why_triggered=%s outcome=answered "
        "duration_ms=%d answer_chars=%d",
        op_id, reason, dur, len(sanitized),
    )
    return ClarifyResponse(
        outcome="answered",
        answer_raw=str(raw),
        answer_sanitized=sanitized,
        duration_ms=dur,
        why_triggered=reason,
        question=question,
    )


def _compose_question(*, description: str, reason: str) -> str:
    """Render a concise question text based on the ambiguity reason."""
    trimmed = description.strip()[:120] if description else "(empty)"
    if reason == "generic_target_file_list":
        return (
            f"[ClassifyClarify] Goal: {trimmed!r} has a generic target "
            "list. Which specific file(s)? (≤1 line, blank to skip):"
        )
    if reason == "short_description_no_target_files":
        return (
            f"[ClassifyClarify] Goal: {trimmed!r} is short and has no "
            "target files. Can you add detail? (≤1 line, blank to skip):"
        )
    if reason == "no_goal_keyword_match":
        return (
            f"[ClassifyClarify] Goal: {trimmed!r} doesn't match any "
            "active goal keyword. Clarify intent? (≤1 line, blank to skip):"
        )
    return f"[ClassifyClarify] Clarify: {trimmed!r} (blank to skip):"


# ---------------------------------------------------------------------------
# Merge helper — enriches ctx.description / evidence without mutating risk
# ---------------------------------------------------------------------------


def merge_into_context(
    *,
    original_description: str,
    response: ClarifyResponse,
) -> Tuple[str, dict]:
    """Given a clarification response, return ``(new_description, evidence_patch)``.

    The evidence_patch is a dict the caller can merge into
    ``ctx.evidence`` (or ctx.strategic_memory_digest, wherever evidence
    is persisted). It carries:

      * ``clarification_outcome`` — "answered" | "timeout" | …
      * ``clarification_why`` — reason_code that triggered the ask
      * ``clarification_answer`` — sanitized text (empty when timeout/declined)
      * ``clarification_duration_ms`` — latency

    **Authority invariant**: the returned description is
    ``original_description`` + the sanitized answer only. Risk
    classification continues to run on ctx *as a whole* — this function
    never hands the clarifier direct write-access to risk_tier,
    provider_route, or any deterministic engine output.
    """
    evidence_patch = {
        "clarification_outcome": response.outcome,
        "clarification_why": response.why_triggered,
        "clarification_duration_ms": response.duration_ms,
        "clarification_answer": response.answer_sanitized,
    }
    if response.outcome == "answered" and response.answer_sanitized:
        # Append the answer to the description so downstream prompt
        # builders see the clarified intent without overwriting the
        # original sensor text (operators can audit both halves).
        new_desc = (
            original_description.rstrip()
            + "\n\n[operator clarification]\n"
            + response.answer_sanitized
        )
        return (new_desc, evidence_patch)
    return (original_description, evidence_patch)
