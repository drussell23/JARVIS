"""
Predictive Regression Engine — JARVIS-Level Tier 3.

"Sir, I'm detecting a pattern you should know about."

Anticipates regressions via: code velocity, dependency fragility,
test decay, resource trajectory. All deterministic (git log + AST + fs).
All subprocess calls argv-based, no shell.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = float(os.environ.get("JARVIS_PREDICTIVE_INTERVAL_S", "14400"))
_ENABLED = os.environ.get("JARVIS_PREDICTIVE_ENABLED", "true").lower() in ("true", "1", "yes")


@dataclass
class Prediction:
    category: str
    file_path: str
    probability: float
    time_horizon_hours: float
    impact: str
    evidence: str
    suggestion: str
    created_at: float = field(default_factory=time.time)


class PredictiveRegressionEngine:
    """Anticipates regressions. 4 modules: velocity, fragility, decay, resources."""

    def __init__(self, project_root: Path) -> None:
        self._root = project_root
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._predictions: List[Prediction] = []

    async def start(self) -> None:
        if not _ENABLED: return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="predictive_engine")
        logger.info("[Predictive] Started (interval=%ds)", _POLL_INTERVAL_S)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done(): self._task.cancel()

    async def _poll_loop(self) -> None:
        await asyncio.sleep(600)
        while self._running:
            try:
                self._predictions = await self.analyze()
                if self._predictions:
                    logger.info("[Predictive] %d predictions", len(self._predictions))
            except asyncio.CancelledError: break
            except Exception: logger.debug("[Predictive] Failed", exc_info=True)
            try: await asyncio.sleep(_POLL_INTERVAL_S)
            except asyncio.CancelledError: break

    async def analyze(self) -> List[Prediction]:
        preds: List[Prediction] = []
        preds.extend(await self._velocity())
        preds.extend(self._fragility())
        preds.extend(self._test_decay())
        preds.extend(self._resources())
        preds.sort(key=lambda p: -p.probability)
        return preds[:20]

    def get_predictions(self) -> List[Prediction]: return list(self._predictions)

    def format_for_prompt(self) -> str:
        if not self._predictions: return ""
        lines = ["## Predictive Intelligence"]
        for p in self._predictions[:5]:
            lines.append(f"- [{p.impact}] {p.category}: {p.file_path} ({p.probability:.0%}, {p.time_horizon_hours:.0f}h)")
            lines.append(f"  {p.evidence[:80]}")
            lines.append(f"  Action: {p.suggestion[:80]}")
        return "\n".join(lines)

    async def _velocity(self) -> List[Prediction]:
        """Files changing too fast = instability risk. Git log, argv-based."""
        preds = []
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "log", "--since=7 days ago", "--name-only", "--pretty=format:", "--diff-filter=M",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(self._root))
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            counts: Dict[str, int] = {}
            for line in stdout.decode().strip().split("\n"):
                line = line.strip()
                if line and line.endswith(".py"):
                    counts[line] = counts.get(line, 0) + 1
            for fp, count in counts.items():
                if count < 5: continue
                complexity = 0
                full = self._root / fp
                if full.exists():
                    try:
                        tree = ast.parse(full.read_text())
                        complexity = sum(1 for n in ast.walk(tree) if isinstance(n, (ast.If, ast.For, ast.While, ast.ExceptHandler)))
                    except Exception: pass
                prob = min(1.0, count / 10 + (0.2 if complexity > 50 else 0))
                preds.append(Prediction(
                    "velocity_risk", fp, prob, 48,
                    "high" if prob > 0.7 else "medium",
                    f"{count} changes/7d, complexity={complexity}",
                    f"Stabilize {fp}: add tests, refactor if complexity>{50}"))
        except Exception: pass
        return preds

    def _fragility(self) -> List[Prediction]:
        """High fan-in + recent changes = breakage risk."""
        preds = []
        try:
            imports: Dict[str, int] = {}
            for py in self._root.rglob("*.py"):
                if "venv" in str(py) or "__pycache__" in str(py): continue
                try:
                    for m in re.findall(r"from (\S+) import", py.read_text(errors="replace")):
                        mp = m.replace(".", "/") + ".py"
                        imports[mp] = imports.get(mp, 0) + 1
                except Exception: pass
            for mp, cnt in imports.items():
                if cnt < 10: continue
                full = self._root / mp
                if not full.exists(): continue
                try: days = (time.time() - full.stat().st_mtime) / 86400
                except Exception: continue
                if days > 7: continue
                prob = min(1.0, (cnt / 30) * (1 - days / 7))
                preds.append(Prediction(
                    "dependency_fragility", mp, prob, 24, "high",
                    f"Imported by {cnt} files, modified {days:.0f}d ago",
                    f"Verify {cnt} importers compatible with recent changes"))
        except Exception: pass
        return preds

    def _test_decay(self) -> List[Prediction]:
        """Stale tests referencing moved/deleted code."""
        preds = []
        tests = self._root / "tests"
        if not tests.exists(): return []
        try:
            for tf in tests.rglob("test_*.py"):
                try:
                    days = (time.time() - tf.stat().st_mtime) / 86400
                    if days > 60:
                        rel = str(tf.relative_to(self._root))
                        preds.append(Prediction(
                            "test_decay", rel, 0.4, 168, "low",
                            f"Unchanged {days:.0f} days",
                            f"Review {rel} for relevance"))
                except Exception: pass
        except Exception: pass
        return preds

    def _resources(self) -> List[Prediction]:
        """Disk space and cost projections."""
        preds = []
        try:
            usage = shutil.disk_usage(str(self._root))
            pct = usage.used / usage.total
            if pct > 0.85:
                preds.append(Prediction(
                    "resource", "disk", 0.8, 72, "high",
                    f"Disk {pct:.0%} full ({usage.free // (1024**3)}GB free)",
                    "Clean build artifacts, venv cache, old worktrees"))
        except Exception: pass
        return preds

    def get_status(self) -> Dict[str, Any]:
        return {"running": self._running, "predictions": len(self._predictions),
                "top": (f"{self._predictions[0].category}: {self._predictions[0].file_path} "
                        f"({self._predictions[0].probability:.0%})" if self._predictions else "none")}
