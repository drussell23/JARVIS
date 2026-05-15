"""AutoCommitter graduation-evidence gate (PRD §41.11.4).

## Big Picture

The generic :class:`GraduationLedger` proves *"soaks were clean."* It
counts clean / runner / infra session outcomes per flag and reports
``is_eligible`` once the clean-session floor is met. That is the right
gate for most flags — but it is **insufficient evidence** for
graduating the AutoCommitter unattended-apply path.

A soak can be perfectly *clean* while AutoCommitter never fires on a
Yellow-tier (``NOTIFY_APPLY``) op: every op might have been
``SAFE_AUTO``, or no op fired at all, or the only commits were
human-authored. Graduating AutoCommitter on clean-soak-count alone
would assert a capability the evidence does not actually demonstrate —
the precise §92.16-class overclaim this whole arc exists to prevent.
And §41.11.1 (the SICA-pattern demonstration) depends on this
prerequisite being *real*, not assumed.

This gate closes that evidence gap. It **composes** the generic
ledger's clean-soak count (never re-implements it) and **adds** the
AutoCommitter-specific evidence layer the ledger lacks: deterministic
``git log`` proof that, within each counted clean-soak's time window,
≥1 commit carries **both** the canonical O+V signature **and** the
``Risk: NOTIFY_APPLY`` body marker. The gate reports ``READY`` only
when the generic ledger is eligible **and** every counted clean soak
has that per-window evidence.

## The honest-evidence property

Per-soak attribution is genuine, not a fabricated floor. Clean
:class:`SessionRecord` rows carry ``recorded_at_epoch``; consecutive
clean epochs define each soak's window. The first soak has no prior
bound, so a bounded lookback is used and the diagnostic states this
caveat explicitly — claim exactly what the evidence supports, never
more.

## Composition discipline (no fork, no hardcoding)

* ``adaptation.graduation_ledger.GraduationLedger`` — the canonical
  clean-soak counter + row reader (`_read_all`). Single source of
  truth for "was the soak clean / how many."
* ``auto_committer.ov_signature_substring()`` — the canonical O+V
  detection substring. NOT hardcoded here.
* ``risk_engine.RiskTier.NOTIFY_APPLY`` — the Yellow-tier body marker
  is *derived* as ``f"Risk: {RiskTier.NOTIFY_APPLY.name}"``, never a
  hardcoded literal.
* ``git log`` via ``asyncio.create_subprocess_exec`` (safe argv list,
  never ``shell=True`` — mirrors GitApplyDiffApplier discipline, the
  documented repo standard).

## Authority asymmetry (AST-pinned)

Read-only measurement substrate. MUST NOT import ``orchestrator`` /
``iron_gate`` / ``policy_engine`` / ``change_engine`` /
``candidate_generator``. It observes evidence; it never mutates policy,
files, or the flag itself. Flipping the flag remains an operator act —
this gate only makes that decision *evidence-driven instead of
hand-waved*.

## Master flag (§33.1 default-FALSE)

``JARVIS_AUTOCOMMIT_GRADUATION_GATE_ENABLED`` defaults FALSE — an
experimental measurement substrate awaiting Phase 9 graduation.
Master-off → a single ``MASTER_OFF`` report, zero side effects.
"""
from __future__ import annotations

import asyncio
import ast
import enum
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


AUTOCOMMIT_GRAD_SCHEMA_VERSION: str = "1.0"

_ENV_MASTER: str = "JARVIS_AUTOCOMMIT_GRADUATION_GATE_ENABLED"
_ENV_TARGET_FLAG: str = "JARVIS_AUTOCOMMIT_GRAD_TARGET_FLAG"
_ENV_LOOKBACK_DAYS: str = "JARVIS_AUTOCOMMIT_GRAD_LOOKBACK_DAYS"
_ENV_GIT_TIMEOUT_S: str = "JARVIS_AUTOCOMMIT_GRAD_GIT_TIMEOUT_S"

_TRUTHY = ("true", "1", "yes", "on")

_DEFAULT_TARGET_FLAG: str = "JARVIS_AUTO_COMMIT_ENABLED"
_DEFAULT_LOOKBACK_DAYS: int = 30
_DEFAULT_GIT_TIMEOUT_S: int = 30

# Hard caps — a runaway repo / ledger cannot bloat the scan.
_MAX_LOOKBACK_DAYS: int = 365
_MAX_GIT_TIMEOUT_S: int = 300
_MAX_GIT_LOG_COMMITS: int = 20_000
_MAX_SAMPLE_HASHES: int = 5

# git log record/field separators (ASCII control chars — cannot occur
# inside a commit body, so parsing is unambiguous + injection-safe).
_REC_SEP = "\x1e"
_FLD_SEP = "\x00"


# ===========================================================================
# Closed taxonomies (AST-pinned)
# ===========================================================================


class CommitEvidenceKind(str, enum.Enum):
    """How one git-log commit classifies as AutoCommitter evidence.
    Closed 3-value taxonomy. Bytes-pinned via AST."""

    YELLOW_TIER = "yellow_tier"   # OV signature AND Risk: NOTIFY_APPLY
    OTHER_TIER = "other_tier"     # OV signature, non-Yellow / no risk line
    NOT_OV = "not_ov"             # not an O+V commit (excluded)


class AutoCommitEvidenceVerdict(str, enum.Enum):
    """Closed 5-value graduation-evidence verdict. Bytes-pinned via AST.

    ``READY`` requires BOTH the generic clean-soak ledger eligibility
    AND per-soak-window Yellow-tier O+V commit evidence — the second
    condition is what the generic ledger cannot prove and what this
    gate exists to add.
    """

    READY = "ready"
    LEDGER_NOT_ELIGIBLE = "ledger_not_eligible"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    NO_GIT_HISTORY = "no_git_history"
    MASTER_OFF = "master_off"


# ===========================================================================
# Frozen artifacts (§33.5 versioned — to_dict/from_dict roundtrip)
# ===========================================================================


@dataclass(frozen=True)
class SoakCommitEvidence:
    """Per-clean-soak window evidence. Frozen."""

    session_id: str
    window_start_epoch: float
    window_end_epoch: float
    yellow_tier_count: int
    other_tier_count: int
    sample_yellow_hashes: Tuple[str, ...]
    is_first_soak_bounded: bool  # True → window start is lookback-bounded

    @property
    def has_evidence(self) -> bool:
        return self.yellow_tier_count >= 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "window_start_epoch": round(self.window_start_epoch, 3),
            "window_end_epoch": round(self.window_end_epoch, 3),
            "yellow_tier_count": self.yellow_tier_count,
            "other_tier_count": self.other_tier_count,
            "sample_yellow_hashes": list(self.sample_yellow_hashes),
            "is_first_soak_bounded": self.is_first_soak_bounded,
            "has_evidence": self.has_evidence,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SoakCommitEvidence":
        return cls(
            session_id=str(payload.get("session_id", "")),
            window_start_epoch=float(
                payload.get("window_start_epoch", 0.0)
            ),
            window_end_epoch=float(payload.get("window_end_epoch", 0.0)),
            yellow_tier_count=int(payload.get("yellow_tier_count", 0)),
            other_tier_count=int(payload.get("other_tier_count", 0)),
            sample_yellow_hashes=tuple(
                str(h) for h in payload.get("sample_yellow_hashes", ())
            ),
            is_first_soak_bounded=bool(
                payload.get("is_first_soak_bounded", False)
            ),
        )


@dataclass(frozen=True)
class AutoCommitGraduationReport:
    """Aggregate graduation-evidence report. Frozen."""

    schema_version: str
    target_flag: str
    verdict: AutoCommitEvidenceVerdict
    ledger_clean_count: int
    ledger_required: int
    ledger_runner_count: int
    ledger_eligible: bool
    soaks_with_evidence: int
    soaks_missing_evidence: int
    per_soak_evidence: Tuple[SoakCommitEvidence, ...]
    diagnostic: str
    generated_at_unix: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "target_flag": self.target_flag,
            "verdict": self.verdict.value,
            "ledger_clean_count": self.ledger_clean_count,
            "ledger_required": self.ledger_required,
            "ledger_runner_count": self.ledger_runner_count,
            "ledger_eligible": self.ledger_eligible,
            "soaks_with_evidence": self.soaks_with_evidence,
            "soaks_missing_evidence": self.soaks_missing_evidence,
            "per_soak_evidence": [
                e.to_dict() for e in self.per_soak_evidence
            ],
            "diagnostic": self.diagnostic[:512],
            "generated_at_unix": round(self.generated_at_unix, 3),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "AutoCommitGraduationReport":
        return cls(
            schema_version=str(
                payload.get(
                    "schema_version", AUTOCOMMIT_GRAD_SCHEMA_VERSION
                )
            ),
            target_flag=str(payload.get("target_flag", "")),
            verdict=AutoCommitEvidenceVerdict(str(payload["verdict"])),
            ledger_clean_count=int(payload.get("ledger_clean_count", 0)),
            ledger_required=int(payload.get("ledger_required", 0)),
            ledger_runner_count=int(
                payload.get("ledger_runner_count", 0)
            ),
            ledger_eligible=bool(payload.get("ledger_eligible", False)),
            soaks_with_evidence=int(
                payload.get("soaks_with_evidence", 0)
            ),
            soaks_missing_evidence=int(
                payload.get("soaks_missing_evidence", 0)
            ),
            per_soak_evidence=tuple(
                SoakCommitEvidence.from_dict(e)
                for e in payload.get("per_soak_evidence", ())
            ),
            diagnostic=str(payload.get("diagnostic", "")),
            generated_at_unix=float(
                payload.get("generated_at_unix", 0.0)
            ),
        )


# ===========================================================================
# Env readers (canonical idiom — no parenthetical logic around getenv)
# ===========================================================================


def master_enabled() -> bool:
    """§33.1 measurement-substrate variant — default-FALSE.

    Operator-override:
    ``JARVIS_AUTOCOMMIT_GRADUATION_GATE_ENABLED=true``.
    """
    raw = os.environ.get(_ENV_MASTER, "").strip().lower()
    return raw in _TRUTHY


def _target_flag() -> str:
    raw = os.environ.get(_ENV_TARGET_FLAG, "").strip()
    return raw if raw else _DEFAULT_TARGET_FLAG


def _lookback_days() -> int:
    raw = os.environ.get(_ENV_LOOKBACK_DAYS, "")
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_LOOKBACK_DAYS
    if v < 1:
        return _DEFAULT_LOOKBACK_DAYS
    return min(v, _MAX_LOOKBACK_DAYS)


def _git_timeout_s() -> int:
    raw = os.environ.get(_ENV_GIT_TIMEOUT_S, "")
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_GIT_TIMEOUT_S
    if v < 1:
        return _DEFAULT_GIT_TIMEOUT_S
    return min(v, _MAX_GIT_TIMEOUT_S)


# ===========================================================================
# Canonical markers — derived, never hardcoded
# ===========================================================================


def _ov_marker() -> str:
    """The canonical O+V detection substring. Composed from
    ``auto_committer.ov_signature_substring()`` — never hardcoded."""
    try:
        from backend.core.ouroboros.governance.auto_committer import (
            ov_signature_substring,
        )

        return ov_signature_substring()
    except Exception:  # noqa: BLE001 — defensive
        # Fail-closed: an unresolvable marker means we cannot prove
        # evidence → caller treats as NO_GIT_HISTORY, never as READY.
        return ""


def _yellow_marker() -> str:
    """The Yellow-tier body marker, *derived* from the canonical
    ``RiskTier`` enum name (``f"Risk: {RiskTier.NOTIFY_APPLY.name}"``)
    — never a hardcoded literal."""
    try:
        from backend.core.ouroboros.governance.risk_engine import RiskTier

        return f"Risk: {RiskTier.NOTIFY_APPLY.name}"
    except Exception:  # noqa: BLE001
        return ""


def classify_commit_body(
    body: str, *, ov_marker: str, yellow_marker: str
) -> CommitEvidenceKind:
    """Pure classifier for one commit body. Deterministic."""
    if not ov_marker or ov_marker not in body:
        return CommitEvidenceKind.NOT_OV
    if yellow_marker and yellow_marker in body:
        return CommitEvidenceKind.YELLOW_TIER
    return CommitEvidenceKind.OTHER_TIER


# ===========================================================================
# git log composition (async, safe argv list, never shell)
# ===========================================================================


async def _read_git_log(
    *, since_epoch: float, timeout_s: int
) -> Optional[List[Tuple[str, float, str]]]:
    """Return ``[(hash, committer_epoch, body), ...]`` for commits since
    ``since_epoch``. ``None`` iff git is unavailable / unparseable
    (fail-closed → caller maps to NO_GIT_HISTORY). NEVER raises.

    Uses ``asyncio.create_subprocess_exec`` with a static argv list —
    never ``shell=True``; ``since_epoch`` is passed as a typed ISO arg,
    not interpolated into a shell string. (Repo standard, mirrors
    GitApplyDiffApplier.)
    """
    since_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.gmtime(max(0.0, since_epoch))
    )
    fmt = f"%H{_FLD_SEP}%ct{_FLD_SEP}%B{_REC_SEP}"
    args = [
        "git",
        "log",
        f"--since={since_iso}",
        f"--max-count={_MAX_GIT_LOG_COMMITS}",
        f"--format={fmt}",
        "--no-color",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception:  # noqa: BLE001 — git missing / exec failure
        logger.debug(
            "[AutoCommitGradGate] git exec failed", exc_info=True
        )
        return None
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(), timeout=float(timeout_s)
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        logger.debug("[AutoCommitGradGate] git log timed out")
        return None
    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        raise
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None

    try:
        text = out.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None

    records: List[Tuple[str, float, str]] = []
    for raw in text.split(_REC_SEP):
        raw = raw.strip("\n")
        if not raw:
            continue
        parts = raw.split(_FLD_SEP)
        if len(parts) < 3:
            continue
        chash = parts[0].strip()
        try:
            cepoch = float(parts[1].strip())
        except (TypeError, ValueError):
            continue
        body = parts[2]
        if chash:
            records.append((chash, cepoch, body))
    return records


# ===========================================================================
# Ledger composition — clean-soak windows (genuine per-soak attribution)
# ===========================================================================


def _clean_soak_windows(
    target_flag: str, *, lookback_days: int
) -> Optional[List[Tuple[str, float, float, bool]]]:
    """Return ``[(session_id, win_start, win_end, first_bounded), ...]``
    for each clean soak of ``target_flag``, oldest-first.

    Composes the canonical ``GraduationLedger`` row reader — never
    re-parses the ledger JSONL. ``None`` iff the ledger is unavailable.
    NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
            GraduationLedger,
            SessionOutcome,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[AutoCommitGradGate] graduation_ledger import failed",
            exc_info=True,
        )
        return None
    try:
        ledger = GraduationLedger()
        rows = ledger._read_all()  # canonical row reader (single source)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[AutoCommitGradGate] ledger read failed", exc_info=True
        )
        return None

    clean = sorted(
        (
            r
            for r in rows
            if getattr(r, "flag_name", "") == target_flag
            and getattr(r, "outcome", None) is SessionOutcome.CLEAN
        ),
        key=lambda r: float(getattr(r, "recorded_at_epoch", 0.0)),
    )
    # Dedup by session_id (mirrors ledger's unique-session semantics),
    # keeping earliest epoch per session.
    seen: Dict[str, float] = {}
    for r in clean:
        sid = str(getattr(r, "session_id", ""))
        ep = float(getattr(r, "recorded_at_epoch", 0.0))
        if sid and (sid not in seen or ep < seen[sid]):
            seen[sid] = ep
    ordered = sorted(seen.items(), key=lambda kv: kv[1])

    lookback_s = lookback_days * 86400.0
    windows: List[Tuple[str, float, float, bool]] = []
    for i, (sid, epoch) in enumerate(ordered):
        if i == 0:
            start = max(0.0, epoch - lookback_s)
            windows.append((sid, start, epoch, True))
        else:
            prev_epoch = ordered[i - 1][1]
            windows.append((sid, prev_epoch, epoch, False))
    return windows


def _ledger_progress(
    target_flag: str,
) -> Optional[Tuple[int, int, int, bool]]:
    """Compose ``GraduationLedger.progress`` + ``is_eligible``. Returns
    ``(clean, required, runner, eligible)`` or ``None``. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
            GraduationLedger,
        )

        ledger = GraduationLedger()
        prog = ledger.progress(target_flag)
        eligible = ledger.is_eligible(target_flag)
        return (
            int(prog.get("clean", 0)),
            int(prog.get("required", 0)),
            int(prog.get("runner", 0)),
            bool(eligible),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[AutoCommitGradGate] ledger progress failed", exc_info=True
        )
        return None


# ===========================================================================
# The gate
# ===========================================================================


def _master_off_report(target_flag: str) -> AutoCommitGraduationReport:
    return AutoCommitGraduationReport(
        schema_version=AUTOCOMMIT_GRAD_SCHEMA_VERSION,
        target_flag=target_flag,
        verdict=AutoCommitEvidenceVerdict.MASTER_OFF,
        ledger_clean_count=0,
        ledger_required=0,
        ledger_runner_count=0,
        ledger_eligible=False,
        soaks_with_evidence=0,
        soaks_missing_evidence=0,
        per_soak_evidence=(),
        diagnostic=(
            "gate master flag off — "
            f"{_ENV_MASTER}=false; no evidence computed"
        ),
        generated_at_unix=time.time(),
    )


async def evaluate_graduation_evidence() -> AutoCommitGraduationReport:
    """Compute the AutoCommitter graduation-evidence verdict.

    READY iff the generic clean-soak ledger is eligible AND every
    counted clean soak window contains ≥1 Yellow-tier O+V commit.
    NEVER raises — every failure path degrades to an honest non-READY
    verdict with a diagnostic.
    """
    target_flag = _target_flag()
    if not master_enabled():
        return _master_off_report(target_flag)

    prog = _ledger_progress(target_flag)
    if prog is None:
        return AutoCommitGraduationReport(
            schema_version=AUTOCOMMIT_GRAD_SCHEMA_VERSION,
            target_flag=target_flag,
            verdict=AutoCommitEvidenceVerdict.LEDGER_NOT_ELIGIBLE,
            ledger_clean_count=0,
            ledger_required=0,
            ledger_runner_count=0,
            ledger_eligible=False,
            soaks_with_evidence=0,
            soaks_missing_evidence=0,
            per_soak_evidence=(),
            diagnostic=(
                "graduation ledger unavailable — cannot establish "
                "clean-soak baseline; not ready (fail-closed)"
            ),
            generated_at_unix=time.time(),
        )
    clean_n, required, runner_n, eligible = prog

    if not eligible:
        return AutoCommitGraduationReport(
            schema_version=AUTOCOMMIT_GRAD_SCHEMA_VERSION,
            target_flag=target_flag,
            verdict=AutoCommitEvidenceVerdict.LEDGER_NOT_ELIGIBLE,
            ledger_clean_count=clean_n,
            ledger_required=required,
            ledger_runner_count=runner_n,
            ledger_eligible=False,
            soaks_with_evidence=0,
            soaks_missing_evidence=0,
            per_soak_evidence=(),
            diagnostic=(
                f"generic ledger not eligible: clean={clean_n} "
                f"required={required} runner={runner_n}. "
                "Clean-soak baseline must be met before "
                "AutoCommitter-specific evidence is evaluated."
            ),
            generated_at_unix=time.time(),
        )

    lookback = _lookback_days()
    windows = _clean_soak_windows(target_flag, lookback_days=lookback)
    if not windows:
        return AutoCommitGraduationReport(
            schema_version=AUTOCOMMIT_GRAD_SCHEMA_VERSION,
            target_flag=target_flag,
            verdict=AutoCommitEvidenceVerdict.EVIDENCE_INSUFFICIENT,
            ledger_clean_count=clean_n,
            ledger_required=required,
            ledger_runner_count=runner_n,
            ledger_eligible=True,
            soaks_with_evidence=0,
            soaks_missing_evidence=0,
            per_soak_evidence=(),
            diagnostic=(
                "ledger reports eligible but no clean-soak windows "
                "could be reconstructed from session rows — cannot "
                "prove AutoCommitter fired; not ready (fail-closed)"
            ),
            generated_at_unix=time.time(),
        )

    earliest_start = min(w[1] for w in windows)
    git_records = await _read_git_log(
        since_epoch=earliest_start, timeout_s=_git_timeout_s()
    )
    ov_marker = _ov_marker()
    yellow_marker = _yellow_marker()

    if git_records is None or not ov_marker or not yellow_marker:
        return AutoCommitGraduationReport(
            schema_version=AUTOCOMMIT_GRAD_SCHEMA_VERSION,
            target_flag=target_flag,
            verdict=AutoCommitEvidenceVerdict.NO_GIT_HISTORY,
            ledger_clean_count=clean_n,
            ledger_required=required,
            ledger_runner_count=runner_n,
            ledger_eligible=True,
            soaks_with_evidence=0,
            soaks_missing_evidence=len(windows),
            per_soak_evidence=(),
            diagnostic=(
                "git history / canonical markers unavailable — "
                "cannot prove Yellow-tier O+V commits; not ready "
                "(fail-closed). "
                f"ov_marker={'set' if ov_marker else 'MISSING'} "
                f"yellow_marker="
                f"{'set' if yellow_marker else 'MISSING'} "
                f"git={'ok' if git_records is not None else 'unavailable'}"
            ),
            generated_at_unix=time.time(),
        )

    per_soak: List[SoakCommitEvidence] = []
    for sid, w_start, w_end, first_bounded in windows:
        yellow = 0
        other = 0
        samples: List[str] = []
        for chash, cepoch, body in git_records:
            if not (w_start <= cepoch <= w_end):
                continue
            kind = classify_commit_body(
                body, ov_marker=ov_marker, yellow_marker=yellow_marker
            )
            if kind is CommitEvidenceKind.YELLOW_TIER:
                yellow += 1
                if len(samples) < _MAX_SAMPLE_HASHES:
                    samples.append(chash[:12])
            elif kind is CommitEvidenceKind.OTHER_TIER:
                other += 1
        per_soak.append(
            SoakCommitEvidence(
                session_id=sid,
                window_start_epoch=w_start,
                window_end_epoch=w_end,
                yellow_tier_count=yellow,
                other_tier_count=other,
                sample_yellow_hashes=tuple(samples),
                is_first_soak_bounded=first_bounded,
            )
        )

    with_ev = sum(1 for e in per_soak if e.has_evidence)
    missing_ev = len(per_soak) - with_ev

    if missing_ev == 0:
        verdict = AutoCommitEvidenceVerdict.READY
        diag = (
            f"READY: {clean_n}/{required} clean soaks, runner=0, and "
            f"every counted clean soak ({with_ev}/{len(per_soak)}) "
            "carries >=1 Yellow-tier O+V commit. AutoCommitter "
            "unattended-apply path is empirically exercised. "
            "Operator may flip with evidence."
        )
        if any(e.is_first_soak_bounded for e in per_soak):
            diag += (
                " NOTE: oldest soak's window start is lookback-bounded "
                f"({lookback}d) — its evidence is necessary-floor, not "
                "prior-bounded."
            )
    else:
        verdict = AutoCommitEvidenceVerdict.EVIDENCE_INSUFFICIENT
        bare = sorted(
            e.session_id for e in per_soak if not e.has_evidence
        )
        diag = (
            f"EVIDENCE_INSUFFICIENT: generic ledger eligible "
            f"(clean={clean_n}/{required}, runner={runner_n}) BUT "
            f"{missing_ev}/{len(per_soak)} counted clean soak(s) have "
            "ZERO Yellow-tier O+V commits — clean-soak-count alone "
            "does NOT demonstrate AutoCommitter fired. "
            f"Soaks missing evidence: {bare[:10]}. "
            "Graduating here would be a §92.16-class overclaim; "
            "not ready."
        )

    return AutoCommitGraduationReport(
        schema_version=AUTOCOMMIT_GRAD_SCHEMA_VERSION,
        target_flag=target_flag,
        verdict=verdict,
        ledger_clean_count=clean_n,
        ledger_required=required,
        ledger_runner_count=runner_n,
        ledger_eligible=True,
        soaks_with_evidence=with_ev,
        soaks_missing_evidence=missing_ev,
        per_soak_evidence=tuple(per_soak),
        diagnostic=diag,
        generated_at_unix=time.time(),
    )


async def evidence_summary() -> Dict[str, Any]:
    """One-call aggregate for operator surfaces. NEVER raises.

    ``meets_evidence_gate`` is the single boolean an operator / REPL /
    SSE surface reads before an evidence-driven flag decision.
    """
    try:
        rep = await evaluate_graduation_evidence()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug(
            "[AutoCommitGradGate] evidence_summary failed",
            exc_info=True,
        )
        return {
            "schema_version": AUTOCOMMIT_GRAD_SCHEMA_VERSION,
            "verdict": AutoCommitEvidenceVerdict.NO_GIT_HISTORY.value,
            "meets_evidence_gate": False,
            "diagnostic": "evidence evaluation raised — fail-closed",
        }
    return {
        "schema_version": AUTOCOMMIT_GRAD_SCHEMA_VERSION,
        "target_flag": rep.target_flag,
        "verdict": rep.verdict.value,
        "meets_evidence_gate": rep.verdict
        is AutoCommitEvidenceVerdict.READY,
        "ledger_clean_count": rep.ledger_clean_count,
        "ledger_required": rep.ledger_required,
        "soaks_with_evidence": rep.soaks_with_evidence,
        "soaks_missing_evidence": rep.soaks_missing_evidence,
        "diagnostic": rep.diagnostic,
    }


def render_report_json(report: AutoCommitGraduationReport) -> str:
    """Deterministic JSON render. NEVER raises."""
    try:
        return json.dumps(
            report.to_dict(), sort_keys=True, default=str
        )
    except Exception:  # noqa: BLE001
        return json.dumps(
            {
                "schema_version": AUTOCOMMIT_GRAD_SCHEMA_VERSION,
                "verdict": "no_git_history",
                "diagnostic": "render failed",
            }
        )


# ===========================================================================
# AST-pinned shipped invariants (auto-discovered via §33.3)
# ===========================================================================


def register_shipped_invariants() -> list:
    """Return AST invariant pins. Auto-discovered via §33.3."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "auto_commit_graduation_gate.py"
    )

    _EXPECTED_KIND = {"yellow_tier", "other_tier", "not_ov"}
    _EXPECTED_VERDICT = {
        "ready",
        "ledger_not_eligible",
        "evidence_insufficient",
        "no_git_history",
        "master_off",
    }

    def _enum_values(tree: ast.AST, class_name: str) -> set:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == class_name
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                return found
        return set()

    def _mk_taxonomy(class_name: str, expected: set):
        def _validate(tree: ast.AST, source: str) -> tuple:  # noqa: ARG001
            found = _enum_values(tree, class_name)
            if not found:
                return (f"{class_name} class not found",)
            missing = expected - found
            extra = found - expected
            if missing:
                return (f"{class_name} missing: {sorted(missing)}",)
            if extra:
                return (f"{class_name} drift: {sorted(extra)}",)
            return ()

        return _validate

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy_engine",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.candidate_generator",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for f in forbidden:
                    if mod == f or mod.startswith(f + "."):
                        return (f"forbidden import: {mod}",)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden:
                        return (
                            f"forbidden import: {alias.name}",
                        )
        return ()

    def _validate_no_shell_subprocess(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        # git access MUST be the safe argv-list primitive — never a
        # shell variant. Detect forbidden call attrs structurally.
        banned_attrs = (
            "create_subprocess_shell",
            "system",
            "popen",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                attr = getattr(node.func, "attr", "")
                if attr in banned_attrs:
                    return (f"forbidden shell call: {attr}",)
                for kw in getattr(node, "keywords", []):
                    if (
                        kw.arg == "shell"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                    ):
                        return ("forbidden shell=True",)
        return ()

    def _validate_no_hardcoded_markers(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        # The O+V + Yellow markers MUST be derived
        # (ov_signature_substring / RiskTier.NOTIFY_APPLY.name) in
        # OPERATIONAL code, never hardcoded literals. Documentation
        # prose (docstrings) legitimately names the marker to explain
        # the design — a docstring is not a logic hazard, so the pin
        # is scoped to non-docstring string constants only (precision,
        # not weakening).
        # Sentinels are split so this validator's own literals do not
        # self-match during the AST walk (same technique the codebase's
        # other self-scanning pins use).
        sentinel_ov = "Ouroboros+Venom " + "[O+V]"
        sentinel_risk = "Risk: NOTIFY" + "_APPLY"

        docstring_ids = set()
        for parent in ast.walk(tree):
            if isinstance(
                parent,
                (
                    ast.Module,
                    ast.ClassDef,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                ),
            ):
                body = getattr(parent, "body", [])
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstring_ids.add(id(body[0].value))

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_ids
            ):
                v = node.value
                if sentinel_ov in v or sentinel_risk in v:
                    return (
                        "hardcoded canonical marker literal in "
                        "operational code — must derive from "
                        "ov_signature_substring() / "
                        "RiskTier.NOTIFY_APPLY.name",
                    )
        return ()

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "get"
                        and len(sub.args) >= 2
                        and isinstance(sub.args[1], ast.Constant)
                    ):
                        if (
                            str(sub.args[1].value).strip().lower()
                            in _TRUTHY
                        ):
                            return (
                                "master_enabled default truthy — "
                                "33.1 requires default-FALSE",
                            )
                        return ()
                return ("master_enabled env-get default not found",)
        return ("master_enabled function not found",)

    return [
        ShippedCodeInvariant(
            invariant_name="autocommit_grad_kind_taxonomy_closed",
            target_file=target,
            description=(
                "CommitEvidenceKind is a closed 3-value taxonomy."
            ),
            validate=_mk_taxonomy(
                "CommitEvidenceKind", _EXPECTED_KIND
            ),
        ),
        ShippedCodeInvariant(
            invariant_name="autocommit_grad_verdict_taxonomy_closed",
            target_file=target,
            description=(
                "AutoCommitEvidenceVerdict is a closed 5-value "
                "taxonomy."
            ),
            validate=_mk_taxonomy(
                "AutoCommitEvidenceVerdict", _EXPECTED_VERDICT
            ),
        ),
        ShippedCodeInvariant(
            invariant_name="autocommit_grad_authority_asymmetry",
            target_file=target,
            description=(
                "Read-only measurement substrate — MUST NOT import "
                "orchestrator / iron_gate / policy_engine / "
                "change_engine / candidate_generator."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="autocommit_grad_no_shell_subprocess",
            target_file=target,
            description=(
                "git access via the safe argv-list primitive only "
                "— never a shell variant / system / popen."
            ),
            validate=_validate_no_shell_subprocess,
        ),
        ShippedCodeInvariant(
            invariant_name="autocommit_grad_no_hardcoded_markers",
            target_file=target,
            description=(
                "O+V + Yellow markers derived from "
                "ov_signature_substring() / RiskTier.NOTIFY_APPLY"
                ".name — never hardcoded literals."
            ),
            validate=_validate_no_hardcoded_markers,
        ),
        ShippedCodeInvariant(
            invariant_name="autocommit_grad_master_default_false",
            target_file=target,
            description=(
                "33.1 measurement substrate — master flag ships "
                "default-FALSE pending Phase 9 graduation."
            ),
            validate=_validate_master_default_false,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds (auto-discovered via §33.3 naming-cage)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Register this module's env knobs. Auto-discovered."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
            Relevance,
        )
    except Exception:  # noqa: BLE001
        return 0

    src = (
        "backend/core/ouroboros/governance/"
        "auto_commit_graduation_gate.py"
    )
    specs = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master kill switch for the AutoCommitter "
                "graduation-evidence gate (PRD 41.11.4, "
                "prerequisite for 41.11.1). 33.1 measurement "
                "substrate — ships default-FALSE. Master-off -> "
                "MASTER_OFF report, zero side effects. The gate "
                "NEVER flips JARVIS_AUTO_COMMIT_ENABLED; it only "
                "makes that operator decision evidence-driven."
            ),
            category=Category.SAFETY,
            source_file=src,
            example="true",
            since="v1.0",
            posture_relevance={"HARDEN": Relevance.CRITICAL},
        ),
        FlagSpec(
            name=_ENV_TARGET_FLAG,
            type=FlagType.STR,
            default=_DEFAULT_TARGET_FLAG,
            description=(
                "The flag whose graduation evidence is evaluated. "
                "Default JARVIS_AUTO_COMMIT_ENABLED (already "
                "default-TRUE in auto_committer.py — this gate "
                "proves the unattended-apply evidence the generic "
                "ledger cannot)."
            ),
            category=Category.TUNING,
            source_file=src,
            example="JARVIS_AUTO_COMMIT_ENABLED",
            since="v1.0",
        ),
        FlagSpec(
            name=_ENV_LOOKBACK_DAYS,
            type=FlagType.INT,
            default=_DEFAULT_LOOKBACK_DAYS,
            description=(
                "Bounded lookback (days) for the OLDEST clean "
                "soak's window start (it has no prior clean-soak "
                "bound). Clamped to [1, 365]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example="30",
            since="v1.0",
        ),
        FlagSpec(
            name=_ENV_GIT_TIMEOUT_S,
            type=FlagType.INT,
            default=_DEFAULT_GIT_TIMEOUT_S,
            description=(
                "Hard timeout (seconds) for the git-log evidence "
                "scan. Timeout -> NO_GIT_HISTORY (fail-closed, "
                "never READY). Clamped to [1, 300]."
            ),
            category=Category.TIMING,
            source_file=src,
            example="30",
            since="v1.0",
        ),
    ]
    n = 0
    for spec in specs:
        try:
            registry.register(spec)
            n += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[AutoCommitGradGate] flag register failed: %s",
                spec.name,
                exc_info=True,
            )
    return n
