"""Dynamic Entropy Engine — Slice 101 Phase 8 (Compositional Curiosity).

The PROACTIVE counterpart to the defensive substrates: instead of waiting for a
signal, O+V mathematically scans its own semantic map to find the architectural
zones it knows LEAST about, and proactively schedules governed exploration there.

ROOT-PROBLEM-FIRST (verify-first, 2026-06-06): we do NOT build a random-walk or
an 'explore' flag, and we do NOT duplicate the existing curiosity substrates
(``curiosity_gradient`` already computes per-generation logprob/prophecy entropy;
``compositional_curiosity`` computes maturity-novelty pairs). The one genuinely
missing signal is **knowledge sparsity over codebase domains**: a Shannon-entropy
read of the ``semantic_index`` cluster distribution. Sparse clusters (few samples)
are the zones O+V has explored least → the highest-value curiosity targets.

    H(X) = -Σ P(x) log2 P(x)      P(cluster_i) = size_i / Σ size

CAGE INVARIANT (load-bearing): curiosity chooses WHERE to look; it NEVER runs a
probe directly. Each identified zone becomes a GOVERNED ``IntentEnvelope``
(source=exploration, urgency=low) routed through ``unified_intake_router.ingest``
— so every proactive probe passes the full First-Order cage (Iron Gate,
SemanticGuardian, rehearsal floor, worktree isolation, risk tiers) exactly like
any other op. Bypassing the cage for curiosity would unravel Phases 1-7.

BUDGET (recoverable throttle): the number of zones emitted per scan is a PURE
function of the current ``cognitive_load_shedding`` verdict — NORMAL → full,
ELEVATED → halved, OVERLOADED → zero. No latch: when load recovers, curiosity
recovers automatically (the Slice 98 recovery invariant). The intake's own
cognitive-shed gate (Phase 4) is a second backstop on these low-urgency ops.

CLOSED LOOP (no new wiring): a governed exploration op completes → publishes
post_apply/post_failure → the Phase-3 belief subscriber records it → the Phase-6
sleep daemon consolidates it into the Synthetic Soul. Mapping new territory
permanently updates deep memory, for free.

Master ``JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED`` — §33.1 default-FALSE. NEVER raises.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("ouroboros.domain_entropy_engine")

_ENV_ENABLED = "JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED"
_ENV_BASE_BUDGET = "JARVIS_DOMAIN_ENTROPY_BASE_BUDGET"
_ENV_MAX_ZONES = "JARVIS_DOMAIN_ENTROPY_MAX_ZONES"
_TRUTHY = ("1", "true", "yes", "on")

_DEFAULT_BASE_BUDGET = 3
_DEFAULT_MAX_ZONES = 8

DOMAIN_ENTROPY_SCHEMA_VERSION = "domain_entropy.1"

# Exploration ops are the lowest-urgency (deferrable) tier so the Phase-4
# cognitive-shed gate can throttle them under load. Mirrors the existing
# ProactiveExplorationSensor curiosity emission.
_EXPLORATION_SOURCE = "exploration"
_EXPLORATION_URGENCY = "low"


def domain_entropy_engine_enabled() -> bool:
    """§33.1 master — default FALSE. Never raises."""
    try:
        raw = os.environ.get(_ENV_ENABLED)
        if raw is None:
            return False
        return raw.strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return False


def _base_budget() -> int:
    try:
        return max(0, int(os.environ.get(_ENV_BASE_BUDGET, str(_DEFAULT_BASE_BUDGET))))
    except Exception:  # noqa: BLE001
        return _DEFAULT_BASE_BUDGET


def _max_zones() -> int:
    try:
        return max(1, int(os.environ.get(_ENV_MAX_ZONES, str(_DEFAULT_MAX_ZONES))))
    except Exception:  # noqa: BLE001
        return _DEFAULT_MAX_ZONES


@dataclass(frozen=True)
class SparseZone:
    """One under-explored domain — a curiosity target."""

    cluster_id: str
    kind: str
    size: int
    probability: float       # P(cluster) = size / total
    sparsity_score: float    # 1 - normalized density, in [0, 1] (higher = sparser)
    representative_paths: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DomainEntropyReport:
    """Shannon-entropy read of the semantic-index domain distribution."""

    master_enabled: bool
    cluster_count: int
    total_samples: int
    total_entropy_bits: float        # H(X)
    max_entropy_bits: float          # log2(n) — uniform reference
    normalized_entropy: float        # H / max, in [0, 1] (1 = maximally spread)
    sparse_zones: Tuple[SparseZone, ...]
    diagnostic: str
    schema_version: str = DOMAIN_ENTROPY_SCHEMA_VERSION


@dataclass(frozen=True)
class CuriosityScanReport:
    """Outcome of one proactive scan + governed-emission cycle."""

    master_enabled: bool
    normalized_entropy: float
    zones_identified: int
    budget: int
    emitted: int
    ingest_results: Tuple[str, ...]
    diagnostic: str
    schema_version: str = DOMAIN_ENTROPY_SCHEMA_VERSION


def _disabled_entropy_report() -> DomainEntropyReport:
    return DomainEntropyReport(
        master_enabled=False,
        cluster_count=0,
        total_samples=0,
        total_entropy_bits=0.0,
        max_entropy_bits=0.0,
        normalized_entropy=0.0,
        sparse_zones=(),
        diagnostic=f"disabled via {_ENV_ENABLED}=false",
    )


def _load_clusters() -> Sequence[Mapping[str, Any]]:
    """Read the live semantic-index cluster distribution. NEVER raises;
    returns () when the index is unavailable."""
    try:
        from backend.core.ouroboros.governance.semantic_index import (
            get_default_index,
        )
        stats = get_default_index().stats()
        clusters = getattr(stats, "clusters", None) or []
        return [c for c in clusters if isinstance(c, Mapping)]
    except Exception as exc:  # noqa: BLE001
        logger.debug("[DomainEntropy] cluster load failed: %s", exc)
        return ()


def compute_domain_entropy(
    *,
    clusters: Optional[Sequence[Mapping[str, Any]]] = None,
    now_unix: Optional[float] = None,
) -> DomainEntropyReport:
    """Compute the Shannon entropy of the domain (cluster) distribution and rank
    the sparsest zones. Pure + NEVER raises. ``clusters`` is an injectable seam
    (each item a mapping with at least ``size``; optional ``kind`` /
    ``cluster_id`` / ``representative_paths``); defaults to the live index.
    Returns a DISABLED report when the master flag is off.
    """
    if not domain_entropy_engine_enabled():
        return _disabled_entropy_report()
    rows = clusters if clusters is not None else _load_clusters()

    sized: List[Tuple[str, str, int, Tuple[str, ...]]] = []
    for i, c in enumerate(rows):
        try:
            size = int(c.get("size", 0) or 0)
            if size <= 0:
                continue
            cid = str(c.get("cluster_id", c.get("id", i)))
            kind = str(c.get("kind", "mixed"))
            reps = tuple(str(p) for p in (c.get("representative_paths", ()) or ()))[:8]
            sized.append((cid, kind, size, reps))
        except Exception:  # noqa: BLE001 — skip a malformed cluster, never abort
            continue

    total = sum(s[2] for s in sized)
    if total <= 0 or not sized:
        return DomainEntropyReport(
            master_enabled=True,
            cluster_count=0,
            total_samples=0,
            total_entropy_bits=0.0,
            max_entropy_bits=0.0,
            normalized_entropy=0.0,
            sparse_zones=(),
            diagnostic="no clusters with samples — index empty or unbuilt",
        )

    n = len(sized)
    max_size = max(s[2] for s in sized)
    entropy = 0.0
    for _cid, _kind, size, _reps in sized:
        p = size / total
        if p > 0.0:
            entropy -= p * math.log2(p)
    max_entropy = math.log2(n) if n > 1 else 0.0
    normalized = (entropy / max_entropy) if max_entropy > 0.0 else 0.0

    # Sparse zones: ascending size — the fewest-sample domains are least-explored.
    zones: List[SparseZone] = []
    for cid, kind, size, reps in sorted(sized, key=lambda s: (s[2], s[0])):
        p = size / total
        sparsity = 1.0 - (size / max_size) if max_size > 0 else 0.0
        zones.append(SparseZone(
            cluster_id=cid,
            kind=kind,
            size=size,
            probability=p,
            sparsity_score=max(0.0, min(1.0, sparsity)),
            representative_paths=reps,
        ))

    return DomainEntropyReport(
        master_enabled=True,
        cluster_count=n,
        total_samples=total,
        total_entropy_bits=entropy,
        max_entropy_bits=max_entropy,
        normalized_entropy=max(0.0, min(1.0, normalized)),
        sparse_zones=tuple(zones[: _max_zones()]),
        diagnostic=(
            f"H={entropy:.4f}b / max={max_entropy:.4f}b "
            f"(norm={normalized:.3f}) over {n} domains, {total} samples"
        ),
    )


def exploration_budget(
    *,
    load_report: Optional[Any] = None,
    base_budget: Optional[int] = None,
) -> int:
    """Recoverable curiosity budget = PURE function of the current cognitive-load
    verdict. NORMAL/DISABLED → full base; ELEVATED → halved; OVERLOADED → zero.
    No latch: budget recovers automatically when load recovers. NEVER raises.
    ``load_report`` is injectable (defaults to a live evaluate_cognitive_load).
    """
    base = _base_budget() if base_budget is None else max(0, int(base_budget))
    try:
        report = load_report
        if report is None:
            from backend.core.ouroboros.governance.cognitive_load_shedding import (
                evaluate_cognitive_load,
            )
            report = evaluate_cognitive_load()
        verdict = str(getattr(getattr(report, "verdict", None), "value", "") or "")
        if verdict == "overloaded":
            return 0
        if verdict == "elevated":
            return base // 2
        # normal / disabled (load substrate off → no throttle signal) → full budget
        return base
    except Exception:  # noqa: BLE001 — never let the budget calc break the scan
        return base


def build_exploration_envelopes(
    report: DomainEntropyReport,
    budget: int,
    *,
    repo: str = ".",
    make_envelope_fn: Any = None,
) -> List[Any]:
    """Map the top-``budget`` sparsest zones to GOVERNED exploration envelopes.
    Pure (no emission). Each envelope is source=exploration, urgency=low (so the
    cognitive-shed gate can throttle it) — a request for the cage-protected loop
    to explore, NOT a probe to run directly. NEVER raises; returns [] on any
    failure or empty budget.
    """
    if budget <= 0 or not report.sparse_zones:
        return []
    try:
        if make_envelope_fn is None:
            from backend.core.ouroboros.governance.intake.intent_envelope import (
                make_envelope,
            )
            make_envelope_fn = make_envelope
    except Exception:  # noqa: BLE001
        return []

    envelopes: List[Any] = []
    for zone in report.sparse_zones[: max(0, int(budget))]:
        try:
            tf = zone.representative_paths if zone.representative_paths else (".",)
            env = make_envelope_fn(
                source=_EXPLORATION_SOURCE,
                description=(
                    f"Entropy-driven exploration: domain '{zone.cluster_id}' "
                    f"(kind={zone.kind}) is sparsely mapped — only {zone.size} "
                    f"sample(s), P={zone.probability:.3f}, sparsity="
                    f"{zone.sparsity_score:.2f}. The organism has explored this "
                    f"architectural zone least; map it (read + characterize, "
                    f"propose a test or minimal safe improvement)."
                ),
                target_files=tuple(tf),
                repo=repo,
                confidence=round(min(1.0, max(0.0, zone.sparsity_score)), 4),
                urgency=_EXPLORATION_URGENCY,
                evidence={
                    "category": "entropy_driven_curiosity",
                    "cluster_id": zone.cluster_id,
                    "kind": zone.kind,
                    "size": zone.size,
                    "probability": zone.probability,
                    "sparsity_score": zone.sparsity_score,
                    "normalized_entropy": report.normalized_entropy,
                    "sensor": "DomainEntropyEngine",
                },
                requires_human_ack=False,
            )
            envelopes.append(env)
        except Exception as exc:  # noqa: BLE001 — skip a bad zone, never abort
            logger.debug("[DomainEntropy] envelope build failed: %s", exc)
            continue
    return envelopes


async def run_curiosity_scan_once(
    *,
    router: Any,
    clusters: Optional[Sequence[Mapping[str, Any]]] = None,
    load_report: Optional[Any] = None,
    repo: str = ".",
    now_unix: Optional[float] = None,
) -> CuriosityScanReport:
    """Run ONE proactive scan + GOVERNED emission cycle. Computes domain entropy,
    allocates a recoverable budget, and emits the sparsest zones as governed
    exploration ops via ``router.ingest`` — every probe passes the full cage.
    NEVER raises; inert (emits nothing) when the master flag is off, the budget
    is zero (overloaded — recoverable), or the router is missing.
    """
    if not domain_entropy_engine_enabled():
        return CuriosityScanReport(
            master_enabled=False, normalized_entropy=0.0, zones_identified=0,
            budget=0, emitted=0, ingest_results=(),
            diagnostic="domain entropy engine disabled",
        )
    report = compute_domain_entropy(clusters=clusters, now_unix=now_unix)
    budget = exploration_budget(load_report=load_report)
    envelopes = build_exploration_envelopes(report, budget, repo=repo)
    results: List[str] = []
    emitted = 0
    if router is not None:
        for env in envelopes:
            try:
                res = await router.ingest(env)
                results.append(str(res))
                if str(res) in ("enqueued", "pending_ack"):
                    emitted += 1
            except Exception as exc:  # noqa: BLE001 — one bad ingest never aborts the scan
                logger.debug("[DomainEntropy] ingest failed: %s", exc)
                results.append("error")
    diag = (
        f"entropy_norm={report.normalized_entropy:.3f} "
        f"zones={len(report.sparse_zones)} budget={budget} emitted={emitted}"
    )
    if emitted > 0:
        logger.info("[DomainEntropy] proactive scan — %s", diag)
    return CuriosityScanReport(
        master_enabled=True,
        normalized_entropy=report.normalized_entropy,
        zones_identified=len(report.sparse_zones),
        budget=budget,
        emitted=emitted,
        ingest_results=tuple(results),
        diagnostic=diag,
    )
