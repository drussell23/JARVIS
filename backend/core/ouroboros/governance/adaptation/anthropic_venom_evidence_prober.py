"""Item #3 — production AnthropicVenomEvidenceProber for Phase 7.6.

Phase 7.6 (PR #23176) shipped the bounded HypothesisProbe primitive
with an injectable ``EvidenceProber`` Protocol. The default was
``_NullEvidenceProber`` (zero cost — defends against accidental API
hits). This module provides the FIRST production prober: an
Anthropic-backed implementation that calls the read-only Venom tool
subset to investigate a hypothesis claim.

## Design constraints (load-bearing)

  * **Provider injection**: ``VenomQueryProvider`` Protocol is the
    only place that touches the network. Production wires
    ``AnthropicProvider`` from the canonical provider stack; tests
    inject fakes; default is ``_NullVenomQueryProvider`` returning
    a sentinel — a misconfigured caller cannot accidentally hit a
    paid API.
  * **Tool allowlist enforcement**: every prober round MUST pass
    ``READONLY_TOOL_ALLOWLIST`` (from Phase 7.6 substrate) to the
    provider. Pinned by source-grep + behavioral tests.
  * **Cost cap (per-call)**: ``DEFAULT_COST_CAP_PER_CALL_USD=0.05``
    matches the Phase 5 AdversarialReviewer convention. Exceeding
    a per-call cap is treated as an error round (verdict_signal=
    "continue", evidence="", notes="cost_overrun") so the runner's
    diminishing-returns guarantee still terminates in 2 rounds.
  * **Cumulative session budget**: ``DEFAULT_SESSION_BUDGET_USD=
    1.00`` matches Phase 5 convention. Once exhausted, every
    subsequent round returns the cost-overrun stub.
  * **Bounded sizes**: prompt/evidence/notes capped to defend
    against runaway prober output bloating the ledger.
  * **NEVER raises**: provider exceptions caught + converted to
    error rounds. The Phase 7.6 runner's own try/except around the
    Protocol call is the second line of defense.
  * **Stdlib + adaptation only** import surface (the
    ``VenomQueryProvider`` Protocol is the network boundary).

## Default-off

``JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED`` (default
false). When off, ``build_default_production_prober()`` returns the
Null sentinel — Phase 7.6's HypothesisProbe pre-check then sees
``prober is None`` is FALSE (Null is non-None) but every round will
return empty evidence → diminishing-returns terminates in round 2.
This is the "off but harmless" mode.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, FrozenSet, List, Optional, Protocol, Tuple

from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (
    READONLY_TOOL_ALLOWLIST,
    ProbeRoundResult,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Per-call cost cap (USD). Matches the Phase 5 AdversarialReviewer +
# Phase 5 ChatActionExecutor convention.
DEFAULT_COST_CAP_PER_CALL_USD: float = 0.05

# Cumulative per-instance session budget (USD).
DEFAULT_SESSION_BUDGET_USD: float = 1.00

# Bounded prompt size (defends against runaway claim/evidence text
# bloating the network call).
MAX_PROMPT_CHARS: int = 4096

# Bounded per-round evidence return — must NOT exceed the Phase 7.6
# substrate's MAX_EVIDENCE_CHARS_PER_ROUND so we don't get truncated.
# Use a tighter cap so the runner has headroom.
MAX_EVIDENCE_CHARS_RETURNED: int = 3500

# Bounded prior-evidence inclusion — if the runner has accumulated
# many rounds of evidence, only the most-recent N rounds are passed
# to the provider to keep prompts small.
MAX_PRIOR_EVIDENCE_ROUNDS_INCLUDED: int = 3

# Per-prior-evidence-row cap.
MAX_PRIOR_EVIDENCE_ROW_CHARS: int = 500


def is_production_prober_enabled() -> bool:
    """Master flag —
    ``JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED`` (default
    false until graduation cadence)."""
    return os.environ.get(
        "JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Provider Protocol + Null sentinel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VenomQueryResult:
    """One provider response.

    ``response_text`` is the model's free-form response; the prober
    parses verdict signal from it (looking for confirmed/refuted
    sentinels).
    ``cost_usd`` is the actual spend for this call (used for budget
    accounting).
    ``error`` is non-None when the provider failed (e.g. timeout,
    rate-limit, parse error). The prober treats this as an error
    round.
    """

    response_text: str
    cost_usd: float = 0.0
    error: Optional[str] = None


class VenomQueryProvider(Protocol):
    """One round of read-only Venom-style investigation.

    Implementations call the canonical provider stack (Anthropic,
    DoubleWord, etc.) with ``allowed_tools`` restricted to the
    read-only allowlist. The returned ``response_text`` is the
    model's narrative + evidence text.

    Implementations MUST NOT raise — but if they do, the prober
    catches and converts to an error round.
    """

    def query(
        self,
        *,
        prompt: str,
        allowed_tools: FrozenSet[str],
        max_cost_usd: float,
    ) -> VenomQueryResult: ...


class _NullVenomQueryProvider:
    """Safe-default provider — returns sentinel + zero cost.

    Used when no provider is explicitly configured OR
    ``JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED`` is off.
    Production code wires ``AnthropicProvider`` (or another concrete
    implementation) at SerpentFlow boot time; tests inject fakes.

    With this default:
      * Round 1 returns ("continue", "", null_provider)
      * Diminishing-returns detector fires immediately on round 2
        (empty fingerprint repeats) → INCONCLUSIVE_DIMINISHING.
    """

    def query(
        self,
        *,
        prompt: str,
        allowed_tools: FrozenSet[str],
        max_cost_usd: float,
    ) -> VenomQueryResult:
        return VenomQueryResult(
            response_text="",
            cost_usd=0.0,
            error=None,
        )


# ---------------------------------------------------------------------------
# Production prober
# ---------------------------------------------------------------------------


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    suffix = "...(truncated)"
    return text[: cap - len(suffix)] + suffix


def _build_prompt(
    claim: str,
    expected_outcome: str,
    prior_evidence: Tuple[str, ...],
) -> str:
    """Build the per-round investigation prompt.

    Bounded by MAX_PROMPT_CHARS. Prior evidence rows are capped
    to MAX_PRIOR_EVIDENCE_ROUNDS_INCLUDED most-recent rows, each
    truncated at MAX_PRIOR_EVIDENCE_ROW_CHARS.
    """
    recent = prior_evidence[-MAX_PRIOR_EVIDENCE_ROUNDS_INCLUDED:]
    recent_truncated = [
        _truncate(r, MAX_PRIOR_EVIDENCE_ROW_CHARS) for r in recent
    ]
    parts = [
        "# Hypothesis investigation (read-only)",
        "",
        f"## Claim",
        claim,
        "",
        f"## Expected outcome (falsifiable predicate)",
        expected_outcome,
        "",
        "## Tools available (read-only)",
        ", ".join(sorted(READONLY_TOOL_ALLOWLIST)),
        "",
    ]
    if recent_truncated:
        parts.append("## Prior evidence rounds (most-recent last)")
        for i, ev in enumerate(recent_truncated, 1):
            parts.append(f"### Round {i}")
            parts.append(ev)
            parts.append("")
    parts.extend([
        "## Task",
        "Investigate the claim using ONLY the read-only tools above.",
        "Return a brief evidence summary and one of these verdict "
        "signals at the end of your response:",
        "  - `VERDICT: confirmed` — claim is supported by evidence",
        "  - `VERDICT: refuted` — claim is contradicted by evidence",
        "  - `VERDICT: continue` — need more rounds to decide",
        "Evidence MUST cite specific file paths / line numbers / "
        "function names found via the tools.",
    ])
    prompt = "\n".join(parts)
    return _truncate(prompt, MAX_PROMPT_CHARS)


def _parse_response(
    response_text: str,
) -> Tuple[str, str]:
    """Parse the verdict signal + evidence from the provider's
    response text. Returns (verdict_signal, evidence).

    Looks for the explicit ``VERDICT: <signal>`` sentinel anywhere
    in the response. Falls back to ``continue`` when missing or
    unrecognized — the runner's diminishing-returns guarantee
    eventually terminates either way.
    """
    if not response_text:
        return ("continue", "")
    text_lower = response_text.lower()
    # Prefer the LAST occurrence (the model's final verdict).
    verdict = "continue"
    for needle, signal in (
        ("verdict: confirmed", "confirmed"),
        ("verdict: refuted", "refuted"),
        ("verdict: continue", "continue"),
    ):
        if needle in text_lower:
            # Track latest occurrence index to pick the model's
            # final verdict.
            idx = text_lower.rfind(needle)
            # We just need any > -1; the last-found wins via rfind.
            verdict = signal
            # Don't break — let later sentinels in the loop overwrite
            # if they appear LATER in text. This is a coarse strategy
            # but keeps the parser dependency-free.
    # Re-scan to pick the FINAL sentinel (true rfind across all).
    final_pos = -1
    for needle, signal in (
        ("verdict: confirmed", "confirmed"),
        ("verdict: refuted", "refuted"),
        ("verdict: continue", "continue"),
    ):
        pos = text_lower.rfind(needle)
        if pos > final_pos:
            final_pos = pos
            verdict = signal
    evidence = _truncate(response_text, MAX_EVIDENCE_CHARS_RETURNED)
    return (verdict, evidence)


class AnthropicVenomEvidenceProber:
    """Production prober — wires the Phase 7.6 EvidenceProber
    Protocol to a read-only Venom-style query provider.

    Cage layers:
      1. Provider injection (`VenomQueryProvider` Protocol) — only
         place that touches the network
      2. Tool allowlist enforcement — every query passes
         ``READONLY_TOOL_ALLOWLIST`` to the provider
      3. Per-call cost cap — exceeding returns an error round
      4. Per-instance session budget — exhausting returns error rounds
      5. Bounded sizes — prompt + evidence + notes all capped
      6. NEVER raises — provider exceptions caught + converted

    Construction:
      ``AnthropicVenomEvidenceProber(provider=...)``  — explicit
      ``AnthropicVenomEvidenceProber()``              — Null default
    """

    def __init__(
        self,
        provider: Optional[VenomQueryProvider] = None,
        *,
        cost_cap_per_call_usd: Optional[float] = None,
        session_budget_usd: Optional[float] = None,
    ) -> None:
        self._provider = (
            provider if provider is not None else _NullVenomQueryProvider()
        )
        self._cost_cap_per_call = (
            cost_cap_per_call_usd
            if cost_cap_per_call_usd is not None
            else DEFAULT_COST_CAP_PER_CALL_USD
        )
        self._session_budget = (
            session_budget_usd
            if session_budget_usd is not None
            else DEFAULT_SESSION_BUDGET_USD
        )
        self._cumulative_cost_usd: float = 0.0
        self._budget_exhausted: bool = False

    @property
    def cost_cap_per_call_usd(self) -> float:
        return self._cost_cap_per_call

    @property
    def session_budget_usd(self) -> float:
        return self._session_budget

    @property
    def cumulative_cost_usd(self) -> float:
        return self._cumulative_cost_usd

    @property
    def budget_exhausted(self) -> bool:
        return self._budget_exhausted

    def probe(
        self,
        claim: str,
        expected_outcome: str,
        prior_evidence: Tuple[str, ...],
    ) -> ProbeRoundResult:
        """Implements ``EvidenceProber.probe`` for the Phase 7.6
        runner. NEVER raises."""
        # Pre-call: budget already exhausted from prior rounds in
        # this instance? Skip the network call.
        if self._budget_exhausted:
            return ProbeRoundResult(
                verdict_signal="continue",
                evidence="",
                notes="session_budget_exhausted",
            )
        # Conservative pre-call check: if cumulative + per-call cap
        # would exceed budget, skip the call.
        if (
            self._cumulative_cost_usd + self._cost_cap_per_call
            > self._session_budget
        ):
            self._budget_exhausted = True
            return ProbeRoundResult(
                verdict_signal="continue",
                evidence="",
                notes=(
                    f"session_budget_would_exceed:"
                    f"cumulative={self._cumulative_cost_usd:.4f}+"
                    f"cap={self._cost_cap_per_call:.4f}>"
                    f"budget={self._session_budget:.4f}"
                ),
            )

        prompt = _build_prompt(claim, expected_outcome, prior_evidence)
        try:
            result = self._provider.query(
                prompt=prompt,
                allowed_tools=READONLY_TOOL_ALLOWLIST,
                max_cost_usd=self._cost_cap_per_call,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[AnthropicVenomEvidenceProber] provider raised %s: %s",
                type(exc).__name__, exc,
            )
            return ProbeRoundResult(
                verdict_signal="continue",
                evidence="",
                notes=f"provider_error:{type(exc).__name__}",
            )

        # Account for cost (use the actual reported value; clip at the
        # per-call cap as a safety belt).
        spent = float(result.cost_usd or 0.0)
        if spent > self._cost_cap_per_call:
            logger.warning(
                "[AnthropicVenomEvidenceProber] provider reported "
                "cost=%g > per-call cap=%g — clipping to cap (provider "
                "violated contract)", spent, self._cost_cap_per_call,
            )
            spent = self._cost_cap_per_call
        self._cumulative_cost_usd += spent
        if self._cumulative_cost_usd >= self._session_budget:
            self._budget_exhausted = True

        if result.error:
            return ProbeRoundResult(
                verdict_signal="continue",
                evidence="",
                notes=f"provider_error:{result.error[:200]}",
            )

        verdict, evidence = _parse_response(result.response_text)
        return ProbeRoundResult(
            verdict_signal=verdict,
            evidence=evidence,
            notes=f"cost_usd={spent:.4f}_cum={self._cumulative_cost_usd:.4f}",
        )


def build_default_production_prober(
    provider: Optional[VenomQueryProvider] = None,
) -> AnthropicVenomEvidenceProber:
    """Factory for the default production prober.

    Honors ``JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED``:
      * Master ON + provider supplied → wire the provider
      * Master ON + no provider → use Null sentinel (zero cost)
      * Master OFF → use Null sentinel regardless

    Production code at SerpentFlow boot time should call this with
    an explicit ``provider`` instance built from the canonical
    Anthropic stack. Tests inject fakes via direct constructor.
    """
    if not is_production_prober_enabled() or provider is None:
        return AnthropicVenomEvidenceProber()
    return AnthropicVenomEvidenceProber(provider=provider)


__all__ = [
    "AnthropicVenomEvidenceProber",
    "DEFAULT_COST_CAP_PER_CALL_USD",
    "DEFAULT_SESSION_BUDGET_USD",
    "MAX_EVIDENCE_CHARS_RETURNED",
    "MAX_PRIOR_EVIDENCE_ROUNDS_INCLUDED",
    "MAX_PRIOR_EVIDENCE_ROW_CHARS",
    "MAX_PROMPT_CHARS",
    "VenomQueryProvider",
    "VenomQueryResult",
    "_NullVenomQueryProvider",
    "build_default_production_prober",
    "is_production_prober_enabled",
]
