"""
TodoScannerSensor — Finds unfinished work in the Trinity codebase.

Scans all Python files for TODO, FIXME, HACK, XXX, NOQA, and
DEPRECATED markers. Classifies by priority and routes actionable
items through the Ouroboros pipeline.

Boundary Principle:
  Deterministic: Regex scan, file enumeration, priority classification.
  Agentic: Resolution of the TODO (code generation) via pipeline.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import asyncio

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = float(os.environ.get("JARVIS_TODO_SCAN_INTERVAL_S", "86400"))

# Markers to scan for, with priority weights
_MARKERS: Dict[str, Tuple[str, float]] = {
    "FIXME":      ("high", 0.9),
    "HACK":       ("high", 0.8),
    "XXX":        ("high", 0.7),
    "BUG":        ("high", 0.9),
    "TODO":       ("normal", 0.5),
    "DEPRECATED": ("normal", 0.4),
    "OPTIMIZE":   ("low", 0.3),
    "REFACTOR":   ("low", 0.3),
    "NOTE":       ("low", 0.1),  # Low priority — informational only
}

_MARKER_PATTERN = re.compile(
    r"#\s*(" + "|".join(_MARKERS.keys()) + r")\b[:\s]*(.*)",
    re.IGNORECASE,
)

_SCAN_DIRS = (
    "backend/",
    "tests/",
    "scripts/",
)

_SKIP_DIRS = frozenset({
    "venv", "__pycache__", "node_modules", ".git",
    "site-packages", ".worktrees", "venv_py39_backup",
})


@dataclass
class TodoItem:
    """One TODO/FIXME/HACK found in the codebase."""
    file_path: str
    line_number: int
    marker: str                # TODO, FIXME, HACK, etc.
    text: str                  # The comment text after the marker
    urgency: str               # high, normal, low
    priority: float            # 0.0–1.0
    auto_resolvable: bool = False  # Can Ouroboros fix this?


class TodoScannerSensor:
    """Scans Trinity codebases for unfinished work markers.

    Finds TODO, FIXME, HACK, XXX, BUG, DEPRECATED, OPTIMIZE, REFACTOR
    comments and classifies them by urgency. High-urgency items (FIXME,
    HACK, BUG) are emitted as IntentEnvelopes for Ouroboros resolution.

    Follows the implicit sensor protocol: start(), stop(), scan_once().
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        poll_interval_s: float = _POLL_INTERVAL_S,
        project_root: Optional[Path] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._root = project_root or Path(".")
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seen: set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"todo_scanner_{self._repo}",
        )
        logger.info("[TodoScanner] Started for repo=%s", self._repo)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _poll_loop(self) -> None:
        await asyncio.sleep(180)  # Delay after boot
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("[TodoScanner] Poll error", exc_info=True)
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break

    async def scan_once(self) -> List[TodoItem]:
        """Scan all Python files for TODO markers. Returns found items."""
        items: List[TodoItem] = []

        for scan_dir in _SCAN_DIRS:
            full_dir = self._root / scan_dir
            if not full_dir.exists():
                continue

            for py_file in full_dir.rglob("*.py"):
                # Skip excluded directories
                if any(skip in py_file.parts for skip in _SKIP_DIRS):
                    continue

                try:
                    content = py_file.read_text(errors="replace")
                    for line_num, line in enumerate(content.split("\n"), 1):
                        match = _MARKER_PATTERN.search(line)
                        if match:
                            marker = match.group(1).upper()
                            text = match.group(2).strip()
                            urgency, priority = _MARKERS.get(marker, ("low", 0.1))

                            # Determine if auto-resolvable
                            auto = self._is_auto_resolvable(marker, text)

                            rel_path = str(py_file.relative_to(self._root))
                            items.append(TodoItem(
                                file_path=rel_path,
                                line_number=line_num,
                                marker=marker,
                                text=text[:200],
                                urgency=urgency,
                                priority=priority,
                                auto_resolvable=auto,
                            ))
                except Exception:
                    pass

        # Emit high-priority items as IntentEnvelopes
        emitted = 0
        for item in items:
            if item.urgency not in ("high",):
                continue

            dedup_key = f"{item.file_path}:{item.line_number}:{item.marker}"
            if dedup_key in self._seen:
                continue
            self._seen.add(dedup_key)

            try:
                envelope = make_envelope(
                    source="runtime_health",
                    description=(
                        f"{item.marker} at {item.file_path}:{item.line_number}: "
                        f"{item.text}"
                    ),
                    target_files=(item.file_path,),
                    repo=self._repo,
                    confidence=item.priority,
                    urgency=item.urgency,
                    evidence={
                        "category": "todo_marker",
                        "marker": item.marker,
                        "line_number": item.line_number,
                        "text": item.text,
                        "auto_resolvable": item.auto_resolvable,
                        "sensor": "TodoScannerSensor",
                    },
                    requires_human_ack=not item.auto_resolvable,
                )
                result = await self._router.ingest(envelope)
                if result == "enqueued":
                    emitted += 1
            except Exception:
                pass

        if items:
            by_marker = {}
            for item in items:
                by_marker[item.marker] = by_marker.get(item.marker, 0) + 1
            logger.info(
                "[TodoScanner] Found %d markers: %s (%d emitted)",
                len(items),
                ", ".join(f"{k}={v}" for k, v in sorted(by_marker.items())),
                emitted,
            )
        return items

    @staticmethod
    def _is_auto_resolvable(marker: str, text: str) -> bool:
        """Determine if Ouroboros can auto-resolve this TODO."""
        text_lower = text.lower()

        # Auto-resolvable patterns
        if marker in ("FIXME", "BUG") and any(w in text_lower for w in (
            "import", "typo", "rename", "unused", "missing return",
            "type error", "none check",
        )):
            return True

        # NOT auto-resolvable
        if any(w in text_lower for w in (
            "design", "decide", "discuss", "should we",
            "architecture", "breaking change",
        )):
            return False

        # HACK/XXX are often quick fixes that can be resolved
        if marker in ("HACK", "XXX") and any(w in text_lower for w in (
            "temporary", "workaround", "cleanup", "remove", "replace", "simplify", "quick fix", "todo",
        )):
            return True

        return False

    def health(self) -> Dict[str, Any]:
        return {
            "sensor": "TodoScannerSensor",
            "repo": self._repo,
            "running": self._running,
            "items_seen": len(self._seen),
        }
