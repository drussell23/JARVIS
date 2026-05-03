"""ClusterExplorationCascadeObserver -- Slice 4 of
ClusterIntelligence-CrossSession arc.

Bridges:
  * Post-verify orchestrator hook (write side) -> persists
    successful cluster_coverage explorations into DomainMapStore
    so the next session sees prior context for the same cluster.
  * ProactiveExplorationSensor envelope build (read side) ->
    threads prior-exploration context (theme, role, files,
    exploration_count) into the envelope description so the
    model sees "previously explored: X.py, Y.py. Architectural
    role: voice biometric primitive (last touched 2d ago,
    exploration_count=3)" instead of starting from scratch.

Architecture decisions
----------------------

* No new spawn surface: write side is a single async function
  callable from the orchestrator's existing post-verify hook;
  read side is a single sync function callable from the sensor's
  envelope build.
* No OpsDigestObserver registration: that surface uses a
  process-global singleton (one observer wins) which would
  conflict with the SessionRecorder. We use a direct function
  call instead -- one additive line at the orchestrator hook
  point.
* Master flag default false until Slice 5 graduation.
* Defensive at every boundary -- never raises into the caller.
* Architectural-role inference (Slice 4 optional path) is
  STUBBED OUT in the default code path: the cascade observer
  records the exploration with empty role unless an explicit
  role string was passed by the caller. The actual Venom-round
  role inference is gated behind ``JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED``
  and is an opt-in cost surface (deferred to a post-Slice-5
  follow-up so the substrate ships first).

Reuse contract (no duplication)
-------------------------------

* :class:`DomainMapStore` (Slice 3) -- write/read surface
  unchanged
* :class:`OperationContext.intake_evidence_json` (Slice 4
  additive ctx field) -- structured tag carriage
* No new flock infrastructure (DomainMapStore owns it)
* No new persistence surface (DomainMapStore owns it)
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.domain_map_memory import (
    DomainMapEntry,
    DomainMapStore,
    domain_map_enabled,
    get_default_store,
)

logger = logging.getLogger("Ouroboros.ClusterExplorationCascade")


CLUSTER_CASCADE_SCHEMA_VERSION: str = "cluster_cascade.v1"

# The only category tag we care about. Sensor stamps this in the
# envelope.evidence dict; intake propagates via
# OperationContext.intake_evidence_json. No regex, no fuzzy
# matching -- exact string equality.
_CLUSTER_COVERAGE_CATEGORY: str = "cluster_coverage"


# ---------------------------------------------------------------------------
# Master flag + sub-flags
# ---------------------------------------------------------------------------


def cascade_observer_enabled() -> bool:
    """``JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED`` (default
    ``true`` post Slice 5 graduation, 2026-05-03).

    When off:
      * :func:`observe_cluster_coverage_completion` short-circuits
        to no-op (no DomainMap write).
      * :func:`render_prior_context_block` short-circuits to
        empty string (no envelope enrichment).

    Composes with the Slice 3 DomainMap master flag -- when
    DomainMap is off the persistence layer also short-circuits
    so the cascade is doubly-safe.
    """
    raw = os.environ.get(
        "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


def auto_role_enabled() -> bool:
    """``JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED`` (default ``false``).

    When on, Slice 4 invokes a brief Venom round to infer the
    architectural role of the touched files. **Currently stubbed**
    -- the wiring to actually call Venom lives in a future
    post-Slice-5 cost-authorized arc. Today this flag controls
    only whether a placeholder ``role_inference_pending`` marker
    is written, so operators see whether the cost surface would
    have engaged. The substrate is ready; the cost commitment
    is not.
    """
    raw = os.environ.get(
        "JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in ("1", "true", "yes", "on")


def _prior_context_max_files() -> int:
    """``JARVIS_CLUSTER_CASCADE_PRIOR_CONTEXT_MAX_FILES`` (default
    8, floor 1, ceiling 32). Number of prior-discovered files
    surfaced in the envelope description block so we don't
    explode prompt size for highly-explored clusters."""
    raw = os.environ.get(
        "JARVIS_CLUSTER_CASCADE_PRIOR_CONTEXT_MAX_FILES", "",
    ).strip()
    try:
        n = int(raw) if raw else 8
    except ValueError:
        n = 8
    return max(1, min(32, n))


# ---------------------------------------------------------------------------
# Internal: cluster_coverage tag extraction
# ---------------------------------------------------------------------------


def _parse_cluster_coverage_tag(
    intake_evidence_json: str,
) -> Optional[Mapping[str, Any]]:
    """Parse ``ctx.intake_evidence_json`` and return the dict
    if it carries ``category=="cluster_coverage"``; None otherwise.
    NEVER raises.

    The shape we expect (from
    :func:`ProactiveExplorationSensor._emit_cluster_coverage_signals`)::

        {
            "category": "cluster_coverage",
            "cluster_id": int,
            "centroid_hash8": str,
            "kind": str,
            "theme_label": str,
            "cluster_size": int,
            "sensor": "ProactiveExplorationSensor",
            "target_files_source": "representative_paths" | "project_root_sentinel",
            "representative_paths_count": int,
        }
    """
    try:
        if not intake_evidence_json:
            return None
        if not isinstance(intake_evidence_json, str):
            return None
        data = json.loads(intake_evidence_json)
        if not isinstance(data, dict):
            return None
        if data.get("category") != _CLUSTER_COVERAGE_CATEGORY:
            return None
        # Required fields for cascade write.
        hash8 = data.get("centroid_hash8")
        if not isinstance(hash8, str) or not hash8.strip():
            return None
        return data
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    except Exception as exc:  # noqa: BLE001 -- last-resort
        logger.debug(
            "[ClusterCascade] _parse_cluster_coverage_tag "
            "degraded: %s", exc,
        )
        return None


# ---------------------------------------------------------------------------
# Write side: persist on successful verify
# ---------------------------------------------------------------------------


async def observe_cluster_coverage_completion(
    *,
    op_id: str,
    intake_evidence_json: str,
    touched_files: Tuple[str, ...] = (),
    verify_passed: bool = True,
    project_root: Optional[Path] = None,
    store: Optional[DomainMapStore] = None,
) -> Optional[DomainMapEntry]:
    """Async post-verify hook. Records the exploration into
    DomainMap when the op was a cluster_coverage exploration that
    completed successfully (verify passed). NEVER raises.

    Returns the persisted :class:`DomainMapEntry` on write; None
    on every short-circuit / failure path:

      * cascade observer flag off
      * DomainMap flag off
      * verify did NOT pass (we only memorialize successful
        explorations -- failed ones contribute no domain knowledge)
      * intake_evidence_json doesn't carry cluster_coverage tag
      * DomainMapStore singleton not yet initialized AND
        project_root not provided
      * persistence layer returned None (lock timeout / disk error)

    The async signature matches the orchestrator's existing
    post-verify hook discipline; the body is sync (no await),
    but keeping async preserves the contract for future
    extensions (Venom role inference round in a follow-up arc).
    """
    try:
        if not cascade_observer_enabled():
            return None
        if not domain_map_enabled():
            return None
        if not verify_passed:
            return None
        tag = _parse_cluster_coverage_tag(intake_evidence_json)
        if tag is None:
            return None

        # Resolve store: caller-injected wins; otherwise the
        # singleton (which the orchestrator init-time wires with
        # project_root). If neither is available, short-circuit.
        target_store = store
        if target_store is None:
            target_store = get_default_store(project_root)
        if target_store is None:
            logger.debug(
                "[ClusterCascade] no DomainMapStore available "
                "for op=%s -- cascade skipped", op_id,
            )
            return None

        # Architectural-role inference: STUBBED. When
        # auto_role_enabled() is on, we mark the entry with a
        # ``role_inference_pending`` placeholder so future arcs
        # can identify entries that would have benefited from a
        # Venom round. When off, we record empty -- never
        # clobbers any pre-existing role thanks to DomainMapStore's
        # caller-wins-if-non-empty merge semantics.
        role = (
            "role_inference_pending"
            if auto_role_enabled() else ""
        )

        # Filter touched_files: must be string + non-empty + not
        # the project-root sentinel (Slice 2's fall-through). The
        # cascade observer only persists files the model actually
        # touched.
        filtered_files: Tuple[str, ...] = tuple(
            p for p in (touched_files or ())
            if isinstance(p, str) and p and p != "."
        )

        # Coerce optional fields from tag (defensive).
        try:
            cluster_id = int(tag.get("cluster_id", -1))
        except (TypeError, ValueError):
            cluster_id = -1
        theme_label = str(tag.get("theme_label", "") or "")
        centroid_hash8 = str(tag.get("centroid_hash8", "") or "")

        merged = target_store.record_exploration(
            centroid_hash8,
            theme_label=theme_label,
            discovered_files=filtered_files,
            architectural_role=role,
            confidence=1.0,  # verify passed -> high confidence
            cluster_id=cluster_id,
            op_id=op_id,
        )
        # Slice 5 graduation: best-effort SSE publish on every
        # successful persist so observability sees the full
        # cross-session memory evolution. Lazy import keeps the
        # cascade observer importable when the IDE observability
        # stream module isn't available (test isolation, partial
        # deployments). NEVER raises.
        if merged is not None:
            try:
                from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                    publish_domain_map_update as _publish_domain_map_update,
                )
                _publish_domain_map_update(
                    centroid_hash8=merged.centroid_hash8,
                    cluster_id=merged.cluster_id,
                    theme_label=merged.theme_label,
                    discovered_files_count=len(
                        merged.discovered_files,
                    ),
                    architectural_role=merged.architectural_role,
                    confidence=merged.confidence,
                    exploration_count=merged.exploration_count,
                    populated_by_op_id=merged.populated_by_op_id,
                )
            except Exception as exc:  # noqa: BLE001 -- best-effort
                logger.debug(
                    "[ClusterCascade] SSE publish degraded: %s", exc,
                )
        return merged
    except Exception as exc:  # noqa: BLE001 -- last-resort defensive
        logger.debug(
            "[ClusterCascade] observe_cluster_coverage_completion "
            "last-resort degraded for op=%s: %s", op_id, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Read side: render prior-context block for envelope description
# ---------------------------------------------------------------------------


def render_prior_context_block(
    centroid_hash8: str,
    *,
    project_root: Optional[Path] = None,
    store: Optional[DomainMapStore] = None,
) -> str:
    """Return a markdown snippet summarizing prior exploration
    of this cluster, or empty string when no prior entry exists.
    Consumed by ProactiveExplorationSensor's envelope build to
    enrich the description with cross-session memory.

    Output shape (when entry exists with files + role)::

        Previously explored (count=3): voice/auth.py, voice/util.py.
        Architectural role: voice biometric primitive.

    Output shape (entry exists but no role yet)::

        Previously explored (count=1): voice/auth.py.

    Output shape (no entry / observer off / store missing)::

        ""

    NEVER raises.
    """
    try:
        if not cascade_observer_enabled():
            return ""
        if not domain_map_enabled():
            return ""
        if not isinstance(centroid_hash8, str) or not centroid_hash8.strip():
            return ""
        target_store = store
        if target_store is None:
            target_store = get_default_store(project_root)
        if target_store is None:
            return ""
        entry = target_store.lookup_by_centroid_hash8(centroid_hash8)
        if entry is None:
            return ""
        # Build the snippet.
        max_files = _prior_context_max_files()
        files_to_show = entry.discovered_files[:max_files]
        files_str = ", ".join(files_to_show)
        if not files_str:
            files_str = "(no files recorded)"
        line1 = (
            f"Previously explored "
            f"(count={entry.exploration_count}): {files_str}."
        )
        # Role line is conditional -- only when known + not the
        # placeholder marker.
        role = (entry.architectural_role or "").strip()
        if role and role != "role_inference_pending":
            return (
                f"{line1} Architectural role: {role}."
            )
        return line1
    except Exception as exc:  # noqa: BLE001 -- last-resort
        logger.debug(
            "[ClusterCascade] render_prior_context_block "
            "degraded for hash=%s: %s", centroid_hash8, exc,
        )
        return ""


__all__ = [
    "CLUSTER_CASCADE_SCHEMA_VERSION",
    "auto_role_enabled",
    "cascade_observer_enabled",
    "observe_cluster_coverage_completion",
    "register_flags",
    "register_shipped_invariants",
    "render_prior_context_block",
]


# ---------------------------------------------------------------------------
# Slice 5 -- Module-owned FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.warning(
            "[ClusterCascade] register_flags degraded: %s", exc,
        )
        return 0
    target = (
        "backend/core/ouroboros/governance/"
        "cluster_exploration_cascade_observer.py"
    )
    specs = [
        FlagSpec(
            name="JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED",
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=target,
            example=(
                "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED=true"
            ),
            description=(
                "Master switch for the ClusterIntelligence cascade "
                "observer. When off, observe_cluster_coverage_"
                "completion is no-op + render_prior_context_block "
                "returns empty string. Graduated default-true "
                "2026-05-03 in Slice 5."
            ),
        ),
        FlagSpec(
            name="JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED",
            type=FlagType.BOOL, default=False,
            category=Category.EXPERIMENTAL,
            source_file=target,
            example="JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED=true",
            description=(
                "Stub flag for architectural-role inference via "
                "Venom round. Today only stamps a "
                "``role_inference_pending`` placeholder marker; "
                "actual Venom call deferred to a post-Slice-5 "
                "cost-authorized arc. Default false (no cost "
                "commitment)."
            ),
        ),
        FlagSpec(
            name="JARVIS_CLUSTER_CASCADE_PRIOR_CONTEXT_MAX_FILES",
            type=FlagType.INT, default=8,
            category=Category.CAPACITY,
            source_file=target,
            example=(
                "JARVIS_CLUSTER_CASCADE_PRIOR_CONTEXT_MAX_FILES=16"
            ),
            description=(
                "Hard cap on prior-discovered files surfaced in "
                "the envelope description block. Floor 1, ceiling "
                "32. Independent of Slice 1's K knob."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[ClusterCascade] register_flags spec %s "
                "skipped: %s", spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Slice 5 -- Module-owned shipped_code_invariants
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Slice 4 invariants: authority allowlist (only DomainMap +
    additive observability + flag registration) + NEVER-raise
    discipline pin."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    _ALLOWED = {
        "domain_map_memory",
        # Additive observability (Slice 5 SSE publish + module-owned
        # registration). Never reached on the hot path's defensive
        # short-circuit branches.
        "ide_observability_stream",
        "flag_registry",
        "shipped_code_invariants",
    }
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
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                if "backend." not in module and "governance" not in module:
                    continue
                tail = module.rsplit(".", 1)[-1]
                if tail in _FORBIDDEN:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"forbidden module {module!r}"
                    )
                elif tail not in _ALLOWED:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"unexpected governance import {module!r}"
                    )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"Slice 4 MUST NOT {node.func.id}()"
                        )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/"
        "cluster_exploration_cascade_observer.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="cluster_cascade_observer_authority",
            target_file=target,
            description=(
                "Slice 4 cascade observer authority: imports "
                "only domain_map_memory + the registration "
                "contract (flag_registry + shipped_code_invariants) "
                "+ ide_observability_stream (SSE publish). "
                "Forbidden: orchestrator / iron_gate / providers / "
                "tool_executor / etc. No exec/eval/compile."
            ),
            validate=_validate,
        ),
    ]
