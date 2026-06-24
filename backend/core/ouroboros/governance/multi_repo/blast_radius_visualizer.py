"""blast_radius_visualizer -- human-readable cross-repo blast-radius TREE.

Guardrail-1 companion: renders the Oracle-traced cross-repo dependents of a
mutation target as a lightweight ASCII dependency tree so the OPERATOR can SEE
which Body (jarvis) / Mind (prime) files map to a mutated Nerves (reactor) (or
Mind) symbol BEFORE approving the cross-repo PR.

    CROSS-REPO BLAST RADIUS  (mutating reactor::TelemetryAdapter.emit)
    reactor/telemetry_adapter.py::TelemetryAdapter.emit
    └─ depended on by:
       ├─ [jarvis] backend/.../foo.py::caller_a
       └─ [prime]  jarvis_prime/.../baz.py::caller_c
    <N> Body/Mind files mapped to this Nerves mutation.

Design invariants:
  * **Pure render, authority-free.** This module imports NOTHING from the
    orchestrator / policy / change_engine / risk_tier layers. It takes a
    ``BlastRadiusContext`` and returns a string. A source-grep test enforces the
    no-authority-imports invariant.
  * **ASCII box-drawing only.** Uses only the lightweight tree glyphs
    ``|- `- |  ` (rendered here as the box-drawing chars below) -- no Unicode
    beyond the four standard box-drawing codepoints; everything else is ASCII.
  * **Capped.** At most ``JARVIS_BLAST_TREE_MAX_NODES`` (default 40) dependents
    are drawn; the remainder is summarised as ``... +M more ...`` (never silently
    dropped). No hardcoded cap.
  * **Fail-soft.** Any rendering error returns ``""`` -- the visualizer is an
    operator convenience, never load-bearing for safety.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:  # avoid a runtime import cycle; pure type hint only
    from backend.core.ouroboros.governance.multi_repo.cross_repo_blast_context import (
        BlastRadiusContext,
    )

# Env knob (NO hardcoded cap).
_MAX_NODES_ENV = "JARVIS_BLAST_TREE_MAX_NODES"
_DEFAULT_MAX_NODES = 40

# Box-drawing glyphs (the only non-ASCII codepoints, by design).
_TEE = "├─ "  # |-
_ELBOW = "└─ "  # `-
_ROOT_ELBOW = "└─ "  # `-
_PIPE = "│  "  # |
_BLANK = "   "

# Repo -> Trinity role label.
_REPO_ROLE = {
    "prime": "Mind",
    "reactor": "Nerves",
    "jarvis": "Body",
}


def _max_nodes() -> int:
    try:
        raw = os.environ.get(_MAX_NODES_ENV, "").strip()
        if not raw:
            return _DEFAULT_MAX_NODES
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_NODES


def _role(repo: str) -> str:
    return _REPO_ROLE.get(repo, repo)


def render_blast_tree(ctx: "BlastRadiusContext") -> str:
    """Render the cross-repo blast radius as an ASCII dependency tree.

    Dependents are grouped by repo (deterministic alpha order) and capped at
    ``JARVIS_BLAST_TREE_MAX_NODES``; the overflow is summarised as ``+M more``.

    Returns ``""`` when there are no cross-repo dependents (nothing to show) or
    on any rendering error (fail-soft).
    """
    try:
        deps = list(getattr(ctx, "dependents", ()) or ())
        if not deps:
            return ""

        target_repo = getattr(ctx, "target_repo", "") or ""
        target_symbol = getattr(ctx, "target_symbol", "") or ""

        cap = _max_nodes()
        total = len(deps)

        # Group by repo (deterministic order), preserving nearest-first order
        # within each repo (the trace already ordered them).
        by_repo: dict = {}
        for d in deps:
            by_repo.setdefault(getattr(d, "repo", "") or "", []).append(d)
        ordered_repos = sorted(by_repo.keys())

        lines: List[str] = []
        # Header.
        lines.append(
            "CROSS-REPO BLAST RADIUS  (mutating %s::%s)"
            % (target_repo or "?", target_symbol or "?")
        )
        # The mutated root node line.
        lines.append("%s::%s" % (target_repo or "?", target_symbol or "?"))
        lines.append("%sdepended on by:" % _ROOT_ELBOW)

        drawn = 0
        capped = False
        # Render repo groups. We flatten into a single list of (repo, dep) so the
        # last drawn entry across all groups gets the elbow connector.
        flat: List = []
        for repo in ordered_repos:
            for d in by_repo[repo]:
                flat.append(d)

        # Apply the cap.
        if total > cap:
            flat = flat[:cap]
            capped = True

        n = len(flat)
        for idx, d in enumerate(flat):
            is_last = (idx == n - 1) and not capped
            connector = _ELBOW if is_last else _TEE
            repo = getattr(d, "repo", "") or "?"
            file = getattr(d, "file", "") or "?"
            symbol = getattr(d, "symbol", "") or "?"
            lines.append(
                "   %s[%s] %s::%s" % (connector, repo, file, symbol)
            )
            drawn += 1

        if capped:
            more = total - drawn
            lines.append("   %s... +%d more ..." % (_ELBOW, more))

        # Footer: how many Body/Mind files map to this mutation.
        role = _role(target_repo)
        lines.append(
            "%d Body/Mind files mapped to this %s mutation."
            % (total, role)
        )

        return "\n".join(lines)
    except Exception:  # noqa: BLE001 -- pure operator convenience; never raise
        return ""


__all__ = ["render_blast_tree"]
