"""RR Pass B Slice 4 — Shadow-replay corpus + structural-equality diff.

Per ``memory/project_reverse_russian_doll_pass_b.md`` §6:

  > A new ``PhaseRunner`` subclass is structurally well-formed (§5)
  > but might still break the FSM dynamically — mishandle a corner
  > case, change behavior on retry, silently drop telemetry. Replay-
  > against-golden is the regression cage.
  >
  > Schema:
  > ``.jarvis/order2_replay_corpus/manifest.yaml`` (op_id → snapshot
  > path + tags) + ``ops/<op_id>/<phase>.json`` snapshots at each
  > phase boundary.
  >
  > Comparison metric: byte-identical for ``next_phase``, ``status``,
  > ``reason``; structural-equality for ``next_ctx`` (whitelisted
  > fields — phase log entries can differ in timestamp; ``risk_tier``,
  > ``op_id``, candidate set must match).

Slice 4 ships the **primitive**:

  * :class:`ReplaySnapshot` — frozen dataclass, one phase boundary
    capture (pre-phase ctx + expected post-phase result).
  * :class:`ReplayCorpus` — loader + indexer over the on-disk
    corpus directory.
  * :func:`structural_equal` — whitelist-driven dict comparison
    used for the ``next_ctx`` field in §6.3.
  * :func:`compare_phase_result_to_expected` — high-level diff
    function returning a :class:`ReplayDivergence` or ``None``.

Slice 5 (MetaPhaseRunner) composes these with the actual candidate-
runner invocation: build a fake ctx from ``snapshot.pre_phase_ctx``,
``await runner.run(fake_ctx)``, then diff the produced PhaseResult
against ``snapshot.expected_*``. Slice 4 deliberately does NOT
invoke runners — building real ``OperationContext`` instances has
heavy import surface that this slice avoids by design.

Authority invariants (Pass B §6 + §3.4):
  * Pure data + read-only file I/O of the corpus directory ONLY.
    No subprocess, no env mutation, no network. The corpus YAML
    + JSON files are operator-curated; Slice 6 amendment protocol
    will gate writes (corpus is part of the cage per §6.5).
  * No imports of orchestrator / policy / iron_gate / risk_tier_floor
    / change_engine / candidate_generator / gate / semantic_guardian
    / semantic_firewall / scoped_tool_backend.
  * Allowed: stdlib + ``meta.order2_manifest`` (for any future
    cross-checks, currently unused at slice-1 scope).
  * Best-effort throughout — every load failure is mapped to a
    structured :class:`ReplayLoadStatus`; never raises.
  * Per-corpus-op cap at MAX_SNAPSHOT_BYTES so a malformed JSON
    blob can't pin the loader.

Default-off behind ``JARVIS_SHADOW_PIPELINE_ENABLED`` until Slice
4's own clean-session graduation. When off, :func:`load_corpus`
returns an empty corpus with status NOT_LOADED. Slice 5 hook treats
NOT_LOADED as "no shadow replay enforcement" — the cage degrades
to the existing review path.
"""
from __future__ import annotations

import enum
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any, Dict, FrozenSet, List, Mapping, Optional, Tuple,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Schema version frozen at v1 — bumped on any ReplaySnapshot field
# change so Slice 5 MetaPhaseRunner can pin a parser version.
SHADOW_REPLAY_SCHEMA_VERSION: int = 1

# Per-snapshot byte ceiling. 256 KiB is generous for a single phase
# boundary capture; larger blobs are dropped at load time.
MAX_SNAPSHOT_BYTES: int = 256 * 1024

# Per-corpus snapshot count cap. Pass B §6.2 specifies an initial
# 20-op corpus; this is a soft 1000-op ceiling for future growth
# (each op typically has 11 phase snapshots — 11k snapshots is the
# upper bound).
MAX_SNAPSHOTS_PER_CORPUS: int = 11_000

# Whitelisted fields per Pass B §6.3: must match exactly across
# inline-vs-runner comparison. Other ctx fields are allowed to
# differ (phase log timestamps, perf counters, etc.).
DEFAULT_CTX_WHITELIST: FrozenSet[str] = frozenset({
    "op_id",
    "risk_tier",
    "phase",
    "target_files",
    "candidate_files",
})


def is_enabled() -> bool:
    """Master flag — ``JARVIS_SHADOW_PIPELINE_ENABLED`` (default
    false until Slice 4's own clean-session graduation).

    When off, :func:`load_corpus` returns an empty corpus with status
    NOT_LOADED. Slice 5 MetaPhaseRunner consumer treats this as
    "no shadow replay enforcement" so the cage degrades to the
    existing review path."""
    raw = os.environ.get(
        "JARVIS_SHADOW_PIPELINE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated 2026-05-03 (Pass B Slice 4)
    return raw in _TRUTHY


def corpus_root() -> Path:
    """Return the corpus root directory. Env-overridable via
    ``JARVIS_SHADOW_REPLAY_CORPUS_PATH``; defaults to
    ``.jarvis/order2_replay_corpus`` under the cwd."""
    raw = os.environ.get("JARVIS_SHADOW_REPLAY_CORPUS_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "order2_replay_corpus"


# ---------------------------------------------------------------------------
# Status enum + frozen dataclasses
# ---------------------------------------------------------------------------


class ReplayLoadStatus(str, enum.Enum):
    """Outcome of a corpus load attempt. Pinned for Slice 5 hook
    status checks."""

    LOADED = "LOADED"
    NOT_LOADED = "NOT_LOADED"          # master flag off
    DIR_MISSING = "DIR_MISSING"
    MANIFEST_MISSING = "MANIFEST_MISSING"
    MANIFEST_PARSE_ERROR = "MANIFEST_PARSE_ERROR"
    EMPTY = "EMPTY"                     # manifest loads but has zero entries


@dataclass(frozen=True)
class ReplaySnapshot:
    """One phase-boundary capture from a completed Order-1 op.

    ``pre_phase_ctx`` is a JSON-serializable mapping (the
    OperationContext at phase X-1 — what the candidate runner
    receives as input).

    ``expected_*`` fields are the recorded post-phase result the
    candidate must reproduce: byte-identical for next_phase /
    status / reason; structural-equality (over the whitelist) for
    next_ctx."""

    op_id: str
    phase: str
    pre_phase_ctx: Dict[str, Any] = field(default_factory=dict)
    expected_next_phase: Optional[str] = None
    expected_status: str = ""
    expected_reason: Optional[str] = None
    expected_next_ctx: Dict[str, Any] = field(default_factory=dict)
    tags: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "phase": self.phase,
            "pre_phase_ctx": dict(self.pre_phase_ctx),
            "expected_next_phase": self.expected_next_phase,
            "expected_status": self.expected_status,
            "expected_reason": self.expected_reason,
            "expected_next_ctx": dict(self.expected_next_ctx),
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class ReplayCorpus:
    """Loaded corpus — bundle of snapshots indexed by op_id and phase.

    Slice 5 MetaPhaseRunner queries via :meth:`for_phase` to find all
    snapshots for a candidate runner's target phase. The corpus
    itself never mutates — Slice 6 amendment protocol rebuilds +
    reloads via :func:`reset_default_corpus` after corpus YAML edit."""

    schema_version: int = SHADOW_REPLAY_SCHEMA_VERSION
    snapshots: Tuple[ReplaySnapshot, ...] = field(default_factory=tuple)
    status: ReplayLoadStatus = ReplayLoadStatus.NOT_LOADED
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def for_phase(self, phase: str) -> Tuple[ReplaySnapshot, ...]:
        """Return all snapshots for a given phase name. Used by
        Slice 5 MetaPhaseRunner to filter the corpus to a candidate
        runner's target phase."""
        return tuple(s for s in self.snapshots if s.phase == phase)

    def for_op(self, op_id: str) -> Tuple[ReplaySnapshot, ...]:
        return tuple(s for s in self.snapshots if s.op_id == op_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "snapshots_count": len(self.snapshots),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ReplayDivergence:
    """One regression finding from
    :func:`compare_phase_result_to_expected`.

    ``field_path`` describes WHERE the divergence occurred (e.g.
    ``"next_phase"``, ``"next_ctx.risk_tier"``). ``expected`` /
    ``actual`` are the values that mismatched."""

    op_id: str
    phase: str
    field_path: str
    expected: Any
    actual: Any
    detail: str = ""


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_corpus(
    root: Optional[Path] = None,
) -> ReplayCorpus:
    """Load the corpus from disk. NEVER raises — every failure path
    returns a corpus with the appropriate :class:`ReplayLoadStatus`.

    Skip behaviour:
      * Master flag off → ``NOT_LOADED`` with empty snapshots.
      * Root dir missing → ``DIR_MISSING``.
      * Manifest YAML missing → ``MANIFEST_MISSING``.
      * Manifest parse error → ``MANIFEST_PARSE_ERROR``.
      * Manifest loads with zero usable entries → ``EMPTY``.
    """
    if not is_enabled():
        return ReplayCorpus(
            status=ReplayLoadStatus.NOT_LOADED,
            notes=("master_flag_off",),
        )
    r = root or corpus_root()
    if not r.exists() or not r.is_dir():
        return ReplayCorpus(
            status=ReplayLoadStatus.DIR_MISSING,
            notes=(f"root_missing:{r}",),
        )
    manifest_path = r / "manifest.yaml"
    if not manifest_path.exists():
        return ReplayCorpus(
            status=ReplayLoadStatus.MANIFEST_MISSING,
            notes=(f"manifest_missing:{manifest_path}",),
        )
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ReplayCorpus(
            status=ReplayLoadStatus.MANIFEST_PARSE_ERROR,
            notes=(f"manifest_read_failed:{exc}",),
        )
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return ReplayCorpus(
            status=ReplayLoadStatus.MANIFEST_PARSE_ERROR,
            notes=("yaml_module_missing",),
        )
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return ReplayCorpus(
            status=ReplayLoadStatus.MANIFEST_PARSE_ERROR,
            notes=(f"yaml_parse_failed:{exc}",),
        )
    if not isinstance(doc, dict):
        return ReplayCorpus(
            status=ReplayLoadStatus.MANIFEST_PARSE_ERROR,
            notes=("manifest_not_mapping",),
        )

    notes: List[str] = []
    declared_version = doc.get("schema_version")
    if declared_version != SHADOW_REPLAY_SCHEMA_VERSION:
        notes.append(
            f"schema_version_mismatch:declared={declared_version},"
            f"expected={SHADOW_REPLAY_SCHEMA_VERSION}"
        )
    raw_entries = doc.get("entries")
    if not isinstance(raw_entries, list):
        return ReplayCorpus(
            status=ReplayLoadStatus.MANIFEST_PARSE_ERROR,
            notes=tuple(notes + ["entries_key_missing_or_not_list"]),
        )

    snapshots: List[ReplaySnapshot] = []
    for i, raw_entry in enumerate(raw_entries):
        if len(snapshots) >= MAX_SNAPSHOTS_PER_CORPUS:
            notes.append(
                f"snapshots_truncated_at_max_{MAX_SNAPSHOTS_PER_CORPUS}",
            )
            break
        loaded = _load_snapshot_entry(raw_entry, r, notes, idx=i)
        snapshots.extend(loaded)

    if not snapshots:
        return ReplayCorpus(
            status=ReplayLoadStatus.EMPTY,
            notes=tuple(notes) or ("no_usable_snapshots",),
        )

    logger.info(
        "[ShadowReplay] loaded %d snapshots across %d ops from %s",
        len(snapshots),
        len({s.op_id for s in snapshots}),
        r,
    )
    return ReplayCorpus(
        schema_version=SHADOW_REPLAY_SCHEMA_VERSION,
        snapshots=tuple(snapshots),
        status=ReplayLoadStatus.LOADED,
        notes=tuple(notes),
    )


def _load_snapshot_entry(
    raw_entry: Any,
    root: Path,
    notes: List[str],
    idx: int,
) -> List[ReplaySnapshot]:
    """Load all phase-boundary snapshots for one corpus op.

    Manifest entry shape::

        - op_id: op-019d9368-654b
          path: ops/op-019d9368-654b
          phases: [classify, route, plan, generate, validate, gate, ...]
          tags: [multi-file, session-u-w]
    """
    if not isinstance(raw_entry, dict):
        notes.append(f"entry_{idx}_not_mapping")
        return []
    op_id = str(raw_entry.get("op_id") or "").strip()
    if not op_id:
        notes.append(f"entry_{idx}_missing_op_id")
        return []
    rel_path = str(raw_entry.get("path") or "").strip()
    if not rel_path:
        notes.append(f"entry_{idx}_missing_path")
        return []
    phases = raw_entry.get("phases")
    if not isinstance(phases, list) or not phases:
        notes.append(f"entry_{idx}_missing_phases")
        return []
    tags_raw = raw_entry.get("tags") or []
    tags = tuple(str(t) for t in tags_raw) if isinstance(tags_raw, list) else ()

    op_dir = root / rel_path
    if not op_dir.exists() or not op_dir.is_dir():
        notes.append(f"entry_{idx}_op_dir_missing:{op_dir}")
        return []

    out: List[ReplaySnapshot] = []
    for phase in phases:
        snapshot_path = op_dir / f"{phase}.json"
        if not snapshot_path.exists():
            notes.append(
                f"entry_{idx}_snapshot_missing:{snapshot_path.name}",
            )
            continue
        try:
            sz = snapshot_path.stat().st_size
        except OSError:
            sz = 0
        if sz > MAX_SNAPSHOT_BYTES:
            notes.append(
                f"entry_{idx}_snapshot_oversize:{snapshot_path.name}"
                f":{sz} > {MAX_SNAPSHOT_BYTES}"
            )
            continue
        try:
            text = snapshot_path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            notes.append(
                f"entry_{idx}_snapshot_unreadable:"
                f"{snapshot_path.name}:{exc}"
            )
            continue
        if not isinstance(data, dict):
            notes.append(
                f"entry_{idx}_snapshot_not_mapping:{snapshot_path.name}",
            )
            continue
        out.append(ReplaySnapshot(
            op_id=op_id,
            phase=str(phase),
            pre_phase_ctx=data.get("pre_phase_ctx") or {},
            expected_next_phase=data.get("expected_next_phase"),
            expected_status=str(data.get("expected_status") or ""),
            expected_reason=data.get("expected_reason"),
            expected_next_ctx=data.get("expected_next_ctx") or {},
            tags=tags,
        ))
    return out


# ---------------------------------------------------------------------------
# Structural equality + diff
# ---------------------------------------------------------------------------


def structural_equal(
    a: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    whitelist: FrozenSet[str] = DEFAULT_CTX_WHITELIST,
) -> bool:
    """Return True iff every whitelisted key is byte-identical
    between ``a`` and ``b``. Non-whitelisted keys are ignored
    (timestamps, phase log entries, etc. allowed to diverge per
    Pass B §6.3)."""
    for key in whitelist:
        if a.get(key) != b.get(key):
            return False
    return True


def compare_phase_result_to_expected(
    actual_next_phase: Optional[str],
    actual_status: str,
    actual_reason: Optional[str],
    actual_next_ctx: Mapping[str, Any],
    snapshot: ReplaySnapshot,
    *,
    ctx_whitelist: FrozenSet[str] = DEFAULT_CTX_WHITELIST,
) -> Optional[ReplayDivergence]:
    """Compare a candidate runner's produced result against the
    snapshot's recorded expected result.

    Per Pass B §6.3:
      * ``next_phase``, ``status``, ``reason`` — byte-identical.
      * ``next_ctx`` — structural-equality over the whitelist.

    Returns ``None`` on match; else the FIRST :class:`ReplayDivergence`
    found (callers can keep diff'ing if they want richer reporting,
    but Slice 5 MetaPhaseRunner stops on first mismatch)."""
    if actual_next_phase != snapshot.expected_next_phase:
        return ReplayDivergence(
            op_id=snapshot.op_id, phase=snapshot.phase,
            field_path="next_phase",
            expected=snapshot.expected_next_phase,
            actual=actual_next_phase,
            detail="next_phase mismatch",
        )
    if actual_status != snapshot.expected_status:
        return ReplayDivergence(
            op_id=snapshot.op_id, phase=snapshot.phase,
            field_path="status",
            expected=snapshot.expected_status,
            actual=actual_status,
            detail="status mismatch",
        )
    if actual_reason != snapshot.expected_reason:
        return ReplayDivergence(
            op_id=snapshot.op_id, phase=snapshot.phase,
            field_path="reason",
            expected=snapshot.expected_reason,
            actual=actual_reason,
            detail="reason mismatch",
        )
    for key in ctx_whitelist:
        expected_val = snapshot.expected_next_ctx.get(key)
        actual_val = actual_next_ctx.get(key)
        if expected_val != actual_val:
            return ReplayDivergence(
                op_id=snapshot.op_id, phase=snapshot.phase,
                field_path=f"next_ctx.{key}",
                expected=expected_val,
                actual=actual_val,
                detail=f"next_ctx whitelisted field {key!r} mismatch",
            )
    return None


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_default_corpus: Optional[ReplayCorpus] = None
_default_lock = threading.Lock()


def get_default_corpus() -> ReplayCorpus:
    """Process-wide corpus. Lazy-load on first call.

    Boot wiring: any module that wants the corpus calls this
    function. Slice 5 MetaPhaseRunner consumer MUST treat ``status
    != LOADED`` as "no shadow replay enforcement" so the cage
    degrades to the existing review path when the corpus is
    missing/disabled/malformed."""
    global _default_corpus
    with _default_lock:
        if _default_corpus is None:
            _default_corpus = load_corpus()
    return _default_corpus


def reset_default_corpus() -> None:
    """Reset the cached corpus. Slice 6 amendment protocol calls
    this after writing the corpus YAML / JSON. Tests use it for
    isolation."""
    global _default_corpus
    with _default_lock:
        _default_corpus = None


__all__ = [
    "DEFAULT_CTX_WHITELIST",
    "MAX_SNAPSHOTS_PER_CORPUS",
    "MAX_SNAPSHOT_BYTES",
    "ReplayCorpus",
    "ReplayDivergence",
    "ReplayLoadStatus",
    "ReplaySnapshot",
    "SHADOW_REPLAY_SCHEMA_VERSION",
    "compare_phase_result_to_expected",
    "corpus_root",
    "get_default_corpus",
    "is_enabled",
    "load_corpus",
    "reset_default_corpus",
    "structural_equal",
]


# ---------------------------------------------------------------------------
# Pass B Graduation Slice 2 — substrate AST pin
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta._invariant_helpers import (
        make_pass_b_substrate_invariant,
    )
    inv = make_pass_b_substrate_invariant(
        invariant_name="pass_b_shadow_replay_substrate",
        target_file=(
            "backend/core/ouroboros/governance/meta/shadow_replay.py"
        ),
        description=(
            "Pass B Slice 4 substrate: is_enabled + corpus_root + "
            "load_corpus + ReplaySnapshot/Corpus/Divergence (all "
            "frozen) present; no dynamic-code calls."
        ),
        required_funcs=("is_enabled", "corpus_root", "load_corpus"),
        required_classes=(
            "ReplaySnapshot", "ReplayCorpus", "ReplayDivergence",
        ),
        frozen_classes=(
            "ReplaySnapshot", "ReplayCorpus", "ReplayDivergence",
        ),
    )
    return [inv] if inv is not None else []
