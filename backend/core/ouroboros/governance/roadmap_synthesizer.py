"""Slice 122 Phase 2 — Dynamic Roadmap Synthesizer.

Audits the system's current state and drafts an UNSIGNED, authority-free
`.jarvis/roadmap.draft.yaml` for the operator to review and sign. The draft is a
*proposal*: it carries no signature, so on its own it grants nothing (Slice 120
fails it closed). Authority is conferred only when the operator signs it
(Slice 122 ``sovereign_keys.sign_roadmap``).

Conservative by construction (the operator can widen, never the synthesizer):
  • scopes are drawn from a SAFE allow-list (docs, tests, observability, …) —
    NEVER governance/cage/Order-2 scopes (those are un-signable anyway, §1),
  • ``max_recursion_depth`` is clamped to the autonomous Slice-104 cap,
  • ``max_budget_usd`` defaults small,
  • ``expires_at`` is a bounded ~12-month window (no open-ended authority).
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_DEFAULT_DRAFT_PATH = ".jarvis/roadmap.draft.yaml"
_DEFAULT_WINDOW_DAYS = 365
_DEFAULT_BUDGET_USD = 25.0

# The ONLY scopes the synthesizer will propose for unattended auto-approval.
# Deliberately excludes anything authority-bearing — those hit the Slice-120
# un-signable floor regardless and must never be auto-approved.
_SAFE_SCOPE_ALLOWLIST = (
    "docs",
    "test-hardening",
    "observability",
    "type-annotations",
    "lint-cleanup",
    "dependency-pin",
)


def _recent_commit_scopes(limit: int = 50) -> List[str]:
    """Best-effort audit of recent conventional-commit scopes (read-only)."""
    try:
        out = subprocess.run(
            ["git", "log", f"-{limit}", "--pretty=%s"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
        scopes = set()
        for line in out.splitlines():
            if "(" in line and ")" in line and line.index("(") < line.index(")"):
                scopes.add(line[line.index("(") + 1: line.index(")")].strip().lower())
        return sorted(scopes)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[RoadmapSynth] commit audit skipped: %s", exc)
        return []


def _safe_recursion_depth() -> int:
    try:
        from backend.core.ouroboros.governance.recursion_depth_gate import max_recursion_depth

        return int(max_recursion_depth())
    except Exception:  # noqa: BLE001
        return 3


def synthesize_draft(
    *,
    now: int,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    budget_usd: float = _DEFAULT_BUDGET_USD,
) -> Dict[str, Any]:
    """Produce the unsigned draft roadmap dict (authority-free until signed)."""
    observed = _recent_commit_scopes()
    return {
        "version": "layer4.roadmap.draft.1",
        "generated_at": now,
        "expires_at": now + window_days * 86400,
        "authorized_scopes": list(_SAFE_SCOPE_ALLOWLIST),
        "max_budget_usd": float(budget_usd),
        "max_recursion_depth": _safe_recursion_depth(),
        "audit": {
            "observed_commit_scopes": observed,
            "note": (
                "UNSIGNED DRAFT. Grants nothing until the operator signs it with "
                "the Ed25519 private key (sovereign_keys.sign_roadmap). The "
                "un-signable floor (§1) still excludes Order-2/M10, recursion "
                "breach, governance/cage, and APPROVAL_REQUIRED ops from any "
                "auto-approval — review scopes/budget/window before signing."
            ),
        },
    }


def write_draft(draft: Dict[str, Any], path: str = _DEFAULT_DRAFT_PATH) -> Path:
    """Serialize the draft to YAML for operator review."""
    import yaml

    p = Path(os.getenv("JARVIS_ROADMAP_DRAFT_PATH", path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(draft, sort_keys=True, default_flow_style=False), encoding="utf-8")
    return p


__all__ = ["synthesize_draft", "write_draft"]


if __name__ == "__main__":  # pragma: no cover - operator entrypoint
    import json

    draft = synthesize_draft(now=int(time.time()))
    out = write_draft(draft)
    print(f"Drafted unsigned roadmap → {out}")
    print(json.dumps(draft, indent=2))
    print("\n# Review, then sign with: python3 -m backend.core.ouroboros.governance.sovereign_keys sign")
