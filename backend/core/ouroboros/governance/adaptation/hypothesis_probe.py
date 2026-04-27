"""Phase 7.6 — bounded HypothesisProbe primitive (closes the curiosity gap).

Per `OUROBOROS_VENOM_PRD.md` §9 Phase 7.6:

  > HypothesisLedger (Phase 2 P1.5) records hypotheses but has no
  > closing loop — no "test the hypothesis with a probe."
  > Solution: HypothesisProbe primitive + bounded probe runner
  > with 3 termination guarantees.

This module ships the primitive (data model + cage + runner) plus
a Protocol for the evidence prober (which production wires to a
read-only Venom subset; tests inject fakes; default is a Null
sentinel — no model call, no cost — so a misconfigured caller
CANNOT accidentally hit a paid API).

## Design constraints (load-bearing)

  * **Three independent termination guarantees** (per Pass C §7.6
    spec). At least one MUST fire on every probe — pinned by AST
    + behavioral tests:
      1. **Call cap**: `MAX_CALLS_PER_PROBE` reads max. After N
         calls, terminate with `INCONCLUSIVE_BUDGET`.
      2. **Wall-clock cap**: `TIMEOUT_S` seconds max from probe
         start. After T seconds, terminate with
         `INCONCLUSIVE_TIMEOUT`. Checked against
         `time.monotonic()` (NOT wall clock — defends against
         system clock changes mid-probe).
      3. **Diminishing-returns**: sha256 of every round's
         evidence fingerprint. If round N+1 returns the same
         fingerprint as round N, terminate with
         `INCONCLUSIVE_DIMINISHING`. Defends against a stuck
         prober loop wasting budget on identical reads.
  * **Read-only tool allowlist** (frozen set). Production
    `EvidenceProber` implementations MUST restrict the Venom
    invocation to `read_file` / `search_code` / `get_callers` /
    `glob_files` / `list_dir`. This module exposes the allowlist
    constant; the cage of `EvidenceProber` callers honoring it is
    asserted at the production-wiring follow-up site.
  * **Stdlib-only import surface.** Same cage discipline as the
    rest of `adaptation/`. Does NOT import HypothesisLedger
    (callers may pass a `Hypothesis` shape, but the probe runs
    against caller-supplied claim/expected_outcome strings —
    this primitive is ledger-agnostic). Does NOT import
    `tool_executor.py` or any Venom module (`EvidenceProber` is
    Protocol-typed; concrete implementations live elsewhere).
  * **Default-off + safe-default prober**: master flag default
    false; `_NullEvidenceProber` returns no evidence so a
    misconfigured caller cannot accidentally invoke a real model.
  * **NEVER raises into the caller** — Protocol implementations
    that raise are caught by the runner; verdict becomes
    `INCONCLUSIVE_PROBER_ERROR` with the error class name in the
    notes.

## Default-off

`JARVIS_HYPOTHESIS_PROBE_ENABLED` (default false).

## Verdict enum

  * `CONFIRMED` — prober's most-recent round signaled
    `verdict="confirmed"` AND evidence is non-empty.
  * `REFUTED` — prober's most-recent round signaled
    `verdict="refuted"` AND evidence is non-empty.
  * `INCONCLUSIVE_BUDGET` — call cap hit before
    confirmed/refuted.
  * `INCONCLUSIVE_TIMEOUT` — wall-clock cap hit.
  * `INCONCLUSIVE_DIMINISHING` — duplicate evidence fingerprint
    across consecutive rounds.
  * `INCONCLUSIVE_PROBER_ERROR` — prober raised; runner caught.
  * `SKIPPED_MASTER_OFF` — `JARVIS_HYPOTHESIS_PROBE_ENABLED`
    not truthy.
  * `SKIPPED_NO_PROBER` — caller passed `prober=None` and no
    default is configured (Null sentinel only used when explicit).
  * `SKIPPED_EMPTY_HYPOTHESIS` — claim or expected_outcome blank.

## Bridge to Pass C

Per the PRD spec: confirmed hypotheses become adaptation
proposals (feeds Slice 2 + 3 mining surfaces); refuted hypotheses
become POSTMORTEMs (feeds PostmortemRecall). This bridge is a
follow-up — the probe runner returns a `ProbeResult` containing
verdict + evidence; downstream wiring decides what to do with
the result.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Read-only Venom tool allowlist. Production `EvidenceProber`
# implementations MUST restrict their Venom invocation to this
# subset. Pinned by tests + production-wiring AST check (when
# the wiring follow-up lands).
READONLY_TOOL_ALLOWLIST: FrozenSet[str] = frozenset({
    "read_file",
    "search_code",
    "get_callers",
    "glob_files",
    "list_dir",
})


# Hard call cap per probe. Each prober round counts as one call.
MAX_CALLS_PER_PROBE_DEFAULT: int = 5

# Per-probe wall-clock cap (seconds). Measured against
# time.monotonic() — defends against system clock changes mid-probe.
TIMEOUT_S_DEFAULT: float = 30.0

# Bounded sizes for evidence storage (defense-in-depth against a
# prober returning massive blobs that bloat the ledger).
MAX_EVIDENCE_CHARS_PER_ROUND: int = 4096
MAX_NOTES_CHARS: int = 1024


def is_probe_enabled() -> bool:
    """Master flag — ``JARVIS_HYPOTHESIS_PROBE_ENABLED``
    (default false until Phase 7.6 graduation)."""
    return os.environ.get(
        "JARVIS_HYPOTHESIS_PROBE_ENABLED", "",
    ).strip().lower() in _TRUTHY


def get_max_calls_per_probe() -> int:
    """Env-overridable max-calls — ``JARVIS_HYPOTHESIS_PROBE_MAX_CALLS``."""
    raw = os.environ.get("JARVIS_HYPOTHESIS_PROBE_MAX_CALLS")
    if raw is None:
        return MAX_CALLS_PER_PROBE_DEFAULT
    try:
        v = int(raw)
        return v if v >= 1 else MAX_CALLS_PER_PROBE_DEFAULT
    except ValueError:
        return MAX_CALLS_PER_PROBE_DEFAULT


def get_timeout_s() -> float:
    """Env-overridable timeout — ``JARVIS_HYPOTHESIS_PROBE_TIMEOUT_S``."""
    raw = os.environ.get("JARVIS_HYPOTHESIS_PROBE_TIMEOUT_S")
    if raw is None:
        return TIMEOUT_S_DEFAULT
    try:
        v = float(raw)
        return v if v > 0 else TIMEOUT_S_DEFAULT
    except ValueError:
        return TIMEOUT_S_DEFAULT


# ---------------------------------------------------------------------------
# Verdict + result shapes
# ---------------------------------------------------------------------------


class ProbeVerdict(str, enum.Enum):
    """Probe terminal verdict. The 3 INCONCLUSIVE_* values are the
    structural-termination guarantees; SKIPPED_* are pre-checks;
    CONFIRMED/REFUTED come from the prober's signal."""

    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    INCONCLUSIVE_BUDGET = "inconclusive_budget"
    INCONCLUSIVE_TIMEOUT = "inconclusive_timeout"
    INCONCLUSIVE_DIMINISHING = "inconclusive_diminishing"
    INCONCLUSIVE_PROBER_ERROR = "inconclusive_prober_error"
    SKIPPED_MASTER_OFF = "skipped_master_off"
    SKIPPED_NO_PROBER = "skipped_no_prober"
    SKIPPED_EMPTY_HYPOTHESIS = "skipped_empty_hypothesis"


@dataclass(frozen=True)
class ProbeRoundResult:
    """Single round's output from a prober.

    ``verdict_signal`` is the prober's per-round read:
      * "confirmed" / "refuted" → terminate runner with that verdict.
      * "continue" / anything else → runner consults termination
        guarantees and either advances to the next round or terminates
        inconclusive.

    ``evidence`` is free-form text — a fingerprint hash is computed
    from it for the diminishing-returns guarantee.
    """

    verdict_signal: str
    evidence: str
    notes: str = ""


@dataclass(frozen=True)
class ProbeResult:
    """Terminal result of a HypothesisProbe.test() call."""

    verdict: ProbeVerdict
    rounds: int
    elapsed_s: float
    evidence_hashes: Tuple[str, ...] = field(default_factory=tuple)
    final_evidence: str = ""
    notes: str = ""

    @property
    def is_confirmed(self) -> bool:
        return self.verdict == ProbeVerdict.CONFIRMED

    @property
    def is_refuted(self) -> bool:
        return self.verdict == ProbeVerdict.REFUTED

    @property
    def is_inconclusive(self) -> bool:
        return self.verdict in (
            ProbeVerdict.INCONCLUSIVE_BUDGET,
            ProbeVerdict.INCONCLUSIVE_TIMEOUT,
            ProbeVerdict.INCONCLUSIVE_DIMINISHING,
            ProbeVerdict.INCONCLUSIVE_PROBER_ERROR,
        )

    @property
    def is_skipped(self) -> bool:
        return self.verdict in (
            ProbeVerdict.SKIPPED_MASTER_OFF,
            ProbeVerdict.SKIPPED_NO_PROBER,
            ProbeVerdict.SKIPPED_EMPTY_HYPOTHESIS,
        )


# ---------------------------------------------------------------------------
# EvidenceProber Protocol + Null sentinel
# ---------------------------------------------------------------------------


class EvidenceProber(Protocol):
    """One round of evidence-gathering against a hypothesis.

    Production implementations call a read-only Venom subset
    (allowlist enforced at the implementation, not here — this
    primitive is Venom-agnostic). The runner calls
    ``probe(claim, expected_outcome, prior_evidence)`` repeatedly
    until termination.

    ``prior_evidence`` is the list of evidence strings from prior
    rounds in this probe (most-recent last). Implementations may
    use it to refine the next read.

    Implementations MUST NOT raise — but if they do, the runner
    catches and terminates with `INCONCLUSIVE_PROBER_ERROR`.
    """

    def probe(
        self,
        claim: str,
        expected_outcome: str,
        prior_evidence: Tuple[str, ...],
    ) -> ProbeRoundResult: ...


class _NullEvidenceProber:
    """Safe-default prober that returns NO evidence on every round.

    Used when no prober is explicitly configured. With a Null
    prober:
      * Round 1 returns ("continue", "", ...)
      * Diminishing-returns detector fires immediately on round 2
        (empty fingerprint repeats) → INCONCLUSIVE_DIMINISHING.

    This means **a misconfigured caller cannot accidentally hit a
    real model**: every probe with the Null prober terminates
    inconclusive within 2 rounds with zero cost.
    """

    def probe(
        self,
        claim: str,
        expected_outcome: str,
        prior_evidence: Tuple[str, ...],
    ) -> ProbeRoundResult:
        return ProbeRoundResult(
            verdict_signal="continue",
            evidence="",
            notes="null_prober",
        )


# ---------------------------------------------------------------------------
# HypothesisProbe runner
# ---------------------------------------------------------------------------


def _evidence_fingerprint(evidence: str) -> str:
    """Stable fingerprint of an evidence blob for diminishing-returns."""
    h = hashlib.sha256()
    h.update(evidence.encode("utf-8", errors="replace"))
    return f"sha256:{h.hexdigest()}"


_TRUNC_SUFFIX = "...(truncated)"


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - len(_TRUNC_SUFFIX)] + _TRUNC_SUFFIX


class HypothesisProbe:
    """Bounded probe runner.

    Construct with an optional ``EvidenceProber``. Call
    ``test(claim, expected_outcome)`` to run one probe.

    The runner enforces all three termination guarantees
    structurally — no prober configuration can override them.
    """

    def __init__(
        self,
        prober: Optional[EvidenceProber] = None,
        *,
        max_calls: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self._prober = prober
        self._max_calls = (
            max_calls if max_calls is not None else get_max_calls_per_probe()
        )
        self._timeout_s = (
            timeout_s if timeout_s is not None else get_timeout_s()
        )

    @property
    def max_calls(self) -> int:
        return self._max_calls

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    def test(
        self,
        claim: str,
        expected_outcome: str,
    ) -> ProbeResult:
        """Run a probe against the supplied hypothesis.

        Returns a `ProbeResult` with the terminal verdict + evidence
        + structural-termination metadata. NEVER raises.

        Pre-checks (in order):
          1. Master flag off → SKIPPED_MASTER_OFF
          2. Empty claim or expected_outcome → SKIPPED_EMPTY_HYPOTHESIS
          3. No prober + no default → SKIPPED_NO_PROBER

        Then runs prober rounds until one of the 3 termination
        guarantees fires OR prober signals confirmed/refuted.
        """
        if not is_probe_enabled():
            return ProbeResult(
                verdict=ProbeVerdict.SKIPPED_MASTER_OFF,
                rounds=0, elapsed_s=0.0,
                notes="master_off",
            )
        if not claim or not claim.strip():
            return ProbeResult(
                verdict=ProbeVerdict.SKIPPED_EMPTY_HYPOTHESIS,
                rounds=0, elapsed_s=0.0,
                notes="empty_claim",
            )
        if not expected_outcome or not expected_outcome.strip():
            return ProbeResult(
                verdict=ProbeVerdict.SKIPPED_EMPTY_HYPOTHESIS,
                rounds=0, elapsed_s=0.0,
                notes="empty_expected_outcome",
            )
        if self._prober is None:
            return ProbeResult(
                verdict=ProbeVerdict.SKIPPED_NO_PROBER,
                rounds=0, elapsed_s=0.0,
                notes="no_prober_configured",
            )

        start = time.monotonic()
        evidence_history: List[str] = []
        hash_history: List[str] = []
        rounds = 0
        last_round: Optional[ProbeRoundResult] = None

        while True:
            # Termination guarantee 1: call cap (checked BEFORE the call).
            if rounds >= self._max_calls:
                return ProbeResult(
                    verdict=ProbeVerdict.INCONCLUSIVE_BUDGET,
                    rounds=rounds,
                    elapsed_s=time.monotonic() - start,
                    evidence_hashes=tuple(hash_history),
                    final_evidence=(
                        last_round.evidence if last_round else ""
                    ),
                    notes=_truncate(
                        f"call_cap={self._max_calls}", MAX_NOTES_CHARS,
                    ),
                )
            # Termination guarantee 2: wall-clock cap (checked BEFORE call).
            elapsed = time.monotonic() - start
            if elapsed >= self._timeout_s:
                return ProbeResult(
                    verdict=ProbeVerdict.INCONCLUSIVE_TIMEOUT,
                    rounds=rounds,
                    elapsed_s=elapsed,
                    evidence_hashes=tuple(hash_history),
                    final_evidence=(
                        last_round.evidence if last_round else ""
                    ),
                    notes=_truncate(
                        f"timeout_s={self._timeout_s:.2f}", MAX_NOTES_CHARS,
                    ),
                )

            try:
                round_result = self._prober.probe(
                    claim,
                    expected_outcome,
                    tuple(evidence_history),
                )
            except Exception as exc:  # defensive — Protocol may raise
                return ProbeResult(
                    verdict=ProbeVerdict.INCONCLUSIVE_PROBER_ERROR,
                    rounds=rounds,
                    elapsed_s=time.monotonic() - start,
                    evidence_hashes=tuple(hash_history),
                    final_evidence=(
                        last_round.evidence if last_round else ""
                    ),
                    notes=_truncate(
                        f"prober_error:{type(exc).__name__}:{exc}",
                        MAX_NOTES_CHARS,
                    ),
                )

            rounds += 1
            last_round = round_result
            truncated_evidence = _truncate(
                round_result.evidence or "", MAX_EVIDENCE_CHARS_PER_ROUND,
            )
            evidence_history.append(truncated_evidence)
            current_hash = _evidence_fingerprint(truncated_evidence)
            hash_history.append(current_hash)

            # Prober's per-round signal — terminate on confirmed/refuted.
            signal = (round_result.verdict_signal or "").strip().lower()
            if signal == "confirmed" and truncated_evidence:
                return ProbeResult(
                    verdict=ProbeVerdict.CONFIRMED,
                    rounds=rounds,
                    elapsed_s=time.monotonic() - start,
                    evidence_hashes=tuple(hash_history),
                    final_evidence=truncated_evidence,
                    notes=_truncate(
                        round_result.notes or "", MAX_NOTES_CHARS,
                    ),
                )
            if signal == "refuted" and truncated_evidence:
                return ProbeResult(
                    verdict=ProbeVerdict.REFUTED,
                    rounds=rounds,
                    elapsed_s=time.monotonic() - start,
                    evidence_hashes=tuple(hash_history),
                    final_evidence=truncated_evidence,
                    notes=_truncate(
                        round_result.notes or "", MAX_NOTES_CHARS,
                    ),
                )

            # Termination guarantee 3: diminishing-returns. Compare
            # current hash to immediately-prior hash.
            if (
                len(hash_history) >= 2
                and hash_history[-1] == hash_history[-2]
            ):
                return ProbeResult(
                    verdict=ProbeVerdict.INCONCLUSIVE_DIMINISHING,
                    rounds=rounds,
                    elapsed_s=time.monotonic() - start,
                    evidence_hashes=tuple(hash_history),
                    final_evidence=truncated_evidence,
                    notes=_truncate(
                        f"duplicate_evidence_at_round={rounds}",
                        MAX_NOTES_CHARS,
                    ),
                )


__all__ = [
    "EvidenceProber",
    "HypothesisProbe",
    "MAX_CALLS_PER_PROBE_DEFAULT",
    "MAX_EVIDENCE_CHARS_PER_ROUND",
    "MAX_NOTES_CHARS",
    "ProbeResult",
    "ProbeRoundResult",
    "ProbeVerdict",
    "READONLY_TOOL_ALLOWLIST",
    "TIMEOUT_S_DEFAULT",
    "_NullEvidenceProber",
    "get_max_calls_per_probe",
    "get_timeout_s",
    "is_probe_enabled",
]
