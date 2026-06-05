"""
Mirror-Self Spec-Drift Validator
=================================

Closes the spec-drift half of §40 #14 — the "Mirror-Self Test".
The metacognition-calibration half (prediction × actual) already
ships in :mod:`mirror_self_test`. The PRD §40 #14 binding also
requires:

  "the system reproduces its own canonical behavior from the
   CLAUDE.md spec; if implementation drifts from spec, file
   repair ops."

This substrate is a **pure read-only validator** that compares
what CLAUDE.md *CLAIMS* an env-flag default is against what the
:class:`flag_registry.FlagRegistry` *ACTUALLY* registers. A
mismatch is **spec drift** — the system testing itself against
its own documentation. Detection only — it NEVER mutates
CLAUDE.md or any flag; it files an *advisory* repair op through
the existing invariant-drift pipeline.

The machine-checkable claim surface
-----------------------------------

CLAUDE.md documents env-flag defaults in a handful of consistent,
parseable phrasings:

  * ``\`JARVIS_FOO_ENABLED\` default-TRUE``
  * ``\`JARVIS_FOO_ENABLED\` default-FALSE``
  * ``\`JARVIS_FOO_ENABLED\` (default \`true\`)``
  * ``\`JARVIS_FOO_ENABLED\` default \`false\```

:func:`_parse_claimed_flag_defaults` regex-extracts ONLY these
unambiguous boolean claims. A flag claimed with *conflicting*
defaults in different places is marked ambiguous and EXCLUDED
(fail-open — never emit a false-positive drift on a doc the
parser can't read unambiguously).

Composition — no parallel machinery
-----------------------------------

* :class:`flag_registry.FlagRegistry` — the canonical
  ``ensure_seeded()`` registry is the authority on ACTUAL
  defaults. We never duplicate flag metadata.
* :mod:`invariant_drift_auditor` — spec-drift records convert to
  :class:`InvariantDriftRecord` (with the new ``DriftKind.SPEC_DRIFT``
  member) so they flow through the **existing**
  :class:`invariant_drift_auto_action_bridge.InvariantDriftAutoActionBridge`.
  No new observer, no new auto-action bridge, no duplicated drift
  machinery — :func:`to_invariant_drift_records` +
  :func:`to_bridge_snapshot` are the only adapter surface.
* :mod:`second_order_doll_metric` (#15) — composes
  ``aggregate_doll_completion().completion_ratio`` as a *severity
  gate*: spec drift is WARNING by default and only escalates to
  CRITICAL once the organism has empirical governance-stability
  evidence (completion ratio ≥ the gate ratio). This avoids
  early-autonomy thrashing where every spec edit trips a CRITICAL.

NEVER raises. Unreadable CLAUDE.md / missing registry / disabled
master / doll metric unavailable all degrade to an empty or
conservative report — never an exception.

Closed 4-value :class:`SpecDriftVerdict`:

  ALIGNED            claims parsed; zero drift records
  DRIFTED            ≥1 claimed default ≠ registry actual default
  INSUFFICIENT_DATA  no parseable boolean claims found
  DISABLED           master flag off (§33.1)

§33.1 cognitive substrate ``JARVIS_MIRROR_SELF_SPEC_DRIFT_ENABLED``
default-**FALSE** — operator-paced opt-in. Sub-knob
``JARVIS_MIRROR_SELF_SPEC_DRIFT_DOLL_GATE_RATIO`` (float, default
0.75) is the completion-ratio escalation threshold.

Authority asymmetry (AST-pinned): imports stdlib only at
module-load. ``flag_registry`` / ``invariant_drift_auditor`` /
``second_order_doll_metric`` are all lazy-imported behind composer
helpers. Does NOT import orchestrator / iron_gate / policy /
providers / candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor.
"""
from __future__ import annotations

import ast
import enum
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

MIRROR_SELF_SPEC_DRIFT_SCHEMA_VERSION: str = "mirror_self_spec_drift.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_MIRROR_SELF_SPEC_DRIFT_ENABLED"
_ENV_DOLL_GATE_RATIO = "JARVIS_MIRROR_SELF_SPEC_DRIFT_DOLL_GATE_RATIO"

_DEFAULT_DOLL_GATE_RATIO = 0.75

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced opt-in. When off, :func:`detect_spec_drift`
    returns a DISABLED report with zero records.
    """
    return _flag(_ENV_MASTER, default=False)


def doll_gate_ratio() -> float:
    """Completion-ratio threshold at/above which spec drift escalates
    from WARNING to CRITICAL. Defaults to 0.75. Clamped to [0.0, 1.0].
    NEVER raises."""
    raw = os.environ.get(_ENV_DOLL_GATE_RATIO, "").strip()
    if not raw:
        return _DEFAULT_DOLL_GATE_RATIO
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_DOLL_GATE_RATIO
    return max(0.0, min(1.0, val))


# ===========================================================================
# Closed taxonomy
# ===========================================================================


class SpecDriftVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    ALIGNED = "aligned"
    DRIFTED = "drifted"
    INSUFFICIENT_DATA = "insufficient_data"
    DISABLED = "disabled"


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class SpecDriftRecord:
    """One CLAUDE.md claim that contradicts the registry's actual
    default for the same flag."""

    flag: str
    claimed_default: bool
    actual_default: bool
    source_file: str
    severity: Any  # invariant_drift_auditor.DriftSeverity (lazy enum)
    schema_version: str = MIRROR_SELF_SPEC_DRIFT_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flag": self.flag,
            "claimed_default": bool(self.claimed_default),
            "actual_default": bool(self.actual_default),
            "source_file": self.source_file,
            "severity": getattr(
                self.severity, "value", str(self.severity),
            ),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class SpecDriftReport:
    """Aggregate of one spec-vs-registry validation pass."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: SpecDriftVerdict
    records: Tuple[SpecDriftRecord, ...]
    claims_evaluated: int
    unregistered_count: int
    completion_ratio: Optional[float]
    diagnostic: str
    schema_version: str = MIRROR_SELF_SPEC_DRIFT_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "records": [r.to_dict() for r in self.records],
            "claims_evaluated": int(self.claims_evaluated),
            "unregistered_count": int(self.unregistered_count),
            "completion_ratio": self.completion_ratio,
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Parser — the machine-checkable claim surface
# ===========================================================================


# A flag token: JARVIS_ followed by uppercase/digit/underscore. We only
# ever treat JARVIS_-prefixed identifiers as flags to avoid matching
# prose. The flag may be backtick-wrapped in the doc.
_FLAG_RE = r"JARVIS_[A-Z0-9_]+"

# Form 1+2: ``FLAG`` default-TRUE / default-FALSE  (case-insensitive)
_DASH_RE = re.compile(
    r"`?(" + _FLAG_RE + r")`?\s+default-(true|false)\b",
    re.IGNORECASE,
)

# Form 3+4: ``FLAG`` (default `true`)  /  ``FLAG`` default `false`
# Allows an optional "(" and requires the value in backticks.
_BACKTICK_RE = re.compile(
    r"`?(" + _FLAG_RE + r")`?\s+\(?default\s+`(true|false)`",
    re.IGNORECASE,
)


def _parse_claimed_flag_defaults(spec_text: Any) -> Mapping[str, bool]:
    """Regex-extract ``(JARVIS_FLAG -> claimed_bool)`` from CLAUDE.md's
    consistent default phrasings.

    Only unambiguous boolean claims are returned. A flag claimed with
    *conflicting* defaults anywhere in the text is dropped (fail-open).
    Pure. NEVER raises.
    """
    try:
        text = spec_text if isinstance(spec_text, str) else ""
    except Exception:  # noqa: BLE001
        return {}
    if not text:
        return {}

    # flag -> set of claimed bools seen (so we can detect conflict)
    seen: Dict[str, set] = {}
    try:
        for rx in (_DASH_RE, _BACKTICK_RE):
            for m in rx.finditer(text):
                flag = m.group(1)
                claimed = m.group(2).strip().lower() == "true"
                seen.setdefault(flag, set()).add(claimed)
    except Exception:  # noqa: BLE001 — defensive
        return {}

    out: Dict[str, bool] = {}
    for flag, vals in seen.items():
        if len(vals) == 1:  # unambiguous
            out[flag] = next(iter(vals))
        # len > 1 → conflicting claims → ambiguous → exclude
    return out


# ===========================================================================
# Composers — canonical surfaces (all lazy-imported)
# ===========================================================================


def _read_default_spec_text() -> str:
    """Read the repo-root CLAUDE.md. Resolves the repo root
    structurally (this module lives at
    ``backend/core/ouroboros/governance/`` → repo root is
    ``parents[4]``). Returns "" if unreadable. NEVER raises."""
    try:
        repo_root = Path(__file__).resolve().parents[4]
        target = repo_root / "CLAUDE.md"
        if not target.exists():
            return ""
        return target.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _resolve_registry(registry: Optional[Any]) -> Optional[Any]:
    """Resolve the canonically-populated FlagRegistry when the caller
    passes None. Composes ``flag_registry.ensure_seeded()`` — the same
    surface the unified /help + observability surfaces use. NEVER
    raises."""
    if registry is not None:
        return registry
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            ensure_seeded,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        return ensure_seeded()
    except Exception:  # noqa: BLE001 — defensive
        return None


def _doll_completion_ratio() -> Optional[float]:
    """Compose #15 ``second_order_doll_metric.aggregate_doll_completion``
    to read the governance-stability completion ratio. Returns None when
    the doll metric is disabled / unavailable (caller stays at the
    conservative WARNING severity). Pure read. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.second_order_doll_metric import (  # noqa: E501
            aggregate_doll_completion,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        snap = aggregate_doll_completion()
    except Exception:  # noqa: BLE001 — defensive
        return None
    if snap is None or not getattr(snap, "master_enabled", False):
        return None
    try:
        return float(getattr(snap, "completion_ratio", 0.0))
    except Exception:  # noqa: BLE001 — defensive
        return None


def _severity_for(completion_ratio: Optional[float]) -> Any:
    """Doll-gated severity. WARNING by default, escalates to CRITICAL
    only when the empirical completion ratio ≥ the gate ratio. Lazy
    import of the canonical DriftSeverity. NEVER raises."""
    from backend.core.ouroboros.governance.invariant_drift_auditor import (
        DriftSeverity,
    )
    if completion_ratio is None:
        return DriftSeverity.WARNING
    try:
        if completion_ratio >= doll_gate_ratio():
            return DriftSeverity.CRITICAL
    except Exception:  # noqa: BLE001 — defensive
        return DriftSeverity.WARNING
    return DriftSeverity.WARNING


def _lookup_spec(registry: Any, flag: str) -> Optional[Any]:
    """Best-effort FlagSpec lookup. NEVER raises."""
    try:
        getter = getattr(registry, "get_spec", None)
        if getter is None:
            return None
        return getter(flag)
    except Exception:  # noqa: BLE001 — defensive
        return None


def _spec_is_bool(spec: Any) -> bool:
    """True iff the FlagSpec's type is BOOL. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            FlagType,
        )
        return getattr(spec, "type", None) is FlagType.BOOL
    except Exception:  # noqa: BLE001 — defensive
        return False


# ===========================================================================
# Detection
# ===========================================================================


def detect_spec_drift(
    spec_text: Optional[str] = None,
    registry: Optional[Any] = None,
    *,
    now_unix: Optional[float] = None,
) -> SpecDriftReport:
    """Validate CLAUDE.md's claimed env-flag defaults against the
    FlagRegistry's actual registered defaults.

    A drift record is emitted ONLY when the flag IS registered AND its
    type is BOOL AND ``actual_default != claimed_default``. Absent /
    non-bool / ambiguous claims are SKIPPED (no false positive).

    Detection only — never mutates CLAUDE.md or any flag. NEVER raises.
    """
    now = time.time() if now_unix is None else float(now_unix)

    if not master_enabled():
        return SpecDriftReport(
            evaluated_at_unix=now,
            master_enabled=False,
            verdict=SpecDriftVerdict.DISABLED,
            records=(),
            claims_evaluated=0,
            unregistered_count=0,
            completion_ratio=None,
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
        )

    text = spec_text if spec_text is not None else _read_default_spec_text()
    claims = _parse_claimed_flag_defaults(text)
    reg = _resolve_registry(registry)

    if not claims:
        return SpecDriftReport(
            evaluated_at_unix=now,
            master_enabled=True,
            verdict=SpecDriftVerdict.INSUFFICIENT_DATA,
            records=(),
            claims_evaluated=0,
            unregistered_count=0,
            completion_ratio=None,
            diagnostic="no parseable boolean flag-default claims found",
        )

    # Read the doll-metric completion ratio ONCE per pass for the
    # severity gate (cheap, cached inside the doll metric).
    completion_ratio = _doll_completion_ratio()
    severity = _severity_for(completion_ratio)

    records: List[SpecDriftRecord] = []
    unregistered = 0
    evaluated = 0

    for flag, claimed_default in sorted(claims.items()):
        if reg is None:
            unregistered += 1
            continue
        spec = _lookup_spec(reg, flag)
        if spec is None:
            # Doc-only / unregistered flag — separate concern, NOT
            # drift. Count it but emit no record (no FP).
            unregistered += 1
            continue
        if not _spec_is_bool(spec):
            # Non-bool flag claimed as a boolean default — skip.
            continue
        evaluated += 1
        try:
            actual_default = bool(getattr(spec, "default", None))
        except Exception:  # noqa: BLE001 — defensive
            continue
        if actual_default != bool(claimed_default):
            records.append(
                SpecDriftRecord(
                    flag=flag,
                    claimed_default=bool(claimed_default),
                    actual_default=actual_default,
                    source_file=str(getattr(spec, "source_file", "")),
                    severity=severity,
                )
            )

    verdict = (
        SpecDriftVerdict.DRIFTED if records
        else SpecDriftVerdict.ALIGNED
    )
    diagnostic = (
        f"{len(records)} drift(s) across {evaluated} bool claim(s); "
        f"unregistered={unregistered}; "
        f"completion_ratio={completion_ratio}; "
        f"severity={getattr(severity, 'value', severity)} → "
        f"{verdict.value}"
    )

    return SpecDriftReport(
        evaluated_at_unix=now,
        master_enabled=True,
        verdict=verdict,
        records=tuple(records),
        claims_evaluated=evaluated,
        unregistered_count=unregistered,
        completion_ratio=completion_ratio,
        diagnostic=diagnostic,
    )


# ===========================================================================
# Adapter — feed the EXISTING invariant-drift pipeline (no duplication)
# ===========================================================================


def to_invariant_drift_records(
    report: Optional[SpecDriftReport],
) -> Tuple[Any, ...]:
    """Convert spec-drift records into canonical
    :class:`invariant_drift_auditor.InvariantDriftRecord` instances —
    tagged ``DriftKind.SPEC_DRIFT`` — so they flow through the EXISTING
    :class:`invariant_drift_auto_action_bridge` (severity → advisory
    action) with zero parallel machinery. NEVER raises."""
    if report is None:
        return ()
    try:
        from backend.core.ouroboros.governance.invariant_drift_auditor import (  # noqa: E501
            DriftKind,
            InvariantDriftRecord,
        )
    except Exception:  # noqa: BLE001 — defensive
        return ()
    out: List[Any] = []
    try:
        for rec in report.records:
            out.append(
                InvariantDriftRecord(
                    drift_kind=DriftKind.SPEC_DRIFT,
                    severity=rec.severity,
                    detail=(
                        f"CLAUDE.md claims {rec.flag} default="
                        f"{rec.claimed_default} but FlagRegistry "
                        f"registers default={rec.actual_default} "
                        f"(source={rec.source_file})"
                    ),
                    affected_keys=(rec.flag,),
                )
            )
    except Exception:  # noqa: BLE001 — defensive
        return tuple(out)
    return tuple(out)


def to_bridge_snapshot(report: Optional[SpecDriftReport]) -> Any:
    """Build a minimal :class:`InvariantSnapshot` carrier so a DRIFTED
    report can flow straight into the existing bridge's ``emit(snapshot,
    records)`` signature. The bridge reads only ``snapshot_id`` +
    ``posture_value`` from it, so we populate a stable, empty-but-valid
    snapshot. NEVER raises — returns a best-effort object even on
    garbage input."""
    try:
        from backend.core.ouroboros.governance.invariant_drift_auditor import (  # noqa: E501
            InvariantSnapshot,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        ts = (
            float(getattr(report, "evaluated_at_unix", 0.0))
            if report is not None else 0.0
        )
        sid = "spec-drift"
        if report is not None:
            sid = f"spec-drift-{int(ts)}"
        return InvariantSnapshot(
            snapshot_id=sid,
            captured_at_utc=ts,
            shipped_invariant_names=(),
            shipped_violation_signature="",
            shipped_violation_count=0,
            flag_registry_hash="",
            flag_count=0,
            exploration_floor_pins=(),
            posture_value=None,
            posture_confidence=None,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None


# ===========================================================================
# Renderer
# ===========================================================================


def format_spec_drift_panel(
    report: Optional[SpecDriftReport] = None,
) -> str:
    """Operator-facing spec-drift summary. NEVER raises."""
    if report is None:
        if not master_enabled():
            return f"spec-drift: disabled ({_ENV_MASTER}=false)"
        return "spec-drift: no report"
    if report.verdict is SpecDriftVerdict.DISABLED:
        return f"spec-drift: disabled ({_ENV_MASTER}=false)"
    lines = [
        f"🪞 Mirror-Self Spec-Drift — {report.verdict.value}",
        f"  claims={report.claims_evaluated} "
        f"drift={len(report.records)} "
        f"unregistered={report.unregistered_count} "
        f"completion_ratio={report.completion_ratio}",
    ]
    for r in report.records:
        sev = getattr(r.severity, "value", str(r.severity))
        lines.append(
            f"  [{sev}] {r.flag}: CLAUDE.md={r.claimed_default} "
            f"registry={r.actual_default} ({r.source_file})"
        )
    return "\n".join(lines)


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/mirror_self_spec_drift.py"
    )

    _EXPECTED_VERDICTS = {
        "aligned", "drifted", "insufficient_data", "disabled",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "SpecDriftVerdict"
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
                missing = _EXPECTED_VERDICTS - found
                extra = found - _EXPECTED_VERDICTS
                if missing:
                    return (
                        f"SpecDriftVerdict missing: {sorted(missing)}",
                    )
                if extra:
                    return (
                        f"SpecDriftVerdict drift: {sorted(extra)}",
                    )
                return ()
        return ("SpecDriftVerdict class not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

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
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) with "
                    "default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "flag_registry" not in source:
            violations.append(
                "must compose canonical flag_registry (FlagRegistry "
                "is the authority on actual flag defaults)",
            )
        if "invariant_drift_auditor" not in source:
            violations.append(
                "must compose invariant_drift_auditor (spec-drift "
                "records flow through the EXISTING drift pipeline "
                "— no parallel observer/bridge)",
            )
        if "second_order_doll_metric" not in source:
            violations.append(
                "must compose #15 second_order_doll_metric "
                "(completion_ratio severity gate)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="spec_drift_verdict_taxonomy_closed",
            target_file=target,
            description=(
                "SpecDriftVerdict 4-value taxonomy bytes-pinned "
                "(ALIGNED / DRIFTED / INSUFFICIENT_DATA / DISABLED)."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="spec_drift_authority_asymmetry",
            target_file=target,
            description=(
                "Read-only validator — MUST NOT import orchestrator "
                "/ iron_gate / policy / providers / "
                "candidate_generator / urgency_router / "
                "change_engine / semantic_guardian / auto_committer "
                "/ risk_tier_floor. Detection + advisory only."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="spec_drift_master_default_false",
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="spec_drift_composes_canonical",
            target_file=target,
            description=(
                "Composes canonical flag_registry (actual defaults) "
                "+ invariant_drift_auditor (existing drift pipeline) "
                "+ #15 second_order_doll_metric (severity gate) — "
                "no parallel implementations."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/mirror_self_spec_drift.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Mirror-Self spec-drift validator master switch. "
                "§33.1 cognitive substrate default-FALSE. When on, "
                "detect_spec_drift compares CLAUDE.md's claimed "
                "env-flag defaults (parseable default-TRUE / "
                "default-FALSE / (default `true`) phrasings) against "
                "the FlagRegistry's ACTUAL registered defaults; a "
                "mismatch is spec drift that flows through the "
                "existing invariant_drift auto-action bridge as an "
                "advisory repair op. Detection only — never mutates "
                "CLAUDE.md or any flag. Closes the spec-drift half "
                "of §40 #14."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_DOLL_GATE_RATIO,
            type=FlagType.FLOAT,
            default=_DEFAULT_DOLL_GATE_RATIO,
            description=(
                "#15 doll-metric completion-ratio threshold at/above "
                "which spec drift escalates from WARNING to CRITICAL. "
                "Below it (or when the doll metric is disabled/"
                "unavailable) spec drift stays WARNING — avoids "
                "early-autonomy thrashing before the organism has "
                "empirical governance-stability evidence. Defaults "
                "to 0.75. Clamped to [0.0, 1.0]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_DOLL_GATE_RATIO}=0.9",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "MIRROR_SELF_SPEC_DRIFT_SCHEMA_VERSION",
    "SpecDriftVerdict",
    "SpecDriftRecord",
    "SpecDriftReport",
    "master_enabled",
    "doll_gate_ratio",
    "detect_spec_drift",
    "to_invariant_drift_records",
    "to_bridge_snapshot",
    "format_spec_drift_panel",
    "register_shipped_invariants",
    "register_flags",
]
