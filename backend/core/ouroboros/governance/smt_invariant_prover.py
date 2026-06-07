"""Slice 128 Phase 2 (Phase 0 substrate) — SMT/Z3 invariant prover.

The dissertation summit: turn an *engineering assertion* ("Anti-Venom contains
RSI", "the cage never auto-applies an Order-2 self-mod", ...) into a
**machine-checked theorem** with a **tamper-evident certificate** — WITHOUT
replacing the deterministic AST checks (``ShippedCodeInvariant``) that remain
authoritative. This is the *additive* formal layer.

Phase 0 ships the SUBSTRATE, not a catalogue of proven theorems:

  * ``SmtSpec`` — a frozen spec carrying an SMT-LIB2 program whose assertion is
    the **negation** of the invariant. The invariant is PROVED iff Z3 returns
    ``unsat`` (the negation is impossible). Optional ``linked_invariant_name``
    references a ``ShippedCodeInvariant`` it formalizes (composition, not
    replacement).
  * ``ProofVerdict`` — closed 5-value taxonomy.
  * ``prove()`` — runs Z3 **out-of-process** (a hang/crash cannot wedge the
    engine; bounded by ``timeout_ms``). The solver runner is **injectable** so
    the verdict logic is unit-testable with no z3 installed.
  * ``attest_invariant()`` — proves + records a hash-chained receipt to the
    ``BlueEvidenceLedger`` (the cryptographic certificate).

Invariants (non-negotiable):
  * **Import-guarded** — NEVER ``import z3`` at module top; availability probed
    lazily (the engine must boot without z3-solver).
  * **Default-FALSE** — ``JARVIS_SMT_PROVER_ENABLED`` off → ``UNAVAILABLE``.
  * **Fail-closed** — ONLY ``PROVED`` is trustworthy. ``UNKNOWN`` (Z3 gave up),
    ``UNAVAILABLE``, ``ERROR`` and ``REFUTED`` are NOT proofs; a missing/flaky
    solver can never yield a false PROVED.
  * **Additive** — does NOT replace ``ShippedCodeInvariant.validate`` (the AST
    deterministic check stays the authority).
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import os
import shutil
import subprocess  # noqa: S404 — out-of-process Z3 by design, bounded timeout
from typing import Any, Callable, Optional


_ENV_MASTER = "JARVIS_SMT_PROVER_ENABLED"
_SCHEMA_VERSION = "smt.1"
_DEFAULT_TIMEOUT_MS = 5000


# ============================================================================
# Closed taxonomy
# ============================================================================


class ProofVerdict(str, enum.Enum):
    """Closed 5-value proof verdict. Only ``PROVED`` is trustworthy."""

    PROVED = "proved"            # Z3 unsat(negation) — the invariant holds
    REFUTED = "refuted"          # Z3 sat(negation) — counterexample exists
    UNKNOWN = "unknown"          # Z3 gave up — fail-closed (NOT a proof)
    UNAVAILABLE = "unavailable"  # master off / z3 not installed
    ERROR = "error"              # harness/solver error — fail-closed


@dataclasses.dataclass(frozen=True)
class SmtSpec:
    """One invariant to prove. ``smt2`` asserts the NEGATION of the invariant
    (PROVED iff Z3 returns ``unsat``)."""

    name: str
    smt2: str
    schema_version: str = _SCHEMA_VERSION
    timeout_ms: int = _DEFAULT_TIMEOUT_MS
    description: str = ""
    # Compose (not replace) a ShippedCodeInvariant this formalizes.
    linked_invariant_name: str = ""


@dataclasses.dataclass(frozen=True)
class SolverRun:
    """Raw result of one out-of-process solver invocation."""

    status: str          # "unsat" | "sat" | "unknown" | "timeout" | "error"
    raw_output: str = ""


@dataclasses.dataclass(frozen=True)
class ProofResult:
    """Frozen proof outcome + tamper-evident certificate."""

    verdict: ProofVerdict
    spec_name: str
    schema_version: str = _SCHEMA_VERSION
    detail: str = ""
    certificate_sha256: str = ""
    solver: str = "z3"
    linked_invariant_name: str = ""


# A solver runner maps (smt2, timeout_ms) → SolverRun. Injectable for tests.
SolverRunner = Callable[[str, int], SolverRun]


# ============================================================================
# Gates + availability (import-guarded, lazy)
# ============================================================================


def smt_prover_enabled() -> bool:
    """Master gate. Default **FALSE** per §33.1. NEVER raises."""
    try:
        return os.environ.get(_ENV_MASTER, "false").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


def z3_available() -> bool:
    """True iff Z3 can be run out-of-process: the ``z3`` CLI is on PATH, OR the
    ``z3`` python module is importable (probed lazily — NO top-level import).
    NEVER raises."""
    try:
        if shutil.which("z3"):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        import importlib.util
        return importlib.util.find_spec("z3") is not None
    except Exception:  # noqa: BLE001
        return False


def is_proof_trustworthy(result: ProofResult) -> bool:
    """The fail-closed gate: ONLY a ``PROVED`` verdict is a trustworthy
    machine-checked proof. Everything else (UNKNOWN/UNAVAILABLE/ERROR/REFUTED)
    is NOT — callers must treat them as 'not proven'."""
    return bool(result is not None and result.verdict is ProofVerdict.PROVED)


# ============================================================================
# Out-of-process default runner (z3 CLI, bounded timeout)
# ============================================================================


def _default_z3_runner(smt2: str, timeout_ms: int) -> SolverRun:
    """Run the ``z3`` CLI out-of-process over SMT-LIB2 on stdin. Bounded by a
    hard timeout so a Z3 hang cannot wedge the caller. NEVER raises — failure
    modes map to ``timeout`` / ``error`` SolverRun statuses (fail-closed)."""
    z3_bin = shutil.which("z3")
    if not z3_bin:
        return SolverRun(status="error", raw_output="z3 cli not found")
    timeout_s = max(0.1, float(timeout_ms) / 1000.0)
    try:
        proc = subprocess.run(  # noqa: S603 — fixed binary, no shell
            [z3_bin, "-in"],
            input=smt2,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return SolverRun(status="timeout", raw_output="z3 timeout")
    except Exception as exc:  # noqa: BLE001
        return SolverRun(status="error", raw_output=f"{type(exc).__name__}: {exc}")
    out = (proc.stdout or "") + (proc.stderr or "")
    low = out.lower()
    # First decisive token wins; unknown/empty → unknown (fail-closed).
    if "unsat" in low:
        status = "unsat"
    elif "sat" in low:
        status = "sat"
    elif "unknown" in low:
        status = "unknown"
    else:
        status = "unknown"
    return SolverRun(status=status, raw_output=out[:4000])


_STATUS_TO_VERDICT = {
    "unsat": ProofVerdict.PROVED,
    "sat": ProofVerdict.REFUTED,
    "unknown": ProofVerdict.UNKNOWN,
    "timeout": ProofVerdict.UNKNOWN,   # fail-closed: a timeout is NOT a proof
    "error": ProofVerdict.ERROR,
}


def _certificate(spec: SmtSpec, verdict: ProofVerdict, raw: str) -> str:
    """Deterministic tamper-evident certificate over the spec + verdict + raw
    solver output. Same (spec, verdict, raw) → same digest."""
    canonical = "\x1f".join((
        spec.schema_version, spec.name, spec.smt2,
        verdict.value, raw or "",
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ============================================================================
# Public API — prove() + attest_invariant()
# ============================================================================


def prove(
    spec: SmtSpec,
    *,
    runner: Optional[SolverRunner] = None,
) -> ProofResult:
    """Attempt to prove ``spec`` (PROVED iff Z3 returns ``unsat`` on the
    negation). Out-of-process + bounded + fail-closed. NEVER raises.

    Resolution:
      * master OFF → ``UNAVAILABLE``.
      * default runner + z3 absent → ``UNAVAILABLE`` (never a false PROVED).
      * injected ``runner`` → used as-is (the test/embedded solver).
    """
    def _result(v: ProofVerdict, raw: str = "", detail: str = "") -> ProofResult:
        return ProofResult(
            verdict=v,
            spec_name=spec.name,
            schema_version=spec.schema_version,
            detail=detail,
            certificate_sha256=_certificate(spec, v, raw),
            linked_invariant_name=spec.linked_invariant_name,
        )

    if not smt_prover_enabled():
        return _result(ProofVerdict.UNAVAILABLE, detail="master_disabled")

    use_runner = runner
    if use_runner is None:
        if not z3_available():
            return _result(ProofVerdict.UNAVAILABLE, detail="z3_not_installed")
        use_runner = _default_z3_runner

    try:
        run = use_runner(spec.smt2, spec.timeout_ms)
    except Exception as exc:  # noqa: BLE001 — fail-closed
        return _result(
            ProofVerdict.ERROR, raw=f"{type(exc).__name__}: {exc}",
            detail="runner_raised",
        )

    if run is None:
        return _result(ProofVerdict.ERROR, detail="runner_returned_none")

    verdict = _STATUS_TO_VERDICT.get(
        str(run.status).strip().lower(), ProofVerdict.UNKNOWN,
    )
    return _result(verdict, raw=run.raw_output, detail=f"status={run.status}")


def attest_invariant(
    spec: SmtSpec,
    *,
    ledger: Optional[Any] = None,
    runner: Optional[SolverRunner] = None,
) -> ProofResult:
    """Prove ``spec`` and, when a ``BlueEvidenceLedger`` is supplied, append a
    hash-chained receipt (the cryptographic certificate). The receipt records
    the verdict; ``blocked=True`` ONLY for a trustworthy PROVED. This COMPOSES
    the existing ledger — it does not replace any AST deterministic check.
    NEVER raises."""
    result = prove(spec, runner=runner)
    if ledger is not None:
        try:
            ledger.record(
                attack_class="smt_invariant_proof",
                payload=f"{spec.name}\n{spec.smt2}",
                verdict=result.verdict.value,
                blocked=is_proof_trustworthy(result),
                blocked_by=f"z3:{spec.linked_invariant_name or spec.name}",
            )
        except Exception:  # noqa: BLE001 — recording must never break proving
            pass
    return result


__all__ = [
    "ProofVerdict",
    "SmtSpec",
    "SolverRun",
    "SolverRunner",
    "ProofResult",
    "smt_prover_enabled",
    "z3_available",
    "is_proof_trustworthy",
    "prove",
    "attest_invariant",
]
