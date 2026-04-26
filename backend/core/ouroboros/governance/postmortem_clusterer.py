"""P1 Slice 1 — POSTMORTEM clusterer + signature dedup.

Pure-data primitive that walks a sequence of ``PostmortemRecord`` objects
(produced by ``postmortem_recall.PostmortemRecallService``), groups them
by structural similarity, and emits ``ProposalCandidate`` rows that the
upcoming Slice 2 ``SelfGoalFormationEngine`` will use to decide whether
to propose a self-formed backlog entry.

Why a separate module (P1 Slice 1, PRD §9 Phase 2):
    The clustering logic is the load-bearing safety primitive for the
    entire Curiosity Engine v2 arc. By isolating it from any LLM call,
    env knob, or authority module, we get:
      * Determinism + cheap regression coverage (no API key needed)
      * A signature-based dedup key the upcoming engine will use to
        block runaway proposal loops (per PRD §9 P1 edge case)
      * A clean Layer 1 unit-test boundary (per PRD §11)

Authority invariants (PRD §12.2 / Manifesto §1 Boundary):
    - **Read-only data transform**: in → list of ``PostmortemRecord``,
      out → list of ``ProposalCandidate``. No I/O, no subprocess, no
      env mutation, no governance state change.
    - **No banned imports**: this module MUST NOT import ``orchestrator``,
      ``policy``, ``iron_gate``, ``risk_tier``, ``change_engine``,
      ``candidate_generator``, ``gate``, or ``semantic_guardian``.
      Pinned by ``test_postmortem_clusterer_no_authority_imports``.
    - **Zero LLM**: clustering is a deterministic structural similarity
      computation — same inputs always produce same clusters. The LLM
      call (if any) is the engine's job in Slice 2, never the clusterer.
    - **Best-effort**: malformed / empty inputs return ``[]``, never raise.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Tuple

from backend.core.ouroboros.governance.postmortem_recall import PostmortemRecord


# Default cluster-size threshold per PRD §9 P1: "3+ similar failures"
# triggers a recurring-pattern signal. Configurable per-call.
DEFAULT_MIN_CLUSTER_SIZE: int = 3

# Default cap on the maximum number of clusters returned per call. Keeps
# downstream Slice 2 cost-cap math bounded by construction (engine can
# only consider this many proposal candidates per scan).
DEFAULT_MAX_CLUSTERS: int = 10

# How many leading characters of the root_cause string get used as the
# coarse "class" key. Long enough to disambiguate distinct failure modes,
# short enough to collapse minor wording variants of the same root cause.
_ROOT_CAUSE_CLASS_PREFIX_LEN: int = 80

# Drop these noise tokens from the root_cause class key. Keeps token-
# based variants (e.g. "all_providers_exhausted:fallback_failed" vs
# "all_providers_exhausted:retry_failed") from inflating cluster counts.
_ROOT_CAUSE_NOISE_TOKENS: FrozenSet[str] = frozenset({
    "exception", "error", "failed", "failure", "raised",
})

# Regex used to normalize hex IDs / file hashes / timestamps out of the
# root cause text before classification (so cluster signatures are
# stable across runs that differ only in incidental identifiers).
_ID_LIKE_RE: re.Pattern[str] = re.compile(r"\b[0-9a-f]{6,}\b", re.IGNORECASE)
_TIMESTAMP_LIKE_RE: re.Pattern[str] = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]?\d{0,2}:?\d{0,2}:?\d{0,2}\.?\d*\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClusterSignature:
    """Structural fingerprint of a postmortem cluster.

    Two postmortems land in the same cluster when their signatures are
    equal. The signature is intentionally coarse — we want recurring
    patterns to surface, not perfect string matches.

    Attributes
    ----------
    failed_phase:
        The pipeline phase that failed (CLASSIFY / GENERATE / VALIDATE
        / APPLY / VERIFY / etc.). Required key — different phases never
        cluster together.
    root_cause_class:
        Normalized prefix of ``root_cause`` with ID-like tokens stripped
        and noise tokens dropped. See ``_normalize_root_cause`` for the
        full transform.
    """

    failed_phase: str
    root_cause_class: str

    def signature_hash(self) -> str:
        """Stable sha256[:12] hex digest of this signature.

        Used by the upcoming Slice 2 engine for blocklist dedup against
        previously-proposed clusters — prevents the "infinite postmortem
        loop" failure mode from PRD §9 P1 edge cases."""
        joined = f"{self.failed_phase}|{self.root_cause_class}"
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ProposalCandidate:
    """A cluster-of-postmortems shaped as a self-formed-goal proposal input.

    Attributes
    ----------
    signature:
        ``ClusterSignature`` shared by all members. Use ``signature_hash()``
        for dedup keys.
    member_op_ids:
        Tuple of distinct ``op_id`` strings of cluster members, ordered
        newest-first.
    member_count:
        Number of distinct ops represented (== ``len(member_op_ids)``;
        carried explicitly so callers can sort without re-counting).
    target_files_union:
        Sorted tuple of every distinct ``target_file`` path mentioned by
        any cluster member. Capped to the first 30 to bound the Slice 2
        prompt budget.
    dominant_next_safe_action:
        Plurality-vote of the cluster members' ``next_safe_action`` strings.
        Empty string when no member proposed an action or when the modal
        action is `"none"` (matching the ``lesson_text`` rendering).
    oldest_unix / newest_unix:
        Wall-clock span of cluster members. Useful for posture-aware
        weighting (very-recent recurrences are louder).
    representative_root_cause:
        The longest root_cause string among members — kept for
        prompt-rendering transparency (the model sees a real example,
        not a normalized class key).
    """

    signature: ClusterSignature
    member_op_ids: Tuple[str, ...]
    member_count: int
    target_files_union: Tuple[str, ...]
    dominant_next_safe_action: str
    oldest_unix: float
    newest_unix: float
    representative_root_cause: str

    def is_recurring(self, threshold: int = DEFAULT_MIN_CLUSTER_SIZE) -> bool:
        """True when ``member_count`` clears the recurring-pattern bar."""
        return self.member_count >= threshold

    def wall_seconds_span(self) -> float:
        """Wall-clock seconds between oldest + newest member."""
        return max(0.0, self.newest_unix - self.oldest_unix)


def _normalize_root_cause(raw: str) -> str:
    """Collapse ``root_cause`` into a stable class key.

    Steps:
      1. Truncate to ``_ROOT_CAUSE_CLASS_PREFIX_LEN`` chars (dominant
         signal lives in the prefix, e.g.
         ``all_providers_exhausted:fallback_failed``).
      2. Lowercase + trim whitespace.
      3. Strip ID-like hex tokens + ISO-ish timestamps (incidental
         identifiers that vary per session and should not split clusters).
      4. Drop the ``_ROOT_CAUSE_NOISE_TOKENS`` set (English filler
         common to many failure classes).
    """
    if not raw:
        return ""
    s = raw[:_ROOT_CAUSE_CLASS_PREFIX_LEN].strip().lower()
    s = _ID_LIKE_RE.sub("", s)
    s = _TIMESTAMP_LIKE_RE.sub("", s)
    if _ROOT_CAUSE_NOISE_TOKENS:
        tokens = re.split(r"[\s,;:]+", s)
        tokens = [t for t in tokens if t and t not in _ROOT_CAUSE_NOISE_TOKENS]
        s = " ".join(tokens)
    # Collapse multiple whitespaces to single spaces.
    return re.sub(r"\s+", " ", s).strip()


def cluster_postmortems(
    records: Iterable[PostmortemRecord],
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    max_clusters: int = DEFAULT_MAX_CLUSTERS,
) -> List[ProposalCandidate]:
    """Group postmortems into clusters and emit ``ProposalCandidate`` rows.

    Algorithm (deterministic, zero-LLM):
      1. Compute a ``ClusterSignature`` for each record from the
         (failed_phase, normalized_root_cause) pair.
      2. Group records by signature.
      3. For each group with ``len >= min_cluster_size``, emit one
         ``ProposalCandidate`` row.
      4. Sort emitted rows by (member_count desc, newest_unix desc) so
         the most actionable + most recent clusters surface first.
      5. Cap to ``max_clusters``.

    Parameters
    ----------
    records:
        Iterable of ``PostmortemRecord`` instances. Order does not matter
        — the function sorts by timestamp internally for stable output.
    min_cluster_size:
        Minimum member count for a group to be reported. Defaults to
        ``DEFAULT_MIN_CLUSTER_SIZE`` (3) per PRD §9 P1 "3+ similar
        failures" trigger.
    max_clusters:
        Cap on the returned list length. Defaults to
        ``DEFAULT_MAX_CLUSTERS`` (10).

    Returns
    -------
    List[ProposalCandidate]
        Possibly empty. ``[]`` on empty input or when no group meets the
        size threshold.
    """
    record_list: List[PostmortemRecord] = [r for r in records]
    if not record_list:
        return []

    by_signature: Dict[ClusterSignature, List[PostmortemRecord]] = {}
    for rec in record_list:
        sig = ClusterSignature(
            failed_phase=(rec.failed_phase or "").strip(),
            root_cause_class=_normalize_root_cause(rec.root_cause or ""),
        )
        # Skip records with empty signature key — they carry no cluster signal.
        if not sig.failed_phase and not sig.root_cause_class:
            continue
        by_signature.setdefault(sig, []).append(rec)

    candidates: List[ProposalCandidate] = []
    for sig, members in by_signature.items():
        if len(members) < min_cluster_size:
            continue
        members_sorted = sorted(members, key=lambda r: r.timestamp_unix, reverse=True)

        # Distinct op_ids preserved newest-first; dedup while preserving order.
        seen_ops: "set[str]" = set()
        unique_op_ids: List[str] = []
        for r in members_sorted:
            if r.op_id and r.op_id not in seen_ops:
                seen_ops.add(r.op_id)
                unique_op_ids.append(r.op_id)
        if len(unique_op_ids) < min_cluster_size:
            # Cluster size requirement uses distinct ops, not raw rows
            # (one op spamming POSTMORTEM lines must NOT count as a pattern).
            continue

        files_union: List[str] = []
        files_seen: "set[str]" = set()
        for r in members_sorted:
            for f in (r.target_files or ()):
                if f and f not in files_seen:
                    files_seen.add(f)
                    files_union.append(f)
        files_union_sorted = tuple(sorted(files_union))[:30]

        action_votes = Counter(
            (r.next_safe_action or "").strip()
            for r in members_sorted
            if (r.next_safe_action or "").strip()
            and (r.next_safe_action or "").strip().lower() != "none"
        )
        if action_votes:
            dominant_action, _ = action_votes.most_common(1)[0]
        else:
            dominant_action = ""

        rep_root_cause = max(
            (r.root_cause or "" for r in members_sorted), key=len, default="",
        )

        candidates.append(
            ProposalCandidate(
                signature=sig,
                member_op_ids=tuple(unique_op_ids),
                member_count=len(unique_op_ids),
                target_files_union=files_union_sorted,
                dominant_next_safe_action=dominant_action,
                oldest_unix=min(r.timestamp_unix for r in members_sorted),
                newest_unix=max(r.timestamp_unix for r in members_sorted),
                representative_root_cause=rep_root_cause,
            )
        )

    candidates.sort(
        key=lambda c: (-c.member_count, -c.newest_unix),
    )
    return candidates[:max_clusters]


def is_signature_in_blocklist(
    signature: ClusterSignature,
    blocklist_hashes: Iterable[str],
) -> bool:
    """Dedup helper for the upcoming Slice 2 engine.

    Returns True when the cluster's ``signature_hash()`` matches any
    entry in ``blocklist_hashes``. Used to prevent runaway proposals on
    the same recurring pattern (PRD §9 P1 "blocklist signature dedup"
    edge case)."""
    target = signature.signature_hash()
    return target in set(blocklist_hashes)


__all__ = [
    "ClusterSignature",
    "ProposalCandidate",
    "DEFAULT_MIN_CLUSTER_SIZE",
    "DEFAULT_MAX_CLUSTERS",
    "cluster_postmortems",
    "is_signature_in_blocklist",
]
