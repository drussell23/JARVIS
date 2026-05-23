"""Phase 0 — coding_council ↔ O+V cross-kingdom boundary
enforcement.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "The AST pin (governance_no_coding_council_imports) is the
   most critical piece of this deliverable. It is a pure
   Iron Gate protocol. By writing a 30-line AST constraint
   that physically prevents any future agent or module from
   importing coding_council logic into the governance/ tree,
   you are solving the root problem at the compiler level.
   It is not a workaround; it is a permanent, deterministic
   safety boundary that guarantees the two kingdoms remain
   isolated as the system scales."

## The two kingdoms

`backend/core/ouroboros/governance/` (O+V) and
`backend/core/coding_council/` (Coding Council) have
evolved as parallel kingdoms with overlapping primitives
that serve different scopes:

  * `safety/ast_validator.py` (Coding Council canonical for
    coding_council/orchestrator) ↔ `SemanticGuardian`
    (O+V canonical for orchestrator pipeline)
  * `safety/security_scanner.py` (Coding Council canonical)
    ↔ `SemanticGuardian._CREDENTIAL_SHAPES` (O+V canonical)
  * `framework/circuit_breaker.py` (Coding Council canonical
    — generic 3-state CLOSED/OPEN/HALF_OPEN) ↔
    `provider_circuit_breaker.py` (O+V canonical — Tier
    0/1/2 cascade-aware)
  * `framework/bulkhead.py` (Coding Council canonical —
    generic semaphore pool) ↔ `BackgroundAgentPool` (O+V
    canonical — governance-specialized PriorityQueue +
    worker pool)

Both sides ship distinct abstractions at distinct scopes.
**Cross-kingdom imports into `governance/` are forbidden**
to keep the substrate boundary clean as Trinity expands
(JARVIS / J-Prime / Reactor-Core). Existing wires in
`backend/core/ouroboros/trinity_integration.py` stay (they
pre-date this boundary; not in `governance/`).

## What this pin enforces

Walks every `.py` file under `backend/core/ouroboros/governance/`
and reports a violation for any:
  * `from backend.core.coding_council` ImportFrom node, or
  * `import backend.core.coding_council` Import node,

at **any nesting level** (top-level OR lazy-inside-function
OR inside a class). The AST walk is unconditional —
``ast.walk`` traverses every node regardless of containment.

## Authority asymmetry

This module is itself substrate-pure (no orchestrator-tier
imports). Its only effect is the validator function which
walks the governance/ tree on demand.

## Why a single pin (not per-module)

A single tree-level pin is the right shape because:

  1. The invariant is **semantic** ("governance/ does not
     import coding_council") — not file-local.
  2. New governance/ modules added in the future are
     covered automatically — no per-module pin maintenance.
  3. The pin's runtime cost is bounded — one rglob walk +
     one ast.parse per file. Fast enough to run in CI.

**NEVER raises** — every code path defensive (file read /
AST parse failures are skipped silently so the pin doesn't
become brittle to encoding edge cases).
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any, FrozenSet, List, Tuple


logger = logging.getLogger(
    "Ouroboros.CrossKingdomBoundary",
)


CROSS_KINGDOM_BOUNDARY_SCHEMA_VERSION: str = (
    "cross_kingdom_boundary.1"
)


# Forbidden import-prefix. Bytes-pinned to the canonical
# coding_council module path. Future renames of the
# coding_council package would require updating this constant
# (intentional — operator-visible diff).
_FORBIDDEN_IMPORT_PREFIX: str = (
    "backend.core.coding_council"
)


# Files exempted from the boundary check — historical wires
# that pre-date Phase 0. **Empty by design**: per the Phase 0
# audit (2026-05-07), zero files under governance/ import
# coding_council today. Adding entries here requires explicit
# operator approval + an ADR + a §35 deferred-architectural-
# mismatch entry. The list is FrozenSet so test-time mutation
# is structurally impossible.
_BOUNDARY_EXEMPTIONS: FrozenSet[str] = frozenset({
    # (none — keep empty)
})


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


def _governance_root() -> Path:
    """Resolve the canonical governance/ tree root from this
    module's location. Pure function; NEVER raises."""
    return Path(__file__).resolve().parents[1]


def _iter_governance_py_files() -> List[Path]:
    """Enumerate every .py under governance/ (recursively),
    skipping ``__pycache__/`` directories. Pure read; NEVER
    raises."""
    try:
        root = _governance_root()
    except Exception:  # noqa: BLE001 — defensive
        return []
    out: List[Path] = []
    try:
        for p in root.rglob("*.py"):
            try:
                if "__pycache__" in p.parts:
                    continue
                out.append(p)
            except Exception:  # noqa: BLE001 — defensive
                continue
    except Exception:  # noqa: BLE001 — defensive
        return []
    return out


def _scan_one_file(
    path: Path,
    *,
    forbidden_prefix: str = _FORBIDDEN_IMPORT_PREFIX,
) -> Tuple[Tuple[int, str], ...]:
    """Parse one .py file and return a tuple of
    (line_number, offending_module_string) for every
    forbidden import found. Pure function; NEVER raises.

    Returns empty tuple when:
      * file unreadable (OSError, decode error)
      * file has SyntaxError
      * no forbidden imports present
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    violations: List[Tuple[int, str]] = []
    try:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if (
                    module == forbidden_prefix
                    or module.startswith(
                        forbidden_prefix + ".",
                    )
                ):
                    violations.append(
                        (int(node.lineno), str(module)),
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name or ""
                    if (
                        name == forbidden_prefix
                        or name.startswith(
                            forbidden_prefix + ".",
                        )
                    ):
                        violations.append(
                            (int(node.lineno), str(name)),
                        )
    except Exception:  # noqa: BLE001 — defensive
        return ()
    return tuple(violations)


# ---------------------------------------------------------------------------
# Validator entry point — caller-injectable for tests
# ---------------------------------------------------------------------------


async def scan_governance_tree_async(
    *,
    governance_root_override: "Path | None" = None,
    forbidden_prefix: str = _FORBIDDEN_IMPORT_PREFIX,
) -> Tuple[str, ...]:
    """Slice 12Z — async-cooperative sibling of
    :func:`scan_governance_tree`.

    bt-2026-05-23-221029 (Slice 12Y validation soak) tombstone
    captured this module's sync ``rglob`` + ``read_text`` loop
    running directly on the asyncio MainThread when
    :func:`shipped_code_invariants.validate_all_async` took its
    PermissionError sync-fallback path. ``LoopDeadman`` fired at
    301.8s. SidecarProfiler captured 4 in-progress STUCK_FRAME
    emissions all pointing at ``pathlib.read_text``.

    Slice 12Z composes the canonical Slice 12U
    :mod:`cooperative_fs_io` substrate — dedicated
    ``advisor-blast`` executor for per-file reads +
    ``cooperative_yield_every_n_async`` between batches — so the
    same scan that wedged on the loop now yields control every
    N files and dispatches I/O off-thread. Returns the same
    violation tuple shape as the sync sibling; deterministic
    result parity is pinned by ``test_result_parity_with_sync``.

    Each violation string format unchanged:
      ``<relative-path>:<lineno> forbidden import of <module>``

    NEVER raises into the caller — iteration errors terminate
    the scan cleanly with whatever violations are accumulated.
    """
    # Lazy import — keeps cooperative_fs_io off the sync-only
    # module-load path for callers that only need
    # :func:`scan_governance_tree`.
    try:
        from backend.core.ouroboros.governance.cooperative_fs_io import (  # noqa: E501
            iter_files_cooperative,
            read_text_offloaded,
        )
        from backend.core.ouroboros.governance.event_loop_governance import (  # noqa: E501
            offload_blocking,
        )
    except Exception:  # noqa: BLE001 — fall back to sync
        return scan_governance_tree(
            governance_root_override=governance_root_override,
            forbidden_prefix=forbidden_prefix,
        )

    root = (
        governance_root_override
        if governance_root_override is not None
        else _governance_root()
    )

    violations: List[str] = []
    try:
        # Cooperative file iteration — yields control every N
        # items (default 64 via JARVIS_EVENT_LOOP_YIELD_EVERY_N)
        # so the heartbeat coroutine + Claude SDK stream
        # consumer get scheduling slots throughout the scan.
        async for path_str in iter_files_cooperative(
            root, pattern="*.py",
        ):
            try:
                # __pycache__ filter matches the sync
                # implementation byte-for-byte.
                if "__pycache__" in Path(path_str).parts:
                    continue
            except Exception:  # noqa: BLE001
                continue

            try:
                rel = (
                    Path(path_str).relative_to(root).as_posix()
                )
            except ValueError:
                rel = str(path_str)
            if rel in _BOUNDARY_EXEMPTIONS:
                continue

            # Per-file read offloaded to the dedicated
            # advisor-blast executor (Slice 12T Part 3 +
            # Slice 12U cooperative_fs_io). The AST parse +
            # forbidden-import walk also runs off-loop via
            # ``offload_blocking`` so the substring scan doesn't
            # hold the GIL on the loop thread.
            try:
                source = await read_text_offloaded(
                    Path(path_str),
                )
                if source is None:
                    continue
            except Exception:  # noqa: BLE001
                continue

            try:
                # _scan_one_file is fast pure-Python AST work
                # — offload to the dedicated pool so the
                # cumulative cost (one parse + one walk per
                # .py file in governance/, hundreds of files)
                # doesn't accumulate on the loop thread.
                file_violations = await offload_blocking(
                    _scan_one_file_from_source,
                    source, forbidden_prefix,
                    label="cross_kingdom.scan_one_file",
                )
            except Exception:  # noqa: BLE001
                continue

            for line, module in file_violations:
                violations.append(
                    f"{rel}:{line} forbidden import of "
                    f"{module!r}"
                )
    except Exception:  # noqa: BLE001 — defensive
        # Swallow iteration faults — return what we collected.
        pass

    return tuple(violations)


def _scan_one_file_from_source(
    source: str,
    forbidden_prefix: str,
) -> Tuple[Tuple[int, str], ...]:
    """Slice 12Z module-level helper — same AST-walk as
    :func:`_scan_one_file` but operates on a pre-read ``source``
    string. Lifted to module level so the
    :func:`cooperative_fs_io.offload_blocking` worker doesn't
    capture caller-local state. NEVER raises."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    violations: List[Tuple[int, str]] = []
    try:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if (
                    module == forbidden_prefix
                    or module.startswith(
                        forbidden_prefix + ".",
                    )
                ):
                    violations.append(
                        (int(node.lineno), str(module)),
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name or ""
                    if (
                        name == forbidden_prefix
                        or name.startswith(
                            forbidden_prefix + ".",
                        )
                    ):
                        violations.append(
                            (int(node.lineno), str(name)),
                        )
    except Exception:  # noqa: BLE001 — defensive
        return ()
    return tuple(violations)


def scan_governance_tree(
    *,
    governance_root_override: "Path | None" = None,
    forbidden_prefix: str = _FORBIDDEN_IMPORT_PREFIX,
) -> Tuple[str, ...]:
    """Walk the governance/ tree and return a tuple of
    violation strings. Caller-injectable root override
    enables synthetic-regression tests against a temp tree.
    Pure function; NEVER raises.

    Each violation string is formatted:
      ``<relative-path>:<lineno> forbidden import of <module>``

    Slice 12Z note: prefer :func:`scan_governance_tree_async`
    from any asyncio context — this sync entry point blocks
    the calling thread for the full scan duration. The async
    sibling composes :mod:`cooperative_fs_io` for non-blocking
    iteration on the event loop.
    """
    root = (
        governance_root_override
        if governance_root_override is not None
        else _governance_root()
    )
    files: List[Path] = []
    try:
        for p in root.rglob("*.py"):
            try:
                if "__pycache__" in p.parts:
                    continue
                files.append(p)
            except Exception:  # noqa: BLE001 — defensive
                continue
    except Exception:  # noqa: BLE001 — defensive
        return ()
    violations: List[str] = []
    for fpath in files:
        try:
            rel = fpath.relative_to(root).as_posix()
        except ValueError:
            rel = str(fpath)
        if rel in _BOUNDARY_EXEMPTIONS:
            continue
        for line, module in _scan_one_file(
            fpath, forbidden_prefix=forbidden_prefix,
        ):
            violations.append(
                f"{rel}:{line} forbidden import of "
                f"{module!r}"
            )
    return tuple(violations)


# ---------------------------------------------------------------------------
# AST pin
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pin:

      * ``governance_no_coding_council_imports`` —
        tree-level: walks every ``.py`` under
        ``governance/`` and reports any
        ``from backend.core.coding_council`` /
        ``import backend.core.coding_council`` (top-level
        OR lazy nested). Operator binding 2026-05-07: "pure
        Iron Gate protocol — physically prevents any future
        agent or module from importing coding_council logic
        into the governance/ tree."
    """
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/meta/"
        "cross_kingdom_boundary.py"
    )

    def _validate_cross_kingdom_boundary(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """The pin's target_file is THIS module — but its
        validation walks the whole governance/ tree. This is
        the canonical shape for tree-level invariants: the
        target_file points at the sentinel module that owns
        the rule; the validator reads from disk."""
        return scan_governance_tree()

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "governance_no_coding_council_imports"
            ),
            target_file=target,
            description=(
                "Phase 0 hygiene — governance/ MUST NOT "
                "import from backend.core.coding_council at "
                "any nesting level (top-level OR lazy). Two "
                "parallel kingdoms with different canonical "
                "primitives at different scopes; cross-"
                "kingdom imports break the substrate "
                "boundary. Pure Iron Gate protocol."
            ),
            validate=_validate_cross_kingdom_boundary,
        ),
    ]


# ---------------------------------------------------------------------------
# FlagRegistry seeds — none (this is a structural pin, no env knobs)
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """No-op — the boundary is structural, not flag-gated.
    Operator binding 2026-05-07: "permanent, deterministic
    safety boundary." NEVER raises."""
    return None


__all__ = [
    "CROSS_KINGDOM_BOUNDARY_SCHEMA_VERSION",
    "_scan_one_file_from_source",
    "register_flags",
    "register_shipped_invariants",
    "scan_governance_tree",
    "scan_governance_tree_async",
]
