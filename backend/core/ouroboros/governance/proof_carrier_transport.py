"""
Proof Carrier Transport
=======================

Closes §40 Wave 5 #19. Per the operator binding:

  "Per-candidate proof of correctness — aggregate Wave 3 #5
   (MCP scanner) + #6 (coherence) + #7 (rehearsal) evidence
   into a single structured carrier the dispatcher can inspect
   before APPLY."

Pure-function evidence aggregator. For one candidate op (keyed
by ``op_id`` + ``target_files`` + caller-supplied
``mcp_output``), it composes:

* Wave 3 #5 ``mcp_output_scanner`` — credential/injection
  findings on ``mcp_output``.
* Wave 3 #6 ``cross_session_coherence_rig`` — drift report
  (caller-injectable for per-candidate context; defaults to
  global coherence read).
* Wave 3 #7 ``counterfactual_rehearsal_mode`` — postmortem
  overlap concerns on the candidate's ``target_files``.

Composed into closed 4-value :class:`ProofVerdict`
(CLEAN / WARN / BLOCK / DISABLED) + closed 4-value
:class:`EvidenceSource` (MCP_SCAN / COHERENCE / REHEARSAL /
NONE) attribution of the strongest concern.

Deterministic — same evidence inputs → same proof. Zero LLM.
Advisory — surfaces the proof carrier; dispatcher reads it as
extra evidence at GATE; substrate claims no authority over
APPLY/BLOCK decisions.

§33.1 ``JARVIS_PROOF_CARRIER_ENABLED`` default-FALSE.

Authority asymmetry: no orchestrator / iron_gate / policy /
providers / candidate_generator / urgency_router /
change_engine / semantic_guardian / auto_committer /
risk_tier_floor.
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


PROOF_CARRIER_SCHEMA_VERSION: str = "proof_carrier.1"


_ENV_MASTER = "JARVIS_PROOF_CARRIER_ENABLED"
_ENV_PERSIST = "JARVIS_PROOF_CARRIER_PERSIST_ENABLED"
_ENV_BLOCK_ON_MCP = "JARVIS_PROOF_CARRIER_BLOCK_ON_MCP"
_ENV_BLOCK_ON_COHERENCE = "JARVIS_PROOF_CARRIER_BLOCK_ON_COHERENCE"
_ENV_LEDGER_PATH = "JARVIS_PROOF_CARRIER_LEDGER_PATH"

_DEFAULT_LEDGER_REL = ".jarvis/proof_carrier_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    return _flag(_ENV_PERSIST, default=True)


def block_on_mcp_finding() -> bool:
    """When MCP scanner finds credential/injection content,
    proof carrier escalates to BLOCK verdict. Default TRUE —
    credentials in MCP output are never advisory."""
    return _flag(_ENV_BLOCK_ON_MCP, default=True)


def block_on_coherence_drift() -> bool:
    """When coherence rig reports CRITICAL drift, proof carrier
    escalates to BLOCK. Default FALSE — coherence drift is
    advisory in most workflows."""
    return _flag(_ENV_BLOCK_ON_COHERENCE, default=False)


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# Closed taxonomies


class ProofVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    CLEAN = "clean"
    WARN = "warn"
    BLOCK = "block"
    DISABLED = "disabled"


class EvidenceSource(str, enum.Enum):
    """Closed 4-value source — bytes-pinned via AST."""

    MCP_SCAN = "mcp_scan"
    COHERENCE = "coherence"
    REHEARSAL = "rehearsal"
    NONE = "none"


_VERDICT_GLYPH: Dict[str, str] = {
    ProofVerdict.CLEAN.value: "✓",
    ProofVerdict.WARN.value: "⚠",
    ProofVerdict.BLOCK.value: "🚫",
    ProofVerdict.DISABLED.value: "◌",
}


_SOURCE_GLYPH: Dict[str, str] = {
    EvidenceSource.MCP_SCAN.value: "🔍",
    EvidenceSource.COHERENCE.value: "🧠",
    EvidenceSource.REHEARSAL.value: "🎭",
    EvidenceSource.NONE.value: "·",
}


def verdict_glyph(verdict: object) -> str:
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def source_glyph(source: object) -> str:
    try:
        if hasattr(source, "value"):
            return _SOURCE_GLYPH.get(str(source.value), "?")
        return _SOURCE_GLYPH.get(
            str(source or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# §33.5 frozen artifact


@dataclass(frozen=True)
class ProofCarrier:
    """Aggregate per-candidate proof carrier."""

    op_id: str
    candidate_target_files: Tuple[str, ...]
    verdict: ProofVerdict
    dominant_source: EvidenceSource
    mcp_finding_count: int
    mcp_finding_kinds: Tuple[str, ...]
    coherence_drift_level: str
    rehearsal_concern_count: int
    rehearsal_verdict: str
    boundary_crossed: bool
    diagnostic: str
    evaluated_at_unix: float
    elapsed_s: float
    master_enabled: bool
    schema_version: str = PROOF_CARRIER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id[:128],
            "candidate_target_files": list(
                self.candidate_target_files,
            ),
            "verdict": self.verdict.value,
            "dominant_source": self.dominant_source.value,
            "mcp_finding_count": int(self.mcp_finding_count),
            "mcp_finding_kinds": list(self.mcp_finding_kinds),
            "coherence_drift_level": self.coherence_drift_level[:32],
            "rehearsal_concern_count": int(
                self.rehearsal_concern_count,
            ),
            "rehearsal_verdict": self.rehearsal_verdict[:32],
            "boundary_crossed": bool(self.boundary_crossed),
            "diagnostic": self.diagnostic[:512],
            "evaluated_at_unix": self.evaluated_at_unix,
            "elapsed_s": float(self.elapsed_s),
            "master_enabled": self.master_enabled,
            "schema_version": self.schema_version,
        }


# Composers


def _scan_mcp_output(text: str) -> Tuple[int, Tuple[str, ...]]:
    """Compose Wave 3 #5. Returns (count, kinds_tuple).
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.mcp_output_scanner import (  # noqa: E501
            scan_mcp_output,
        )
    except ImportError:
        return 0, ()
    if not text:
        return 0, ()
    try:
        report = scan_mcp_output(text)
        findings = tuple(getattr(report, "findings", ()) or ())
        kinds: List[str] = []
        for f in findings:
            try:
                k = getattr(getattr(f, "kind", None), "value", "")
                if k:
                    kinds.append(str(k))
            except Exception:  # noqa: BLE001
                continue
        return len(findings), tuple(sorted(set(kinds)))
    except Exception:  # noqa: BLE001
        return 0, ()


def _coherence_drift() -> str:
    """Compose Wave 3 #6. Returns drift_level value or empty.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cross_session_coherence_rig import (  # noqa: E501
            run_coherence_arc,
        )
    except ImportError:
        return ""
    try:
        report = run_coherence_arc()
        return str(
            getattr(
                getattr(report, "drift_level", None), "value", "",
            ) or "",
        )
    except Exception:  # noqa: BLE001
        return ""


def _rehearsal_for_files(
    target_files: Sequence[str],
) -> Tuple[int, str]:
    """Compose Wave 3 #7. Returns (concern_count, verdict_value).
    NEVER raises."""
    if not target_files:
        return 0, ""
    try:
        from backend.core.ouroboros.governance.counterfactual_rehearsal_mode import (  # noqa: E501
            evaluate_rehearsal,
        )
    except ImportError:
        return 0, ""
    try:
        report = evaluate_rehearsal(list(target_files))
        return (
            len(getattr(report, "concerns", ()) or ()),
            str(
                getattr(
                    getattr(report, "verdict", None),
                    "value", "",
                ) or "",
            ),
        )
    except Exception:  # noqa: BLE001
        return 0, ""


def _is_boundary_crossed(files: Sequence[str]) -> bool:
    if not files:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed(files))
    except Exception:  # noqa: BLE001
        return False


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Best-effort §33.4 write. NEVER raises."""
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


def _classify_source(
    mcp_count: int,
    coherence_drift: str,
    rehearsal_count: int,
) -> EvidenceSource:
    if mcp_count == 0 and rehearsal_count == 0 and not coherence_drift:
        return EvidenceSource.NONE
    # MCP findings are highest priority (credentials > drift)
    if mcp_count > 0:
        return EvidenceSource.MCP_SCAN
    if rehearsal_count > 0:
        return EvidenceSource.REHEARSAL
    return EvidenceSource.COHERENCE


def _build_verdict(
    mcp_count: int,
    coherence_drift: str,
    rehearsal_count: int,
    rehearsal_verdict: str,
) -> ProofVerdict:
    """Pure classifier. NEVER raises."""
    drift_critical = (
        coherence_drift.strip().lower() in ("critical", "severe")
    )
    rehearsal_escalate = (
        rehearsal_verdict.strip().lower() == "escalate"
    )
    if mcp_count > 0 and block_on_mcp_finding():
        return ProofVerdict.BLOCK
    if drift_critical and block_on_coherence_drift():
        return ProofVerdict.BLOCK
    if rehearsal_escalate:
        return ProofVerdict.BLOCK
    if (
        mcp_count > 0
        or rehearsal_count > 0
        or drift_critical
    ):
        return ProofVerdict.WARN
    if coherence_drift.strip().lower() in ("moderate", "high"):
        return ProofVerdict.WARN
    return ProofVerdict.CLEAN


def build_proof_carrier(
    op_id: str,
    candidate_target_files: Sequence[Any],
    *,
    mcp_output: str = "",
    mcp_count_override: Optional[int] = None,
    mcp_kinds_override: Optional[Sequence[str]] = None,
    coherence_drift_override: Optional[str] = None,
    rehearsal_count_override: Optional[int] = None,
    rehearsal_verdict_override: Optional[str] = None,
    now_unix: Optional[float] = None,
) -> ProofCarrier:
    """Top-level proof aggregator. NEVER raises."""
    started = time.time() if now_unix is None else float(now_unix)
    oid = str(op_id or "").strip()
    files = tuple(
        str(f or "").strip()
        for f in (candidate_target_files or ())
        if f
    )

    if not master_enabled():
        return ProofCarrier(
            op_id=oid,
            candidate_target_files=files,
            verdict=ProofVerdict.DISABLED,
            dominant_source=EvidenceSource.NONE,
            mcp_finding_count=0,
            mcp_finding_kinds=(),
            coherence_drift_level="",
            rehearsal_concern_count=0,
            rehearsal_verdict="",
            boundary_crossed=False,
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false"
            ),
            evaluated_at_unix=started,
            elapsed_s=0.0,
            master_enabled=False,
        )

    if mcp_count_override is None or mcp_kinds_override is None:
        mc, mk = _scan_mcp_output(mcp_output)
    else:
        mc = mcp_count_override
        mk = tuple(mcp_kinds_override)
    coh = (
        coherence_drift_override
        if coherence_drift_override is not None
        else _coherence_drift()
    )
    if (
        rehearsal_count_override is None
        or rehearsal_verdict_override is None
    ):
        rc, rv = _rehearsal_for_files(files)
    else:
        rc = rehearsal_count_override
        rv = rehearsal_verdict_override
    boundary = _is_boundary_crossed(files)
    verdict = _build_verdict(mc, coh, rc, rv)
    source = _classify_source(mc, coh, rc)

    diagnostic = (
        f"verdict={verdict.value} source={source.value} "
        f"mcp={mc}({len(mk)} kinds) "
        f"coherence={coh or 'n/a'} "
        f"rehearsal={rc}({rv or 'n/a'})"
        + (" [cage]" if boundary else "")
    )

    carrier = ProofCarrier(
        op_id=oid,
        candidate_target_files=files,
        verdict=verdict,
        dominant_source=source,
        mcp_finding_count=mc,
        mcp_finding_kinds=mk,
        coherence_drift_level=coh,
        rehearsal_concern_count=rc,
        rehearsal_verdict=rv,
        boundary_crossed=boundary,
        diagnostic=diagnostic,
        evaluated_at_unix=started,
        elapsed_s=max(0.0, time.time() - started),
        master_enabled=True,
    )
    _persist_carrier(carrier)
    _publish_event(carrier)
    return carrier


def _persist_carrier(carrier: ProofCarrier) -> None:
    """Best-effort §33.4 write. NEVER raises. Skips CLEAN."""
    if carrier.verdict is ProofVerdict.CLEAN:
        return
    _flock_append({"kind": "proof_carrier", "payload": carrier.to_dict()})


def _publish_event(carrier: ProofCarrier) -> None:
    """Best-effort SSE publish. NEVER raises."""
    if not master_enabled():
        return
    if carrier.verdict is ProofVerdict.CLEAN:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_PROOF_CARRIER_BUILT,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_PROOF_CARRIER_BUILT,
            (
                f"system::proof_carrier::"
                f"{carrier.schema_version}"
            ),
            {
                "op_id": carrier.op_id[:64],
                "verdict": carrier.verdict.value,
                "dominant_source": carrier.dominant_source.value,
                "mcp_finding_count": carrier.mcp_finding_count,
                "rehearsal_concern_count": (
                    carrier.rehearsal_concern_count
                ),
                "coherence_drift_level": (
                    carrier.coherence_drift_level
                ),
                "boundary_crossed": carrier.boundary_crossed,
                "elapsed_s": carrier.elapsed_s,
                "schema_version": carrier.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_proof_panel(
    carrier: Optional[ProofCarrier] = None,
) -> str:
    """NEVER raises."""
    if carrier is None:
        if not master_enabled():
            return (
                f"proof carrier: disabled ({_ENV_MASTER}=false)"
            )
        return "proof carrier: no carrier"
    if not carrier.master_enabled:
        return f"proof carrier: disabled ({_ENV_MASTER}=false)"
    vg = verdict_glyph(carrier.verdict)
    sg = source_glyph(carrier.dominant_source)
    lines = [
        f"📜 Proof Carrier  {vg} {carrier.verdict.value}",
        f"  op_id              : {carrier.op_id[:32] or '?'}",
        f"  target_files       : {len(carrier.candidate_target_files)}",
        f"  dominant_source    : {sg} {carrier.dominant_source.value}",
        f"  mcp_findings       : {carrier.mcp_finding_count}",
        f"  coherence_drift    : "
        f"{carrier.coherence_drift_level or 'n/a'}",
        f"  rehearsal_concerns : {carrier.rehearsal_concern_count} "
        f"({carrier.rehearsal_verdict or 'n/a'})",
        f"  diagnostic         : {carrier.diagnostic}",
    ]
    return "\n".join(lines)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "proof_carrier_transport.py"
    )

    _EXPECTED_VERDICTS = {"clean", "warn", "block", "disabled"}
    _EXPECTED_SOURCES = {
        "mcp_scan", "coherence", "rehearsal", "none",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ProofVerdict"
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
                        f"ProofVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"ProofVerdict drift: {sorted(extra)}",
                    )
                return ()
        return ("ProofVerdict class not found",)

    def _validate_source_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "EvidenceSource"
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
                missing = _EXPECTED_SOURCES - found
                extra = found - _EXPECTED_SOURCES
                if missing:
                    return (
                        f"EvidenceSource missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"EvidenceSource drift: {sorted(extra)}",
                    )
                return ()
        return ("EvidenceSource class not found",)

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
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "mcp_output_scanner" not in source:
            violations.append("must compose Wave 3 #5 mcp_output_scanner")
        if "cross_session_coherence_rig" not in source:
            violations.append("must compose Wave 3 #6 cross_session_coherence_rig")
        if "counterfactual_rehearsal_mode" not in source:
            violations.append("must compose Wave 3 #7 counterfactual_rehearsal_mode")
        if "cross_process_jsonl" not in source:
            violations.append("must compose cross_process_jsonl")
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="proof_carrier_verdict_taxonomy_closed",
            target_file=target,
            description="ProofVerdict 4-value taxonomy bytes-pinned.",
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="proof_carrier_source_taxonomy_closed",
            target_file=target,
            description="EvidenceSource 4-value taxonomy bytes-pinned.",
            validate=_validate_source_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="proof_carrier_authority_asymmetry",
            target_file=target,
            description=(
                "Substrate purity — advisory only. MUST NOT "
                "import orchestrator / iron_gate / etc."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="proof_carrier_master_default_false",
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="proof_carrier_composes_canonical",
            target_file=target,
            description=(
                "Composes Wave 3 #5 + #6 + #7 + cross_process_jsonl."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "proof_carrier_transport.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Proof carrier transport master. §33.1 "
                "default-FALSE. Closes §40 Wave 5 #19. "
                "Aggregates Wave 3 #5/#6/#7 evidence into "
                "per-candidate ProofCarrier (CLEAN / WARN / "
                "BLOCK / DISABLED)."
            ),
            category=Category.EXPERIMENTAL,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description="Sub-flag — gate §33.4 writes.",
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_BLOCK_ON_MCP,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Escalate to BLOCK when MCP scanner finds any "
                "credential/injection. Default TRUE — "
                "credentials in MCP output are never advisory."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_BLOCK_ON_MCP}=false",
        ),
        FlagSpec(
            name=_ENV_BLOCK_ON_COHERENCE,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Escalate to BLOCK when coherence reports "
                "CRITICAL drift. Default FALSE — coherence "
                "drift is usually advisory."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_BLOCK_ON_COHERENCE}=true",
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
    "PROOF_CARRIER_SCHEMA_VERSION",
    "ProofVerdict",
    "EvidenceSource",
    "ProofCarrier",
    "master_enabled",
    "persistence_enabled",
    "block_on_mcp_finding",
    "block_on_coherence_drift",
    "ledger_path",
    "verdict_glyph",
    "source_glyph",
    "build_proof_carrier",
    "format_proof_panel",
    "register_shipped_invariants",
    "register_flags",
]
