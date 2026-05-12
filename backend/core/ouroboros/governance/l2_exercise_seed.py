"""L2 Exercise Seed — Phase 1.5 corpus-based L2-triggering injection.

Closes the v3.5 soak-infrastructure gap surfaced by the first
Treefinement validation soak: synthetic seed intents + sensor-driven
background ops all pass VALIDATE on first try, so L2 never fires +
the v3.4 production wiring's strategy gate never engages under
soak load.

The structural fix here is a deliberately-failing fixture corpus
(buggy code + genuinely-failing pytest assertions, stored in test
fixtures) that the soak boot hook lifts into a fresh isolated
worktree and emits as a canonical IntentEnvelope.  The pipeline
runs naturally end-to-end: provider sees the failing tests via
the canonical TestRunner, generates a fix, VALIDATE runs the
tests against the fix, the bug is genuinely hard so VALIDATE
fails, L2 fires, ``RepairEngine._maybe_run_treefinement``
engages, tree mode runs (per v3.4 wiring), ``repair_tree.jsonl``
receives a row.

Composition discipline
----------------------

Phase 1.5 substrate composes existing canonical surfaces ONLY:

  * :func:`backend.core.ouroboros.governance.intake.intent_envelope.
    make_envelope` — single-source envelope builder (no parallel
    construction); same primitive Phase 9 synthetic-workload uses
  * ``source="cadence_synthetic"`` — the canonical synthetic-
    workload source token added 2026-05-05 in
    ``intake/intent_envelope.py``'s ``_VALID_SOURCES`` whitelist
  * :class:`backend.core.ouroboros.governance.worktree_manager.
    WorktreeManager` — branch isolation (same primitive Treefinement
    uses; reap-orphans on boot covers SIGKILL recovery)
  * :meth:`backend.core.ouroboros.governance.intake.unified_intake_
    router.UnifiedIntakeRouter.ingest` — canonical envelope ingestion
  * ``flag_registry_seed._discover_module_provided_flags`` walker —
    §33.3 naming-cage auto-discovery for the 3 env knobs

Pipeline runs naturally (no bypass): the envelope flows through
CLASSIFY → ROUTE → CONTEXT_EXPANSION → PLAN → GENERATE → VALIDATE
exactly like every other op.  Failures happen because the bug is
genuinely hard, not because we mocked VALIDATE.

Authority asymmetry (§1 Boundary)
---------------------------------

This module orchestrates fixture injection; it makes NO policy
decisions.  Forbidden imports (AST-pinned):

  * ``orchestrator`` / ``iron_gate`` / ``change_engine`` /
    ``candidate_generator`` / ``policy_engine`` / ``risk_tier`` /
    ``repair_engine``

The substrate is descriptive — pipeline / validator / Iron Gate /
SemanticGuardian / repair_engine all stay byte-identical.

§7 fail-closed contract
-----------------------

Every public surface NEVER raises into the caller:

  * ``load_exercise_problem`` — returns ``None`` on any failure
    (missing dir / missing manifest / malformed JSON / unknown
    kind / missing files)
  * ``setup_exercise_worktree`` — returns ``None`` on
    ``WorktreeManager.create`` failure
  * ``build_exercise_intent`` — pure function over a valid
    :class:`ExerciseProblem`; structurally cannot raise
  * ``maybe_inject_exercise_at_boot`` — orchestrates all 3 +
    catches any unexpected exception; returns
    :class:`ExerciseInjectionVerdict` enum, never raises
  * ``asyncio.CancelledError`` is the SOLE exception that
    propagates (orchestrator POSTMORTEM contract — same as every
    other ouroboros substrate)

§33.1 graduation contract
-------------------------

Master flag ``JARVIS_L2_EXERCISE_CORPUS_ENABLED`` defaults FALSE.
Production behavior is byte-identical when unset; the module's
boot hook short-circuits at the master-flag check before any
fixture I/O / worktree allocation / envelope construction.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)

logger = logging.getLogger("Ouroboros.L2ExerciseSeed")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


L2_EXERCISE_SEED_SCHEMA_VERSION: str = "l2_exercise_seed.v1"


# Master flag (§33.1 default-FALSE).  Production behavior is
# byte-identical when unset; the boot hook short-circuits at the
# enabled() check before any fixture I/O.
MASTER_FLAG_ENV_VAR: str = "JARVIS_L2_EXERCISE_CORPUS_ENABLED"

# Fixture-corpus directory.  Default points at the in-repo fixture
# location.  Master flag default-FALSE means this default is only
# consulted when an operator EXPLICITLY opts in; deployments outside
# the repo would override via the env var (or simply not enable the
# master flag).  No production code path reads this when the master
# flag is off.
CORPUS_PATH_ENV_VAR: str = "JARVIS_L2_EXERCISE_CORPUS_PATH"

# Number of problems to inject per boot.  Clamped [1, 5].  Default 1.
# A single L2-firing op per soak is enough to validate the acceptance
# predicate (.jarvis/ouroboros/repair_tree.jsonl ≥ 1 row).  More than
# one would multiply cost without adding signal at this stage.
CORPUS_COUNT_ENV_VAR: str = "JARVIS_L2_EXERCISE_CORPUS_COUNT"


_DEFAULT_CORPUS_PATH: str = (
    "tests/governance/fixtures/l2_exercise_corpus"
)
_DEFAULT_COUNT: int = 1
_MIN_COUNT: int = 1
_MAX_COUNT: int = 5


# Canonical synthetic-workload source token.  MUST match the entry in
# ``intake/intent_envelope.py``'s ``_VALID_SOURCES`` whitelist (added
# 2026-05-05 for Phase 9 synthetic injection).  AST-pinned: the
# composition test asserts ``build_exercise_intent`` uses THIS
# constant, not a hardcoded literal anywhere in production code.
CADENCE_SYNTHETIC_SOURCE: str = "cadence_synthetic"


# Confidence + urgency for the synthetic envelope.  ``urgency="low"``
# routes via BACKGROUND ProviderRoute (DW-only cascade; NEVER burns
# Claude budget on cadence-injected workload).  Same discipline as
# Phase 9 synthetic-workload module.
_EXERCISE_CONFIDENCE: float = 0.9
_EXERCISE_URGENCY: str = "low"

# Evidence-category marker for operator-facing observability filters
# (e.g., /backlog --filter category=l2_exercise_corpus).  Distinct
# from "cadence_synthetic" (which marks Phase 9 generic synthetics)
# so a future REPL surface can grep for L2-exercise envelopes
# specifically.
_EVIDENCE_CATEGORY: str = "l2_exercise_corpus"


# ===========================================================================
# Closed taxonomies (AST bytes-pinned)
# ===========================================================================


class ExerciseProblemKind(str, enum.Enum):
    """Five canonical bug categories the fixture corpus may contain.

    Closed taxonomy.  Adding a new kind requires a Phase tag + soak
    validation; the AST pin asserts the value-set bytes are exactly
    these 5 entries.
    """

    OFF_BY_ONE = "off_by_one"
    LOGIC_INVERSION = "logic_inversion"
    MISSING_NULL_CHECK = "missing_null_check"
    TYPE_MISMATCH = "type_mismatch"
    DICT_KEYERROR = "dict_keyerror"


class ExerciseInjectionVerdict(str, enum.Enum):
    """Five canonical injection-boot outcomes.

    Closed taxonomy.  Returned from :func:`maybe_inject_exercise_at_boot`
    so operator-visible telemetry can distinguish "nothing to do" from
    "tried but failed" without parsing log strings.
    """

    INJECTED = "injected"
    SKIPPED_DISABLED = "skipped_disabled"
    SKIPPED_NO_CORPUS = "skipped_no_corpus"
    FAILED_LOAD = "failed_load"
    FAILED_INJECT = "failed_inject"


# ===========================================================================
# Frozen ExerciseProblem dataclass (§33.5 symmetric to_dict/from_dict)
# ===========================================================================


@dataclass(frozen=True)
class ExerciseProblem:
    """One corpus problem loaded from a fixture directory.

    Fields are populated by :func:`load_exercise_problem` from:

      * ``manifest.json`` — problem metadata (id, kind, file names)
      * ``<target_file_name>`` — the buggy code (e.g. ``before.py``)
      * ``<test_file_name>`` — pytest assertions that fail against
        the buggy code (e.g. ``test_before.py``)

    Frozen post-construction; immutable across the boot lifecycle.
    """

    problem_id: str
    kind: ExerciseProblemKind
    target_file_name: str
    test_file_name: str
    before_content: str
    test_content: str
    manifest_metadata: Dict[str, Any]
    schema_version: str = L2_EXERCISE_SEED_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "problem_id": self.problem_id,
            "kind": self.kind.value,
            "target_file_name": self.target_file_name,
            "test_file_name": self.test_file_name,
            "before_content": self.before_content,
            "test_content": self.test_content,
            "manifest_metadata": dict(self.manifest_metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExerciseProblem":
        return cls(
            schema_version=payload.get(
                "schema_version", L2_EXERCISE_SEED_SCHEMA_VERSION,
            ),
            problem_id=str(payload["problem_id"]),
            kind=ExerciseProblemKind(payload["kind"]),
            target_file_name=str(payload["target_file_name"]),
            test_file_name=str(payload["test_file_name"]),
            before_content=str(payload["before_content"]),
            test_content=str(payload["test_content"]),
            manifest_metadata=dict(payload.get("manifest_metadata", {})),
        )


# ===========================================================================
# Env loaders (NEVER raise; clamped; garbage-tolerant)
# ===========================================================================


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "false", "0", "no", "off")


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = 2**31 - 1,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except (ValueError, TypeError):
        logger.warning(
            "[L2ExerciseSeed] invalid %s=%r — using default %d",
            name, raw, default,
        )
        return default


def corpus_enabled() -> bool:
    """Master-flag accessor.  Default FALSE per §33.1.  NEVER raises."""
    return _env_bool(MASTER_FLAG_ENV_VAR, default=False)


def corpus_path() -> Path:
    """Fixture-corpus directory accessor.  Default points at
    in-repo fixtures; operator overrides via env var when running
    from a different cwd.  NEVER raises."""
    raw = os.environ.get(CORPUS_PATH_ENV_VAR, "").strip()
    return Path(raw) if raw else Path(_DEFAULT_CORPUS_PATH)


def corpus_count() -> int:
    """Number of problems to inject per boot.  Clamped [1, 5].
    NEVER raises."""
    return _env_int(
        CORPUS_COUNT_ENV_VAR,
        _DEFAULT_COUNT,
        minimum=_MIN_COUNT,
        maximum=_MAX_COUNT,
    )


# ===========================================================================
# Corpus loader (pure function over fixture directory)
# ===========================================================================


def list_corpus_problems(corpus_dir: Path) -> List[Path]:
    """Return sorted list of subdirectories that look like problems.

    A problem directory is any direct child of ``corpus_dir`` that is
    itself a directory AND whose name does not start with ``_``
    (convention: ``_private``, ``_archive`` etc. are skipped).

    Sorted output makes injection order deterministic per boot.
    NEVER raises.
    """
    try:
        if not corpus_dir.is_dir():
            return []
        return sorted(
            p for p in corpus_dir.iterdir()
            if p.is_dir() and not p.name.startswith("_")
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[L2ExerciseSeed] list_corpus_problems failed for %r",
            corpus_dir, exc_info=True,
        )
        return []


def load_exercise_problem(problem_dir: Path) -> Optional[ExerciseProblem]:
    """Load one problem from a fixture directory.

    Expected directory shape::

        problem_dir/
        ├── manifest.json        # required; contains id, kind, file names
        ├── <target_file_name>   # required (default: before.py)
        └── <test_file_name>     # required (default: test_before.py)

    ``manifest.json`` must include ``kind`` (matching an
    :class:`ExerciseProblemKind` value).  Optional fields:
    ``id`` (defaults to directory basename), ``target_file_name``
    (defaults ``before.py``), ``test_file_name`` (defaults
    ``test_before.py``), plus arbitrary metadata preserved verbatim.

    Returns ``None`` on ANY failure (missing dir / missing manifest /
    malformed JSON / unknown kind / missing files / I/O error).
    NEVER raises.
    """
    try:
        if not problem_dir.is_dir():
            return None
        manifest_path = problem_dir / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.debug(
                "[L2ExerciseSeed] malformed manifest at %r",
                manifest_path, exc_info=True,
            )
            return None
        if not isinstance(manifest, dict):
            return None
        kind_raw = manifest.get("kind")
        if not isinstance(kind_raw, str):
            return None
        try:
            kind = ExerciseProblemKind(kind_raw)
        except ValueError:
            logger.debug(
                "[L2ExerciseSeed] unknown kind %r in %r",
                kind_raw, manifest_path,
            )
            return None
        target_file_name = str(manifest.get("target_file_name", "before.py"))
        test_file_name = str(manifest.get("test_file_name", "test_before.py"))
        before_path = problem_dir / target_file_name
        test_path = problem_dir / test_file_name
        if not before_path.is_file() or not test_path.is_file():
            return None
        try:
            before_content = before_path.read_text(encoding="utf-8")
            test_content = test_path.read_text(encoding="utf-8")
        except OSError:
            logger.debug(
                "[L2ExerciseSeed] could not read fixture files in %r",
                problem_dir, exc_info=True,
            )
            return None
        problem_id = str(manifest.get("id") or problem_dir.name)
        return ExerciseProblem(
            problem_id=problem_id,
            kind=kind,
            target_file_name=target_file_name,
            test_file_name=test_file_name,
            before_content=before_content,
            test_content=test_content,
            manifest_metadata=manifest,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[L2ExerciseSeed] load_exercise_problem raised", exc_info=True,
        )
        return None


# ===========================================================================
# Worktree setup (composes WorktreeManager — single source of isolation)
# ===========================================================================


async def setup_exercise_worktree(
    problem: ExerciseProblem,
    worktree_manager: Any,
) -> Optional[Path]:
    """Create an isolated worktree for the exercise problem + write
    the buggy file + the failing test file into it.

    Composes :class:`worktree_manager.WorktreeManager` — the canonical
    isolation primitive (same one Treefinement uses).  No parallel
    isolation logic; if ``WorktreeManager.create`` fails, this
    returns ``None`` and the boot hook records ``FAILED_INJECT``.

    Branch naming follows the canonical ``ouroboros/l2-exercise/<id>``
    pattern (mirrors Treefinement's ``ouroboros/repair-tree/<...>``
    convention so reap-orphans sweep recognises both flavours).

    Returns the worktree :class:`Path` on success, ``None`` on any
    failure.  ``asyncio.CancelledError`` propagates; every other
    exception is caught + logged + flattened to ``None``.
    """
    try:
        branch_name = f"ouroboros/l2-exercise/{problem.problem_id}"
        wt_path_raw = await worktree_manager.create(branch_name)
        wt_path = Path(wt_path_raw)
        target_path = wt_path / problem.target_file_name
        test_path = wt_path / problem.test_file_name
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(problem.before_content, encoding="utf-8")
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text(problem.test_content, encoding="utf-8")
        except OSError:
            logger.warning(
                "[L2ExerciseSeed] could not write fixture files into "
                "worktree %r", wt_path, exc_info=True,
            )
            return None
        return wt_path
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — fail-open contract
        logger.warning(
            "[L2ExerciseSeed] setup_exercise_worktree failed for "
            "problem=%r", problem.problem_id, exc_info=True,
        )
        return None


# ===========================================================================
# Intent builder (composes canonical make_envelope — no parallel construction)
# ===========================================================================


def build_exercise_intent(
    problem: ExerciseProblem,
    worktree_path: Path,
    *,
    repo_root: str = "",
) -> IntentEnvelope:
    """Construct a canonical :class:`IntentEnvelope` for the exercise
    problem.

    Composes :func:`intake.intent_envelope.make_envelope` — the single
    canonical envelope builder (same primitive Phase 9 synthetic-
    workload + every real sensor use).  No parallel ``IntentEnvelope(...)``
    construction; AST-pinned via the composition test.

    Envelope shape:

      * ``source = CADENCE_SYNTHETIC_SOURCE`` — canonical synthetic-
        workload token (matches the ``_VALID_SOURCES`` whitelist
        entry added 2026-05-05).  Honest source-token convention
        per Phase 9 graduation contract.
      * ``urgency = "low"`` — routes via BACKGROUND ProviderRoute
        (DW-only cascade; never burns Claude budget on cadence
        injection).
      * ``target_files`` — the buggy file (relative path inside
        the worktree).
      * ``evidence["category"] = "l2_exercise_corpus"`` — operator-
        facing filter marker distinct from generic ``cadence_synthetic``.
      * ``evidence["worktree_path"]`` — the isolated worktree the
        downstream pipeline operates on.
      * ``evidence["problem_id"]`` + ``evidence["kind"]`` — telemetry
        for /backlog REPL / SSE feed.

    Pure function; structurally cannot raise (composes only the
    canonical builder, which raises only on schema validation —
    which we don't trigger because all fields are pre-validated).
    """
    description = (
        f"L2 exercise problem {problem.problem_id} "
        f"(kind={problem.kind.value}): the file "
        f"{problem.target_file_name} contains buggy code that fails "
        f"the pytest assertions in {problem.test_file_name}. Diagnose "
        f"the bug + produce a unified-diff patch so all tests pass."
    )
    evidence: Dict[str, Any] = {
        "category": _EVIDENCE_CATEGORY,
        "problem_id": problem.problem_id,
        "kind": problem.kind.value,
        "worktree_path": str(worktree_path),
        "test_file_name": problem.test_file_name,
        "manifest_metadata": dict(problem.manifest_metadata),
        "schema_version": L2_EXERCISE_SEED_SCHEMA_VERSION,
    }
    return make_envelope(
        source=CADENCE_SYNTHETIC_SOURCE,
        description=description,
        target_files=(problem.target_file_name,),
        repo=repo_root,
        confidence=_EXERCISE_CONFIDENCE,
        urgency=_EXERCISE_URGENCY,
        evidence=evidence,
        requires_human_ack=False,
    )


# ===========================================================================
# Boot orchestrator (composes load + setup + build + router.ingest)
# ===========================================================================


async def maybe_inject_exercise_at_boot(
    intake_router: Any,
    *,
    worktree_manager: Any,
    repo_root: str = "",
) -> ExerciseInjectionVerdict:
    """Battle-test harness boot hook.

    Orchestrates the four-stage injection pipeline:

      1. Master-flag check  → ``SKIPPED_DISABLED`` if ``False``
      2. Corpus directory enumeration  → ``SKIPPED_NO_CORPUS`` if empty
      3. Per-problem load + worktree setup + envelope construction
      4. Canonical ``UnifiedIntakeRouter.ingest`` submission

    Returns one of five :class:`ExerciseInjectionVerdict` outcomes.
    NEVER raises into the caller; ``asyncio.CancelledError`` is the
    sole exception that propagates (orchestrator POSTMORTEM contract).

    The boot hook is called once per battle-test session after
    ``intake_router`` + ``worktree_manager`` have both been
    constructed.  The harness wires this via a lazy import so
    non-exercise-mode boots pay zero cost.
    """
    if not corpus_enabled():
        return ExerciseInjectionVerdict.SKIPPED_DISABLED
    try:
        path = corpus_path()
        problem_dirs = list_corpus_problems(path)
        if not problem_dirs:
            logger.info(
                "[L2ExerciseSeed] master flag ON but no problems "
                "found at %r — nothing to inject", path,
            )
            return ExerciseInjectionVerdict.SKIPPED_NO_CORPUS
        count = min(corpus_count(), len(problem_dirs))
        loaded_count = 0
        injected_count = 0
        for problem_dir in problem_dirs[:count]:
            problem = load_exercise_problem(problem_dir)
            if problem is None:
                logger.info(
                    "[L2ExerciseSeed] could not load problem from %r — "
                    "skipping", problem_dir,
                )
                continue
            loaded_count += 1
            worktree = await setup_exercise_worktree(
                problem, worktree_manager,
            )
            if worktree is None:
                logger.warning(
                    "[L2ExerciseSeed] worktree setup failed for "
                    "problem=%r — skipping", problem.problem_id,
                )
                continue
            envelope = build_exercise_intent(
                problem, worktree, repo_root=repo_root,
            )
            try:
                ingest_result = await intake_router.ingest(envelope)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — fail-open
                logger.warning(
                    "[L2ExerciseSeed] intake_router.ingest failed for "
                    "problem=%r", problem.problem_id, exc_info=True,
                )
                continue
            logger.info(
                "[L2ExerciseSeed] injected problem=%r worktree=%r "
                "ingest=%r", problem.problem_id, worktree, ingest_result,
            )
            injected_count += 1
        if loaded_count == 0:
            return ExerciseInjectionVerdict.FAILED_LOAD
        if injected_count == 0:
            return ExerciseInjectionVerdict.FAILED_INJECT
        return ExerciseInjectionVerdict.INJECTED
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — fail-open contract
        logger.warning(
            "[L2ExerciseSeed] maybe_inject_exercise_at_boot raised",
            exc_info=True,
        )
        return ExerciseInjectionVerdict.FAILED_LOAD


# ===========================================================================
# FlagRegistry self-registration (auto-discovered by §33.3 walker)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration.

    Picked up zero-edit by
    ``flag_registry_seed._discover_module_provided_flags`` walker on
    next boot (the walker scans direct submodules of
    ``backend.core.ouroboros.governance``).  NEVER raises — fail-open
    per §33.1.

    Returns the count of FlagSpecs successfully registered.
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
            name=MASTER_FLAG_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master kill switch for L2 exercise corpus injection "
                "(Phase 1.5 substrate; v3.6).  When TRUE, the battle-"
                "test boot hook lifts the first N problems from "
                "JARVIS_L2_EXERCISE_CORPUS_PATH into isolated worktrees "
                "and emits them as canonical IntentEnvelope(source="
                "'cadence_synthetic') ops.  Default FALSE per §33.1 "
                "graduation contract — flip on for Phase 9 graduation "
                "soaks to deliberately trigger L2 → tree mode → "
                "repair_tree.jsonl row."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/l2_exercise_seed.py"
            ),
            example="true",
            since="v3.6 Phase 1.5.A (2026-05-12)",
        ),
        FlagSpec(
            name=CORPUS_PATH_ENV_VAR,
            type=FlagType.STR,
            default=_DEFAULT_CORPUS_PATH,
            description=(
                "Fixture-corpus directory containing one subdirectory "
                "per problem (each with manifest.json + before.py + "
                "test_before.py).  Default points at the in-repo "
                "fixture location; operator overrides when running "
                "from a different cwd.  Only consulted when "
                f"{MASTER_FLAG_ENV_VAR}=true."
            ),
            category=Category.INTEGRATION,
            source_file=(
                "backend/core/ouroboros/governance/l2_exercise_seed.py"
            ),
            example=_DEFAULT_CORPUS_PATH,
            since="v3.6 Phase 1.5.A (2026-05-12)",
        ),
        FlagSpec(
            name=CORPUS_COUNT_ENV_VAR,
            type=FlagType.INT,
            default=_DEFAULT_COUNT,
            description=(
                "Number of corpus problems to inject per battle-test "
                "boot.  Clamped [1, 5]; default 1 — a single L2-firing "
                "op per soak is enough for the v3.6 acceptance "
                "predicate (.jarvis/ouroboros/repair_tree.jsonl ≥ 1 "
                "row).  Increase only for multi-problem A/B soaks."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/l2_exercise_seed.py"
            ),
            example="1",
            since="v3.6 Phase 1.5.A (2026-05-12)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — boot-time fail-open
            logger.debug(
                "[L2ExerciseSeed] flag registration failed for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count


__all__ = [
    "L2_EXERCISE_SEED_SCHEMA_VERSION",
    "MASTER_FLAG_ENV_VAR",
    "CORPUS_PATH_ENV_VAR",
    "CORPUS_COUNT_ENV_VAR",
    "CADENCE_SYNTHETIC_SOURCE",
    "ExerciseProblemKind",
    "ExerciseInjectionVerdict",
    "ExerciseProblem",
    "corpus_enabled",
    "corpus_path",
    "corpus_count",
    "list_corpus_problems",
    "load_exercise_problem",
    "setup_exercise_worktree",
    "build_exercise_intent",
    "maybe_inject_exercise_at_boot",
    "register_flags",
]
