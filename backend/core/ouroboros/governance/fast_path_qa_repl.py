"""§41.3 #26 Phase 2 Slice 5 — ``/qa`` REPL dispatcher.
======================================================

Operator-facing CLI surface for the :mod:`fast_path_qa` q-N ring
(Phase 0 substrate). Auto-discovered by
:mod:`repl_dispatch_registry` via the §33.3 naming-cage:
filename basename ``fast_path_qa_repl.py`` → verb
``fast_path_qa`` → ``/fast_path_qa`` matches at runtime zero-edit
to ``serpent_flow.py``'s dispatch ladder.

Pattern parallel to :mod:`tool_permissions_repl` (Venom V2
Slice 2, v2.90), :mod:`decisions_repl`, :mod:`curiosity_repl`,
:mod:`outcomes_repl`. The /qa surface intentionally uses
``fast_path_qa`` as the verb (matching the substrate filename) —
operators discover it via ``/help verbs`` and the
auto-completer.

Subcommands
-----------

* ``/fast_path_qa``                — alias for
  ``/fast_path_qa recent``
* ``/fast_path_qa recent [N]``     — most-recent N artifacts
  (default 20, max 200)
* ``/fast_path_qa path <name>``    — artifacts for one
  retrieval_path (exact match; open-vocabulary —
  retrieval_only / hybrid_grounded / claude_direct /
  retrieval_disabled)
* ``/fast_path_qa op <op_id>``     — artifacts for one op_id
  (exact match)
* ``/fast_path_qa ref <q-N>``      — single artifact lookup
  (alias for ``/expand q-N`` from a discovery-friendly verb)
* ``/fast_path_qa stats``          — ring snapshot (capacity /
  size / utilization / next_seq / schema_version)
* ``/fast_path_qa help``           — usage listing (always
  available; bypasses master-flag gate)

Master gate: :func:`fast_path_qa.master_enabled` (default-FALSE
per §33.1 graduation contract). When off, every subcommand
returns a friendly disabled-notice and points at the canonical
env-var to flip — no fake-empty-list output.

Authority invariants (AST-pinned)
---------------------------------

* Imports stdlib + ``fast_path_qa`` ONLY (lazy at call site).
* NEVER imports orchestrator / iron_gate / policy_engine /
  candidate_generator / tool_executor / urgency_router /
  change_engine / semantic_guardian / providers — REPL surface
  stays authority-free.
* **READ-ONLY** — no subcommand mutates the ring.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


_HELP = (
    "/fast_path_qa — §41.3 #26 fast-path Q&A artifact ring "
    "(Phase 2 Slice 5 / PRD §41.3 #26)\n"
    "\n"
    "Subcommands:\n"
    "  /fast_path_qa                       alias for "
    "/fast_path_qa recent\n"
    "  /fast_path_qa recent [N]            most-recent N "
    "artifacts (default 20, max 200)\n"
    "  /fast_path_qa path <retrieval_path> artifacts for one "
    "retrieval path (exact match)\n"
    "  /fast_path_qa op <op_id>            artifacts for one "
    "op_id (exact match)\n"
    "  /fast_path_qa ref <q-N>             single artifact lookup\n"
    "  /fast_path_qa stats                 ring snapshot "
    "(capacity / size / utilization)\n"
    "  /fast_path_qa help                  this text\n"
    "\n"
    "Retrieval-path taxonomy (open-vocabulary, from "
    "fast_path_qa.py):\n"
    "  retrieval_only / hybrid_grounded / claude_direct / "
    "retrieval_disabled\n"
    "\n"
    "Master flag: JARVIS_FAST_PATH_QA_ENABLED (default FALSE — "
    "Phase 9 cadence pending)\n"
    "Capacity env:  JARVIS_FAST_PATH_QA_STORE_CAPACITY "
    "(default 100, bounds [1, 10000])\n"
    "Cross-substrate: each artifact carries a ``q-N`` ref usable "
    "with /expand <q-N>\n"
    "Route:        every artifact is routed as "
    "ProviderRoute.INFORMATIONAL (closed-5→6 expansion, §41.3.1 "
    "D3b)\n"
)


_DEFAULT_RECENT_LIMIT: int = 20
_MAX_RECENT_LIMIT: int = 200
_DEFAULT_FILTER_LIMIT: int = 20


# ---------------------------------------------------------------------------
# Frozen result container — mirrors ToolPermissionsReplDispatchResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FastPathQAReplDispatchResult:
    """Result of a ``/fast_path_qa`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/fast_path_qa`` invocation (caller routes elsewhere).

    §33.5 frozen-artifact contract: symmetric ``to_dict`` for
    transport across substrates (SSE bridges, IDE serialization,
    audit logs)."""

    ok: bool
    text: str
    matched: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "matched": self.matched,
        }


# ---------------------------------------------------------------------------
# Master-flag gate — defers to canonical fast_path_qa.master_enabled
# ---------------------------------------------------------------------------


def _master_enabled() -> bool:
    """Defers to the canonical :func:`fast_path_qa.master_enabled`
    — no parallel flag. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.fast_path_qa import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Dispatch matchers + parsers
# ---------------------------------------------------------------------------


def _matches(line: object) -> bool:
    """Defensive — coerce any input to str before parsing. The
    NEVER-raises contract means callers may pass non-str
    garbage; we yield matched=False without crashing."""
    try:
        s = str(line or "").strip()
    except Exception:  # noqa: BLE001
        return False
    if not s:
        return False
    return (
        s == "/fast_path_qa"
        or s == "fast_path_qa"
        or s.startswith("/fast_path_qa ")
        or s.startswith("fast_path_qa ")
    )


def _parse_limit(
    args: List[str], *, default: int, ceiling: int,
) -> int:
    """Parse limit from the ``args[1]`` slot. Falls through to
    default on parse failure / out-of-bounds."""
    if len(args) < 2:
        return default
    try:
        n = int(args[1])
        if n < 1:
            return 1
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_fast_path_qa_command(
    line: str,
) -> FastPathQAReplDispatchResult:
    """Parse a ``/fast_path_qa`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return FastPathQAReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return FastPathQAReplDispatchResult(
            ok=False,
            text=f"  /fast_path_qa parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "recent")

    if head in ("help", "?"):
        return FastPathQAReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return FastPathQAReplDispatchResult(
            ok=False,
            text=(
                "  /fast_path_qa: ring disabled — set "
                "JARVIS_FAST_PATH_QA_ENABLED=true (Phase 9 "
                "cadence pending; see /fast_path_qa help)"
            ),
        )

    if head == "recent":
        return _render_recent(
            _parse_limit(
                args,
                default=_DEFAULT_RECENT_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )
    if head == "path":
        if len(args) < 2:
            return FastPathQAReplDispatchResult(
                ok=False,
                text=(
                    "  /fast_path_qa path <retrieval_path>: "
                    "missing retrieval_path argument."
                ),
            )
        return _render_by_path(
            args[1],
            _parse_limit(
                args[1:],
                default=_DEFAULT_FILTER_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )
    if head == "op":
        if len(args) < 2:
            return FastPathQAReplDispatchResult(
                ok=False,
                text=(
                    "  /fast_path_qa op <op_id>: missing "
                    "op_id argument."
                ),
            )
        return _render_by_op(
            args[1],
            _parse_limit(
                args[1:],
                default=_DEFAULT_FILTER_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )
    if head == "ref":
        if len(args) < 2:
            return FastPathQAReplDispatchResult(
                ok=False,
                text=(
                    "  /fast_path_qa ref <q-N>: missing q-N ref "
                    "argument. Tip: refs look like q-1, q-2, ..."
                ),
            )
        return _render_by_ref(args[1])
    if head == "stats":
        return _render_stats()
    return FastPathQAReplDispatchResult(
        ok=False,
        text=(
            f"  /fast_path_qa: unknown subcommand {head!r}. "
            f"Try /fast_path_qa help."
        ),
    )


# ---------------------------------------------------------------------------
# Renderers — read-only, NEVER raise. Duck-typed attribute access
# so a foreign object doesn't crash rendering.
# ---------------------------------------------------------------------------


def _format_artifact_one_line(art: object) -> str:
    """One-line rendering. Reads via duck-typed attribute access."""
    ref = getattr(art, "ref", "") or ""
    op_id = (getattr(art, "op_id", "") or "")[:18]
    path = (getattr(art, "retrieval_path", "") or "")[:18]
    try:
        cost = float(getattr(art, "cost_usd", 0.0))
    except (TypeError, ValueError):
        cost = 0.0
    try:
        score = float(getattr(art, "top_score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    return (
        f"  {ref:<6}  path={path:<18}  "
        f"cost=${cost:.5f}  score={score:.2f}  "
        f"op={op_id}"
    )


def _render_recent(
    limit: int,
) -> FastPathQAReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.fast_path_qa import (  # noqa: E501
            get_default_qa_store,
        )
        records = get_default_qa_store().recent(limit=limit)
    except Exception as exc:  # noqa: BLE001 — defensive
        return FastPathQAReplDispatchResult(
            ok=False,
            text=f"  /fast_path_qa recent error: {exc}",
        )
    if not records:
        return FastPathQAReplDispatchResult(
            ok=True,
            text=(
                "  /fast_path_qa: no Q&A artifacts recorded yet. "
                "Ring is empty (or master flag was just enabled "
                "and no /ask invocation has happened since)."
            ),
        )
    lines = [
        f"  /fast_path_qa recent (last {len(records)}):",
    ]
    for art in records:
        lines.append(_format_artifact_one_line(art))
    return FastPathQAReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_by_path(
    retrieval_path: str, limit: int,
) -> FastPathQAReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.fast_path_qa import (  # noqa: E501
            get_default_qa_store,
        )
        records = get_default_qa_store().by_path(
            retrieval_path, limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return FastPathQAReplDispatchResult(
            ok=False,
            text=f"  /fast_path_qa path error: {exc}",
        )
    if not records:
        return FastPathQAReplDispatchResult(
            ok=True,
            text=(
                f"  /fast_path_qa path {retrieval_path!r}: "
                f"no artifacts recorded for this retrieval path."
            ),
        )
    lines = [
        f"  /fast_path_qa path {retrieval_path!r} "
        f"(last {len(records)}):",
    ]
    for art in records:
        lines.append(_format_artifact_one_line(art))
    return FastPathQAReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_by_op(
    op_id: str, limit: int,
) -> FastPathQAReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.fast_path_qa import (  # noqa: E501
            get_default_qa_store,
        )
        records = get_default_qa_store().by_op(
            op_id, limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return FastPathQAReplDispatchResult(
            ok=False,
            text=f"  /fast_path_qa op error: {exc}",
        )
    if not records:
        return FastPathQAReplDispatchResult(
            ok=True,
            text=(
                f"  /fast_path_qa op {op_id!r}: no artifacts "
                f"recorded for this op."
            ),
        )
    lines = [
        f"  /fast_path_qa op {op_id!r} (last "
        f"{len(records)}):",
    ]
    for art in records:
        lines.append(_format_artifact_one_line(art))
    return FastPathQAReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_by_ref(
    ref: str,
) -> FastPathQAReplDispatchResult:
    """Single-artifact lookup. Renders question + answer + cost +
    model — more detail than the one-line listing rendering."""
    try:
        from backend.core.ouroboros.governance.fast_path_qa import (  # noqa: E501
            get_default_qa_store,
        )
        artifact = get_default_qa_store().lookup(ref)
    except Exception as exc:  # noqa: BLE001 — defensive
        return FastPathQAReplDispatchResult(
            ok=False,
            text=f"  /fast_path_qa ref error: {exc}",
        )
    if artifact is None:
        return FastPathQAReplDispatchResult(
            ok=False,
            text=(
                f"  /fast_path_qa ref {ref!r}: not found (ring "
                f"may have evicted it; refs are monotonic but the "
                f"ring is bounded by drop-oldest)."
            ),
        )
    try:
        q = (artifact.question or "")[:512]
        a = (artifact.answer or "")[:2048]
        path = artifact.retrieval_path
        cost = float(artifact.cost_usd)
        model = artifact.model
        elapsed = float(artifact.elapsed_s)
        score = float(artifact.top_score)
        op_id = artifact.op_id
    except Exception as exc:  # noqa: BLE001 — defensive
        return FastPathQAReplDispatchResult(
            ok=False,
            text=f"  /fast_path_qa ref projection error: {exc}",
        )
    text = (
        f"  /fast_path_qa ref {ref!r}:\n"
        f"    op_id:          {op_id}\n"
        f"    retrieval_path: {path}\n"
        f"    top_score:      {score:.3f}\n"
        f"    model:          {model}\n"
        f"    cost_usd:       ${cost:.5f}\n"
        f"    elapsed_s:      {elapsed:.2f}\n"
        f"    question:       {q}\n"
        f"    answer:\n"
        f"{a}"
    )
    return FastPathQAReplDispatchResult(ok=True, text=text)


def _render_stats() -> FastPathQAReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.fast_path_qa import (  # noqa: E501
            cost_today_usd,
            daily_budget_usd,
            get_default_qa_store,
        )
        snap = get_default_qa_store().snapshot()
        today_spend = cost_today_usd()
        daily_cap = daily_budget_usd()
    except Exception as exc:  # noqa: BLE001 — defensive
        return FastPathQAReplDispatchResult(
            ok=False,
            text=f"  /fast_path_qa stats error: {exc}",
        )
    remaining = max(0.0, daily_cap - today_spend)
    text = (
        "  /fast_path_qa stats:\n"
        f"    capacity:        {snap.capacity}\n"
        f"    size:            {snap.size}\n"
        f"    next_seq:        {snap.next_seq}\n"
        f"    utilization:     {snap.utilization:.2%}\n"
        f"    schema:          {snap.schema_version}\n"
        f"    today_spend:     ${today_spend:.5f}\n"
        f"    daily_cap:       ${daily_cap:.2f}\n"
        f"    daily_remaining: ${remaining:.5f}"
    )
    return FastPathQAReplDispatchResult(ok=True, text=text)


# ===========================================================================
# §33.1 — register_shipped_invariants self-registration
# ===========================================================================
#
# Auto-discovered by the canonical ``shipped_code_invariants``
# walker. Mirrors the discipline applied to sibling REPL verbs
# (tool_permissions_repl, decisions_repl, etc.): the load-bearing
# structural invariants of this module are pinned in source so a
# future refactor can't silently regress the §33.3 naming-cage
# auto-discovery contract or the authority-asymmetry / read-only
# guarantees.


def register_shipped_invariants() -> list:
    """FastPathQA REPL substrate invariants. Pins:

      * Module-level ``dispatch_fast_path_qa_command(line)``
        callable present — the §33.3 naming-cage hook.
      * Authority asymmetry: NEVER imports policy / orchestrator /
        iron_gate / tool_executor / candidate_generator / providers
        / urgency_router / change_engine / semantic_guardian.
      * READ-ONLY: source MUST NOT contain ``store.store(``
        or other mutation calls — the REPL is a thin projection
        layer over the canonical q-N ring.
    """
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    _FORBIDDEN_IMPORT_MODULES = (
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.policy_engine",
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.tool_executor",
        "backend.core.ouroboros.governance.candidate_generator",
        "backend.core.ouroboros.governance.providers",
        "backend.core.ouroboros.governance.urgency_router",
        "backend.core.ouroboros.governance.change_engine",
        "backend.core.ouroboros.governance.semantic_guardian",
    )

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        saw_dispatcher = False
        # AST-walk: a true mutation looks like ``X.store(...)``
        # where X is any expression (an actual Call expression
        # with an Attribute func whose attr == "store"). The
        # substrate's lookup/recent/by_op/by_path are reads —
        # only .store() mutates.
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                if node.name == "dispatch_fast_path_qa_command":
                    saw_dispatcher = True
            elif isinstance(node, _ast.ImportFrom):
                if node.module in _FORBIDDEN_IMPORT_MODULES:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"forbidden import {node.module!r} — "
                        f"REPL surface MUST stay authority-free"
                    )
            elif isinstance(node, _ast.Call):
                func = node.func
                if (
                    isinstance(func, _ast.Attribute)
                    and func.attr == "store"
                ):
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"READ-ONLY violation — REPL surface "
                        f"MUST NOT call .store() on the q-N ring"
                    )
        if not saw_dispatcher:
            violations.append(
                "module-level dispatch_fast_path_qa_command "
                "callable missing — §33.3 naming-cage hook broken"
            )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/fast_path_qa_repl.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name=(
                "fast_path_qa_repl_substrate"
            ),
            target_file=target,
            description=(
                "FastPathQA REPL: §33.3 naming-cage dispatcher "
                "present + authority-asymmetry + read-only over "
                "canonical q-N ring."
            ),
            validate=_validate,
        ),
    ]


__all__ = [
    "FastPathQAReplDispatchResult",
    "dispatch_fast_path_qa_command",
    "register_shipped_invariants",
]
