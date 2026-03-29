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
import os
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
        max_candidates_per_scan: int = 0,
    ) -> None:
        self._repo_root = repo_root
        self._router = router
        self._scan_paths = scan_paths or ["."]
        self._threshold = complexity_threshold
        self._repo = repo
        self._poll_interval_s = poll_interval_s
        self._running = False
        self._seen_file_paths: set[str] = set()
        # Per-scan cap: only queue the N most complex files. 0 = no cap.
        # Prevents intake flooding on large codebases. Highest CC first
        # ensures intelligence is deployed where it creates most leverage.
        self._max_per_scan = max_candidates_per_scan or int(
            os.environ.get("JARVIS_MINER_MAX_PER_SCAN", "10")
        )

    # v350.4: Third-party / non-project directory segments that must
    # never be scanned. These are structural boundaries — scanning torch,
    # numpy, or joblib source is not production governance.
    _NON_PROJECT_SEGMENTS = frozenset({
        "venv", ".venv", "env", ".env",
        "site-packages", "dist-packages",
        "node_modules", ".git", "__pycache__",
        ".tox", ".nox", ".mypy_cache", ".pytest_cache",
        "tests", "test", "testing", "fixtures",
        "build", "dist", "eggs", ".eggs",
    })

    def _is_production_code(self, py_file: Path, scan_root: Path) -> bool:
        """Return True if the file is production code, not a loose script.

        Structural heuristic with boundary enforcement:
        1. Files inside third-party directories (venv, site-packages,
           node_modules) are always excluded — they are not project code.
        2. Depth is measured from the **repo root**. Files at depth <= 1
           (e.g. ``backend/demo.py``) are skipped as loose scripts.
        3. Files at depth >= 2 are production code inside sub-packages.

        Special files (__init__.py, conftest.py) at any depth are admitted
        (but still rejected if inside a third-party directory).
        """
        # Structural boundary: never scan third-party code
        parts = py_file.relative_to(self._repo_root).parts if self._repo_root in py_file.parents else py_file.parts
        if self._NON_PROJECT_SEGMENTS.intersection(parts):
            return False

        name = py_file.name

        # Test files are not production code regardless of directory.
        # Standard Python convention: test_*.py and *_test.py are tests.
        if name.startswith("test_") or name.endswith("_test.py"):
            return False

        # Scripts directory segments — utility/tooling, not production
        if "scripts" in parts:
            return False

        if name in ("__init__.py", "__main__.py", "conftest.py"):
            return True
        try:
            relative = py_file.relative_to(self._repo_root)
            depth = len(relative.parts) - 1  # -1 for the filename itself
            return depth >= 2  # e.g. backend/core/foo.py = depth 2
                               #      backend/demo.py     = depth 1
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
                candidates.append(candidate)

        # Sort by CC descending — highest complexity first (most leverage).
        candidates.sort(key=lambda c: c.cyclomatic_complexity, reverse=True)

        # Per-scan cap: only ingest the top N to prevent intake flooding.
        if self._max_per_scan > 0 and len(candidates) > self._max_per_scan:
            dropped = len(candidates) - self._max_per_scan
            candidates = candidates[:self._max_per_scan]
            logger.info(
                "OpportunityMinerSensor: capped to top %d candidates "
                "(dropped %d lower-CC files)",
                self._max_per_scan, dropped,
            )

        # Ingest the prioritized candidates.
        ingested: List[StaticCandidate] = []
        for candidate in candidates:
            rel = candidate.file_path
            cc = candidate.cyclomatic_complexity
            confidence = candidate.static_evidence_score

            envelope = make_envelope(
                source="ai_miner",
                description=f"High complexity detected in {rel} (CC={cc})",
                target_files=(rel,),
                repo=self._repo,
                confidence=max(0.1, confidence),
                urgency="low",
                evidence={
                    "cyclomatic_complexity": cc,
                    "static_evidence_score": confidence,
                    "signature": rel,
                },
                requires_human_ack=True,  # AC2 safety invariant
            )
            try:
                result = await self._router.ingest(envelope)
                if result in ("enqueued", "pending_ack"):
                    self._seen_file_paths.add(rel)
                    ingested.append(candidate)
                    logger.info(
                        "OpportunityMinerSensor: queued %s (CC=%d, result=%s)",
                        rel, cc, result,
                    )
            except Exception:
                logger.exception(
                    "OpportunityMinerSensor: ingest failed for %s", rel
                )

        if skipped_non_package > 0 or len(candidates) > 0:
            logger.info(
                "OpportunityMinerSensor: scanned %d files, "
                "skipped %d non-package, found %d above threshold, "
                "ingested %d (cap=%d)",
                scanned, skipped_non_package, len(candidates),
                len(ingested), self._max_per_scan,
            )
        return ingested

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
