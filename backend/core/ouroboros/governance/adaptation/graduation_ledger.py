"""Item #4 — graduation ledger for per-loader cadence tracking.

Phase 7 + Items #2/#3 shipped 12+ master flags that default to
``false`` until per-loader 3-clean-session cadences (Pass B
discipline) or 5-clean-session cadences (Pass C discipline) flip
them to ``true``. This module codifies the cadence machinery.

## What "clean" means

A "clean" session is a battle-test session that ran with the
target master flag explicitly set to ``"1"`` AND completed without
any RUNNER-attributed failures. Infra failures (OOM, TLS errors,
network flakes) are waived per the Wave 1 closure ledger
(``feedback_wave_1_closure_and_slice5_policy.md``).

This module does NOT execute sessions itself — it tracks
operator-recorded session outcomes. The operator (or a scheduled
agent per ``feedback_agent_conducted_soak_delegation.md``) runs
sessions externally; this module records what happened + tells
the operator which flags are eligible to flip.

## Append-only JSONL audit log

Path: ``.jarvis/graduation_ledger.jsonl``. Each row records ONE
session outcome:

```jsonl
{"flag_name": "...", "session_id": "...", "outcome": "clean|infra|runner",
 "recorded_at": "...", "recorded_by": "...", "notes": "..."}
```

State queries (``progress(flag_name)``, ``eligible_flags()``)
reduce the log to per-flag clean-session counts. Master-flag flips
themselves are made by the operator editing source — this module
just SIGNALS readiness.

## Per-flag cadence policy

The policy table below pins, for each known flag:
  * ``required_clean_sessions``: 3 (Pass B / Phase 7 default) or
    5 (Pass C — higher bar per Pass C §4.6)
  * ``cadence_class``: 'pass_b' or 'pass_c'
  * ``description``: human-readable

Adding a new flag = single-line entry in ``CADENCE_POLICY``.

## Default-off

``JARVIS_GRADUATION_LEDGER_ENABLED`` (default false). When off,
``record_session()`` is a no-op + ``progress()`` returns 0.
"""
from __future__ import annotations

import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Hard cap on the ledger file size (defends against operator-typo
# session_id storms bloating the file).
MAX_LEDGER_FILE_BYTES: int = 4 * 1024 * 1024

# Cap on records loaded per `progress()` call (defense against
# pathological reads).
MAX_RECORDS_LOADED: int = 50_000

# Cap on the per-flag clean-session counter return value (just
# defensive — real counts will always be small).
MAX_CLEAN_COUNT: int = 1_000

# Cap on free-form notes per session record.
MAX_NOTES_CHARS: int = 1_000


def is_ledger_enabled() -> bool:
    """Master flag — ``JARVIS_GRADUATION_LEDGER_ENABLED``
    (default false)."""
    return os.environ.get(
        "JARVIS_GRADUATION_LEDGER_ENABLED", "",
    ).strip().lower() in _TRUTHY


def ledger_path() -> Path:
    """Return the ledger path. Env-overridable via
    ``JARVIS_GRADUATION_LEDGER_PATH``; defaults to
    ``.jarvis/graduation_ledger.jsonl`` under cwd."""
    raw = os.environ.get("JARVIS_GRADUATION_LEDGER_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "graduation_ledger.jsonl"


# ---------------------------------------------------------------------------
# Outcome enum
# ---------------------------------------------------------------------------


class SessionOutcome(str, enum.Enum):
    """Per-session outcome contributing to (or skipped from) the
    clean-session count."""

    CLEAN = "clean"           # contributes to required_clean_sessions
    INFRA = "infra"           # waived (OOM / TLS / network flake)
    RUNNER = "runner"         # runner-attributed; resets confidence
    MIGRATION = "migration"   # transient setup change; waived


# ---------------------------------------------------------------------------
# Cadence policy
# ---------------------------------------------------------------------------


class CadenceClass(str, enum.Enum):
    PASS_B = "pass_b"   # Phase 7 + Items 2/3 default — 3 clean
    PASS_C = "pass_c"   # Pass C surfaces — 5 clean (higher bar)


@dataclass(frozen=True)
class CadencePolicyEntry:
    """One flag's cadence policy. Frozen — pinned by source-grep
    + tests so the policy table cannot drift silently."""

    flag_name: str
    required_clean_sessions: int
    cadence_class: CadenceClass
    description: str


# ---------------------------------------------------------------------------
# CANONICAL CADENCE POLICY — the 12+ flags from Phase 7 + Items 2/3
# Adding a new flag = single-line entry below. Removing requires
# operator approval (graduation history may exist for the flag).
# ---------------------------------------------------------------------------


CADENCE_POLICY: Tuple[CadencePolicyEntry, ...] = (
    # Phase 7.1 — SemanticGuardian adapted patterns
    CadencePolicyEntry(
        flag_name="JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Phase 7.1 — SemanticGuardian boot-time adapted-pattern loader"
        ),
    ),
    # Phase 7.2 — IronGate adapted floors
    CadencePolicyEntry(
        flag_name="JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 7.2 — IronGate adapted-floor boot-time loader",
    ),
    # Phase 7.3 — ScopedToolBackend per-Order budget
    CadencePolicyEntry(
        flag_name="JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Phase 7.3 — ScopedToolBackend adapted per-Order budget loader"
        ),
    ),
    # Phase 7.4 — Risk-tier ladder adapted extensions
    CadencePolicyEntry(
        flag_name="JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 7.4 — risk-tier ladder adapted-tier loader",
    ),
    # Phase 7.5 — Category-weight rebalance
    CadencePolicyEntry(
        flag_name="JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 7.5 — ExplorationLedger category-weight loader",
    ),
    # Phase 7.6 — HypothesisProbe primitive
    CadencePolicyEntry(
        flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 7.6 — bounded HypothesisProbe primitive",
    ),
    # Phase 7.9 — Stale-pattern sunset detector (Pass C surface)
    CadencePolicyEntry(
        flag_name="JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description="Phase 7.9 — StalePatternDetector sunset signal",
    ),
    # Item #2 — MetaGovernor YAML writer
    CadencePolicyEntry(
        flag_name="JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Item #2 — MetaGovernor YAML writer (/adapt approve writes YAML)"
        ),
    ),
    # Item #3 — Production EvidenceProber
    CadencePolicyEntry(
        flag_name="JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Item #3 — AnthropicVenomEvidenceProber (production prober)"
        ),
    ),
    # Item #3 — Bridges
    CadencePolicyEntry(
        flag_name="JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Item #3 — bridges (CONFIRMED→AdaptationLedger; "
            "terminal→HypothesisLedger)"
        ),
    ),
    # 5 Pass C mining surfaces (substrate flags — graduate after
    # callers wire payload). Higher bar per Pass C §4.6 = 5 clean.
    CadencePolicyEntry(
        flag_name="JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description=(
            "Pass C Slice 2 — SemanticGuardian POSTMORTEM-mined patterns"
        ),
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description=(
            "Pass C Slice 3 — IronGate exploration-floor auto-tightener"
        ),
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description="Pass C Slice 4a — per-Order mutation budget calibrator",
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description="Pass C Slice 4b — risk-tier ladder extender",
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description="Pass C Slice 5 — category-weight rebalancer",
    ),
)


_POLICY_BY_FLAG: Dict[str, CadencePolicyEntry] = {
    e.flag_name: e for e in CADENCE_POLICY
}


def get_policy(flag_name: str) -> Optional[CadencePolicyEntry]:
    """Return the cadence policy for ``flag_name`` or None if unknown."""
    return _POLICY_BY_FLAG.get(flag_name)


def known_flags() -> FrozenSet[str]:
    """Return the set of all flags governed by this ledger."""
    return frozenset(_POLICY_BY_FLAG.keys())


# ---------------------------------------------------------------------------
# Session record + persistence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRecord:
    """One ledger row. Frozen — append-only history."""

    flag_name: str
    session_id: str
    outcome: SessionOutcome
    recorded_at_iso: str
    recorded_at_epoch: float
    recorded_by: str
    notes: str = ""

    def to_dict(self) -> Dict:
        return {
            "flag_name": self.flag_name,
            "session_id": self.session_id,
            "outcome": self.outcome.value,
            "recorded_at_iso": self.recorded_at_iso,
            "recorded_at_epoch": self.recorded_at_epoch,
            "recorded_by": self.recorded_by,
            "notes": self.notes,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class GraduationLedger:
    """Append-only JSONL ledger of per-flag session outcomes.

    Best-effort — every public method NEVER raises. Same discipline
    as AdaptationLedger.
    """

    path: Path = field(default_factory=ledger_path)

    # ----- write -----

    def record_session(
        self,
        *,
        flag_name: str,
        session_id: str,
        outcome: SessionOutcome,
        recorded_by: str,
        notes: str = "",
    ) -> Tuple[bool, str]:
        """Append ONE session outcome. Returns ``(ok, detail)``.

        Pre-checks:
          1. Master flag off → (False, "master_off")
          2. flag_name not in known_flags → (False, "unknown_flag")
          3. session_id empty → (False, "empty_session_id")

        NEVER raises.
        """
        if not is_ledger_enabled():
            return (False, "master_off")
        flag_clean = (flag_name or "").strip()
        if flag_clean not in _POLICY_BY_FLAG:
            return (False, f"unknown_flag:{flag_clean}")
        sid = (session_id or "").strip()
        if not sid:
            return (False, "empty_session_id")
        recorded_by_clean = (recorded_by or "").strip()[:120] or "unknown"
        notes_clean = (notes or "")[:MAX_NOTES_CHARS]
        record = SessionRecord(
            flag_name=flag_clean,
            session_id=sid,
            outcome=outcome,
            recorded_at_iso=_utc_now_iso(),
            recorded_at_epoch=time.time(),
            recorded_by=recorded_by_clean,
            notes=notes_clean,
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "[GraduationLedger] mkdir failed: %s", exc,
            )
            return (False, f"mkdir_failed:{exc}")
        try:
            line = json.dumps(record.to_dict(), separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            return (False, f"serialize_failed:{exc}")
        try:
            with self.path.open("a", encoding="utf-8") as f:
                # Reuse Phase 7.8's flock for cross-process safety.
                from backend.core.ouroboros.governance.adaptation._file_lock import (  # noqa: E501
                    flock_exclusive,
                )
                with flock_exclusive(f.fileno()):
                    f.write(line)
                    f.write("\n")
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
        except OSError as exc:
            return (False, f"append_failed:{exc}")
        logger.info(
            "[GraduationLedger] flag=%s session=%s outcome=%s by=%s",
            flag_clean, sid, outcome.value, recorded_by_clean,
        )
        return (True, "ok")

    # ----- read -----

    def _read_all(self) -> List[SessionRecord]:
        """Read every record. Bounded + fail-open."""
        if not self.path.exists():
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size > MAX_LEDGER_FILE_BYTES:
            logger.warning(
                "[GraduationLedger] %s exceeds MAX_LEDGER_FILE_BYTES=%d "
                "(was %d) — refusing to load",
                self.path, MAX_LEDGER_FILE_BYTES, size,
            )
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out: List[SessionRecord] = []
        for line in text.splitlines():
            if len(out) >= MAX_RECORDS_LOADED:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            try:
                outcome = SessionOutcome(str(obj.get("outcome") or ""))
            except ValueError:
                continue
            out.append(SessionRecord(
                flag_name=str(obj.get("flag_name") or ""),
                session_id=str(obj.get("session_id") or ""),
                outcome=outcome,
                recorded_at_iso=str(obj.get("recorded_at_iso") or ""),
                recorded_at_epoch=float(obj.get("recorded_at_epoch") or 0.0),
                recorded_by=str(obj.get("recorded_by") or ""),
                notes=str(obj.get("notes") or ""),
            ))
        return out

    def progress(self, flag_name: str) -> Dict[str, int]:
        """Return per-flag counts: clean / infra / runner / migration /
        unique_sessions / required.

        Master-off → all zeros (best-effort).

        Counts UNIQUE session_ids per outcome — the same session
        recorded twice (e.g. operator double-tap) counts once.
        """
        if not is_ledger_enabled():
            return _zero_progress(flag_name)
        policy = _POLICY_BY_FLAG.get(flag_name)
        if policy is None:
            return _zero_progress(flag_name)
        counts = {
            "clean": 0, "infra": 0, "runner": 0, "migration": 0,
            "unique_sessions": 0, "required": policy.required_clean_sessions,
        }
        seen_per_outcome: Dict[str, set] = {
            "clean": set(), "infra": set(),
            "runner": set(), "migration": set(),
        }
        all_sessions: set = set()
        for r in self._read_all():
            if r.flag_name != flag_name:
                continue
            all_sessions.add(r.session_id)
            bucket = seen_per_outcome.get(r.outcome.value)
            if bucket is None:
                continue
            if r.session_id in bucket:
                continue
            bucket.add(r.session_id)
        for k in ("clean", "infra", "runner", "migration"):
            counts[k] = min(len(seen_per_outcome[k]), MAX_CLEAN_COUNT)
        counts["unique_sessions"] = min(
            len(all_sessions), MAX_CLEAN_COUNT,
        )
        return counts

    def is_eligible(self, flag_name: str) -> bool:
        """True iff the flag has reached its required clean-session
        count AND has zero runner-attributed failures."""
        progress = self.progress(flag_name)
        return (
            progress["clean"] >= progress["required"]
            and progress["runner"] == 0
        )

    def eligible_flags(self) -> List[str]:
        """Return all flag_names eligible to flip."""
        return sorted(
            f for f in _POLICY_BY_FLAG if self.is_eligible(f)
        )

    def all_progress(self) -> Dict[str, Dict[str, int]]:
        """Return progress for every known flag — useful for
        operator overview rendering."""
        return {
            f: self.progress(f) for f in sorted(_POLICY_BY_FLAG)
        }


def _zero_progress(flag_name: str) -> Dict[str, int]:
    policy = _POLICY_BY_FLAG.get(flag_name)
    return {
        "clean": 0, "infra": 0, "runner": 0, "migration": 0,
        "unique_sessions": 0,
        "required": policy.required_clean_sessions if policy else 3,
    }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_DEFAULT_LEDGER: Optional[GraduationLedger] = None


def get_default_ledger() -> GraduationLedger:
    global _DEFAULT_LEDGER
    if _DEFAULT_LEDGER is None:
        _DEFAULT_LEDGER = GraduationLedger()
    return _DEFAULT_LEDGER


def reset_default_ledger() -> None:
    """Test-only: reset the singleton."""
    global _DEFAULT_LEDGER
    _DEFAULT_LEDGER = None


__all__ = [
    "CADENCE_POLICY",
    "CadenceClass",
    "CadencePolicyEntry",
    "GraduationLedger",
    "MAX_CLEAN_COUNT",
    "MAX_LEDGER_FILE_BYTES",
    "MAX_NOTES_CHARS",
    "MAX_RECORDS_LOADED",
    "SessionOutcome",
    "SessionRecord",
    "get_default_ledger",
    "get_policy",
    "is_ledger_enabled",
    "known_flags",
    "ledger_path",
    "reset_default_ledger",
]
