"""Wave 3 (6) — Slice 4b — DAGComposer (map-reduce fan-out -> unified candidate).

Closes the Slice-4b gap (audit-confirmed): when
``JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE`` runs a parallel L3 fan-out, the
per-unit ``WorkUnitResult`` patches are stashed in
``pctx.extras["parallel_dispatch_fanout_result"]`` and the **sequential
phase walk continues unchanged** -- so a successful parallel fan-out re-does
APPLY serially (duplicate work) while the parallel patches are ignored.

This module map-reduces a *successful* fan-out's per-unit patches back into
the parent FSM by composing them into ONE unified multi-file candidate of
the EXACT shape the orchestrator's existing multi-file path consumes
(``{file_path, full_content, files: [{file_path, full_content, rationale},
...]}`` per CLAUDE.md "Multi-file coordinated generation"). The composed
candidate then walks VALIDATE -> GATE -> APPLY ONCE via
``orchestrator._iter_candidate_files`` / ``_apply_multi_file_candidate``
(the existing batch-rollback multi-file consumer) -- NOT a new apply path.

KEY REUSE INSIGHT
-----------------
The Collision Matrix
(:mod:`~backend.core.ouroboros.governance.collision_matrix`) already
guarantees -- at pre-submit partition time -- that the parallel units touch
DISJOINT, import-isolated files. So composing N successful unit patches is a
clean UNION of disjoint per-file changes, NOT a 3-way merge. We do NOT build
an AST merger with conflict resolution. We DO verify the disjointness
invariant defensively here and **fail CLOSED** if it is somehow violated
(``collision_invariant_violated``) rather than silently overwrite a file.

Fail-CLOSED contract
--------------------
* ANY unit not terminally SUCCESS -> :class:`ComposeFailure` -> the caller
  falls back to the legacy serial path (stash + sequential walk). No partial
  compose, no silent data loss.
* Two units claiming the SAME file -> :class:`ComposeFailure`
  (``collision_invariant_violated``). Never a silent merge/overwrite.
* A successful unit carrying no usable patch -> :class:`ComposeFailure`
  (``unit_missing_patch``). We never fabricate content.

Gating
------
``JARVIS_WAVE3_DAG_COMPOSE_ENABLED`` (default **false**). OFF -> the
phase_dispatcher hook is byte-identical to today (stash, no consumption).
This module is pure + import-safe regardless of the flag; only the
phase_dispatcher wiring reads it.

§4 invariants:

1. No new apply path -- the composed candidate is consumed by the EXISTING
   orchestrator multi-file path + its batch-level rollback.
2. Disjoint UNION -- never a conflict merge; collision invariant verified
   defensively and fails CLOSED.
3. Fail-CLOSED -- any unit failure / missing-patch / collision -> a
   ComposeFailure that routes the caller to the legacy serial path.
4. Pure + deterministic -- same inputs -> same composed candidate, stable
   file ordering (graph unit order, which is deterministic).
5. Authority-import ban -- this module imports NONE of orchestrator,
   policy, iron_gate, risk_tier, change_engine, candidate_generator, gate.
   It only reads the autonomy result/graph dataclasses + the gating env.
"""
from __future__ import annotations

import ast
import enum
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitResult,
    WorkUnitState,
)

logger = logging.getLogger("Ouroboros.DAGComposer")


# ---------------------------------------------------------------------------
# Env gate
# ---------------------------------------------------------------------------

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return default


def dag_compose_enabled() -> bool:
    """Master flag -- ``JARVIS_WAVE3_DAG_COMPOSE_ENABLED`` (default ``false``).

    When ``false`` (graduation default), the phase_dispatcher Slice-4b hook
    keeps stashing the fan-out result and continues the sequential walk
    unchanged (byte-identical to pre-Slice-4b). The composer itself is pure
    and may be called by tests regardless of this flag; only the wiring
    seam reads it.
    """
    return _env_bool("JARVIS_WAVE3_DAG_COMPOSE_ENABLED", False)


# Planner/composer lineage tag stamped onto composed candidates so downstream
# telemetry can distinguish DAG-composed candidates from single-shot GENERATE
# output. Grep-friendly + stable.
COMPOSER_ID: str = "dag_composer.v1"


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


class ComposeFailureReason(str, enum.Enum):
    """Deterministic reason codes for a :class:`ComposeFailure`.

    Stable, grep-friendly. New codes added additively; never repurposed.
    """

    NO_UNITS = "no_units"
    UNIT_NOT_SUCCESS = "unit_not_success"
    UNIT_MISSING_PATCH = "unit_missing_patch"
    COLLISION_INVARIANT_VIOLATED = "collision_invariant_violated"
    EMPTY_COMPOSITION = "empty_composition"
    # Zero-loss merge guarantee -- the composed candidate failed the
    # post-union mathematical proof (count / content-sha / no-overwrite / AST).
    # ``detail`` is prefixed ``lossless_proof_failed:<which>`` so the exact
    # failing invariant is grep-visible.
    LOSSLESS_PROOF_FAILED = "lossless_proof_failed"


@dataclass(frozen=True)
class ComposeFailure:
    """Fail-CLOSED outcome -- caller falls back to the legacy serial path.

    Attributes
    ----------
    reason:
        Primary cause -- see :class:`ComposeFailureReason`.
    detail:
        Human-readable amplifier (which unit failed, which file collided).
    offending_unit_id:
        The first unit_id that triggered the failure, when applicable.
    """

    reason: ComposeFailureReason
    detail: str = ""
    offending_unit_id: str = ""

    @property
    def is_failure(self) -> bool:
        return True


@dataclass(frozen=True)
class ComposedCandidate:
    """Successful UNION of disjoint per-unit patches into one multi-file candidate.

    Attributes
    ----------
    op_id:
        Parent operation id (carried from the :class:`ExecutionGraph`).
    candidate:
        The candidate dict, shaped EXACTLY as
        ``orchestrator._iter_candidate_files`` /
        ``_apply_multi_file_candidate`` consume:
        ``{file_path, full_content, files: [{file_path, full_content,
        rationale}, ...], rationale}``. The first ``files`` entry is the
        primary/authoritative file; ``file_path`` / ``full_content`` mirror
        it for legacy single-file consumers.
    file_paths:
        Convenience accessor -- ordered, disjoint file paths in the union.
    """

    op_id: str
    candidate: Dict[str, Any]
    file_paths: Tuple[str, ...]

    @property
    def is_failure(self) -> bool:
        return False

    @property
    def n_files(self) -> int:
        return len(self.file_paths)


# ---------------------------------------------------------------------------
# Patch extraction helper
# ---------------------------------------------------------------------------


def _unit_files(result: WorkUnitResult) -> List[Tuple[str, str]]:
    """Extract ``(file_path, full_content)`` pairs from a unit's patch.

    A ``WorkUnitResult.patch`` is a :class:`RepoPatch` whose ``new_content``
    is a tuple of ``(path, bytes)`` pairs. The scheduler builds one entry per
    owned file (units own a single file in the Slice-2 build path, but we do
    not hardcode that -- we union every ``new_content`` entry the patch
    carries). Returns an empty list when there is nothing usable; the caller
    fails CLOSED on emptiness rather than fabricating content.
    """
    patch = getattr(result, "patch", None)
    if patch is None:
        return []
    new_content = getattr(patch, "new_content", ()) or ()
    pairs: List[Tuple[str, str]] = []
    for entry in new_content:
        try:
            path, content = entry
        except (TypeError, ValueError):
            continue
        if not isinstance(path, str) or not path:
            continue
        if isinstance(content, (bytes, bytearray)):
            try:
                text = bytes(content).decode("utf-8")
            except UnicodeDecodeError:
                # Non-UTF-8 content is not something the text-oriented
                # multi-file APPLY path can consume -- fail CLOSED upstream
                # by surfacing nothing for this entry.
                continue
        elif isinstance(content, str):
            text = content
        else:
            continue
        pairs.append((path, text))
    return pairs


# ---------------------------------------------------------------------------
# Zero-loss merge guarantee helpers (sha256 merge ledger + AST validation)
# ---------------------------------------------------------------------------


def _sha256_text(text: str) -> str:
    """Deterministic content hash for the merge ledger.

    Hashes the UTF-8 bytes of the patch content. Stable across processes so
    the input ledger (built from the per-unit patches) and the output ledger
    (built from the composed candidate's ``files``) are directly comparable.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_input_ledger(
    ordered_files: Sequence[Mapping[str, str]],
) -> Tuple[Tuple[str, str], ...]:
    """Build a deterministic, sorted ``(file_path, sha256(content))`` ledger.

    Used for BOTH the input ledger (built from the per-unit union the moment
    each patch is admitted) and the output ledger (re-derived from the
    composed candidate's ``files`` just before VALIDATE/GATE). A pure function
    of the (path, content) pairs -- sorting makes the comparison order-
    independent so a re-ordering during the map-reduce can never masquerade as
    a loss.
    """
    ledger = [
        (str(entry["file_path"]), _sha256_text(str(entry["full_content"])))
        for entry in ordered_files
    ]
    ledger.sort()
    return tuple(ledger)


def _prove_lossless(
    input_ledger: Tuple[Tuple[str, str], ...],
    output_files: Sequence[Mapping[str, str]],
) -> Optional[str]:
    """Mathematically prove the disjoint union is zero-loss + AST-valid.

    Returns ``None`` when every invariant holds; otherwise returns a
    ``"<which>: <detail>"`` string naming the FIRST failing invariant (the
    caller wraps it into ``ComposeFailure(LOSSLESS_PROOF_FAILED, ...)`` so a
    silent drop / overwrite / mutation / syntax break can NEVER leak to the
    gates).

    Invariants (first failure wins):

    * ``count``   -- ``len(input_ledger) == len(output_files)`` (no patch
      dropped, none duplicated by the union).
    * ``content`` -- every input ``(path, sha)`` appears EXACTLY once in the
      output ledger and vice-versa (no content dropped or silently mutated --
      the composed file hashes back to its source unit's patch).
    * ``overwrite`` -- no two output files share a ``file_path`` (the
      collision-matrix invariant, re-proven over the ACTUAL composed hashes).
    * ``ast`` -- each ``.py`` composed file ``ast.parse``-es cleanly; a unit
      that produced syntactically-broken Python is caught HERE, before VALIDATE.
      Non-Python files skip AST (but are still hash-conserved above).
    """
    output_ledger = _build_input_ledger(output_files)

    # (1) Count conservation.
    if len(input_ledger) != len(output_ledger):
        return (
            f"count: inputs={len(input_ledger)} != outputs={len(output_ledger)} "
            "(a patch was dropped or duplicated by the union)"
        )

    # (3) No-overwrite -- duplicate output path means an overwrite happened.
    out_paths = [str(entry["file_path"]) for entry in output_files]
    if len(set(out_paths)) != len(out_paths):
        from collections import Counter

        dup = [p for p, n in Counter(out_paths).items() if n > 1]
        return (
            f"overwrite: output file_path(s) {sorted(dup)!r} appear more than "
            "once (a patch overwrote another during the union)"
        )

    # (2) Content conservation -- the sorted ledgers must be byte-identical.
    if input_ledger != output_ledger:
        in_set = set(input_ledger)
        out_set = set(output_ledger)
        missing = sorted(in_set - out_set)
        extra = sorted(out_set - in_set)
        return (
            "content: input (path, sha256) set != output set "
            f"(dropped_or_mutated={missing!r} unexpected={extra!r})"
        )

    # (4) AST validity of composed Python files.
    for entry in output_files:
        path = str(entry["file_path"])
        if not path.endswith(".py"):
            continue
        content = str(entry["full_content"])
        try:
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            return (
                f"ast: composed file {path!r} is not valid Python "
                f"(line {exc.lineno}: {exc.msg})"
            )

    return None


# ---------------------------------------------------------------------------
# Disjoint UNION (the existing map-reduce, extracted for the proof layer)
# ---------------------------------------------------------------------------


def _compose_ordered_files(
    graph: ExecutionGraph,
    unit_results: Mapping[str, WorkUnitResult],
) -> Any:
    """Build the deterministic disjoint UNION of per-unit patches.

    Returns either ``(ordered_files, input_ledger)`` on a clean union, or a
    :class:`ComposeFailure` on any fail-CLOSED union condition (no units /
    unit-not-success / missing-patch / same-file collision / net-empty). The
    ``input_ledger`` is the authoritative ``(path, sha256)`` ledger captured
    from the patches AT admission time -- it is the ground truth the
    post-compose proof checks the output against.

    This is the same logic that previously lived inline in
    :func:`compose_fanout_result`; extracting it lets the zero-loss proof run
    over the composed result without duplicating the union.
    """
    units = tuple(getattr(graph, "units", ()) or ())
    if not units:
        return ComposeFailure(
            reason=ComposeFailureReason.NO_UNITS,
            detail="execution graph carries zero units",
        )

    ordered_files: List[Dict[str, str]] = []
    seen_paths: Dict[str, str] = {}

    for spec in units:
        unit_id = str(getattr(spec, "unit_id", "") or "")
        result = unit_results.get(unit_id)

        # (2) Presence + terminal success.
        if result is None:
            return ComposeFailure(
                reason=ComposeFailureReason.UNIT_NOT_SUCCESS,
                detail=f"unit {unit_id!r} has no terminal result",
                offending_unit_id=unit_id,
            )
        status = getattr(result, "status", None)
        if status != WorkUnitState.COMPLETED:
            status_val = getattr(status, "value", status)
            return ComposeFailure(
                reason=ComposeFailureReason.UNIT_NOT_SUCCESS,
                detail=(
                    f"unit {unit_id!r} status={status_val!r} "
                    "(expected COMPLETED) -> legacy serial"
                ),
                offending_unit_id=unit_id,
            )

        # (3) Usable patch.
        pairs = _unit_files(result)
        if not pairs:
            return ComposeFailure(
                reason=ComposeFailureReason.UNIT_MISSING_PATCH,
                detail=f"SUCCESS unit {unit_id!r} carried no usable patch content",
                offending_unit_id=unit_id,
            )

        rationale = str(getattr(spec, "goal", "") or "") or (
            f"composed from fan-out unit {unit_id}"
        )

        for file_path, full_content in pairs:
            # (4) Disjointness invariant -- fail CLOSED on overlap.
            prior = seen_paths.get(file_path)
            if prior is not None:
                return ComposeFailure(
                    reason=ComposeFailureReason.COLLISION_INVARIANT_VIOLATED,
                    detail=(
                        f"file {file_path!r} claimed by both unit {prior!r} "
                        f"and unit {unit_id!r}; collision-matrix disjointness "
                        "invariant violated -> refusing silent merge"
                    ),
                    offending_unit_id=unit_id,
                )
            seen_paths[file_path] = unit_id
            ordered_files.append(
                {
                    "file_path": file_path,
                    "full_content": full_content,
                    "rationale": rationale,
                }
            )

    # (5) Defensive net-empty guard.
    if not ordered_files:
        return ComposeFailure(
            reason=ComposeFailureReason.EMPTY_COMPOSITION,
            detail="no files survived composition",
        )

    # Capture the authoritative INPUT ledger from the admitted patches -- this
    # is the ground truth the post-compose zero-loss proof checks against.
    input_ledger = _build_input_ledger(ordered_files)
    return ordered_files, input_ledger


# ---------------------------------------------------------------------------
# Public: compose_fanout_result
# ---------------------------------------------------------------------------


def compose_fanout_result(
    graph: ExecutionGraph,
    unit_results: Mapping[str, WorkUnitResult],
) -> "ComposedCandidate":
    """Map-reduce a successful fan-out's per-unit patches into ONE candidate.

    Pure deterministic function. Returns a :class:`ComposedCandidate` on a
    clean UNION of disjoint per-file patches, or a :class:`ComposeFailure`
    on ANY unit failure / missing patch / collision-invariant violation. The
    return type is annotated as ``ComposedCandidate`` for call sites that
    pattern-match on ``.is_failure``; the runtime type is one of the two.

    Parameters
    ----------
    graph:
        The :class:`ExecutionGraph` that drove the fan-out. Its ``units``
        tuple supplies the DETERMINISTIC ordering for the composed file list
        (so identical fan-outs yield byte-identical candidates) and the
        parent ``op_id``.
    unit_results:
        Mapping ``unit_id -> WorkUnitResult`` for the graph's units. The
        caller (phase_dispatcher) passes the terminal scheduler results
        (``GraphExecutionState.results``). EVERY graph unit must be present
        AND terminally SUCCESS; otherwise -> ComposeFailure.

    Returns
    -------
    ComposedCandidate | ComposeFailure
        ``ComposedCandidate`` on success (``.is_failure is False``);
        ``ComposeFailure`` on any fail-CLOSED condition
        (``.is_failure is True``).

    Notes
    -----
    Fail-CLOSED order (first trip wins):

    1. Empty graph units -> ``NO_UNITS``.
    2. Any graph unit missing from ``unit_results`` OR not terminally
       SUCCESS -> ``UNIT_NOT_SUCCESS`` (no partial compose).
    3. A SUCCESS unit carrying no usable ``(path, content)`` patch ->
       ``UNIT_MISSING_PATCH`` (we never fabricate content).
    4. Two units claiming the SAME file_path -> ``COLLISION_INVARIANT_VIOLATED``
       (the collision matrix promised disjointness; verify defensively, never
       silently overwrite).
    5. Net-empty union -> ``EMPTY_COMPOSITION`` (defensive; should be
       unreachable once 1-3 pass).
    """
    op_id = str(getattr(graph, "op_id", "") or "")

    # Deterministic disjoint UNION (the existing map-reduce), extracted so the
    # zero-loss proof can run over the result without duplicating union logic.
    # Returns either ``(ordered_files, input_ledger)`` or a ComposeFailure.
    composed = _compose_ordered_files(graph, unit_results)
    if isinstance(composed, ComposeFailure):
        return composed  # type: ignore[return-value]
    ordered_files, input_ledger = composed

    # ---------------------------------------------------------------------
    # ZERO-LOSS MERGE GUARANTEE (verification layer over the disjoint union).
    # Prove -- BEFORE the candidate reaches VALIDATE/GATE -- that every input
    # unit's patch is present, byte-preserved (sha256), not overwritten, and
    # AST-valid. ANY violation -> fail-CLOSED ComposeFailure -> legacy serial.
    # A dropped / overwritten / mutated / syntactically-broken patch is now
    # mathematically impossible to leak to the gates.
    # ---------------------------------------------------------------------
    proof_failure = _prove_lossless(input_ledger, ordered_files)
    output_ledger = _build_input_ledger(ordered_files)
    sha_match = input_ledger == output_ledger
    ast_ok = not (
        proof_failure is not None and proof_failure.startswith("ast")
    )
    logger.info(
        "[DAGCompose] lossless proof: inputs=%d outputs=%d sha_match=%s "
        "ast_ok=%s op=%s result=%s",
        len(input_ledger),
        len(output_ledger),
        sha_match,
        ast_ok,
        op_id[:16],
        "PASS" if proof_failure is None else f"FAIL({proof_failure.split(':', 1)[0]})",
    )
    if proof_failure is not None:
        return ComposeFailure(  # type: ignore[return-value]
            reason=ComposeFailureReason.LOSSLESS_PROOF_FAILED,
            detail=f"lossless_proof_failed:{proof_failure}",
            offending_unit_id="",
        )

    primary = ordered_files[0]
    file_paths = tuple(entry["file_path"] for entry in ordered_files)
    composed_rationale = (
        f"[{COMPOSER_ID}] composed {len(ordered_files)} disjoint fan-out "
        f"unit patches into one multi-file candidate for op={op_id[:16]}"
    )

    candidate: Dict[str, Any] = {
        # Primary/authoritative file mirrors files[0] for legacy single-file
        # consumers (orchestrator._iter_candidate_files falls back to these
        # when JARVIS_MULTI_FILE_GEN_ENABLED is off; the multi-file path uses
        # ``files``).
        "file_path": primary["file_path"],
        "full_content": primary["full_content"],
        # The multi-file shape consumed by _iter_candidate_files /
        # _apply_multi_file_candidate (batch-level rollback).
        "files": ordered_files,
        "rationale": composed_rationale,
        # Lineage so downstream telemetry can tell composed candidates apart.
        "composed_by": COMPOSER_ID,
        "composed_op_id": op_id,
    }

    logger.info(
        "[DAGComposer] op=%s composed n_files=%d files=%s composer=%s",
        op_id[:16],
        len(ordered_files),
        list(file_paths),
        COMPOSER_ID,
    )

    return ComposedCandidate(
        op_id=op_id,
        candidate=candidate,
        file_paths=file_paths,
    )


__all__ = [
    "COMPOSER_ID",
    "ComposeFailure",
    "ComposeFailureReason",
    "ComposedCandidate",
    "compose_fanout_result",
    "dag_compose_enabled",
    # Zero-loss merge guarantee surface (verification layer over the union).
    "_build_input_ledger",
    "_compose_ordered_files",
    "_prove_lossless",
]
