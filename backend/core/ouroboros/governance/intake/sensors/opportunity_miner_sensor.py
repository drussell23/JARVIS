"""
OpportunityMinerSensor (Sensor D) — Static complexity analysis → observe-only.

Safety invariant (AC2): ALL miner-generated envelopes require human
acknowledgment before execution.  AI-discovered opportunities must
always be human-approved — auto-submit does NOT apply to this sensor.

Static evidence: AST cyclomatic complexity above threshold.

Confidence formula:
    confidence = static_evidence_score (full weight)
    Used for envelope prioritisation; does NOT affect requires_human_ack.
"""
from __future__ import annotations

import ast
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)


@dataclass
class StaticCandidate:
    file_path: str
    cyclomatic_complexity: int
    static_evidence_score: float


def _cyclomatic_complexity(tree: ast.AST) -> int:
    """Count branching nodes (if/elif/for/while/with/try/except/and/or)."""
    _BRANCH_NODES = (
        ast.If, ast.For, ast.While, ast.With,
        ast.ExceptHandler, ast.BoolOp,
    )
    count = 1  # baseline
    for node in ast.walk(tree):
        if isinstance(node, _BRANCH_NODES):
            count += 1
    return count


class OpportunityMinerSensor:
    """Scans Python files for high cyclomatic complexity and produces envelopes.

    Parameters
    ----------
    repo_root:
        Repository root.
    router:
        UnifiedIntakeRouter.
    scan_paths:
        List of paths (relative to repo_root) to scan recursively for .py files.
    complexity_threshold:
        Minimum cyclomatic complexity to produce an envelope.
    repo:
        Repository name.
    poll_interval_s:
        Seconds between scans in background mode.
    """

    def __init__(
        self,
        repo_root: Path,
        router: Any,
        scan_paths: Optional[List[str]] = None,
        complexity_threshold: int = 10,
        repo: str = "jarvis",
        poll_interval_s: float = 3600.0,
    ) -> None:
        self._repo_root = repo_root
        self._router = router
        self._scan_paths = scan_paths or ["."]
        self._threshold = complexity_threshold
        self._repo = repo
        self._poll_interval_s = poll_interval_s
        self._running = False
        self._seen_file_paths: set[str] = set()

    def _is_production_code(self, py_file: Path, scan_root: Path) -> bool:
        """Return True if the file is production code, not a loose script.

        Structural heuristic — zero hardcoded exclusion lists.
        Depth is measured from the **repo root**, not the scan root.
        Files at depth <= 1 from repo root (e.g. ``backend/demo.py``)
        are skipped — they are standalone scripts, demos, migration
        tools, and one-off utilities.  Files at depth >= 2
        (e.g. ``backend/core/prime_router.py``) are production code
        inside proper sub-packages.

        This automatically adapts: when someone creates a new package
        ``backend/new_feature/``, its files are included.  When someone
        drops a one-off script in ``backend/``, it's excluded.

        When the scan path itself is already a sub-package
        (e.g. ``backend/core/``), all files inside are at depth >= 2
        and are admitted.

        Special files (__init__.py, conftest.py) at any depth are admitted.
        """
        name = py_file.name
        if name in ("__init__.py", "__main__.py", "conftest.py"):
            return True
        try:
            relative = py_file.relative_to(self._repo_root)
            depth = len(relative.parts) - 1  # -1 for the filename itself
            return depth >= 2  # e.g. backend/core/foo.py = depth 2 ✓
                               #      backend/demo.py     = depth 1 ✗
        except ValueError:
            return True  # Not under repo_root — admit conservatively

    async def scan_once(self) -> List[StaticCandidate]:
        """Run one static analysis scan. Returns candidates above threshold.

        Only scans files that belong to Python packages — loose scripts,
        demos, migration tools, and one-off fixes are automatically
        excluded by structural detection (no hardcoded patterns).
        """
        candidates: List[StaticCandidate] = []
        scanned = 0
        skipped_non_package = 0

        for scan_path in self._scan_paths:
            root = self._repo_root / scan_path
            if not root.exists():
                continue
            for py_file in root.rglob("*.py"):
                rel = str(py_file.relative_to(self._repo_root))
                if rel in self._seen_file_paths:
                    continue

                # Structural filter: only scan production code, not loose scripts.
                if not self._is_production_code(py_file, root):
                    skipped_non_package += 1
                    continue

                scanned += 1
                try:
                    source = py_file.read_text(encoding="utf-8")
                    tree = ast.parse(source)
                except SyntaxError:
                    logger.debug("OpportunityMinerSensor: syntax error in %s, skipping", rel)
                    continue
                except (OSError, UnicodeDecodeError) as exc:
                    logger.debug("OpportunityMinerSensor: cannot read %s: %s", rel, exc)
                    continue

                cc = _cyclomatic_complexity(tree)
                if cc < self._threshold:
                    continue

                # Static evidence score: normalize CC against threshold.
                # Used for envelope prioritisation (not for ACK gating).
                static_score = min(1.0, cc / (self._threshold * 2))
                confidence = static_score

                candidate = StaticCandidate(
                    file_path=rel,
                    cyclomatic_complexity=cc,
                    static_evidence_score=static_score,
                )

                envelope = make_envelope(
                    source="ai_miner",
                    description=f"High complexity detected in {rel} (CC={cc})",
                    target_files=(rel,),
                    repo=self._repo,
                    confidence=max(0.1, confidence),  # clamp for envelope; routing uses pre-clamp value
                    urgency="low",
                    evidence={
                        "cyclomatic_complexity": cc,
                        "static_evidence_score": static_score,
                        "signature": rel,
                    },
                    requires_human_ack=True,  # AC2 safety invariant: miner always requires human ACK
                )
                try:
                    result = await self._router.ingest(envelope)
                    if result in ("enqueued", "pending_ack"):
                        self._seen_file_paths.add(rel)
                        candidates.append(candidate)
                        logger.info(
                            "OpportunityMinerSensor: queued %s (CC=%d, result=%s)",
                            rel, cc, result,
                        )
                except Exception:
                    logger.exception(
                        "OpportunityMinerSensor: ingest failed for %s", rel
                    )

        if skipped_non_package > 0:
            logger.info(
                "OpportunityMinerSensor: scanned %d package files, "
                "skipped %d non-package scripts, queued %d candidates",
                scanned, skipped_non_package, len(candidates),
            )
        return candidates

    async def start(self) -> None:
        """Start background scanning loop."""
        self._running = True
        asyncio.create_task(self._poll_loop(), name="opportunity_miner_poll")

    def stop(self) -> None:
        self._running = False

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.scan_once()
            except Exception:
                logger.exception("OpportunityMinerSensor: poll error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break
