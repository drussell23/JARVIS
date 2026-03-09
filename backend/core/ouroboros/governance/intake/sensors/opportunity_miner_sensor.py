"""
OpportunityMinerSensor (Sensor D) — Static complexity analysis → observe-only.

Phase 2C.1: ALL D envelopes have requires_human_ack=True. The router parks
them in PENDING_ACK. Auto-submit is enabled in Phase 2C.4 after confidence
formula tuning and audit pass.

Static evidence: AST cyclomatic complexity above threshold.
LLM triage: NOT implemented in Phase 2C.1 (confidence = static only).

Confidence formula (Phase 2C.1, static-only):
    confidence = static_evidence_score × 0.5
    (llm_quality_score, risk_penalty, novelty_penalty added in Phase 2C.4)
"""
from __future__ import annotations

import ast
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

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
        scan_paths: List[str] = None,
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

    async def scan_once(self) -> List[StaticCandidate]:
        """Run one static analysis scan. Returns candidates above threshold."""
        candidates: List[StaticCandidate] = []
        for scan_path in self._scan_paths:
            root = self._repo_root / scan_path
            if not root.exists():
                continue
            for py_file in root.rglob("*.py"):
                rel = str(py_file.relative_to(self._repo_root))
                if rel in self._seen_file_paths:
                    continue
                try:
                    source = py_file.read_text(encoding="utf-8")
                    tree = ast.parse(source)
                except SyntaxError:
                    logger.debug("OpportunityMinerSensor: syntax error in %s, skipping", rel)
                    continue
                except OSError as exc:
                    logger.warning("OpportunityMinerSensor: cannot read %s: %s", rel, exc)
                    continue

                cc = _cyclomatic_complexity(tree)
                if cc < self._threshold:
                    continue

                # Static evidence score: normalize CC against threshold
                static_score = min(1.0, cc / (self._threshold * 2))
                confidence = static_score * 0.5  # Phase 2C.1: static-only formula

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
                    confidence=max(0.1, confidence),
                    urgency="low",
                    evidence={
                        "cyclomatic_complexity": cc,
                        "static_evidence_score": static_score,
                        "signature": rel,
                    },
                    requires_human_ack=True,  # Phase 2C.1: ALWAYS requires human ack
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
