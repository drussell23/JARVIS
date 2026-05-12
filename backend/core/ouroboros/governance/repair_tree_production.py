"""Treefinement L2 production wiring (v3.4).
================================================

Composes the substrate ``repair_tree`` shipped in v3.3 (Phases 0-5)
into a fully-wired production tree-runner factory the
``RepairEngine.run()`` strategy gate can register and invoke.

Phase B contents (this file at this revision)
---------------------------------------------

* :class:`GitApplyDiffApplier` — production
  :class:`~backend.core.ouroboros.governance.repair_tree.DiffApplier`
  Protocol implementation. Uses Python's ``asyncio.subprocess`` API
  (no shell — args passed as a list of separate tokens) to invoke
  ``git apply --whitespace=nowarn`` in the per-branch worktree
  (already isolated COW via WorktreeManager) and captures per-file
  (path, old, new) tuples for downstream SemanticGuardian inspection.
* :func:`extract_diff_targets` — pure-function unified-diff parser
  identifying target paths + new-file / deleted-file flags.
* :func:`register_flags` — auto-discovered FlagRegistry seed for
  the production knobs.

Security note: this module spawns ``git`` via the safe
``asyncio.create_subprocess_exec`` API which takes program + arg list
as separate parameters (no shell, no string interpolation, no
command-injection surface). The diff content itself is sent via
stdin as bytes — never appears on the command line.

Phases C+D+E will append to this file
--------------------------------------

* :class:`ProductionBranchGenerator` (Phase C) composing
  :meth:`RepairEngine._generate_repair_candidate` (Phase A
  primitive) + :func:`maybe_inject_sibling_outcomes` (Phase 3
  cross-branch substrate).
* :func:`production_tree_runner_factory` (Phase D) composing
  WorktreeManager + GitApplyDiffApplier + ProductionBranchGenerator
  + CanonicalBranchValidator into a fully-wired RepairTreeRunner.
* :func:`tree_result_to_repair_result` (Phase D) deterministic
  RepairTreeResult → RepairResult adapter.
* :func:`register_production_factory_at_boot` (Phase E) lazy-
  registration hook.

Authority asymmetry (§1 Boundary)
---------------------------------

This module composes the substrate; it makes no policy decisions.
Forbidden imports (AST-pinned in Phase E):
``orchestrator`` / ``iron_gate`` / ``change_engine`` /
``candidate_generator`` / ``policy_engine`` / ``risk_tier``.

The only canonical subprocess invocation here is ``git apply``. No
parallel patch primitives.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.repair_tree import (
    BranchGenerator,  # Protocol — Phase C impl below
    BranchOutcome,
    CrossBranchLearningConfig,
    DiffApplier,  # Protocol — Phase B impl below
    DiffApplyResult,
    LayerVerdict,
    Posture,
    PruningReason,
    RepairBranch,
    RepairTreeLayer,
    RepairTreeResult,
    RepairTreeRunner,
    TreefinementBudget,
    maybe_inject_sibling_outcomes,
)

logger = logging.getLogger("Ouroboros.RepairTreeProduction")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


REPAIR_TREE_PRODUCTION_SCHEMA_VERSION: str = "repair_tree_production.v1"

# Per-call timeout for ``git apply``. Default 15s mirrors
# ``RepairSandbox.apply_patch``'s 15s timeout — same canonical
# discipline; same operator expectations.
GIT_APPLY_TIMEOUT_S_ENV_VAR: str = "JARVIS_L2_TREE_GIT_APPLY_TIMEOUT_S"
_DEFAULT_GIT_APPLY_TIMEOUT_S: float = 15.0
_MIN_GIT_APPLY_TIMEOUT_S: float = 1.0
_MAX_GIT_APPLY_TIMEOUT_S: float = 120.0

# Phase C — per-call cost estimate reported by ProductionBranchGenerator.
# Provider response may not directly expose USD; this is a flat
# per-call estimate matching the IMMEDIATE route's typical envelope
# (Claude / DoubleWord generations at ~50-200 input + 200-400 output
# tokens). Real cost tracking lives in cost_governor — this estimate
# feeds the tree-search budget envelope (max_total_validation_runs)
# and operator-visible per-branch cost telemetry.
PER_CALL_COST_USD_ENV_VAR: str = "JARVIS_L2_TREE_GENERATOR_COST_USD_ESTIMATE"
_DEFAULT_PER_CALL_COST_USD: float = 0.005

# Maximum stderr length captured into ``error`` field on git failure
# — operators need enough context to diagnose without truncating
# the JSONL persistence file.
_GIT_STDERR_MAX_CHARS: int = 200


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float = 1e9,
) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, min(maximum, float(raw)))
    except (ValueError, TypeError):
        logger.warning(
            "[RepairTreeProduction] invalid %s=%r — using default %f",
            name, raw, default,
        )
        return default


def _git_apply_timeout_s() -> float:
    """Read per-call ``git apply`` timeout from env. NEVER raises."""
    return _env_float(
        GIT_APPLY_TIMEOUT_S_ENV_VAR,
        _DEFAULT_GIT_APPLY_TIMEOUT_S,
        minimum=_MIN_GIT_APPLY_TIMEOUT_S,
        maximum=_MAX_GIT_APPLY_TIMEOUT_S,
    )


# ===========================================================================
# Unified-diff target extraction (pure function, NEVER raises)
# ===========================================================================


@dataclass(frozen=True)
class _ParsedDiffTarget:
    """One target file identified in a unified diff."""

    path: str
    is_new: bool       # ``--- /dev/null`` (file created by diff)
    is_deleted: bool   # ``+++ /dev/null`` (file deleted by diff)


def extract_diff_targets(diff: str) -> List[_ParsedDiffTarget]:
    """Parse a unified diff for target file paths + new/deleted flags.

    Returns targets in order of first appearance; deduplicates by
    path. NEVER raises — malformed input yields an empty list with
    a structured debug log.

    Path resolution rules:

      * ``+++ b/path`` → strip ``b/`` prefix → use ``path``
      * ``+++ /dev/null`` (deletion) → use ``--- a/path`` (with ``a/``
        prefix stripped)
      * Either side ``/dev/null`` flags the corresponding new/deleted
        attribute
      * Quoted paths (``--- "a/path with spaces"``) are unquoted
        defensively (rare in practice; git's c-quote format)
    """
    if not isinstance(diff, str) or not diff.strip():
        return []

    targets: List[_ParsedDiffTarget] = []
    seen: set = set()

    lines = diff.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("--- ") or i + 1 >= len(lines):
            i += 1
            continue
        next_line = lines[i + 1]
        if not next_line.startswith("+++ "):
            i += 1
            continue

        try:
            src_raw = line[4:].strip()
            dst_raw = next_line[4:].strip()
            # Strip trailing tab + timestamp if present (some diff
            # tools emit ``--- a/path\t2024-01-01 ...``).
            src = src_raw.split("\t", 1)[0]
            dst = dst_raw.split("\t", 1)[0]
            src = _unquote_diff_path(src)
            dst = _unquote_diff_path(dst)

            is_new = src == "/dev/null"
            is_deleted = dst == "/dev/null"

            if is_deleted:
                # +++ /dev/null → take path from src
                path = _strip_git_prefix(src)
            else:
                # Modified or new → take path from dst
                path = _strip_git_prefix(dst)

            if not path or path == "/dev/null":
                i += 2
                continue

            if path in seen:
                i += 2
                continue
            seen.add(path)
            targets.append(_ParsedDiffTarget(
                path=path, is_new=is_new, is_deleted=is_deleted,
            ))
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[RepairTreeProduction] failed to parse diff header "
                "at line %d", i, exc_info=True,
            )
        i += 2

    return targets


def _strip_git_prefix(path: str) -> str:
    """Strip git's default ``a/`` / ``b/`` prefix when present."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _unquote_diff_path(raw: str) -> str:
    """Defensively unquote git c-quoted paths.

    Git emits ``"path with \\"escapes\\""`` for paths containing
    spaces / non-ASCII / quotes. Full c-quote parsing is overkill
    for the common case; we strip surrounding quotes if present
    and decode the obvious escapes.
    """
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        inner = raw[1:-1]
        # Decode the most common git c-quote escapes
        return (
            inner
            .replace('\\"', '"')
            .replace("\\\\", "\\")
            .replace("\\t", "\t")
            .replace("\\n", "\n")
        )
    return raw


# ===========================================================================
# GitApplyDiffApplier — production DiffApplier Protocol implementation
# ===========================================================================


class GitApplyDiffApplier:
    """Production DiffApplier composing canonical ``git apply``.

    Implements the
    :class:`~backend.core.ouroboros.governance.repair_tree.DiffApplier`
    Protocol shipped in Treefinement Phase 2. Composed by
    :class:`CanonicalBranchValidator` (Phase 2) which receives this
    instance via dependency injection.

    Stage discipline (all NEVER-raises except CancelledError)
    ---------------------------------------------------------

    1. Parse diff via :func:`extract_diff_targets` → list of paths
       + new/deleted flags
    2. Capture per-file old content (read worktree files BEFORE apply)
    3. Invoke ``git apply --whitespace=nowarn`` via the safe
       ``asyncio.create_subprocess_exec`` API (program + args as
       a list — no shell, no command injection surface). Diff
       content is piped via stdin as bytes; cwd=worktree_dir.
    4. Capture per-file new content (read worktree files AFTER apply)
    5. Return :class:`DiffApplyResult` with tuples + empty error on
       success; empty tuples + structured error on any failure

    Failure modes (each yields ``DiffApplyResult(files=(),
    error="<code>")`` — caller short-circuits to PRUNED_VALIDATOR):

      * ``empty_diff`` — diff is empty or whitespace-only
      * ``no_targets_in_diff`` — parser found no header pairs
      * ``read_old_failed:<path>:<exc>`` — worktree file read failed
        before apply
      * ``git_not_installed`` — ``git`` binary missing on PATH
      * ``git_subprocess_failed:<exc>`` — subprocess creation failed
      * ``git_apply_timeout`` — exceeded
        ``JARVIS_L2_TREE_GIT_APPLY_TIMEOUT_S`` (default 15s)
      * ``git_apply_failed:exit<N>:<stderr>`` — git exited non-zero
        (rejection, conflict, malformed diff)
      * ``read_new_failed:<path>:<exc>`` — post-apply worktree read
        failed (defensive — git apply lied about success)

    Worktree state on failure
    -------------------------

    On any non-success path, the worktree may be in a partial state
    (some files written, others not). The Phase 1 runner ABANDONS
    failed branches and lets the next branch use a fresh worktree
    (per design open-question #3 from the v3.4 plan). This is the
    "fail fast, isolated" semantic — no rollback in the applier.

    Cancellation contract
    ---------------------

    ``asyncio.CancelledError`` propagates immediately. Best-effort
    subprocess kill on cancellation; ``proc.wait()`` failure during
    cleanup is swallowed (the canonical reap-orphans sweep on next
    boot covers anything we miss).
    """

    def __init__(
        self,
        *,
        timeout_s: Optional[float] = None,
        git_executable: str = "git",
    ) -> None:
        """Construct an applier.

        Parameters
        ----------
        timeout_s : float, optional
            Per-call ``git apply`` timeout. Defaults to
            :func:`_git_apply_timeout_s` (env-loaded). Tests inject
            short values (e.g., 0.05s) to verify timeout path.
        git_executable : str
            Path to the ``git`` binary. Default ``"git"`` (resolved
            via PATH). Tests inject ``"/nonexistent/git"`` to verify
            the not-installed path.
        """
        self._timeout_s = (
            timeout_s if timeout_s is not None
            else _git_apply_timeout_s()
        )
        self._git = git_executable

    # ---------------------------------------------------------------------
    # DiffApplier Protocol implementation
    # ---------------------------------------------------------------------

    async def __call__(
        self,
        *,
        worktree_dir: Path,
        diff: str,
    ) -> DiffApplyResult:
        """Apply diff in worktree + return per-file (path, old, new)
        tuples. NEVER raises except CancelledError."""
        # Stage 0 — input validation
        if not isinstance(diff, str) or not diff.strip():
            return DiffApplyResult(files=(), error="empty_diff")

        # Stage 1 — parse targets
        targets = extract_diff_targets(diff)
        if not targets:
            return DiffApplyResult(
                files=(), error="no_targets_in_diff",
            )

        # Stage 2 — capture old content
        old_content: Dict[str, str] = {}
        for tgt in targets:
            try:
                if tgt.is_new:
                    # File doesn't exist pre-apply by definition
                    old_content[tgt.path] = ""
                else:
                    full_path = worktree_dir / tgt.path
                    try:
                        old_content[tgt.path] = full_path.read_text(
                            encoding="utf-8",
                        )
                    except FileNotFoundError:
                        # Modified file flagged but missing — diff
                        # is stale. Treat as empty old content; git
                        # apply will reject if the diff doesn't match.
                        old_content[tgt.path] = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                return DiffApplyResult(
                    files=(),
                    error=(
                        f"read_old_failed:{tgt.path}:"
                        f"{type(exc).__name__}"
                    ),
                )

        # Stage 3 — invoke ``git apply`` via the safe asyncio API.
        # Program + args passed as separate list elements; no shell
        # interpretation. Diff sent via stdin pipe (never on argv).
        try:
            proc = await asyncio.create_subprocess_exec(
                self._git,
                "apply",
                "--whitespace=nowarn",
                "-",
                cwd=str(worktree_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return DiffApplyResult(
                files=(), error="git_not_installed",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return DiffApplyResult(
                files=(),
                error=f"git_subprocess_failed:{type(exc).__name__}",
            )

        try:
            try:
                _stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=diff.encode("utf-8")),
                    timeout=self._timeout_s,
                )
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass
                return DiffApplyResult(
                    files=(), error="git_apply_timeout",
                )
        except asyncio.CancelledError:
            # Cancellation during communicate — cleanup + propagate
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            raise

        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace").strip()
            if len(stderr_text) > _GIT_STDERR_MAX_CHARS:
                stderr_text = (
                    stderr_text[: _GIT_STDERR_MAX_CHARS - 3] + "..."
                )
            return DiffApplyResult(
                files=(),
                error=(
                    f"git_apply_failed:exit{proc.returncode}:"
                    f"{stderr_text}"
                ),
            )

        # Stage 4 — capture new content + assemble tuples
        files: List[Tuple[str, str, str]] = []
        for tgt in targets:
            try:
                if tgt.is_deleted:
                    # File removed by apply — new content empty
                    new_content = ""
                else:
                    full_path = worktree_dir / tgt.path
                    try:
                        new_content = full_path.read_text(
                            encoding="utf-8",
                        )
                    except FileNotFoundError:
                        return DiffApplyResult(
                            files=(),
                            error=(
                                f"read_new_failed:{tgt.path}:"
                                "not_found"
                            ),
                        )
                files.append((
                    tgt.path,
                    old_content[tgt.path],
                    new_content,
                ))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                return DiffApplyResult(
                    files=(),
                    error=(
                        f"read_new_failed:{tgt.path}:"
                        f"{type(exc).__name__}"
                    ),
                )

        return DiffApplyResult(files=tuple(files), error="")


# ===========================================================================
# Protocol conformance check (runtime)
# ===========================================================================


def _verify_protocol_conformance() -> bool:
    """Best-effort runtime check that GitApplyDiffApplier conforms to
    the DiffApplier Protocol. Returns True on success, False on any
    failure (defensive — never raises into module import)."""
    try:
        instance = GitApplyDiffApplier()
        # Runtime-checkable Protocols accept isinstance() checks
        return isinstance(instance, DiffApplier)
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# Phase C — ProductionBranchGenerator
# ===========================================================================
#
# Composes:
#   * RepairEngine._generate_repair_candidate (Phase A primitive —
#     single-source provider invocation)
#   * maybe_inject_sibling_outcomes (Phase 3 cross-branch substrate)
#   * Posture-aware skip-list (Phase 3 CrossBranchLearningConfig)
#   * RepairContext canonical shape (op_context.RepairContext)
#
# Implements the BranchGenerator Protocol shipped in Treefinement
# Phase 1. The Phase 4 RepairTreeRunner accepts this instance via
# dependency injection (no Protocol changes; pure composition).
#
# Cross-branch learning threading
# -------------------------------
#
# Phase 3 ships ``maybe_inject_sibling_outcomes(prompt, ...)`` as a
# pure-function block builder. Phase C composes it with empty prompt
# to extract the rendered cross-branch block, then threads it via:
#
#   1. ``hypothesis_seed`` parameter on ``_generate_repair_candidate``
#      (Phase A primitive captures this for telemetry; future
#      provider extensions may consume natively).
#   2. ``cross_branch_outcomes`` field added to the augmented
#      RepairContext-like wrapper (provider may read for prompt
#      construction; if not, the cross-branch signal is dormant
#      until a prompt-injection arc enables it).
#
# Phase C MVP accepts this dormancy honestly: tree mode WITH cross-
# branch learning's full delta requires a follow-on provider arc
# that consumes the augmented field. Until then, tree mode still
# delivers parallel exploration + validator pruning + WON
# termination — substantial value over the linear FSM regardless of
# whether the cross-branch signal reaches the model's prompt.


@dataclass(frozen=True)
class _AugmentedRepairContext:
    """Composes the canonical RepairContext shape with an additive
    ``cross_branch_outcomes`` field. Frozen to mirror the canonical
    RepairContext immutability semantic.

    The provider's existing prompt-construction code receives this
    via the ``repair_context`` keyword. Code that uses ``getattr``
    or attribute access reads ALL canonical fields (transparent
    augmentation). Code that does ``isinstance(..., RepairContext)``
    will reject it — but no current code path does that (verified
    in Phase E AST pin).
    """

    iteration: int
    max_iterations: int
    failure_class: str
    failure_signature_hash: str
    failing_tests: Tuple[str, ...]
    failure_summary: str
    current_candidate_content: str
    current_candidate_file_path: str
    # Phase C additive — cross-branch outcomes block. Empty string
    # when no cross-branch context applies (layer 0, master flag
    # off, posture skip, no informative siblings).
    cross_branch_outcomes: str = ""


def _per_call_cost_usd() -> float:
    """Read per-call cost estimate from env. NEVER raises."""
    return _env_float(
        PER_CALL_COST_USD_ENV_VAR,
        _DEFAULT_PER_CALL_COST_USD,
        minimum=0.0,
        maximum=10.0,
    )


class ProductionBranchGenerator:
    """Production BranchGenerator Protocol implementation.

    Composes the Phase A generation primitive + Phase 3 cross-branch
    substrate. Implements the
    :class:`~backend.core.ouroboros.governance.repair_tree.BranchGenerator`
    Protocol (verified at construction time + AST-pinned in Phase E).

    Per-branch flow
    ---------------

    1. Build base ``RepairContext`` via injected
       ``repair_context_builder(parent_branch, layer_index, ctx)``
       OR the default builder (composes ``ctx.generation`` + parent
       branch state — minimal viable shape).
    2. Build cross-branch block via ``maybe_inject_sibling_outcomes``
       (empty-prompt invocation; extracts rendered block).
    3. Wrap base + cross-branch block in ``_AugmentedRepairContext``
       — additive field; canonical fields preserved for transparent
       provider reads.
    4. Derive ``hypothesis_seed`` from parent branch's
       ``fix_hypothesis`` (optional; None for layer 0 or no parent).
    5. Call ``repair_engine._generate_repair_candidate(...)`` —
       single-source provider invocation.
    6. Map ``CandidateGenerationResult`` → ``(diff, hypothesis,
       cost_usd)`` Protocol return:
       - On success with non-empty ``unified_diff``: return tuple.
       - On success with only ``full_content`` (Phase D-deferred
         shape): return ``("", "candidate_full_content_only", cost)``
         — the validator's ``GitApplyDiffApplier`` will quarantine
         to PRUNED_VALIDATOR with structured error code
         ``no_targets_in_diff``. Phase D's factory may inject a
         diff synthesizer to lift this limitation.
       - On primitive failure: return
         ``("", f"generation_failed:{stop_reason}", 0.0)``.

    NEVER raises into the runner — only ``asyncio.CancelledError``
    propagates (orchestrator POSTMORTEM contract; same as Phase A
    primitive).
    """

    def __init__(
        self,
        *,
        repair_engine: Any,
        ctx: Any,
        pipeline_deadline: Any,
        cross_branch_config: Optional[CrossBranchLearningConfig] = None,
        posture: Optional[Posture] = None,
        cost_per_call_usd: Optional[float] = None,
        repair_context_builder: Optional[
            Any  # Callable[[Optional[RepairBranch], int, Any], Any]
        ] = None,
    ) -> None:
        """Construct a generator.

        Parameters
        ----------
        repair_engine : RepairEngine
            The canonical engine instance. Generator composes
            ``repair_engine._generate_repair_candidate`` (Phase A
            primitive). REQUIRED.
        ctx : Any
            OperationContext from the orchestrator (op_id, generation,
            etc.). Threaded through to the primitive.
        pipeline_deadline : datetime
            UTC deadline; threaded through to the primitive.
        cross_branch_config : CrossBranchLearningConfig, optional
            Phase 3 substrate config. Defaults to
            ``CrossBranchLearningConfig.from_env()``. When the
            ``enabled`` flag is False, sibling-outcome enrichment
            is skipped (block remains empty).
        posture : Posture, optional
            Current operator posture. Filters cross-branch injection
            via ``config.skip_postures`` (default skip: MAINTAIN).
            None → no posture filtering applied.
        cost_per_call_usd : float, optional
            Per-call cost estimate. Defaults to
            :func:`_per_call_cost_usd` (env-loaded, default 0.005).
            Reported on the BranchGenerator Protocol return tuple.
        repair_context_builder : Callable, optional
            Function ``(parent_branch, layer_index, ctx) ->
            RepairContext-like`` that builds the canonical context
            shape per-branch. Default uses an internal minimal
            builder that composes ``ctx.generation`` + parent branch
            state. Production wiring (Phase D) may inject a more
            sophisticated builder; tests inject deterministic stubs.
        """
        self._engine = repair_engine
        self._ctx = ctx
        self._pipeline_deadline = pipeline_deadline
        self._cross_branch_config = cross_branch_config
        self._posture = posture
        self._cost_usd = (
            cost_per_call_usd if cost_per_call_usd is not None
            else _per_call_cost_usd()
        )
        self._build_context = (
            repair_context_builder
            if repair_context_builder is not None
            else self._default_repair_context_builder
        )

    # ---------------------------------------------------------------------
    # BranchGenerator Protocol implementation
    # ---------------------------------------------------------------------

    async def __call__(
        self,
        *,
        op_id: str,
        layer_index: int,
        parent_branch: Optional[RepairBranch],
        sibling_outcomes: Tuple[RepairBranch, ...],
    ) -> Tuple[str, str, float]:
        """Generate one branch's candidate. NEVER raises except
        CancelledError. Returns ``(diff, fix_hypothesis, cost_usd)``
        per the BranchGenerator Protocol contract."""
        try:
            # Stage 1 — build base repair_context per branch
            try:
                base_context = self._build_context(
                    parent_branch, layer_index, self._ctx,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[ProductionBranchGenerator] op=%s layer=%d "
                    "context_builder raised: %s",
                    op_id, layer_index, exc, exc_info=True,
                )
                return (
                    "",
                    (
                        f"generation_failed:context_builder:"
                        f"{type(exc).__name__}"
                    ),
                    0.0,
                )

            # Stage 2 — build cross-branch block via Phase 3 substrate.
            # ``maybe_inject_sibling_outcomes`` with empty prompt
            # returns either "" (no injection per master-flag /
            # posture / layer / sibling gating) OR "\n\n<block>".
            # Stripping yields just the block text.
            try:
                injected = maybe_inject_sibling_outcomes(
                    prompt="",
                    sibling_outcomes=sibling_outcomes,
                    layer_index=layer_index,
                    op_id=op_id,
                    posture=self._posture,
                    config=self._cross_branch_config,
                )
                cross_branch_block = injected.strip()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — fail-open
                # Phase 3's function NEVER raises per its own
                # contract; defense in depth here.
                logger.debug(
                    "[ProductionBranchGenerator] cross_branch block "
                    "build raised; proceeding without enrichment",
                    exc_info=True,
                )
                cross_branch_block = ""

            # Stage 3 — wrap context with augmented cross-branch field.
            # Provider's existing prompt-construction code reads the
            # canonical fields transparently; future provider extensions
            # may consume the new ``cross_branch_outcomes`` field.
            augmented_context = self._augment_context(
                base_context, cross_branch_block,
            )

            # Stage 4 — derive hypothesis seed from parent branch.
            # Phase A primitive captures this for telemetry; future
            # provider extensions may consume natively. None for
            # layer 0 / no parent.
            hypothesis_seed = (
                parent_branch.fix_hypothesis
                if parent_branch is not None
                and (parent_branch.fix_hypothesis or "").strip()
                else None
            )

            # Stage 5 — call canonical primitive (single source).
            gen_outcome = await self._engine._generate_repair_candidate(
                self._ctx,
                self._pipeline_deadline,
                repair_context=augmented_context,
                hypothesis_seed=hypothesis_seed,
            )

            # Stage 6 — map CandidateGenerationResult → Protocol tuple
            if gen_outcome.candidate is None:
                return (
                    "",
                    (
                        f"generation_failed:"
                        f"{gen_outcome.stop_reason or 'unknown'}"
                    ),
                    0.0,
                )
            return self._extract_tuple(
                gen_outcome.candidate,
                parent_branch=parent_branch,
                layer_index=layer_index,
            )
        except asyncio.CancelledError:
            raise

    # ---------------------------------------------------------------------
    # Internal helpers — pure functions, no I/O
    # ---------------------------------------------------------------------

    @staticmethod
    def _default_repair_context_builder(
        parent_branch: Optional[RepairBranch],
        layer_index: int,
        ctx: Any,
    ) -> Any:
        """Build a minimal RepairContext per branch.

        Composes ``ctx.generation`` for the initial L1 failure +
        parent_branch state for layer-N+1 inheritance. This is the
        default; Phase D's factory wiring may inject a richer builder
        that pulls failure detail from the canonical OperationLedger.
        """
        from backend.core.ouroboros.governance.op_context import (
            RepairContext,
        )
        # Defaults — minimal viable shape for layer 0 with no
        # parent branch context.
        iteration = max(1, layer_index + 1)
        max_iterations = 5  # mirrors RepairBudget default
        failure_class = "test"
        failure_sig = ""
        failing_tests: Tuple[str, ...] = ()
        failure_summary = ""
        current_content = ""
        current_path = ""

        # Pull from ctx.generation for layer 0 (first L2 attempt).
        if layer_index == 0:
            try:
                gen = getattr(ctx, "generation", None)
                if gen is not None:
                    candidates = getattr(gen, "candidates", ())
                    if candidates:
                        first = candidates[0]
                        if isinstance(first, dict):
                            current_content = (
                                first.get("full_content", "")
                                or first.get("current_candidate", "")
                                or ""
                            )
                            current_path = first.get("file_path", "") or ""
            except Exception:  # noqa: BLE001
                # ctx.generation may not exist or have unexpected shape;
                # fall back to defaults.
                pass

        # Inherit from parent branch for layer N+1.
        if parent_branch is not None:
            failure_class = (
                parent_branch.failure_class or failure_class
            )

        return RepairContext(
            iteration=iteration,
            max_iterations=max_iterations,
            failure_class=failure_class,
            failure_signature_hash=failure_sig,
            failing_tests=failing_tests,
            failure_summary=failure_summary,
            current_candidate_content=current_content,
            current_candidate_file_path=current_path,
        )

    @staticmethod
    def _augment_context(
        base_context: Any,
        cross_branch_block: str,
    ) -> _AugmentedRepairContext:
        """Wrap the base context with the cross-branch block as an
        additive field. NEVER raises — falls back to a minimal
        augmented context on any attribute access failure."""
        try:
            return _AugmentedRepairContext(
                iteration=getattr(base_context, "iteration", 1),
                max_iterations=getattr(base_context, "max_iterations", 5),
                failure_class=getattr(base_context, "failure_class", ""),
                failure_signature_hash=getattr(
                    base_context, "failure_signature_hash", "",
                ),
                failing_tests=tuple(
                    getattr(base_context, "failing_tests", ()) or ()
                ),
                failure_summary=getattr(
                    base_context, "failure_summary", "",
                ),
                current_candidate_content=getattr(
                    base_context, "current_candidate_content", "",
                ),
                current_candidate_file_path=getattr(
                    base_context, "current_candidate_file_path", "",
                ),
                cross_branch_outcomes=cross_branch_block,
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[ProductionBranchGenerator] augment_context fallback",
                exc_info=True,
            )
            return _AugmentedRepairContext(
                iteration=1,
                max_iterations=5,
                failure_class="",
                failure_signature_hash="",
                failing_tests=(),
                failure_summary="",
                current_candidate_content="",
                current_candidate_file_path="",
                cross_branch_outcomes=cross_branch_block,
            )

    def _extract_tuple(
        self,
        candidate: Dict[str, Any],
        *,
        parent_branch: Optional[RepairBranch],
        layer_index: int,
    ) -> Tuple[str, str, float]:
        """Map provider candidate dict → BranchGenerator Protocol
        tuple. Pure function; NEVER raises."""
        try:
            diff = candidate.get("unified_diff", "") or ""
            full_content = candidate.get("full_content", "") or ""
        except Exception:  # noqa: BLE001
            return ("", "generation_failed:malformed_candidate", 0.0)

        hypothesis = self._derive_hypothesis(
            candidate, parent_branch=parent_branch,
            layer_index=layer_index,
        )

        if diff.strip():
            return (diff, hypothesis, self._cost_usd)
        if full_content.strip():
            # Phase C MVP limitation — GitApplyDiffApplier requires
            # unified_diff format. Phase D's factory wiring may inject
            # a difflib-based synthesizer to lift this. Until then,
            # full_content-only candidates surface as a structured
            # failure that the validator quarantines.
            return (
                "",
                "candidate_full_content_only_unsupported_phase_c",
                self._cost_usd,
            )
        return ("", "candidate_no_content", 0.0)

    @staticmethod
    def _derive_hypothesis(
        candidate: Dict[str, Any],
        *,
        parent_branch: Optional[RepairBranch],
        layer_index: int,
    ) -> str:
        """Derive a fix_hypothesis string from the candidate +
        parent context. Pure function; NEVER raises."""
        try:
            for key in ("fix_hypothesis", "rationale", "intent"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    base = value.strip()
                    break
            else:
                base = f"l2_repair_layer_{layer_index}"
            if (
                parent_branch is not None
                and (parent_branch.fix_hypothesis or "").strip()
            ):
                parent_hyp = parent_branch.fix_hypothesis.strip()
                if len(parent_hyp) > 80:
                    parent_hyp = parent_hyp[:77] + "..."
                return f"extends[{parent_hyp}]: {base}"
            return base
        except Exception:  # noqa: BLE001
            return f"l2_repair_layer_{layer_index}"


def _verify_generator_protocol_conformance(
    repair_engine: Any, ctx: Any, pipeline_deadline: Any,
) -> bool:
    """Best-effort runtime check that ProductionBranchGenerator
    conforms to the BranchGenerator Protocol."""
    try:
        instance = ProductionBranchGenerator(
            repair_engine=repair_engine,
            ctx=ctx,
            pipeline_deadline=pipeline_deadline,
        )
        return isinstance(instance, BranchGenerator)
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# FlagRegistry self-registration (auto-discovered by walker)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. Picked up zero-edit
    by ``flag_registry_seed._discover_module_provided_flags``.
    NEVER raises — fail-open per §33.1.

    Phase B ships 1 flag (the ``git apply`` timeout knob). Phases
    C/D/E may extend this list.
    """
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
            name=GIT_APPLY_TIMEOUT_S_ENV_VAR,
            type=FlagType.FLOAT,
            default=_DEFAULT_GIT_APPLY_TIMEOUT_S,
            description=(
                "Per-call timeout (seconds) for ``git apply`` inside "
                "GitApplyDiffApplier. Default 15s mirrors "
                "RepairSandbox.apply_patch's canonical 15s discipline. "
                "On timeout: kill the subprocess, return "
                "DiffApplyResult(files=(), error='git_apply_timeout'). "
                "Clamped [1, 120]."
            ),
            category=Category.TIMING,
            source_file=(
                "backend/core/ouroboros/governance/"
                "repair_tree_production.py"
            ),
            example="15.0",
            since=(
                "Treefinement Production Wiring Phase B "
                "(v3.4, 2026-05-11)"
            ),
        ),
        FlagSpec(
            name=PER_CALL_COST_USD_ENV_VAR,
            type=FlagType.FLOAT,
            default=_DEFAULT_PER_CALL_COST_USD,
            description=(
                "Per-call cost estimate (USD) reported by "
                "ProductionBranchGenerator on each branch generation. "
                "Default 0.005 matches the IMMEDIATE route's typical "
                "envelope (50-200 input + 200-400 output tokens). "
                "Real cost tracking lives in cost_governor; this "
                "estimate feeds the tree-search cost telemetry + "
                "operator-visible per-branch cost field. Clamped "
                "[0, 10]."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/"
                "repair_tree_production.py"
            ),
            example="0.005",
            since=(
                "Treefinement Production Wiring Phase C "
                "(v3.4, 2026-05-11)"
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — boot-time fail-open
            logger.debug(
                "[RepairTreeProduction] flag registration failed "
                "for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count


# ===========================================================================
# Phase D — production factory + RepairTreeResult → RepairResult adapter
# ===========================================================================
#
# Composes Phases A+B+C into a single zero-arg async invocation
# closure that ``RepairEngine.run()``'s strategy gate registers via
# Phase 5's ``register_production_tree_runner_factory``.
#
# Factory contract (the Phase D refinement of Phase 5's typing):
#
#   factory(*, budget, ctx, repair_engine, pipeline_deadline,
#           posture=None) -> Callable[[], Awaitable[RepairTreeResult]]
#
# Single-call invariant: one factory call → one tree-result awaitable.
# The closure captures the WorktreeManager + GitApplyDiffApplier +
# CanonicalBranchValidator + ProductionBranchGenerator + max_layers
# config; the gate just awaits the closure and adapts the result.
#
# Adapter contract:
#
#   tree_result_to_repair_result(tree_result, *, op_id) -> RepairResult
#
# Pure-function deterministic mapping over LayerVerdict × BranchOutcome
# taxonomies. NEVER raises — degraded inputs produce
# ``RepairResult(terminal="L2_STOPPED",
# stop_reason="treefinement_adapter_failed:...")``.


# Static taxonomy mapping — closed sets defined at module scope so
# AST pins in Phase E can verify the mapping table without walking
# code paths.
_TREE_VERDICT_TO_STOP_REASON: Dict[str, str] = {
    LayerVerdict.EXHAUSTED.value: "treefinement_exhausted",
    LayerVerdict.BUDGET_TERMINAL.value: "treefinement_budget_terminal",
}

_TREE_OUTCOME_TO_ITERATION_OUTCOME: Dict[str, str] = {
    BranchOutcome.PROMOTED.value: "progress",
    BranchOutcome.WON.value: "converged",
    BranchOutcome.PRUNED_VALIDATOR.value: "no_progress",
    BranchOutcome.PRUNED_DUPLICATE.value: "no_progress",
    BranchOutcome.PRUNED_BUDGET.value: "stopped",
}


def production_tree_runner_factory(
    *,
    budget: TreefinementBudget,
    ctx: Any,
    repair_engine: Any,
    pipeline_deadline: Any,
    posture: Optional[Posture] = None,
    max_layers: Optional[int] = None,
    test_runner: Optional[Any] = None,
    worktree_manager: Optional[Any] = None,
    diff_applier: Optional[Any] = None,
    semantic_guardian: Optional[Any] = None,
):
    """Construct a zero-arg invocation closure for one tree-search run.

    Composes:

      * :class:`worktree_manager.WorktreeManager` — branch isolation
        (canonical; reap-orphans on boot covers SIGKILL recovery)
      * :class:`GitApplyDiffApplier` (Phase B) — DiffApplier Protocol impl
      * :class:`~backend.core.ouroboros.governance.test_runner.TestRunner`
        — pytest invocation
      * :class:`~backend.core.ouroboros.governance.repair_tree.
        CanonicalBranchValidator` (Phase 2) — composes ascii_strict_gate
        + SemanticGuardian + TestRunner
      * :class:`ProductionBranchGenerator` (Phase C) — composes Phase
        A primitive + Phase 3 cross-branch substrate
      * :class:`~backend.core.ouroboros.governance.repair_tree.
        RepairTreeRunner` (Phase 1) — BFS/BEAM_K layer dispatch

    Returns a zero-arg async callable that runs the tree end-to-end
    and returns a :class:`RepairTreeResult`. Caller's responsibility
    to ``await`` exactly once and adapt the result via
    :func:`tree_result_to_repair_result`.

    Parameters
    ----------
    budget : TreefinementBudget
        Tree-only knobs (strategy / K / beam_width / dedup /
        cross-branch / emergency-demote).
    ctx : Any
        OperationContext from the orchestrator. MUST expose
        ``op_id`` and ``repo_root`` (or equivalent) attributes.
    repair_engine : RepairEngine
        Engine instance — generator composes the Phase A primitive
        from this instance.
    pipeline_deadline : datetime
        UTC deadline; threaded to the generator + each branch's
        validator call.
    posture : Posture, optional
        Current operator posture for K-sizing + cross-branch skip.
    max_layers : int, optional
        Tree depth cap. Defaults to ``repair_budget.max_iterations``
        from the engine (mirrors LINEAR FSM ceiling).
    test_runner : Any, optional
        Injectable for tests; production constructs via
        ``TestRunner(repo_root=ctx.repo_root)``.
    worktree_manager : Any, optional
        Injectable for tests; production constructs via
        ``WorktreeManager(repo_root=ctx.repo_root)``.
    diff_applier : Any, optional
        Injectable for tests; defaults to a fresh
        ``GitApplyDiffApplier()``.
    semantic_guardian : Any, optional
        Injectable for tests; defaults to ``SemanticGuardian()``.

    Returns
    -------
    Callable[[], Awaitable[RepairTreeResult]]
        Zero-arg async closure. The closure NEVER raises except
        ``CancelledError`` — internal failures surface as degraded
        ``RepairTreeResult`` (empty layers + adapter handles).
    """
    # Lazy imports — production-only dependencies. Keeps the
    # production-wiring module free of hard dependencies at boot
    # for non-production paths.
    from backend.core.ouroboros.governance.repair_tree import (
        CanonicalBranchValidator,
    )
    from backend.core.ouroboros.governance.semantic_guardian import (
        SemanticGuardian,
    )
    from backend.core.ouroboros.governance.test_runner import (
        TestRunner,
    )
    from backend.core.ouroboros.governance.worktree_manager import (
        WorktreeManager,
    )

    repo_root = getattr(ctx, "repo_root", None)
    op_id = getattr(ctx, "op_id", "") or ""

    # Resolve injectable dependencies — tests bypass canonical
    # construction; production builds via canonical surfaces.
    # When repo_root is missing AND the corresponding dependency
    # is not injected, raise ValueError — the gate's stage-1
    # try/except catches and falls through to LINEAR _run_inner.
    # This is the structural way to surface misconfiguration
    # without crashing the orchestrator.
    if worktree_manager is None:
        if repo_root is None:
            raise ValueError(
                "production_tree_runner_factory: ctx.repo_root "
                "required (or inject worktree_manager explicitly)"
            )
        worktree_manager = WorktreeManager(Path(repo_root))
    if diff_applier is None:
        diff_applier = GitApplyDiffApplier()
    if test_runner is None:
        if repo_root is None:
            raise ValueError(
                "production_tree_runner_factory: ctx.repo_root "
                "required (or inject test_runner explicitly)"
            )
        test_runner = TestRunner(repo_root=Path(repo_root))
    if semantic_guardian is None:
        semantic_guardian = SemanticGuardian()

    # Test-target resolver — composes TestRunner.resolve_affected_tests
    # against the worktree files Phase B's applier captured. NEVER
    # raises per the TestTargetResolver Protocol contract.
    async def _resolver(
        *,
        op_id: str,
        candidate_files: Tuple[Tuple[str, str, str], ...],
        worktree_dir: Path,
    ) -> Tuple[Path, ...]:
        if test_runner is None:
            return ()
        try:
            changed_files = tuple(
                worktree_dir / file_tuple[0]
                for file_tuple in candidate_files
            )
            return await test_runner.resolve_affected_tests(changed_files)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — Protocol fail-open
            logger.debug(
                "[RepairTreeProduction] resolve_affected_tests raised",
                exc_info=True,
            )
            return ()

    validator = CanonicalBranchValidator(
        diff_applier=diff_applier,
        test_runner=test_runner,
        test_target_resolver=_resolver,
        semantic_guardian=semantic_guardian,
    )

    generator = ProductionBranchGenerator(
        repair_engine=repair_engine,
        ctx=ctx,
        pipeline_deadline=pipeline_deadline,
        posture=posture,
    )

    # Resolve max_layers from repair_engine's RepairBudget if not
    # explicitly provided — mirrors LINEAR FSM ceiling.
    if max_layers is None:
        try:
            rb = getattr(repair_engine, "_budget", None)
            max_layers = (
                int(rb.max_iterations) if rb is not None else 5
            )
        except Exception:  # noqa: BLE001
            max_layers = 5

    runner = RepairTreeRunner(
        budget,
        repair_budget=getattr(repair_engine, "_budget", None),
        worktree_manager=worktree_manager,
    )

    captured_max_layers: int = max_layers

    async def _invoke() -> RepairTreeResult:
        """Zero-arg invocation. NEVER raises except CancelledError."""
        return await runner.run_tree(
            op_id=op_id,
            generator=generator,
            validator=validator,
            posture=posture,
            max_layers=captured_max_layers,
        )

    return _invoke


# ===========================================================================
# tree_result_to_repair_result — pure-function adapter
# ===========================================================================


def _count_diff_lines(diff: str) -> int:
    """Count +/- lines (excluding ---/+++ headers). Composes the
    same heuristic ``repair_engine._count_diff_lines`` uses."""
    n = 0
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            n += 1
    return n


def _files_in_diff(diff: str) -> int:
    """Count unique target files in a diff. Composes Phase B's
    ``extract_diff_targets`` — single-source parsing."""
    return len(extract_diff_targets(diff or ""))


def _primary_file_path_from_diff(diff: str) -> str:
    """Extract the first target file_path from a diff for use as
    the converged candidate's ``file_path`` field. NEVER raises."""
    try:
        targets = extract_diff_targets(diff or "")
        return targets[0].path if targets else ""
    except Exception:  # noqa: BLE001
        return ""


def _branch_to_iteration_record(
    branch: RepairBranch,
    *,
    op_id: str,
) -> Any:
    """Synthesize a RepairIterationRecord from a RepairBranch.

    Maps each tree branch to a synthetic L2 iteration record so
    operator-visible telemetry (LedgerEntry, postmortem digest,
    /repair tree REPL) stays consistent between tree-mode and
    LINEAR-FSM outputs.
    """
    from backend.core.ouroboros.governance.repair_engine import (
        L2State,
        RepairIterationRecord,
    )

    outcome_str = _TREE_OUTCOME_TO_ITERATION_OUTCOME.get(
        branch.outcome.value, "no_progress",
    )
    repair_state = (
        L2State.L2_CONVERGED.value
        if branch.outcome == BranchOutcome.WON
        else L2State.L2_GENERATE_PATCH.value
    )
    stop_reason: Optional[str] = None
    if branch.prune_reason is not None:
        stop_reason = (
            f"treefinement_pruned:{branch.prune_reason.value}"
        )

    return RepairIterationRecord(
        op_id=op_id,
        # 1-based iteration index from layer_index (mirrors LINEAR FSM)
        iteration=max(1, int(branch.layer_index) + 1),
        repair_state=repair_state,
        failure_class=branch.failure_class or "",
        failure_signature_hash="",
        patch_signature_hash=str(branch.branch_id)[:64],
        diff_lines=_count_diff_lines(branch.diff or ""),
        files_changed=_files_in_diff(branch.diff or ""),
        validation_duration_s=0.0,
        outcome=outcome_str,
        stop_reason=stop_reason,
        model_id="",
        provider_name="",
    )


def tree_result_to_repair_result(
    tree_result: RepairTreeResult,
    *,
    op_id: str,
) -> Any:
    """Adapt a :class:`RepairTreeResult` into a :class:`RepairResult`.

    Pure-function deterministic mapping over the closed LayerVerdict
    × BranchOutcome taxonomies. NEVER raises — degraded inputs
    produce ``RepairResult(terminal="L2_STOPPED", stop_reason=
    "treefinement_adapter_failed:<exc>")``.

    Mapping table
    -------------
    * WON_TERMINAL (winning_branch_path non-empty) →
      ``terminal="L2_CONVERGED"``,
      ``candidate={"unified_diff": won.diff, "file_path": primary,
      "fix_hypothesis": won.fix_hypothesis}``,
      ``stop_reason=None``
    * EXHAUSTED last layer → ``terminal="L2_STOPPED"``,
      ``stop_reason="treefinement_exhausted"``,
      ``candidate=None``
    * BUDGET_TERMINAL last layer → ``terminal="L2_STOPPED"``,
      ``stop_reason="treefinement_budget_terminal"``,
      ``candidate=None``
    * Empty ``result.layers`` (gate engaged but tree returned empty
      — master flag flipped mid-run / cancellation) →
      ``terminal="L2_STOPPED"``, ``stop_reason=
      "treefinement_empty_result"``, ``candidate=None``
    * Any unexpected verdict (closed taxonomy drift) →
      ``terminal="L2_STOPPED"``, ``stop_reason=
      "treefinement_unexpected_verdict:<value>"``,
      ``candidate=None``

    All non-WON cases preserve the synthesized
    :class:`RepairIterationRecord` tuple so operator-visible
    telemetry (LedgerEntry, postmortem digest, ``/repair tree``
    REPL) stays consistent between tree-mode and LINEAR-FSM outputs.
    """
    from backend.core.ouroboros.governance.repair_engine import (
        RepairResult,
    )

    try:
        # Synthesize iteration records from every archived branch.
        iterations: List[Any] = []
        for layer in tree_result.layers:
            for branch in layer.branches:
                iterations.append(
                    _branch_to_iteration_record(branch, op_id=op_id),
                )
        iterations_tuple = tuple(iterations)

        # Build summary projection — operator-visible counts.
        summary = _build_tree_summary(tree_result)

        # Empty-layers degraded path — gate engaged but no tree work.
        if not tree_result.layers:
            return RepairResult(
                terminal="L2_STOPPED",
                candidate=None,
                stop_reason="treefinement_empty_result",
                summary=summary,
                iterations=iterations_tuple,
            )

        # WON terminal path — last layer's verdict is WON_TERMINAL.
        last_layer = tree_result.layers[-1]
        if last_layer.verdict == LayerVerdict.WON_TERMINAL:
            won_branch = _find_won_branch(tree_result)
            if won_branch is None:
                # Defensive: verdict says WON but no branch matches.
                # Surface as a structured failure rather than crash.
                return RepairResult(
                    terminal="L2_STOPPED",
                    candidate=None,
                    stop_reason=(
                        "treefinement_adapter_failed:"
                        "won_terminal_without_branch"
                    ),
                    summary=summary,
                    iterations=iterations_tuple,
                )
            return RepairResult(
                terminal="L2_CONVERGED",
                candidate={
                    "unified_diff": won_branch.diff,
                    "file_path": _primary_file_path_from_diff(
                        won_branch.diff,
                    ),
                    "fix_hypothesis": won_branch.fix_hypothesis,
                },
                stop_reason=None,
                summary=summary,
                iterations=iterations_tuple,
            )

        # Non-WON terminal paths — map verdict to stop_reason.
        stop_reason = _TREE_VERDICT_TO_STOP_REASON.get(
            last_layer.verdict.value,
            f"treefinement_unexpected_verdict:{last_layer.verdict.value}",
        )
        return RepairResult(
            terminal="L2_STOPPED",
            candidate=None,
            stop_reason=stop_reason,
            summary=summary,
            iterations=iterations_tuple,
        )
    except Exception as exc:  # noqa: BLE001
        # Defensive — adapter is pure but tree_result shape may drift.
        logger.warning(
            "[RepairTreeProduction] adapter raised: %s",
            exc, exc_info=True,
        )
        return RepairResult(
            terminal="L2_STOPPED",
            candidate=None,
            stop_reason=(
                f"treefinement_adapter_failed:{type(exc).__name__}"
            ),
            summary={"adapter_error": str(exc)},
            iterations=(),
        )


def _find_won_branch(
    tree_result: RepairTreeResult,
) -> Optional[RepairBranch]:
    """Locate the WON branch in a tree result. Returns None if
    no branch has outcome=WON (defensive — caller treats as
    structured failure)."""
    for layer in tree_result.layers:
        for branch in layer.branches:
            if branch.outcome == BranchOutcome.WON:
                return branch
    return None


def _build_tree_summary(
    tree_result: RepairTreeResult,
) -> Dict[str, Any]:
    """Operator-visible counts for the summary field of RepairResult."""
    branch_count = sum(
        len(layer.branches) for layer in tree_result.layers
    )
    won_count = sum(
        1
        for layer in tree_result.layers
        for branch in layer.branches
        if branch.outcome == BranchOutcome.WON
    )
    promoted_count = sum(
        1
        for layer in tree_result.layers
        for branch in layer.branches
        if branch.outcome == BranchOutcome.PROMOTED
    )
    pruned_count = sum(
        1
        for layer in tree_result.layers
        for branch in layer.branches
        if branch.outcome in (
            BranchOutcome.PRUNED_VALIDATOR,
            BranchOutcome.PRUNED_DUPLICATE,
            BranchOutcome.PRUNED_BUDGET,
        )
    )
    total_cost = sum(
        float(branch.cost_usd or 0.0)
        for layer in tree_result.layers
        for branch in layer.branches
    )
    return {
        "treefinement": True,
        "layer_count": len(tree_result.layers),
        "branch_count": branch_count,
        "won_count": won_count,
        "promoted_count": promoted_count,
        "pruned_count": pruned_count,
        "total_cost_usd": total_cost,
        "winning_path_depth": len(tree_result.winning_branch_path),
    }


# ===========================================================================
# Phase E — lazy boot registration
# ===========================================================================
#
# Production-factory registration happens on FIRST tree-mode op
# (not at module import time, not at process boot). This means:
#
#   * Modules that never run tree mode never trigger the wiring
#     (zero unnecessary imports for non-tree-mode callers).
#   * The first ``_maybe_run_treefinement`` call with master-flag ON
#     + strategy != LINEAR registers the factory + proceeds.
#   * Subsequent calls see the registered factory + skip
#     re-registration.
#
# The registration is idempotent + respects operator overrides —
# if an operator (or test) registers a custom factory via
# ``register_production_tree_runner_factory(custom)``, the boot
# registration does NOT overwrite it. This is the load-bearing
# "respect operator intent" invariant: the boot wiring is the
# *default*, not the *authority*.


def register_production_factory_at_boot() -> bool:
    """Register the canonical production factory if no factory is
    currently registered. Idempotent. NEVER raises.

    Returns
    -------
    bool
        True if registration was performed (factory was None and is
        now set to ``production_tree_runner_factory``). False if no
        registration was needed (operator-registered factory already
        present, OR registration call itself failed).

    Contract
    --------
    * **Idempotent**: calling this multiple times is safe — only the
      first call (when registry is None) registers anything.
    * **Respects operator overrides**: if an operator or test has
      already registered a factory, this function leaves it alone.
      Operator intent is authoritative.
    * **Fail-open**: any exception during registration is logged at
      DEBUG and returns False (rather than raising). The gate's
      ``_maybe_run_treefinement`` treats False as "factory still
      unavailable" and falls through to LINEAR.

    Composition
    -----------
    Calls the canonical
    :func:`~backend.core.ouroboros.governance.repair_tree.
    register_production_tree_runner_factory` — no parallel registry
    state. The argument is ``production_tree_runner_factory``
    (Phase D's factory function defined above in this module).
    """
    try:
        from backend.core.ouroboros.governance.repair_tree import (
            get_production_tree_runner_factory,
            register_production_tree_runner_factory,
        )
    except ImportError:
        return False

    try:
        existing = get_production_tree_runner_factory()
    except Exception:  # noqa: BLE001
        return False

    if existing is not None:
        # Operator-registered factory present — respect their choice.
        return False

    try:
        register_production_tree_runner_factory(
            production_tree_runner_factory,
        )
        logger.info(
            "[RepairTreeProduction] production factory registered "
            "lazily on first tree-mode op",
        )
        return True
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[RepairTreeProduction] lazy factory registration "
            "raised; tree mode will fall through to LINEAR",
            exc_info=True,
        )
        return False


__all__ = [
    "REPAIR_TREE_PRODUCTION_SCHEMA_VERSION",
    "GIT_APPLY_TIMEOUT_S_ENV_VAR",
    "PER_CALL_COST_USD_ENV_VAR",
    "extract_diff_targets",
    "GitApplyDiffApplier",
    "ProductionBranchGenerator",
    "production_tree_runner_factory",
    "tree_result_to_repair_result",
    "register_production_factory_at_boot",
    "register_flags",
]
