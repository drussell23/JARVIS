"""Pure posture → markdown renderer for CONTEXT_EXPANSION injection.

Standalone so it is trivially testable, authority-free (no imports of
orchestrator / policy / iron_gate), and reusable by both
``strategic_direction.py`` (prompt injection) and any future surface
(e.g. a `/posture explain` flat-text fallback renderer).

Output contract:
  * Section header ``## Current Strategic Posture``
  * Body under 600 characters (tested; Slice 2 budget — prompt bloat
    guard), so it remains additive next to the Manifesto digest.
  * Top 3 contributing signals rendered as a bullet list.
  * Posture-specific advisory line (one sentence) nudging the model's
    priorities without ever blocking — advisory, never authority.
  * Empty-evidence fallback: ``(baseline state — no strong signals)``.

Manifesto alignment:
  * §1 Boundary Principle — advisory prose only, zero authority
  * §8 Observability — every consumer can cite the same rendered block
"""
from __future__ import annotations

import os
from typing import Optional

from backend.core.ouroboros.governance.posture import (
    Posture,
    PostureReading,
)


_POSTURE_ADVISORY: dict = {
    Posture.EXPLORE: (
        "Advisory: ship new capabilities, accept measured risk, favor "
        "breadth over depth."
    ),
    Posture.CONSOLIDATE: (
        "Advisory: finish in-flight threads, prefer graduation over new "
        "arcs, close open WIP."
    ),
    Posture.HARDEN: (
        "Advisory: stabilize before adding features, tighten gates, favor "
        "test coverage and rollback-safe patterns."
    ),
    Posture.MAINTAIN: (
        "Advisory: no strong directional signal — apply standard diligence."
    ),
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def prompt_injection_enabled() -> bool:
    """Active only when master + injection flags are both on."""
    # Local import to keep this module authority-free (direction_inferrer
    # lives in the same authority-free tier).
    from backend.core.ouroboros.governance.direction_inferrer import is_enabled as _master
    if not _master():
        return False
    return _env_bool("JARVIS_POSTURE_PROMPT_INJECTION_ENABLED", True)


def compose_posture_section(
    reading: Optional[PostureReading],
    *,
    top_n: int = 3,
    force: bool = False,
) -> str:
    """Render the posture markdown block, or empty string when disabled.

    Parameters
    ----------
    reading:
        Current posture. ``None`` → empty string.
    top_n:
        Number of evidence entries to list (default 3).
    force:
        Bypass env-gate checks. Used by tests and ``/posture explain``.
    """
    if reading is None:
        return ""
    if not force and not prompt_injection_enabled():
        return ""

    lines = []
    lines.append("## Current Strategic Posture")
    lines.append("")
    lines.append(
        f"**Posture: {reading.posture.value}** "
        f"(confidence {reading.confidence:.2f})"
    )

    # Evidence block — only meaningful contributors (score != 0)
    meaningful = [c for c in reading.evidence if abs(c.contribution_score) > 1e-6]
    if meaningful:
        lines.append("")
        lines.append("Top contributing signals:")
        for c in meaningful[: max(1, int(top_n))]:
            sign = "+" if c.contribution_score >= 0 else "-"
            lines.append(
                f"- {c.signal_name}={c.raw_value:.2f} "
                f"(contrib {sign}{abs(c.contribution_score):.2f})"
            )
    else:
        lines.append("")
        lines.append("Top contributing signals: (baseline state — no strong signals)")

    advisory = _POSTURE_ADVISORY.get(reading.posture) or _POSTURE_ADVISORY[Posture.MAINTAIN]
    lines.append("")
    lines.append(advisory)

    return "\n".join(lines)


__all__ = [
    "compose_posture_section",
    "prompt_injection_enabled",
]
