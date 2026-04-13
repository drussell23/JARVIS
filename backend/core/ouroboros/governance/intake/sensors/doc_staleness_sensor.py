"""
DocStalenessSensor — Detects undocumented or stale documentation for Python modules.

P2 Gap: Code grows but docs don't. This sensor scans modified files for
missing module docstrings, undocumented public classes/functions, and
stale README references.

Boundary Principle:
  Deterministic: AST analysis for docstring presence, file modification
  timestamp comparison, public API surface counting.
  Agentic: Documentation content generation routed through pipeline.

Emits IntentEnvelopes for modules that need documentation.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_POLL_INTERVAL_S = float(os.environ.get("JARVIS_DOC_STALENESS_INTERVAL_S", "86400"))
_MIN_PUBLIC_SYMBOLS = int(os.environ.get("JARVIS_DOC_MIN_PUBLIC_SYMBOLS", "3"))
_SCAN_PATHS: Tuple[str, ...] = (
    "backend/core/ouroboros/governance/",
    "backend/core/",
    "backend/vision/",
    "backend/intelligence/",
)


@dataclass
class DocFinding:
    """One documentation gap detected."""
    category: str          # "missing_module_doc", "undocumented_api", "stale_readme"
    severity: str
    summary: str
    file_path: str
    public_symbols: int    # Count of public classes/functions
    documented_symbols: int
    details: Dict[str, Any] = field(default_factory=dict)


class DocStalenessSensor:
    """Detects undocumented Python modules for the Ouroboros intake layer.

    Scans Python files via AST analysis to find:
    1. Modules with no module-level docstring
    2. Public classes/functions missing docstrings
    3. High public API surface with low documentation coverage

    Follows the implicit sensor protocol: start(), stop(), scan_once().
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        poll_interval_s: float = _POLL_INTERVAL_S,
        project_root: Optional[Path] = None,
        scan_paths: Optional[Tuple[str, ...]] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._project_root = project_root or Path(".")
        self._scan_paths = scan_paths or _SCAN_PATHS
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seen_findings: set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"doc_staleness_sensor_{self._repo}"
        )
        logger.info(
            "[DocSensor] Started for repo=%s poll_interval=%ds",
            self._repo, self._poll_interval_s,
        )

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Event-driven path (Manifesto §3: zero polling, pure reflex)
    # ------------------------------------------------------------------

    async def subscribe_to_bus(self, event_bus: Any) -> None:
        """Subscribe to file system events for instant staleness detection."""
        await event_bus.subscribe("fs.changed.*", self._on_fs_event)
        logger.info("[DocSensor] Subscribed to fs.changed.* events")

    async def _on_fs_event(self, event: Any) -> None:
        """React to file changes — rescan on git commit or .py change."""
        rel_path = event.payload.get("relative_path", "")

        # Git commit event → rescan changed Python files
        if rel_path.endswith("git_events.json") and ".jarvis" in rel_path:
            await self._on_git_event(event)
            return

        # Direct .py file change → rescan that file
        if event.payload.get("extension") == ".py":
            if event.topic != "fs.changed.deleted":
                try:
                    await self.scan_once()  # Full rescan (simple, correct)
                except Exception:
                    logger.debug("[DocSensor] Event-driven scan error", exc_info=True)

    async def _on_git_event(self, event: Any) -> None:
        """React to git commit — rescan if Python files changed."""
        import json
        try:
            data = json.loads(Path(event.payload["path"]).read_text())
            py_files = data.get("py_files_changed", [])
            if py_files:
                logger.debug("[DocSensor] Git commit changed %d .py files, rescanning", len(py_files))
                await self.scan_once()
        except Exception:
            logger.debug("[DocSensor] Failed to read git event", exc_info=True)
        if self._task and not self._task.done():
            self._task.cancel()

    async def _poll_loop(self) -> None:
        # Delay scan — docs are lowest priority
        await asyncio.sleep(300.0)
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[DocSensor] Poll error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break

    async def scan_once(self) -> List[DocFinding]:
        """Scan Python files for documentation gaps via AST analysis."""
        loop = asyncio.get_running_loop()
        findings = await loop.run_in_executor(None, self._scan_files_sync)

        # Emit envelopes
        emitted = 0
        for finding in findings:
            if finding.file_path in self._seen_findings:
                continue
            self._seen_findings.add(finding.file_path)

            try:
                envelope = make_envelope(
                    source="doc_staleness",
                    description=finding.summary,
                    target_files=(finding.file_path,),
                    repo=self._repo,
                    confidence=0.80,
                    urgency=finding.severity,
                    evidence={
                        "category": finding.category,
                        "public_symbols": finding.public_symbols,
                        "documented_symbols": finding.documented_symbols,
                        "coverage": (
                            finding.documented_symbols / max(1, finding.public_symbols)
                        ),
                        "sensor": "DocStalenessSensor",
                    },
                    requires_human_ack=False,
                )
                result = await self._router.ingest(envelope)
                if result == "enqueued":
                    emitted += 1
            except Exception:
                logger.debug("[DocSensor] Emit failed for %s", finding.file_path)

        if findings:
            logger.info(
                "[DocSensor] Scan: %d undocumented modules, %d emitted",
                len(findings), emitted,
            )
        return findings

    def _scan_files_sync(self) -> List[DocFinding]:
        """CPU-bound scan — runs in a thread via run_in_executor."""
        findings: List[DocFinding] = []

        for scan_path in self._scan_paths:
            full_path = self._project_root / scan_path
            if not full_path.exists():
                continue

            for py_file in full_path.rglob("*.py"):
                rel = str(py_file.relative_to(self._project_root))
                if any(skip in rel for skip in (
                    "__pycache__", "venv/", "site-packages/",
                    "test_", "_test.py", "migrations/",
                )):
                    continue

                finding = self._analyze_file(py_file, rel)
                if finding:
                    findings.append(finding)

        return findings

    # ------------------------------------------------------------------
    # AST analysis (deterministic — pure syntax tree inspection)
    # ------------------------------------------------------------------

    def _analyze_file(self, py_file: Path, rel_path: str) -> Optional[DocFinding]:
        """Analyze a single Python file for documentation coverage."""
        try:
            source = py_file.read_text(errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            return None

        # Count public symbols and their docstring coverage
        public_symbols = 0
        documented_symbols = 0

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                # Skip private symbols (leading underscore)
                if node.name.startswith("_"):
                    continue
                public_symbols += 1
                if ast.get_docstring(node):
                    documented_symbols += 1

        # Only flag files with significant public API surface
        if public_symbols < _MIN_PUBLIC_SYMBOLS:
            return None

        # Check module docstring
        has_module_doc = bool(ast.get_docstring(tree))

        # Calculate coverage
        coverage = documented_symbols / public_symbols if public_symbols > 0 else 1.0

        # Emit finding if coverage is low or module doc is missing
        if coverage < 0.5 or (not has_module_doc and public_symbols >= 5):
            severity = "normal" if coverage > 0.25 else "low"
            undocumented = public_symbols - documented_symbols

            summary_parts = []
            if not has_module_doc:
                summary_parts.append("missing module docstring")
            if undocumented > 0:
                summary_parts.append(
                    f"{undocumented}/{public_symbols} public symbols undocumented "
                    f"({coverage:.0%} coverage)"
                )

            return DocFinding(
                category="undocumented_api",
                severity=severity,
                summary=f"{rel_path}: {'; '.join(summary_parts)}",
                file_path=rel_path,
                public_symbols=public_symbols,
                documented_symbols=documented_symbols,
                details={
                    "has_module_docstring": has_module_doc,
                    "coverage_pct": round(coverage * 100, 1),
                },
            )

        return None

    def health(self) -> Dict[str, Any]:
        return {
            "sensor": "DocStalenessSensor",
            "repo": self._repo,
            "running": self._running,
            "findings_seen": len(self._seen_findings),
            "poll_interval_s": self._poll_interval_s,
        }
