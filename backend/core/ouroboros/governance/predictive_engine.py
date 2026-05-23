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
        # ── Slice 12U Phase 2 — Exorcism ──
        # Pre-Slice-12U: _fragility / _test_decay / _resources were
        # called SYNCHRONOUSLY here (no await). Their internal
        # rglob + read_text loops then ran ON the asyncio loop,
        # holding the GIL through the entire scan. bt-2026-05-23-184213
        # tombstone (Slice 12T Part 1) captured this red-handed at
        # predictive_engine.py:131 in _fragility. Three soaks in a
        # row wedged here.
        #
        # Slice 12U makes every signal method async and routes its
        # FS work through the canonical cooperative_fs_io substrate
        # (dedicated advisor-blast executor + cooperative yields).
        # _resources stays sync because shutil.disk_usage is a fast
        # syscall (no scan).
        preds: List[Prediction] = []
        preds.extend(await self._velocity())
        preds.extend(await self._fragility())
        preds.extend(await self._test_decay())
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
        """Files changing too fast = instability risk. Git log, argv-based.

        Slice 12U: per-file ``read_text`` for AST complexity now
        routes through ``read_text_offloaded`` so the read
        dispatches to the dedicated executor and releases the GIL.
        Pre-Slice-12U this called ``full.read_text()`` directly on
        the loop — same sin pattern as the proven _fragility wedge.
        """
        from backend.core.ouroboros.governance.cooperative_fs_io import (  # noqa: E501
            read_text_offloaded,
        )
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
                        # Slice 12U — off-loop read via dedicated
                        # advisor-blast executor (not default pool).
                        content = await read_text_offloaded(full)
                        if content is not None:
                            tree = ast.parse(content)
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

    async def _fragility(self) -> List[Prediction]:
        """High fan-in + recent changes = breakage risk.

        Slice 12U Phase 2 — THE EXORCISED WEDGE. Pre-Slice-12U
        this was sync (``def _fragility``) called sync from
        ``analyze()`` (no await), doing ``self._root.rglob("*.py")``
        + per-file ``py.read_text(errors="replace")`` directly on
        the asyncio loop. The pure-Python regex scan held the GIL
        across thousands of files. LoopDeadman tombstone proved
        this was the wedge in bt-2026-05-23-184213 (and the
        two soaks prior).

        Now async, composing the canonical cooperative_fs_io
        substrate:
          * iter_files_cooperative — bounded async walker with
            cooperative yields every N items (default 64) so the
            heartbeat coroutine + SDK stream consumer get
            scheduling slots throughout
          * read_text_offloaded — per-file read dispatches to the
            dedicated advisor-blast ThreadPoolExecutor (NOT the
            contested default pool — the Slice 12S antipattern)

        Same result shape; same dependency-fragility predictions;
        same skip rules (venv / __pycache__ now via
        default_skip_dirs at the walker level).
        """
        from backend.core.ouroboros.governance.cooperative_fs_io import (  # noqa: E501
            iter_files_cooperative,
            read_text_offloaded,
        )
        preds = []
        try:
            imports: Dict[str, int] = {}
            async for py_str in iter_files_cooperative(
                self._root, pattern="*.py",
            ):
                # Substring exclusion preserved for byte-identical
                # filter behavior — the bounded walker's
                # default_skip_dirs handles the directory-level
                # skips (.git / __pycache__ / .venv / etc.), but the
                # legacy substring check on the full path was less
                # strict and could match nested vendored trees.
                if "venv" in py_str or "__pycache__" in py_str:
                    continue
                try:
                    content = await read_text_offloaded(
                        Path(py_str),
                    )
                    if content is None:
                        continue
                    for m in re.findall(r"from (\S+) import", content):
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

    async def _test_decay(self) -> List[Prediction]:
        """Stale tests referencing moved/deleted code.

        Slice 12U — async, iterates ``tests/`` via the
        cooperative substrate. The walk is small (single
        directory) but the immunization is free and consistent
        with the rest of the engine's signal methods.
        """
        from backend.core.ouroboros.governance.cooperative_fs_io import (  # noqa: E501
            iter_files_cooperative,
        )
        preds = []
        tests = self._root / "tests"
        if not tests.exists(): return []
        try:
            async for tf_str in iter_files_cooperative(
                tests, pattern="*.py",
            ):
                tf = Path(tf_str)
                # Match the legacy test_*.py filter — the substrate
                # yields all .py files; we add the prefix filter
                # here for byte-identical behavior.
                if not tf.name.startswith("test_"):
                    continue
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
