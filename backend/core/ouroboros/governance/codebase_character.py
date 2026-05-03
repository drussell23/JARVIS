"""CodebaseCharacterDigest — Slice 1 pure-stdlib substrate.

Projects the ``SemanticIndex.clusters`` artifact (already built by the
existing v1.0 k-means path) into a deterministic, prompt-renderable
snapshot of the codebase's semantic character. Consumed at Slice 2 by
``StrategicDirection`` and at Slice 3 by ``ProactiveExplorationSensor``.

Discipline (load-bearing):
  * **Zero clustering of its own** — read-over-existing-artifact only.
    Reuses every byte of compute that ``SemanticIndex._compute_clusters_for_build``
    already paid for.
  * **Zero LLM, zero file I/O, zero git invocations** — pure projection.
  * **Zero caller imports** — substrate stays caller-agnostic.
    ``StrategicDirection`` / ``ProactiveExplorationSensor`` invoke us;
    we never invoke them. AST-pinned at Slice 3.
  * **Total decision function** — ``compute_codebase_character`` NEVER
    raises. Exception path → ``DigestOutcome.FAILED`` → caller fail-open
    (no prompt section emitted, no exploration bias applied).
  * **Cluster source** is a structural ``_ClusterLike`` Protocol so the
    substrate is testable without instantiating ``SemanticIndex`` and
    immune to ``ClusterInfo`` field reordering.

Vocabulary (closed taxonomy, AST-pinned at Slice 3):
  * ``DigestOutcome`` 5-value enum:
    - ``READY`` — snapshot is prompt-injectable
    - ``INSUFFICIENT_CLUSTERS`` — fewer than min_clusters available
    - ``STALE_INDEX`` — built_at_ts is older than stale_after_s
    - ``DISABLED`` — master flag off
    - ``FAILED`` — exception caught; fail-open

Slice 2 will add ``StrategicDirection`` injection. Slice 3 will add
``ProactiveExplorationSensor`` cluster-coverage bias + master flag flip
+ SSE event + GET route + 4 AST pins + 5 FlagSpecs.
"""
from __future__ import annotations

import enum
import logging
import os
import re
from dataclasses import dataclass, field
from typing import (
    Any, Dict, Optional, Protocol, Sequence, Tuple,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema version (pinned in Slice 3 graduation tests)
# ---------------------------------------------------------------------------

CODEBASE_CHARACTER_SCHEMA_VERSION = "codebase_character.v1"


# ---------------------------------------------------------------------------
# Closed vocabulary
# ---------------------------------------------------------------------------


class DigestOutcome(str, enum.Enum):
    """Closed taxonomy of digest outcomes. AST-pinned at Slice 3."""

    READY = "ready"
    INSUFFICIENT_CLUSTERS = "insufficient_clusters"
    STALE_INDEX = "stale_index"
    DISABLED = "disabled"
    FAILED = "failed"


_INJECTABLE_OUTCOMES: Tuple[DigestOutcome, ...] = (
    DigestOutcome.READY,
)
_NON_INJECTABLE_OUTCOMES: Tuple[DigestOutcome, ...] = (
    DigestOutcome.INSUFFICIENT_CLUSTERS,
    DigestOutcome.STALE_INDEX,
    DigestOutcome.DISABLED,
    DigestOutcome.FAILED,
)


# ---------------------------------------------------------------------------
# Env knobs (all clamped, no hardcoding)
# ---------------------------------------------------------------------------


def codebase_character_enabled() -> bool:
    """Master flag. Default-False until Slice 3 graduation.

    Empty / whitespace env value is treated as unset (asymmetric env
    semantics matching AdmissionGate / DirectionInferrer).
    """
    raw = os.environ.get(
        "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # Slice 3 graduation flips this to True.
    return raw in ("1", "true", "yes", "on")


def min_clusters() -> int:
    """Minimum cluster count for ``READY`` outcome.

    Below this, snapshot returns ``INSUFFICIENT_CLUSTERS`` so we don't
    inject a one-cluster digest that adds noise without signal.
    Clamped to ``[1, 16]``.
    """
    raw = os.environ.get(
        "JARVIS_CODEBASE_CHARACTER_MIN_CLUSTERS", "",
    ).strip()
    try:
        val = int(raw) if raw else 2
    except (TypeError, ValueError):
        val = 2
    return max(1, min(16, val))


def stale_after_s() -> float:
    """SemanticIndex build age beyond which the snapshot is ``STALE_INDEX``.

    Default 86400s (24h) — enough that the periodic refresh path
    (``SemanticIndex.build()`` honors ``_refresh_s``) keeps it fresh
    while preventing day-old digests from being injected as authoritative.
    Clamped to ``[60.0, 604800.0]`` (1 min … 7 days).
    """
    raw = os.environ.get(
        "JARVIS_CODEBASE_CHARACTER_STALE_AFTER_S", "",
    ).strip()
    try:
        val = float(raw) if raw else 86400.0
    except (TypeError, ValueError):
        val = 86400.0
    return max(60.0, min(604800.0, val))


def max_clusters_in_digest() -> int:
    """Hard cap on clusters rendered into the prompt section.

    Larger values dilute the per-cluster signal-to-noise ratio without
    helping O+V choose a target. Clamped to ``[1, 32]``.
    """
    raw = os.environ.get(
        "JARVIS_CODEBASE_CHARACTER_MAX_CLUSTERS_IN_DIGEST", "",
    ).strip()
    try:
        val = int(raw) if raw else 8
    except (TypeError, ValueError):
        val = 8
    return max(1, min(32, val))


def excerpt_max_chars() -> int:
    """Per-cluster nearest-item excerpt cap for prompt rendering.

    Clamped to ``[40, 400]``.
    """
    raw = os.environ.get(
        "JARVIS_CODEBASE_CHARACTER_EXCERPT_MAX_CHARS", "",
    ).strip()
    try:
        val = int(raw) if raw else 140
    except (TypeError, ValueError):
        val = 140
    return max(40, min(400, val))


# ---------------------------------------------------------------------------
# Protocol — structural shape of a SemanticIndex ClusterInfo
# ---------------------------------------------------------------------------


class _ClusterLike(Protocol):
    """Structural shape we read from. Anything with these attributes
    works — we never construct ``ClusterInfo`` ourselves, never import
    ``semantic_index`` directly. Immune to field reordering / new
    optional fields landing in ``ClusterInfo``.
    """

    cluster_id: int
    kind: str
    size: int
    nearest_item_text: str
    nearest_item_source: str
    source_composition: Tuple[Tuple[str, int], ...]
    centroid_hash8: str


# ---------------------------------------------------------------------------
# Frozen output records
# ---------------------------------------------------------------------------


_THEME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _extract_theme_label(text: str, *, max_tokens: int = 4) -> str:
    """Extract a deterministic short theme label from raw item text.

    Pure stdlib, byte-level deterministic. Picks the first
    ``max_tokens`` distinct alphanumeric tokens of length ≥3 from the
    leading 200 chars. Matches ``SemanticIndex._theme_label_from_text``
    discipline (no NLP, no embedding) but is a separate implementation
    so the substrate stays decoupled.
    """
    if not text:
        return ""
    head = text[:200]
    seen: list = []
    for match in _THEME_TOKEN_RE.finditer(head):
        tok = match.group(0).lower()
        if tok in seen:
            continue
        seen.append(tok)
        if len(seen) >= max_tokens:
            break
    return " ".join(seen)


def _excerpt(text: str, *, max_chars: int) -> str:
    """Truncate text at a word boundary near max_chars; collapse
    whitespace; never raise.
    """
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) <= max_chars:
        return flat
    cut = flat[:max_chars].rsplit(" ", 1)[0]
    if not cut:
        cut = flat[:max_chars]
    return cut + "..."


@dataclass(frozen=True)
class ClusterCharacter:
    """One cluster projected for prompt + observability surfaces.

    Carries everything downstream consumers need (theme label for
    StrategicDirection rendering, kind for ProactiveExploration bias
    ranking, centroid_hash8 for change detection across rebuilds) and
    nothing they don't (no centroid vector, no per-item vectors, no
    raw scores).
    """

    cluster_id: int
    kind: str
    size: int
    theme_label: str
    nearest_item_excerpt: str
    nearest_item_source: str
    source_composition: Tuple[Tuple[str, int], ...]
    centroid_hash8: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cluster_id": int(self.cluster_id),
            "kind": str(self.kind),
            "size": int(self.size),
            "theme_label": str(self.theme_label),
            "nearest_item_excerpt": str(self.nearest_item_excerpt),
            "nearest_item_source": str(self.nearest_item_source),
            "source_composition": [
                [str(s), int(n)]
                for s, n in self.source_composition
            ],
            "centroid_hash8": str(self.centroid_hash8),
        }


@dataclass(frozen=True)
class CodebaseCharacterSnapshot:
    """Total snapshot returned by ``compute_codebase_character``.

    Frozen / hashable. ``outcome`` is the sole branch criterion for
    consumers — they call ``is_ready()`` and either inject the snapshot
    (READY) or fail-open (every other outcome).
    """

    outcome: DigestOutcome
    clusters: Tuple[ClusterCharacter, ...]
    generated_at_ts: float
    total_corpus_items: int
    cluster_mode: str
    built_at_ts: float
    truncated_count: int = 0  # clusters omitted by max_clusters cap
    failure_reason: str = ""  # populated only on FAILED

    def is_ready(self) -> bool:
        return self.outcome in _INJECTABLE_OUTCOMES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": CODEBASE_CHARACTER_SCHEMA_VERSION,
            "outcome": str(self.outcome.value),
            "clusters": [c.to_dict() for c in self.clusters],
            "generated_at_ts": float(self.generated_at_ts),
            "total_corpus_items": int(self.total_corpus_items),
            "cluster_mode": str(self.cluster_mode),
            "built_at_ts": float(self.built_at_ts),
            "truncated_count": int(self.truncated_count),
            "failure_reason": str(self.failure_reason),
        }

    def to_prompt_section(
        self, *, max_chars: Optional[int] = None,
    ) -> str:
        """Render the deterministic ``## Codebase Character`` section.

        Returns empty string when snapshot is not READY (caller skips
        injection without a code branch). Honors ``max_chars`` budget
        by trimming the cluster list — never truncates a partial
        cluster body. ``max_chars`` ``None`` means no overall cap
        (per-cluster excerpts already capped at ``excerpt_max_chars``).
        """
        if not self.is_ready() or not self.clusters:
            return ""
        lines = [
            "## Codebase Character",
            "",
            (
                "Read-only digest of the semantic clusters present in "
                "the codebase, derived from the SemanticIndex k-means "
                "build. Use this to choose exploration targets in "
                "under-touched domains rather than re-touching the "
                "same areas. Authority over Iron Gate, routing, risk "
                "tier, policy, or FORBIDDEN_PATH matching is "
                "explicitly disclaimed."
            ),
            "",
            (
                f"Total corpus items: {self.total_corpus_items}  ·  "
                f"Cluster mode: {self.cluster_mode}  ·  "
                f"Clusters surfaced: {len(self.clusters)}"
                + (
                    f" (+{self.truncated_count} truncated)"
                    if self.truncated_count else ""
                )
            ),
            "",
        ]
        for cc in self.clusters:
            comp = ", ".join(
                f"{s}={n}" for s, n in cc.source_composition
            )
            header = (
                f"### Cluster {cc.cluster_id} — "
                f"{cc.theme_label or '(unlabeled)'} "
                f"[kind={cc.kind}, size={cc.size}]"
            )
            lines.append(header)
            if comp:
                lines.append(f"Source mix: {comp}")
            if cc.nearest_item_excerpt:
                lines.append(
                    f"Representative: {cc.nearest_item_excerpt}",
                )
            lines.append(
                f"  (signature: {cc.centroid_hash8})",
            )
            lines.append("")
        rendered = "\n".join(lines).rstrip() + "\n"
        if max_chars is not None and len(rendered) > max_chars:
            # Trim by progressively dropping clusters from the tail
            # until under budget. Never split a cluster body.
            trimmed_clusters = list(self.clusters)
            while trimmed_clusters and len(rendered) > max_chars:
                trimmed_clusters.pop()
                trimmed = CodebaseCharacterSnapshot(
                    outcome=self.outcome,
                    clusters=tuple(trimmed_clusters),
                    generated_at_ts=self.generated_at_ts,
                    total_corpus_items=self.total_corpus_items,
                    cluster_mode=self.cluster_mode,
                    built_at_ts=self.built_at_ts,
                    truncated_count=(
                        self.truncated_count
                        + (len(self.clusters) - len(trimmed_clusters))
                    ),
                    failure_reason=self.failure_reason,
                )
                rendered = trimmed.to_prompt_section(max_chars=None)
            if not trimmed_clusters:
                return ""
        return rendered


# ---------------------------------------------------------------------------
# Total decision function
# ---------------------------------------------------------------------------


def compute_codebase_character(
    *,
    enabled: bool,
    clusters: Sequence[_ClusterLike],
    cluster_mode: str,
    total_corpus_items: int,
    built_at_ts: float,
    generated_at_ts: float,
    min_cluster_floor: Optional[int] = None,
    stale_after_s_override: Optional[float] = None,
    max_clusters_cap: Optional[int] = None,
    excerpt_chars: Optional[int] = None,
) -> CodebaseCharacterSnapshot:
    """Total decision function — NEVER raises.

    Decision tree:
      1. ``enabled=False`` → ``DISABLED``
      2. exception path → ``FAILED`` (with ``failure_reason``)
      3. ``built_at_ts == 0`` or build older than ``stale_after_s`` →
         ``STALE_INDEX``
      4. ``len(clusters) < min_clusters`` → ``INSUFFICIENT_CLUSTERS``
      5. otherwise → ``READY`` with up to ``max_clusters_cap`` projected,
         remainder counted in ``truncated_count``

    All env knobs may be overridden by explicit kwargs (test ergonomics
    + production lets the caller pin a config).
    """
    try:
        empty: Tuple[ClusterCharacter, ...] = ()
        if not enabled:
            return CodebaseCharacterSnapshot(
                outcome=DigestOutcome.DISABLED,
                clusters=empty,
                generated_at_ts=float(generated_at_ts),
                total_corpus_items=int(total_corpus_items or 0),
                cluster_mode=str(cluster_mode or ""),
                built_at_ts=float(built_at_ts or 0.0),
            )

        floor = (
            int(min_cluster_floor)
            if min_cluster_floor is not None
            else min_clusters()
        )
        cap = (
            int(max_clusters_cap)
            if max_clusters_cap is not None
            else max_clusters_in_digest()
        )
        stale_s = (
            float(stale_after_s_override)
            if stale_after_s_override is not None
            else stale_after_s()
        )
        excerpt_cap = (
            int(excerpt_chars)
            if excerpt_chars is not None
            else excerpt_max_chars()
        )

        # Stale check — built_at_ts==0 means index has never built;
        # both that and an over-aged build map to STALE_INDEX so the
        # caller fails open uniformly.
        if (
            float(built_at_ts) <= 0.0
            or (
                float(generated_at_ts) - float(built_at_ts)
            ) > stale_s
        ):
            return CodebaseCharacterSnapshot(
                outcome=DigestOutcome.STALE_INDEX,
                clusters=empty,
                generated_at_ts=float(generated_at_ts),
                total_corpus_items=int(total_corpus_items or 0),
                cluster_mode=str(cluster_mode or ""),
                built_at_ts=float(built_at_ts or 0.0),
            )

        if not clusters or len(clusters) < floor:
            return CodebaseCharacterSnapshot(
                outcome=DigestOutcome.INSUFFICIENT_CLUSTERS,
                clusters=empty,
                generated_at_ts=float(generated_at_ts),
                total_corpus_items=int(total_corpus_items or 0),
                cluster_mode=str(cluster_mode or ""),
                built_at_ts=float(built_at_ts or 0.0),
            )

        # Project — order by size descending then by cluster_id for
        # determinism. Apply cap; remainder → truncated_count.
        projected: list = []
        for c in clusters:
            try:
                comp_pairs = tuple(
                    (str(s), int(n))
                    for s, n in (c.source_composition or ())
                )
            except Exception:  # noqa: BLE001
                comp_pairs = ()
            projected.append(
                ClusterCharacter(
                    cluster_id=int(getattr(c, "cluster_id", 0)),
                    kind=str(getattr(c, "kind", "") or "mixed"),
                    size=int(getattr(c, "size", 0) or 0),
                    theme_label=_extract_theme_label(
                        str(getattr(c, "nearest_item_text", "") or ""),
                    ),
                    nearest_item_excerpt=_excerpt(
                        str(getattr(c, "nearest_item_text", "") or ""),
                        max_chars=excerpt_cap,
                    ),
                    nearest_item_source=str(
                        getattr(c, "nearest_item_source", "") or "",
                    ),
                    source_composition=comp_pairs,
                    centroid_hash8=str(
                        getattr(c, "centroid_hash8", "") or "",
                    ),
                ),
            )
        # Stable sort: size desc, then cluster_id asc.
        projected.sort(
            key=lambda x: (-int(x.size), int(x.cluster_id)),
        )
        kept = projected[:cap]
        truncated = max(0, len(projected) - cap)

        return CodebaseCharacterSnapshot(
            outcome=DigestOutcome.READY,
            clusters=tuple(kept),
            generated_at_ts=float(generated_at_ts),
            total_corpus_items=int(total_corpus_items or 0),
            cluster_mode=str(cluster_mode or ""),
            built_at_ts=float(built_at_ts or 0.0),
            truncated_count=int(truncated),
        )
    except Exception as exc:  # noqa: BLE001 — total guarantee
        logger.debug(
            "[CodebaseCharacter] compute degraded: %s",
            exc,
        )
        return CodebaseCharacterSnapshot(
            outcome=DigestOutcome.FAILED,
            clusters=(),
            generated_at_ts=float(generated_at_ts or 0.0),
            total_corpus_items=int(total_corpus_items or 0),
            cluster_mode=str(cluster_mode or ""),
            built_at_ts=float(built_at_ts or 0.0),
            failure_reason=str(exc)[:120],
        )
