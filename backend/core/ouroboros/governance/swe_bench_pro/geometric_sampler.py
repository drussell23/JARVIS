"""SWE-Bench-Pro geometric instance sampler — Stage 2 self-curating
rubric baseline (PRD §40.7.10-stage2).

The Stage-2 discriminator proof requires ONE expected-RESOLVED
("known-good") and ONE expected-UNRESOLVED ("known-hard") problem so
the L2 scorer is shown to distinguish real fixes from non-fixes
*without hallucinating success*.  Hardcoding two static instance IDs
is brittle (manual upkeep, dataset-version drift, contamination
risk).  Instead this sampler **anchors the discriminator to the
physical geometry of the dataset itself**:

  * **known-good** — the instance with the smallest, most localized
    *single-file* gold-patch (absolute lowest changed-line count).
    A tiny one-file diff is the highest-probability RESOLVED target.
  * **known-hard** — the instance with the largest, most sprawling
    *multi-file* gold-patch (absolute highest changed-line count).
    A massive cross-file diff is the highest-probability UNRESOLVED
    target — exactly the case where a scorer that hallucinates
    success would be exposed.

Selection is **fully deterministic**: total-order sort keys with an
``instance_id`` final tiebreak, so the same dataset always yields the
same pair (reproducible rubric — the Tier-C discipline applied to
curation).

Composition discipline (no parallel logic)
-------------------------------------------

  * Dataset scan composes :func:`dataset_loader.iter_all_dataset_records`
    (local JSONL ∪ HF — the SAME records :func:`load_problem` resolves).
  * Gold-patch *file* geometry composes the canonical
    :func:`repair_tree_production.extract_diff_targets` (Treefinement
    v3.4 — the single source of truth for unified-diff parsing).  No
    hand-rolled diff path-parsing anywhere in this module.

§7 fail-closed: every public surface NEVER raises into the caller
(``asyncio.CancelledError`` propagates).  Master-flag gated via the
composed iterator's own short-circuit.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.repair_tree_production import (
    extract_diff_targets,
)
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    iter_all_dataset_records,
)

logger = logging.getLogger(__name__)

GEOMETRIC_SAMPLER_ENABLED_ENV_VAR: str = (
    "JARVIS_SWE_BENCH_PRO_GEOMETRIC_SAMPLER_ENABLED"
)

GEOMETRIC_SAMPLER_SCHEMA_VERSION: str = "geom_sample.1"


# ===========================================================================
# Patch geometry
# ===========================================================================


@dataclass(frozen=True)
class PatchGeometry:
    """Physical shape of one problem's gold patch.

    changed_files
        Count of distinct target paths in the gold diff (canonical
        :func:`extract_diff_targets` — deduped by path).
    changed_lines
        Count of hunk-body added/removed lines (lines starting with
        a single ``+`` / ``-``; diff/index/header lines excluded).
        The "size" dimension of the patch.
    """

    instance_id: str
    changed_files: int
    changed_lines: int

    @property
    def is_single_file(self) -> bool:
        return self.changed_files == 1

    @property
    def is_multi_file(self) -> bool:
        return self.changed_files >= 2

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "changed_files": self.changed_files,
            "changed_lines": self.changed_lines,
            "is_single_file": self.is_single_file,
            "is_multi_file": self.is_multi_file,
        }


def compute_patch_geometry(
    instance_id: str, gold_patch: str,
) -> PatchGeometry:
    """Measure a gold patch's geometry.  NEVER raises — malformed
    input yields a zero-geometry record (the sampler then ignores it
    as a non-candidate)."""
    if not gold_patch:
        return PatchGeometry(instance_id, 0, 0)
    try:
        targets = extract_diff_targets(gold_patch)
        changed_files = len(targets)
        changed_lines = 0
        for line in gold_patch.splitlines():
            if not line:
                continue
            head = line[0]
            if head not in ("+", "-"):
                continue
            # Exclude file-header markers (``+++ b/x`` / ``--- a/x``)
            # — those are not changed *content* lines.
            if line.startswith("+++") or line.startswith("---"):
                continue
            changed_lines += 1
        return PatchGeometry(instance_id, changed_files, changed_lines)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — defensive (fail-soft measure)
        logger.debug(
            "[SWEBenchPro.GeometricSampler] compute_patch_geometry "
            "raised for %r", instance_id, exc_info=True,
        )
        return PatchGeometry(instance_id, 0, 0)


# ===========================================================================
# Discriminator pair
# ===========================================================================


@dataclass(frozen=True)
class GeometricSample:
    """The deterministically-curated discriminator pair."""

    known_good_id: str
    known_hard_id: str
    known_good_geometry: PatchGeometry
    known_hard_geometry: PatchGeometry
    scanned_count: int
    schema_version: str = GEOMETRIC_SAMPLER_SCHEMA_VERSION

    @property
    def instance_ids(self) -> List[str]:
        """Injection order: known-good first (fast RESOLVED signal),
        known-hard second."""
        return [self.known_good_id, self.known_hard_id]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "known_good_id": self.known_good_id,
            "known_hard_id": self.known_hard_id,
            "known_good_geometry": self.known_good_geometry.to_dict(),
            "known_hard_geometry": self.known_hard_geometry.to_dict(),
            "scanned_count": self.scanned_count,
        }


def sample_discriminator_pair(
    *, max_scan: Optional[int] = None,
) -> Optional[GeometricSample]:
    """Scan the full dataset and deterministically curate the
    (known-good, known-hard) discriminator pair.

    Returns ``None`` (fail-open) when the dataset cannot yield a
    valid pair: feature off, no records, no single-file candidate,
    no multi-file candidate, or the two would collapse to the same
    instance.  NEVER raises (``asyncio.CancelledError`` propagates).

    Determinism: ``known_good`` = ``min`` over single-file
    candidates by ``(changed_lines, changed_files, instance_id)``;
    ``known_hard`` = ``max`` over multi-file candidates by
    ``(changed_lines, changed_files)`` with an ``instance_id``
    tiebreak — both keys are total orders, so the selection is
    reproducible across runs and processes.
    """
    try:
        single_file: List[PatchGeometry] = []
        multi_file: List[PatchGeometry] = []
        scanned = 0
        for record in iter_all_dataset_records(max_scan=max_scan):
            scanned += 1
            iid = record.get("instance_id")
            if not isinstance(iid, str) or not iid:
                continue
            gold = record.get("gold_patch")
            if not isinstance(gold, str) or not gold:
                continue
            geom = compute_patch_geometry(iid, gold)
            if geom.changed_lines <= 0 or geom.changed_files <= 0:
                continue  # non-measurable — not a discriminator candidate
            if geom.is_single_file:
                single_file.append(geom)
            elif geom.is_multi_file:
                multi_file.append(geom)

        if not single_file:
            logger.warning(
                "[SWEBenchPro.GeometricSampler] no single-file gold "
                "patch in %d scanned records — cannot curate "
                "known-good", scanned,
            )
            return None
        if not multi_file:
            logger.warning(
                "[SWEBenchPro.GeometricSampler] no multi-file gold "
                "patch in %d scanned records — cannot curate "
                "known-hard", scanned,
            )
            return None

        known_good = min(
            single_file,
            key=lambda g: (g.changed_lines, g.changed_files, g.instance_id),
        )
        known_hard = max(
            multi_file,
            key=lambda g: (
                g.changed_lines, g.changed_files, _inv(g.instance_id),
            ),
        )

        if known_good.instance_id == known_hard.instance_id:
            logger.warning(
                "[SWEBenchPro.GeometricSampler] known-good == "
                "known-hard (%r) — degenerate dataset, no "
                "discriminator", known_good.instance_id,
            )
            return None

        sample = GeometricSample(
            known_good_id=known_good.instance_id,
            known_hard_id=known_hard.instance_id,
            known_good_geometry=known_good,
            known_hard_geometry=known_hard,
            scanned_count=scanned,
        )
        logger.info(
            "[SWEBenchPro.GeometricSampler] curated discriminator "
            "pair from %d records: known_good=%r (%d files / %d "
            "lines) known_hard=%r (%d files / %d lines)",
            scanned,
            sample.known_good_id,
            known_good.changed_files, known_good.changed_lines,
            sample.known_hard_id,
            known_hard.changed_files, known_hard.changed_lines,
        )
        return sample
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — fail-open contract
        logger.warning(
            "[SWEBenchPro.GeometricSampler] sample_discriminator_pair "
            "raised", exc_info=True,
        )
        return None


def _inv(instance_id: str) -> tuple:
    """Tiebreak helper: invert the instance_id ordering so a
    ``max`` over ``(changed_lines, changed_files, _inv(id))`` breaks
    ties toward the LEXICOGRAPHICALLY-SMALLEST id (stable across
    runs) rather than the largest.  Pure, total-order, deterministic.
    """
    return tuple(-ord(c) for c in instance_id)


def geometric_sampler_enabled() -> bool:
    """Opt-in master switch (§33.1 default-FALSE).  NEVER raises."""
    import os

    raw = os.environ.get(
        GEOMETRIC_SAMPLER_ENABLED_ENV_VAR, "",
    ).strip().lower()
    return raw in ("true", "1", "yes", "on")


# ===========================================================================
# FlagRegistry self-registration (§33.3 walker)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration.  Returns count
    successfully registered.  NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    specs = [
        FlagSpec(
            name=GEOMETRIC_SAMPLER_ENABLED_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "When ON (and INJECT_INSTANCE_IDS is empty), the "
                "harness boot hook resolves the inject set via the "
                "GeometricInstanceSampler: a deterministic "
                "(known-good single-file, known-hard multi-file) "
                "discriminator pair curated from the dataset's own "
                "gold-patch geometry — zero hardcoded instance IDs. "
                "§33.1 default-FALSE; opt-in for Stage-2 rubric runs."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "geometric_sampler.py"
            ),
            example="false",
            since="v3.7 Stage 2 geometric-sampler (2026-05-16)",
        ),
    ]

    registered = 0
    for spec in specs:
        try:
            registry.register(spec)
            registered += 1
        except Exception:  # noqa: BLE001 — best-effort registration
            logger.debug(
                "[SWEBenchPro.GeometricSampler] flag register failed "
                "for %s", spec.name, exc_info=True,
            )
    return registered


__all__ = [
    "GEOMETRIC_SAMPLER_ENABLED_ENV_VAR",
    "GEOMETRIC_SAMPLER_SCHEMA_VERSION",
    "PatchGeometry",
    "GeometricSample",
    "compute_patch_geometry",
    "sample_discriminator_pair",
    "geometric_sampler_enabled",
    "register_flags",
]
