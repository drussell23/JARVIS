"""PlanFalsificationDetector Slice 2 — async detector + filesystem probe.

The probing + orchestration layer that takes plan hypotheses + a
stream of upstream-classified EvidenceItems and returns a
:class:`FalsificationVerdict`. Adds the deterministic filesystem
probe (FILE_MISSING) — the only structural signal that doesn't
require an upstream classifier.

Architectural reuse — composes with ZERO duplication
----------------------------------------------------

* Slice 1 :func:`compute_falsification_verdict` — total decision
  function. NEVER raises. Closed-taxonomy outcome via
  match-by-step-index/file-path.
* Slice 1 :class:`EvidenceItem` — the typed wrapper. The
  filesystem probe constructs FILE_MISSING items at the SOURCE
  (this module IS the source for that signal); upstream-classified
  items (VERIFY_REJECTED / REPAIR_STUCK / etc.) flow through
  unchanged.
* Each upstream signal source (VERIFY phase / L2 RepairEngine /
  AdversarialReview / EXPLORE subagent) owns its OWN classifier.
  The detector relays — it does NOT pattern-match payload
  contents. Source classifies; detector routes.

Direct-solve principles
-----------------------

* **Asynchronous-ready** — async function so Slice 4 orchestrator
  wire-up composes with the existing await-driven phase pipeline.
  Filesystem probe wrapped in ``asyncio.to_thread`` for the rare
  case where a slow filesystem (NFS, FUSE) blocks; sub-millisecond
  on local disk.
* **Dynamic** — every numeric (probe budget) flows from env-knob
  helpers with floor + ceiling clamps. No hardcoded magic.
* **Adaptive** — degraded paths (filesystem error, permission
  denied, garbage project_root) all map to "skip the probe" rather
  than raise. Empty probe result composes cleanly with upstream
  evidence — INSUFFICIENT_EVIDENCE if both empty.
* **Intelligent** — probe respects project_root containment: paths
  that would escape the repo (absolute outside repo, contains
  ``..``) are skipped silently (defense-in-depth — the detector
  is read-only and would never write anyway, but skipping reduces
  audit noise).
* **Robust** — every public function NEVER raises out. Caller-
  initiated asyncio.CancelledError propagates per convention.
  Last-resort exception handler returns FailureVerdict with
  FAILED outcome — the orchestrator's existing DynamicRePlanner
  reactive path remains a backstop.
* **No hardcoding** — probe enable flag is operator-controlled;
  project_root is dependency-injected (no implicit cwd lookup
  without explicit fall-through); no special-case file paths.

Authority invariants (AST-pinned by Slice 4 graduation)
-------------------------------------------------------

* MAY import: ``plan_falsification`` (Slice 1 primitive).
  Module-owned ``register_flags`` / ``register_shipped_invariants``
  exempt (registration-contract exemption from Priority #6).
* MUST NOT import: orchestrator / phase_runner / iron_gate /
  change_engine / candidate_generator / providers /
  doubleword_provider / urgency_router / auto_action_router /
  subagent_scheduler / tool_executor / semantic_guardian /
  semantic_firewall / risk_engine.
* No exec/eval/compile.
* The detector NEVER writes to the filesystem — strictly read-only
  probes. Operator audit can trust that detection cannot mutate
  state.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

from backend.core.ouroboros.governance.plan_falsification import (
    EvidenceItem,
    FalsificationKind,
    FalsificationOutcome,
    FalsificationVerdict,
    PlanStepHypothesis,
    compute_falsification_verdict,
    plan_falsification_enabled,
)

logger = logging.getLogger(__name__)


PLAN_FALSIFICATION_DETECTOR_SCHEMA_VERSION: str = (
    "plan_falsification_detector.1"
)


# ---------------------------------------------------------------------------
# Probe-specific feature flags
# ---------------------------------------------------------------------------


def filesystem_probe_enabled() -> bool:
    """``JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED`` (default
    ``true`` post-Slice-2 graduation).

    Per-probe sub-flag for the FILE_MISSING filesystem probe.
    Sits beneath the master ``JARVIS_PLAN_FALSIFICATION_ENABLED``
    flag — when master is off, this flag has no effect.
    Asymmetric env semantics. Operators may flip to false to
    suppress filesystem probes specifically (e.g., on slow NFS)
    while leaving upstream-classified evidence flowing through."""
    raw = os.environ.get(
        "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Filesystem probe — the only deterministic structural classifier
# ---------------------------------------------------------------------------


def _resolve_probe_path(
    file_path: str, *, project_root: Optional[Path],
) -> Optional[Path]:
    """Resolve a hypothesis ``file_path`` to a probable on-disk
    location, RESPECTING project_root containment.

    Returns None for paths the probe should skip (escape attempts,
    garbage input, missing project_root). NEVER raises.

    Containment rules (defense-in-depth — detector is read-only,
    but skipping noisy probes keeps audit clean):
      * Empty path → skip
      * Path starts with "/" AND project_root supplied AND not under
        project_root → skip (absolute escape)
      * Path contains ``..`` segment → skip (relative escape)
      * Otherwise → join with project_root if relative, else use
        as-is when absolute and project_root is None
    """
    try:
        if not file_path:
            return None
        p = str(file_path).strip()
        if not p:
            return None
        # Normalize separators defensively (Windows tests).
        if ".." in Path(p).parts:
            return None
        if project_root is None:
            # No project_root — accept absolute paths as-is, skip
            # relative (we have no anchor).
            candidate = Path(p)
            if not candidate.is_absolute():
                return None
            return candidate
        # project_root supplied.
        candidate = Path(p)
        if candidate.is_absolute():
            try:
                candidate.relative_to(Path(project_root))
            except ValueError:
                # Absolute path outside project_root — skip.
                return None
            return candidate
        return Path(project_root) / candidate
    except Exception:  # noqa: BLE001 — defensive
        return None


def _probe_one_file(
    hypothesis: PlanStepHypothesis,
    *,
    project_root: Optional[Path],
    captured_monotonic: float,
) -> Optional[EvidenceItem]:
    """Probe a single hypothesis's file_path. Returns a
    FILE_MISSING EvidenceItem when the file is absent; None
    otherwise (including the skip cases). NEVER raises.

    Skip conditions:
      * file_path empty / unresolvable
      * change_type explicitly indicates creation ("create" /
        "new") — can't fault a plan for a not-yet-created file
      * filesystem check raises (permission denied, broken FS) —
        skip rather than emit a false positive
    """
    try:
        # Don't probe creates — the plan is asserting the file will
        # NOT exist initially. False-positive otherwise.
        change_type = (hypothesis.change_type or "").strip().lower()
        if change_type in ("create", "new", "add"):
            return None
        resolved = _resolve_probe_path(
            hypothesis.file_path, project_root=project_root,
        )
        if resolved is None:
            return None
        # Filesystem existence check — the structural signal.
        try:
            exists = resolved.exists()
        except (OSError, PermissionError):
            # Filesystem error → skip, not falsify (no evidence is
            # better than false evidence).
            return None
        if exists:
            return None  # File present → no falsification signal
        return EvidenceItem(
            kind=FalsificationKind.FILE_MISSING,
            target_step_index=hypothesis.step_index,
            target_file_path=hypothesis.file_path,
            detail=(
                f"file {hypothesis.file_path!r} not found on disk "
                f"at probe time (resolved={resolved})"
            )[:500],
            source="plan_falsification_detector.filesystem_probe",
            captured_monotonic=captured_monotonic,
            payload={
                "resolved_path": str(resolved),
                "change_type": hypothesis.change_type,
            },
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PlanFalsificationDetector] _probe_one_file degraded: %s",
            exc,
        )
        return None


def _run_filesystem_probe(
    plan_hypotheses: Tuple[PlanStepHypothesis, ...],
    *,
    project_root: Optional[Path],
    captured_monotonic: float,
) -> Tuple[EvidenceItem, ...]:
    """Probe filesystem for every hypothesis. Returns a tuple of
    FILE_MISSING EvidenceItems for the absent files. Empty tuple
    when no files are missing (or all probes were skipped).
    NEVER raises."""
    try:
        items = []
        for hyp in plan_hypotheses:
            if not isinstance(hyp, PlanStepHypothesis):
                continue
            item = _probe_one_file(
                hyp,
                project_root=project_root,
                captured_monotonic=captured_monotonic,
            )
            if item is not None:
                items.append(item)
        return tuple(items)
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[PlanFalsificationDetector] _run_filesystem_probe "
            "degraded: %s", exc,
        )
        return ()


# ---------------------------------------------------------------------------
# Async public detector — the orchestrator-callable surface
# ---------------------------------------------------------------------------


async def detect_falsification(
    plan_hypotheses: Tuple[PlanStepHypothesis, ...],
    *,
    upstream_evidence: Tuple[EvidenceItem, ...] = (),
    project_root: Optional[Path] = None,
    enabled: Optional[bool] = None,
    enable_filesystem_probe: Optional[bool] = None,
) -> FalsificationVerdict:
    """Run all enabled probes + combine with upstream-classified
    evidence + compute the falsification verdict.

    Async because filesystem probe is wrapped in
    ``asyncio.to_thread`` for slow-filesystem cases (NFS, FUSE).
    NEVER raises out — caller-initiated asyncio.CancelledError
    propagates per convention.

    Args:
      plan_hypotheses: tuple of PlanStepHypothesis to check.
      upstream_evidence: pre-classified EvidenceItems from upstream
        sources (VERIFY phase / RepairEngine / AdversarialReview /
        EXPLORE subagent / operator annotation). Each source owns
        its classification; the detector relays without inspection.
      project_root: optional repo root for filesystem probe path
        containment. None disables the absolute-path-escape check
        (no anchor) — most callers should pass this.
      enabled: explicit enable override (test injection). Defaults
        to env via plan_falsification_enabled().
      enable_filesystem_probe: explicit filesystem-probe override.
        Defaults to env via filesystem_probe_enabled().

    Returns:
      FalsificationVerdict — caller branches on outcome.
      FAILED falls through to the existing DynamicRePlanner
      reactive path so a broken detector cannot suppress legacy
      replan triggers.
    """
    captured_monotonic = time.monotonic()
    # 1. Master flag short-circuit (so we don't even probe filesystem
    #    when disabled).
    is_enabled = (
        enabled if enabled is not None
        else plan_falsification_enabled()
    )
    if not is_enabled:
        return FalsificationVerdict(
            outcome=FalsificationOutcome.DISABLED,
            monotonic_tightening_verdict="",
        )

    # 2. Coerce inputs defensively. compute_falsification_verdict
    #    re-coerces (defense-in-depth), but doing it here too means
    #    the probe layer sees a clean tuple.
    if not isinstance(plan_hypotheses, tuple):
        try:
            plan_hypotheses = tuple(plan_hypotheses or ())
        except Exception:  # noqa: BLE001
            plan_hypotheses = ()
    if not isinstance(upstream_evidence, tuple):
        try:
            upstream_evidence = tuple(upstream_evidence or ())
        except Exception:  # noqa: BLE001
            upstream_evidence = ()

    # 3. Run filesystem probe (when enabled). Wrap in to_thread so
    #    slow filesystems don't block the event loop.
    fs_evidence: Tuple[EvidenceItem, ...] = ()
    fs_enabled = (
        enable_filesystem_probe
        if enable_filesystem_probe is not None
        else filesystem_probe_enabled()
    )
    if fs_enabled and plan_hypotheses:
        try:
            fs_evidence = await asyncio.to_thread(
                _run_filesystem_probe,
                plan_hypotheses,
                project_root=project_root,
                captured_monotonic=captured_monotonic,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[PlanFalsificationDetector] filesystem probe "
                "to_thread degraded: %s", exc,
            )
            fs_evidence = ()

    # 4. Combine fs probe results with upstream-classified evidence.
    combined_evidence = fs_evidence + upstream_evidence

    # 5. Delegate to Slice 1's total decision function.
    try:
        verdict = compute_falsification_verdict(
            plan_hypotheses,
            combined_evidence,
            enabled=True,  # we already checked the master flag above
            decision_monotonic=captured_monotonic,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.warning(
            "[PlanFalsificationDetector] compute_falsification_verdict "
            "raised (should not happen — Slice 1 is NEVER-raise): %s "
            "— FAILED falls through to DynamicRePlanner backstop", exc,
        )
        return FalsificationVerdict(
            outcome=FalsificationOutcome.FAILED,
            monotonic_tightening_verdict="",
        )
    return verdict


# ---------------------------------------------------------------------------
# Public surface — Slice 4 will pin via shipped_code_invariants
# ---------------------------------------------------------------------------

__all__ = [
    "PLAN_FALSIFICATION_DETECTOR_SCHEMA_VERSION",
    "detect_falsification",
    "filesystem_probe_enabled",
    "register_flags",
    "register_shipped_invariants",
]


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    """Module-owned :class:`FlagRegistry` registration for the
    Slice 2 sub-flag. Discovered automatically. Returns count."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[PlanFalsificationDetector] register_flags degraded: %s",
            exc,
        )
        return 0
    target = (
        "backend/core/ouroboros/governance/"
        "plan_falsification_detector.py"
    )
    spec = FlagSpec(
        name="JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED",
        type=FlagType.BOOL, default=True,
        category=Category.SAFETY,
        source_file=target,
        example="JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED=true",
        description=(
            "Per-probe sub-flag for the FILE_MISSING filesystem "
            "probe. Independent of master "
            "JARVIS_PLAN_FALSIFICATION_ENABLED. Operators flip "
            "false to suppress probes on slow NFS while keeping "
            "upstream-classified evidence flowing."
        ),
    )
    try:
        registry.register(spec)
        return 1
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PlanFalsificationDetector] register_flags spec %s "
            "skipped: %s", spec.name, exc,
        )
        return 0


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned shipped_code_invariants
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Slice 2 invariant: authority allowlist (only Slice 1
    governance import permitted) + detector-async / helpers-sync
    layout + no exec/eval/compile."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    _ALLOWED = {"plan_falsification"}
    _FORBIDDEN = {
        "orchestrator", "phase_runner", "iron_gate",
        "change_engine", "candidate_generator", "providers",
        "doubleword_provider", "urgency_router",
        "auto_action_router", "subagent_scheduler",
        "tool_executor", "semantic_guardian",
        "semantic_firewall", "risk_engine",
    }

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in _ast.walk(tree):
            if isinstance(fnode, _ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                if "backend." not in module and "governance" not in module:
                    continue
                lineno = getattr(node, "lineno", 0)
                if any(s <= lineno <= e for s, e in exempt_ranges):
                    continue
                tail = module.rsplit(".", 1)[-1]
                if tail in _FORBIDDEN:
                    violations.append(
                        f"line {lineno}: forbidden module {module!r}"
                    )
                elif tail not in _ALLOWED:
                    violations.append(
                        f"line {lineno}: unexpected governance "
                        f"import {module!r}"
                    )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"Slice 2 MUST NOT {node.func.id}()"
                        )
        # detect_falsification must be async; helpers must be sync.
        sync_required = {
            "_resolve_probe_path", "_probe_one_file",
            "_run_filesystem_probe", "filesystem_probe_enabled",
        }
        async_required = {"detect_falsification"}
        async_seen: set = set()
        sync_seen: set = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.AsyncFunctionDef):
                async_seen.add(node.name)
                if node.name in sync_required:
                    violations.append(
                        f"{node.name!r} must be sync but is async "
                        f"def at line {getattr(node, 'lineno', '?')}"
                    )
            elif isinstance(node, _ast.FunctionDef):
                sync_seen.add(node.name)
        for required in async_required:
            if required not in async_seen:
                violations.append(
                    f"missing async def {required!r}"
                )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="plan_falsification_detector_authority",
            target_file=(
                "backend/core/ouroboros/governance/"
                "plan_falsification_detector.py"
            ),
            description=(
                "Slice 2 detector authority: imports only "
                "plan_falsification (Slice 1); detect_falsification "
                "is async + filesystem helpers stay sync; no "
                "exec/eval/compile."
            ),
            validate=_validate,
        ),
    ]
