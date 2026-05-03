"""
ProactiveExplorationSensor — Curiosity-driven domain exploration.

P3 Gap: Ouroboros waits for problems vs seeking knowledge. This sensor
uses the chronic entropy signal to identify domains with high uncertainty,
then triggers Oracle re-indexing and context enrichment for those areas.

Boundary Principle:
  Deterministic: Read chronic entropy scores, identify high-uncertainty
  domains, trigger re-indexing. No model inference for detection.
  Agentic: The enriched context feeds into future GENERATE prompts
  where the model can reason about the newly indexed knowledge.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = float(
    os.environ.get("JARVIS_EXPLORATION_INTERVAL_S", "7200")  # Every 2 hours
)
_ENTROPY_EXPLORATION_THRESHOLD = float(
    os.environ.get("JARVIS_EXPLORATION_ENTROPY_THRESHOLD", "0.4")
)


def _cluster_emit_per_scan() -> int:
    """Per-scan cap on cluster-coverage emissions.

    Default 1 — one under-touched cluster surfaced per scan keeps the
    intake queue from flooding while still inverting the doc_staleness:
    exploration ratio over a single shift. Clamped to [1, 8].
    """
    raw = os.environ.get(
        "JARVIS_EXPLORATION_CLUSTER_EMIT_PER_SCAN", "",
    ).strip()
    try:
        val = int(raw) if raw else 1
    except (TypeError, ValueError):
        val = 1
    return max(1, min(8, val))


def _use_representative_paths_enabled() -> bool:
    """``JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS``
    (default ``true`` post Slice 5 graduation, 2026-05-03).

    ClusterIntelligence-CrossSession Slice 2 sub-flag. When on,
    cluster_coverage envelopes use ``cluster.representative_paths``
    as ``target_files=`` (when the tuple is non-empty) instead of
    the legacy ``(".",)`` project-root sentinel. Composes with
    Slice 1's master flag -- when Slice 1 is off,
    representative_paths is always empty and this flag has no
    visible effect (sentinel fall-through). Operators flip OFF to
    keep the path-aware enrichment in observability without
    affecting envelope routing (shadow mode).
    """
    raw = os.environ.get(
        "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


def _representative_path_envelope_cap() -> int:
    """``JARVIS_EXPLORATION_REPRESENTATIVE_PATH_CAP`` (default 8,
    floor 1, ceiling 32). Hard cap on the number of paths surfaced
    in a cluster_coverage envelope's ``target_files`` -- prevents
    a pathological cluster from flooding intake routing budget.
    Independent of Slice 1's top-K knob (which controls what
    enrichment writes); this knob caps what the sensor consumes."""
    raw = os.environ.get(
        "JARVIS_EXPLORATION_REPRESENTATIVE_PATH_CAP", "",
    ).strip()
    try:
        val = int(raw) if raw else 8
    except (TypeError, ValueError):
        val = 8
    return max(1, min(32, val))


class ProactiveExplorationSensor:
    """Curiosity sensor — explores domains where the organism is uncertain.

    Reads the LearningConsolidator's domain rules and chronic entropy
    scores. When a domain has persistently high uncertainty, triggers:
    1. Oracle re-indexing of files in that domain
    2. IntentEnvelope emission for proactive context gathering

    The organism doesn't just fix problems — it seeks to understand
    areas where it's weak, BEFORE failures occur.
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        poll_interval_s: float = _POLL_INTERVAL_S,
        project_root: Optional[Path] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._project_root = project_root or Path(".")
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._explored_domains: set[str] = set()
        # Slice 3 — per-session dedup of cluster-coverage emissions
        # keyed on centroid_hash8 (stable across rebuilds when the
        # cluster shape doesn't change; new shape → new hash → new
        # emission opportunity).
        self._explored_clusters: set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"exploration_sensor_{self._repo}"
        )
        logger.info("[ExplorationSensor] Started for repo=%s", self._repo)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _poll_loop(self) -> None:
        await asyncio.sleep(600.0)  # Let system stabilize
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[ExplorationSensor] Poll error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break

    async def scan_once(self) -> List[str]:
        """Identify high-uncertainty domains and trigger exploration."""
        explored: List[str] = []

        try:
            from backend.core.ouroboros.governance.adaptive_learning import (
                LearningConsolidator,
            )
            consolidator = LearningConsolidator()
            all_rules = consolidator._rules

            for domain_key, rules in all_rules.items():
                if domain_key in self._explored_domains:
                    continue

                # Check if domain has high failure rules
                failure_rules = [
                    r for r in rules
                    if r.rule_type == "common_failure" and r.confidence > _ENTROPY_EXPLORATION_THRESHOLD
                ]

                if not failure_rules:
                    continue

                # This domain has persistent issues — trigger exploration
                self._explored_domains.add(domain_key)
                explored.append(domain_key)

                # Emit an IntentEnvelope for proactive investigation
                top_rule = max(failure_rules, key=lambda r: r.confidence)
                try:
                    envelope = make_envelope(
                        source="exploration",
                        description=(
                            f"Proactive exploration: domain '{domain_key}' has "
                            f"persistent uncertainty (confidence={top_rule.confidence:.0%}, "
                            f"n={top_rule.sample_size}). {top_rule.description}"
                        ),
                        target_files=self._infer_target_files(domain_key),
                        repo=self._repo,
                        confidence=0.70,
                        urgency="low",
                        evidence={
                            "category": "proactive_exploration",
                            "domain_key": domain_key,
                            "rule_confidence": top_rule.confidence,
                            "rule_type": top_rule.rule_type,
                            "sensor": "ProactiveExplorationSensor",
                        },
                        requires_human_ack=False,
                    )
                    await self._router.ingest(envelope)
                    logger.info(
                        "[ExplorationSensor] Exploring domain=%s "
                        "(confidence=%.0f%%, n=%d)",
                        domain_key, top_rule.confidence * 100, top_rule.sample_size,
                    )
                except Exception:
                    logger.debug(
                        "[ExplorationSensor] Emit failed for %s", domain_key
                    )

        except ImportError as exc:
            # LearningConsolidator unavailable — log once at debug to aid ops
            # triage ("why is proactive_exploration emitting zero signals?")
            # without spamming the poll loop every 2 hours.
            if not self._explored_domains:
                logger.debug(
                    "[ExplorationSensor] LearningConsolidator import failed; "
                    "sensor will be inert this cycle: %s",
                    exc,
                )
        except Exception:
            logger.debug("[ExplorationSensor] Scan error", exc_info=True)

        # CodebaseCharacterDigest Slice 3 — cluster-coverage bias.
        # Independent signal source: emits even when no failure rules
        # exist. Inverts the doc_staleness:exploration ratio observed
        # in soak v3 baseline by giving exploration a structural
        # cadence keyed on the SemanticIndex k-means cluster set.
        cluster_explored = await self._emit_cluster_coverage_signals()
        explored.extend(cluster_explored)

        return explored

    async def _emit_cluster_coverage_signals(self) -> List[str]:
        """Emit IntentEnvelopes for under-touched semantic clusters.

        CodebaseCharacterDigest Slice 3 wire-up. Reads the existing
        ``SemanticIndex.clusters`` artifact (already built by the v1.0
        k-means path) and projects through ``compute_codebase_character``.
        For each READY cluster not yet explored this session, emits a
        single IntentEnvelope inviting O+V to explore the cluster's
        domain. Targets are intentionally empty so the model uses its
        tool loop (read_file / search_code) to discover representative
        files — no hardcoded path inference.

        Discipline:
          * Fail-silent on any exception — never break the parent scan.
          * ImportError-safe — codebase_character / semantic_index
            module missing → empty list.
          * Per-scan cap (env-tunable, default 1) prevents intake flood.
          * Session-dedup on ``centroid_hash8`` — same cluster shape
            doesn't re-fire within a session.
          * Cost contract preserved by construction: zero LLM calls,
            zero file I/O, zero git invocations on the sensor path.
        """
        emitted: List[str] = []
        try:
            from backend.core.ouroboros.governance.codebase_character import (  # noqa: E501
                codebase_character_enabled,
                compute_codebase_character,
            )
            from backend.core.ouroboros.governance.semantic_index import (
                get_default_index,
            )
        except ImportError:
            return emitted
        if not codebase_character_enabled():
            return emitted
        try:
            import time as _time
            idx = get_default_index()
            stats = idx.stats()
            snapshot = compute_codebase_character(
                enabled=True,
                clusters=idx.clusters,
                cluster_mode=getattr(stats, "cluster_mode", "") or "",
                total_corpus_items=int(getattr(stats, "corpus_n", 0) or 0),
                built_at_ts=float(getattr(stats, "built_at", 0.0) or 0.0),
                generated_at_ts=_time.time(),
            )
            if not snapshot.is_ready():
                return emitted
            cap = _cluster_emit_per_scan()
            for cluster in snapshot.clusters:
                if cluster.centroid_hash8 in self._explored_clusters:
                    continue
                if len(emitted) >= cap:
                    break
                self._explored_clusters.add(cluster.centroid_hash8)
                # Slice 2 (ClusterIntelligence-CrossSession): substitute
                # representative_paths for the project-root sentinel
                # when (a) the sub-flag is on AND (b) the cluster has
                # non-empty paths. Composes with Slice 1's master --
                # when Slice 1 is off, representative_paths is always
                # empty and the sentinel fallback fires regardless.
                rep_paths_raw = tuple(
                    getattr(cluster, "representative_paths", ()) or ()
                )
                use_paths = (
                    _use_representative_paths_enabled()
                    and bool(rep_paths_raw)
                )
                if use_paths:
                    cap = _representative_path_envelope_cap()
                    target_files: Tuple[str, ...] = tuple(
                        rep_paths_raw[:cap],
                    )
                    description_path_hint = (
                        f" Representative files: "
                        f"{', '.join(target_files)}."
                    )
                else:
                    target_files = (".",)
                    description_path_hint = (
                        " Use search_code / read_file to discover "
                        "representative files in this domain."
                    )
                # ClusterIntelligence-CrossSession Slice 4 -- prefix
                # the description with prior-exploration context from
                # DomainMap when an entry exists for this cluster.
                # Empty string when: cascade observer flag off, or
                # DomainMap flag off, or no prior entry. Defensive:
                # never raises into the envelope build.
                prior_context_block = ""
                try:
                    from backend.core.ouroboros.governance.cluster_exploration_cascade_observer import (  # noqa: E501
                        render_prior_context_block as _render_prior,
                    )
                    prior_context_block = _render_prior(
                        cluster.centroid_hash8,
                        project_root=self._project_root,
                    )
                except Exception:  # noqa: BLE001 -- defensive
                    prior_context_block = ""
                prior_context_prefix = (
                    f"{prior_context_block} "
                    if prior_context_block else ""
                )
                try:
                    envelope = make_envelope(
                        source="exploration",
                        description=(
                            f"{prior_context_prefix}"
                            f"Cluster-coverage exploration: under-touched "
                            f"semantic cluster '{cluster.theme_label}' "
                            f"(kind={cluster.kind}, size={cluster.size})."
                            f"{description_path_hint}"
                            f" Excerpt: {cluster.nearest_item_excerpt}"
                        ),
                        target_files=target_files,
                        repo=self._repo,
                        confidence=0.65,
                        urgency="low",
                        evidence={
                            "category": "cluster_coverage",
                            "cluster_id": cluster.cluster_id,
                            "centroid_hash8": cluster.centroid_hash8,
                            "kind": cluster.kind,
                            "theme_label": cluster.theme_label,
                            "cluster_size": cluster.size,
                            "sensor": "ProactiveExplorationSensor",
                            # Slice 2: emit the path-routing decision
                            # in evidence so observability + future
                            # cascade observers see whether routing
                            # was sentinel or path-aware.
                            "target_files_source": (
                                "representative_paths" if use_paths
                                else "project_root_sentinel"
                            ),
                            "representative_paths_count": len(
                                rep_paths_raw,
                            ),
                        },
                        requires_human_ack=False,
                    )
                    await self._router.ingest(envelope)
                    emitted.append(cluster.centroid_hash8)
                    logger.info(
                        "[ExplorationSensor] Cluster-coverage emit "
                        "cluster=%s kind=%s size=%d hash=%s",
                        cluster.theme_label or "(unlabeled)",
                        cluster.kind, cluster.size,
                        cluster.centroid_hash8,
                    )
                    # SSE publish — best-effort, never breaks the scan.
                    try:
                        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                            EVENT_TYPE_CODEBASE_CHARACTER_INJECTED,
                            get_default_broker,
                        )
                        broker = get_default_broker()
                        if broker is not None:
                            broker.publish(
                                event_type=(
                                    EVENT_TYPE_CODEBASE_CHARACTER_INJECTED
                                ),
                                op_id="",
                                payload={
                                    "cluster_id": cluster.cluster_id,
                                    "centroid_hash8": cluster.centroid_hash8,
                                    "kind": cluster.kind,
                                    "theme_label": cluster.theme_label,
                                    "size": cluster.size,
                                },
                            )
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:
                    logger.debug(
                        "[ExplorationSensor] Cluster-coverage emit "
                        "failed for hash=%s",
                        cluster.centroid_hash8,
                    )
        except Exception:
            logger.debug(
                "[ExplorationSensor] Cluster-coverage scan error",
                exc_info=True,
            )
        return emitted

    def _infer_target_files(self, domain_key: str) -> Tuple[str, ...]:
        """Infer representative target files from domain key.

        domain_key format: "category::extension"
        Maps to likely file paths. Deterministic.
        """
        parts = domain_key.split("::")
        ext = parts[1] if len(parts) > 1 else ".py"
        category = parts[0] if parts else "code_gen"

        # Map categories to likely directories
        category_dirs = {
            "code_gen": "backend/core/",
            "test_fix": "tests/",
            "config": "backend/api/config/",
            "dependency": "requirements.txt",
            "documentation": "docs/",
        }
        base_dir = category_dirs.get(category, "backend/")

        if base_dir == "requirements.txt":
            return ("requirements.txt",)

        return (base_dir,)

    def health(self) -> Dict[str, Any]:
        return {
            "sensor": "ProactiveExplorationSensor",
            "repo": self._repo,
            "running": self._running,
            "explored_domains": len(self._explored_domains),
        }


# ---------------------------------------------------------------------------
# ClusterIntelligence-CrossSession Slice 5 -- Module-owned FlagRegistry
# seeds for the Slice 2 envelope-routing additions only.
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.warning(
            "[ProactiveExplorationSensor] register_flags "
            "degraded: %s", exc,
        )
        return 0
    target = (
        "backend/core/ouroboros/governance/intake/sensors/"
        "proactive_exploration_sensor.py"
    )
    specs = [
        FlagSpec(
            name="JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=target,
            example=(
                "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS"
                "=true"
            ),
            description=(
                "Slice 2 sub-flag. When on, cluster_coverage "
                "envelopes use ClusterInfo.representative_paths "
                "as target_files (when non-empty) instead of the "
                "(\".\",) project-root sentinel. Composes with "
                "Slice 1's master flag. Graduated default-true "
                "2026-05-03."
            ),
        ),
        FlagSpec(
            name="JARVIS_EXPLORATION_REPRESENTATIVE_PATH_CAP",
            type=FlagType.INT, default=8,
            category=Category.CAPACITY,
            source_file=target,
            example=(
                "JARVIS_EXPLORATION_REPRESENTATIVE_PATH_CAP=16"
            ),
            description=(
                "Hard cap on the number of paths surfaced in a "
                "cluster_coverage envelope's target_files. Floor "
                "1, ceiling 32. Independent of Slice 1's K knob "
                "(which controls what enrichment WRITES); this "
                "knob caps what the SENSOR consumes."
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
                "[ProactiveExplorationSensor] register_flags "
                "spec %s skipped: %s", spec.name, exc,
            )
    return count
