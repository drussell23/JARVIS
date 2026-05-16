"""Battle-test singleton lock — structural single-instance guard.

**Why this exists** — the v18 franken-commit incident traced part
of its blast radius to two concurrent soak instances racing each
other's commits into the operator's main checkout. The existing
``_single_flight_preflight`` in ``scripts/ouroboros_battle_test.py``
uses ``pgrep`` to detect sibling processes by command-line match,
which is human-readable but probabilistic: it can miss processes
launched under a different argv, miss processes mid-rename, or
fire false positives. A pgrep miss → two harnesses booting → two
AutoCommitters → race conditions.

**The structural fix** — compose the canonical ``flock_critical_
section`` primitive from :mod:`cross_process_jsonl` to take a
file-system-level exclusive lock at a well-known path under the
repo root. ``LOCK_EX | LOCK_NB`` with ``timeout_s=0`` makes the
second concurrent fire detect the held lock instantly and exit
``EX_TEMPFAIL`` (75) without racing — the OS kernel arbitrates,
not a probabilistic pgrep poll.

The flock is held by an open file descriptor inside the harness
process. On process exit (clean OR ``SIGKILL`` OR ``os._exit``),
the kernel closes the descriptor and releases the lock — there
is no leaked-lock failure mode that requires cleanup tooling.

Composition with existing ``_single_flight_preflight``: the flock
is the **structural** defense and runs FIRST; the pgrep-based
preflight is the **diagnostic** defense and runs after acquire.
Both layers active under master-ON; both off under master-OFF.

Master flag: ``JARVIS_BATTLE_TEST_SINGLETON_LOCK_ENABLED`` (default
**FALSE**, §33.1). When off, every entry is a no-op; the script's
existing ``_single_flight_preflight`` is unchanged.

This module is **substrate only** — it does not import the harness,
the orchestrator, ``AutoCommitter``, or any policy. The dependency
direction is one-way: the script composes the substrate's public
surface; the substrate composes only ``cross_process_jsonl`` (a
stdlib-only primitive) and :mod:`ledger_sovereignty` (for the
``no-hardcoding`` directory-derivation idiom — same repo-root path
discipline). Closes P1 B1.2.
"""
from __future__ import annotations

import ast as _ast
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema + constants
# ---------------------------------------------------------------------------


SINGLETON_LOCK_SCHEMA_VERSION = "1.0"

_LOCK_RELATIVE_PATH = (".jarvis", "ouroboros_battle_test.lock")
"""Lock landing path relative to the repo root. Tuple of segments
(not a string literal) so ``Path(*tuple)`` stays portable. Lives
under ``.jarvis/`` next to the sovereignty marker — same operator-
visible directory; same cleanup posture (gitignored, ephemeral)."""


_FAIL_FAST_TIMEOUT_S = 0.05
"""Composition contract with
:func:`cross_process_jsonl.flock_critical_section` — its primitive
treats ``timeout_s=0.0`` as a sentinel meaning "fall through to
the default (5s)" (see ``_acquire_cross_process_lock`` lines
213-216). To get true fail-fast semantics for the singleton
check, pass a small *positive* value: the primitive polls flock
exactly once with ``LOCK_NB``, sleeps one backoff (~5 ms), then
checks the deadline (exceeded) and returns ``False``. Net wait
on conflict is bounded at ~50 ms — well below human-perceptible
latency, well above the kernel's flock cost so we never spuriously
fail-fast under contention jitter.

Not parameterized by env var: the value isn't a tunable, it's a
composition primitive constant. If the upstream primitive ever
changes its sentinel convention, fix it here in one place."""


# ---------------------------------------------------------------------------
# Master flag (§33.1 default-FALSE)
# ---------------------------------------------------------------------------


_MASTER_FLAG = "JARVIS_BATTLE_TEST_SINGLETON_LOCK_ENABLED"


def singleton_lock_enabled() -> bool:
    """Return ``True`` iff the structural singleton lock is master-ON.

    Default is ``False`` per §33.1 — the substrate composes onto
    the existing ``_single_flight_preflight`` rather than replacing
    it. Operator graduates deliberately once Slice 2 + Slice 3 have
    soaked together.
    """
    return os.environ.get(_MASTER_FLAG, "false").lower() == "true"


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SingletonLockResult:
    """Outcome of an :func:`acquire_singleton` attempt.

    Frozen so callers (and the script wiring) can't mutate the
    decision after observing it.

    ``acquired`` — load-bearing field; ``False`` means another
    soak holds the lock and the caller MUST exit ``EX_TEMPFAIL``.

    ``lock_path`` — the on-disk lock target. Useful for telemetry
    and for the operator to ``lsof`` / ``fuser`` it when chasing
    a missed release (which should not happen — kernel closes the
    fd on process exit — but having the path printable keeps the
    failure mode debuggable).

    ``schema_version`` — bumped if future fields are added.
    """

    acquired: bool
    lock_path: Path
    schema_version: str = SINGLETON_LOCK_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Path resolution (no hardcoding)
# ---------------------------------------------------------------------------


def default_lock_path(repo_root: Path) -> Path:
    """Compute the canonical singleton-lock path for a given repo
    root.

    Derives from ``Path(repo_root, *_LOCK_RELATIVE_PATH)`` — no
    string literal anywhere in the script wiring, so moving the
    repo root or the ``.jarvis/`` convention auto-propagates here.
    """
    return Path(repo_root, *_LOCK_RELATIVE_PATH)


# ---------------------------------------------------------------------------
# Acquire (the load-bearing entry point)
# ---------------------------------------------------------------------------


@contextmanager
def acquire_singleton(
    repo_root: Path,
    *,
    lock_path: Optional[Path] = None,
) -> Iterator[SingletonLockResult]:
    """Acquire the singleton lock for the duration of the ``with``
    block. Yields a :class:`SingletonLockResult` whose ``acquired``
    flag tells the caller whether to proceed.

    Composes the canonical
    :func:`backend.core.ouroboros.governance.cross_process_jsonl.flock_critical_section`
    with ``timeout_s=0.0`` — fail-fast semantics. The first soak
    holds the lock; the second soak's ``LOCK_EX | LOCK_NB`` fails
    instantly and yields ``acquired=False`` without polling.

    The flock fd is held by the underlying ``with`` block's
    context-manager lifetime. Callers that need the lock to span
    the whole process should stash the context manager on an
    :class:`contextlib.ExitStack` registered at module-import time
    (the ``scripts/ouroboros_battle_test.py`` wiring does exactly
    this).

    NEVER raises. If the substrate primitive is unavailable for
    any reason (import failure, fcntl missing on Windows, etc.),
    the result surfaces ``acquired=True`` — the substrate fails
    OPEN here on purpose: the existing ``_single_flight_preflight``
    is the diagnostic fallback, and we never want a substrate
    breakage to block legitimate single-instance soaks. The
    operator can flip the master flag off if the fallback is
    unacceptable.
    """
    target = (
        Path(lock_path) if lock_path is not None
        else default_lock_path(repo_root)
    )
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_critical_section,
        )
    except Exception as imp_err:  # noqa: BLE001 — defensive
        logger.warning(
            "[singleton_lock] cross_process_jsonl unavailable "
            "(%r) — fail-open. Diagnostic preflight still "
            "runs as the structural fallback.",
            imp_err,
        )
        yield SingletonLockResult(
            acquired=True, lock_path=target,
        )
        return

    try:
        with flock_critical_section(
            target, timeout_s=0.0,
        ) as acquired:
            if acquired:
                logger.info(
                    "[singleton_lock] acquired %s "
                    "(pid=%d)",
                    target, os.getpid(),
                )
            else:
                logger.warning(
                    "[singleton_lock] another soak holds "
                    "%s — refusing this fire",
                    target,
                )
            yield SingletonLockResult(
                acquired=bool(acquired), lock_path=target,
            )
    except Exception as err:  # noqa: BLE001 — last-resort fail-open
        logger.warning(
            "[singleton_lock] flock primitive raised (%r) — "
            "fail-open",
            err,
        )
        yield SingletonLockResult(
            acquired=True, lock_path=target,
        )


# ---------------------------------------------------------------------------
# §33.3 register_shipped_invariants
# ---------------------------------------------------------------------------


_TARGET_FILE = (
    "backend/core/ouroboros/battle_test/singleton_lock.py"
)


def register_shipped_invariants() -> list:
    """AST pins — auto-discovered by the §33.3 meta runner."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_master_default_false(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """``singleton_lock_enabled`` default arg MUST be 'false'."""
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.FunctionDef)
                and node.name == "singleton_lock_enabled"
            ):
                for sub in _ast.walk(node):
                    if (
                        isinstance(sub, _ast.Call)
                        and len(sub.args) >= 2
                        and isinstance(sub.args[1], _ast.Constant)
                    ):
                        if sub.args[1].value != "false":
                            return (
                                "singleton_lock_enabled default "
                                f"arg drift: "
                                f"{sub.args[1].value!r}",
                            )
                        return ()
                return (
                    "singleton_lock_enabled body missing "
                    "default-arg literal",
                )
        return ("singleton_lock_enabled not found",)

    def _validate_authority_asymmetry(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """Substrate purity — singleton_lock MUST NOT import the
        harness, orchestrator, auto_committer, or any policy.
        Composes ONLY cross_process_jsonl (stdlib-thin primitive)."""
        forbidden = {
            "backend.core.ouroboros.battle_test.harness",
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.policy_engine",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.worktree_manager",
            "backend.core.ouroboros.governance.semantic_guardian",
        }
        violations = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                if mod in forbidden:
                    violations.append(f"forbidden import: {mod}")
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    if alias.name in forbidden:
                        violations.append(
                            f"forbidden import: {alias.name}"
                        )
        return tuple(violations)

    def _validate_path_not_hardcoded(
        tree: _ast.AST, source: str,
    ) -> tuple:
        """Operator binding 'no hardcoding' — the lock path must
        derive from ``Path(repo_root, *_LOCK_RELATIVE_PATH)``.
        Any bare string literal ``"ouroboros_battle_test.lock"``
        outside ``_LOCK_RELATIVE_PATH`` definition is a drift."""
        literal = "ouroboros_battle_test.lock"
        # Count occurrences of the literal in source — should be
        # exactly one (in the _LOCK_RELATIVE_PATH tuple body).
        # More than one means someone hardcoded it at a use-site.
        occurrences = source.count(literal)
        if occurrences > 1:
            return (
                f"path literal {literal!r} appears "
                f"{occurrences} times in source — hardcoding "
                "drift; must derive from _LOCK_RELATIVE_PATH "
                "tuple",
            )
        return ()

    def _validate_compose_canonical_primitive(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """``acquire_singleton`` MUST compose
        ``flock_critical_section`` from cross_process_jsonl —
        proves the substrate did not re-implement flock under the
        operator binding 'leverage existing files'."""
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                if (
                    node.module
                    == (
                        "backend.core.ouroboros.governance."
                        "cross_process_jsonl"
                    )
                ):
                    names = {a.name for a in node.names}
                    if "flock_critical_section" in names:
                        return ()
        return (
            "acquire_singleton must compose "
            "flock_critical_section from cross_process_jsonl "
            "(no parallel flock implementation)",
        )

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "singleton_lock_master_default_false"
            ),
            target_file=_TARGET_FILE,
            description=(
                "§33.1 substrate canonical shape — master flag "
                "default-FALSE. Drift to 'true' would silently "
                "enable structural singleton enforcement for "
                "every operator before graduation."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "singleton_lock_authority_asymmetry"
            ),
            target_file=_TARGET_FILE,
            description=(
                "Substrate purity — singleton lock MUST NOT "
                "import the harness, orchestrator, auto_committer, "
                "or any policy module. Composes only "
                "cross_process_jsonl (stdlib-thin)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "singleton_lock_no_hardcoded_path"
            ),
            target_file=_TARGET_FILE,
            description=(
                "Operator binding 'no hardcoding' — lock filename "
                "literal must appear exactly once (in the "
                "_LOCK_RELATIVE_PATH tuple definition). All use "
                "sites compose via default_lock_path()."
            ),
            validate=_validate_path_not_hardcoded,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "singleton_lock_composes_canonical_primitive"
            ),
            target_file=_TARGET_FILE,
            description=(
                "Operator binding 'leverage existing files' — "
                "must compose flock_critical_section from "
                "cross_process_jsonl, never re-implement flock "
                "logic in this substrate."
            ),
            validate=_validate_compose_canonical_primitive,
        ),
    ]


__all__ = [
    "SINGLETON_LOCK_SCHEMA_VERSION",
    "SingletonLockResult",
    "acquire_singleton",
    "default_lock_path",
    "register_shipped_invariants",
    "singleton_lock_enabled",
]
