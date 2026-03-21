# backend/core/ouroboros/governance/correction_writer.py
"""CorrectionWriter — appends human rejection reasons to OUROBOROS.md.

GAP 8: when CLIApprovalProvider.reject() is called with a reason, the
correction is appended to <project_root>/OUROBOROS.md under a persistent
## Auto-Learned Corrections section.  This feeds directly into GAP 3
(ContextMemoryLoader) so the AI learns from human corrections automatically.

This module is intentionally standalone (no imports from governance core)
so it can be called from approval_provider.py without circular imports.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SECTION_HEADER = "## Auto-Learned Corrections"


def write_correction(
    project_root: Path,
    op_id: str,
    reason: str,
    timestamp: Optional[datetime] = None,
) -> None:
    """Append a human correction to <project_root>/OUROBOROS.md.

    Silently swallows all IO errors so approval_provider.reject() never raises
    due to a filesystem issue.

    Parameters
    ----------
    project_root:
        Root of the repository; OUROBOROS.md is at project_root/OUROBOROS.md.
    op_id:
        The operation ID (used as a reference in the correction entry).
    reason:
        Free-text rejection reason provided by the human approver.
    timestamp:
        Timestamp of the rejection (defaults to UTC now).
    """
    if not reason or not reason.strip():
        return

    ts = timestamp or datetime.now(tz=timezone.utc)
    date_str = ts.strftime("%Y-%m-%d")
    entry = f"- {date_str} op:{op_id}: {reason.strip()}"

    try:
        md_path = Path(project_root) / "OUROBOROS.md"

        if md_path.exists():
            existing = md_path.read_text(encoding="utf-8")
        else:
            existing = ""

        if _SECTION_HEADER in existing:
            # Append entry after the section header (preserving following content)
            updated = existing.rstrip() + "\n" + entry + "\n"
        else:
            # Create section at end of file
            separator = "\n\n" if existing.strip() else ""
            updated = existing.rstrip() + separator + f"\n{_SECTION_HEADER}\n{entry}\n"

        md_path.write_text(updated, encoding="utf-8")
        logger.info("[CorrectionWriter] Appended correction for op=%s to %s", op_id, md_path)

    except Exception as exc:
        logger.warning("[CorrectionWriter] Failed to write correction for op=%s: %s", op_id, exc)
