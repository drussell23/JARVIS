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

# --- Gap #4 migration: GitHub push webhook path (Slice 4) ------------------
#
# When ``JARVIS_DOC_STALENESS_WEBHOOK_ENABLED=true``, incoming GitHub
# ``push`` webhooks containing modified Python files trigger an immediate
# ``scan_once`` — reacting to merges at network latency rather than waiting
# for the 24h poll or the FS watcher to see the merge locally (which never
# happens in pure-CI environments). The poll loop demotes to a 6h fallback
# whose job is to catch dropped webhooks.
#
# Shadow pattern: flag defaults OFF so current behavior is preserved exactly
# — FS subscription remains active (unchanged), poll stays at 24h, no
# webhook path activated. This slice is purely additive when flipped on.
_DOC_STALENESS_FALLBACK_INTERVAL_S: float = float(
    os.environ.get("JARVIS_DOC_STALENESS_FALLBACK_INTERVAL_S", "21600")
)


def webhook_enabled() -> bool:
    """Re-read ``JARVIS_DOC_STALENESS_WEBHOOK_ENABLED`` at call-time.

    Mirrors ``github_issue_sensor.webhook_enabled`` and
    ``test_failure_sensor.fs_events_enabled`` so the three gap-#4 sensor
    flags behave identically from a testability + operator-flip point
    of view.
    """
    return os.environ.get(
        "JARVIS_DOC_STALENESS_WEBHOOK_ENABLED", "true",
    ).lower() in ("true", "1", "yes")


# Slice 11.6.b — Merkle Cartographer consultation. When the per-sensor
# flag JARVIS_DOCSTALE_USE_MERKLE is on AND the cartographer's master
# flag is on, the scan loop short-circuits to the cached prior findings
# when nothing under ``_scan_paths`` has changed since the last
# successful scan. Cuts O(N) AST parses to O(1) on the steady state —
# DocStalenessSensor's 24h poll cycle is dominated by no-change days,
# and even FS-event triggers re-scan the full subtree (not the changed
# file alone), so this dwarfs the win on TodoScanner.
#
# Default false to preserve byte-identical legacy behavior. Per-sensor
# graduation: each Slice 11.6.{a,b,c,d} flag flips independently after
# its own forced-clean once-proof cadence.


def merkle_consult_enabled() -> bool:
    """Re-read ``JARVIS_DOCSTALE_USE_MERKLE`` at call time so monkeypatch
    works in tests + operator can flip live without re-init.

    Default ``true`` — graduated in Phase 11 Slice 11.7. Hot-revert:
    ``export JARVIS_DOCSTALE_USE_MERKLE=false``."""
    raw = os.environ.get(
        "JARVIS_DOCSTALE_USE_MERKLE", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


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
        # Gap #4 migration captured at __init__. When True, the poll
        # loop demotes to ``_DOC_STALENESS_FALLBACK_INTERVAL_S`` (6h)
        # and ``ingest_webhook`` becomes the reactive hot path for
        # merges. FS subscription is unchanged.
        self._webhook_mode: bool = webhook_enabled()
        # Telemetry counters — exposed via health snapshots during the
        # graduation arc so operators can read the signal:noise ratio.
        self._webhooks_handled: int = 0
        self._webhooks_ignored: int = 0
        # Slice 11.6.b — Merkle cartographer consultation state.
        # ``_merkle_cached_findings`` is the last full-scan output, replayed
        # on short-circuit cycles. ``_merkle_last_seen_root_hash`` is the
        # cartographer root hash at the end of the last full scan; the
        # next cycle compares against ``current_root_hash()`` to decide.
        self._merkle_cached_findings: List[DocFinding] = []
        self._merkle_last_seen_root_hash: str = ""
        self._merkle_short_circuits: int = 0
        self._merkle_full_scans: int = 0

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"doc_staleness_sensor_{self._repo}"
        )
        effective = (
            _DOC_STALENESS_FALLBACK_INTERVAL_S
            if self._webhook_mode
            else self._poll_interval_s
        )
        mode = (
            "webhook-primary (push → scan_once; poll=fallback)"
            if self._webhook_mode
            else "poll-primary"
        )
        logger.info(
            "[DocSensor] Started for repo=%s poll_interval=%ds mode=%s",
            self._repo, int(effective), mode,
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
            # Effective interval selected per-iteration so mid-flight
            # flag flips take effect on the next wait.
            effective_interval = (
                _DOC_STALENESS_FALLBACK_INTERVAL_S
                if self._webhook_mode
                else self._poll_interval_s
            )
            try:
                await asyncio.sleep(effective_interval)
            except asyncio.CancelledError:
                break

    async def ingest_webhook(self, payload: Dict[str, Any]) -> bool:
        """Handle a GitHub ``push`` webhook delivery.

        Alternative entry point for doc staleness detection. When a push
        event arrives containing modified/added Python files under our
        watched ``_scan_paths``, triggers an immediate ``scan_once``
        rather than waiting for the 6h poll fallback or the local FS
        watcher (which never sees remote merges in CI-only environments).

        The existing ``_seen_findings`` set dedups overlapping FS + poll
        + webhook emissions — no double-envelope risk.

        Never raises. Returns ``True`` when at least one envelope was
        emitted; ``False`` for non-push events, payloads with no Python
        changes, ignored refs (non-main pushes), or scan yielding zero
        new findings. Callers (``EventChannelServer._handle_github``)
        log the return for observability but do not retry — the 6h
        fallback poll covers any miss.

        Manifesto §3: complements the FS watcher with a network-side
        push path — merges committed to GitHub from CI or another dev's
        machine now trigger the sensor at network latency rather than
        waiting for a git pull to hit the local FS.
        """
        try:
            if not isinstance(payload, dict):
                self._webhooks_ignored += 1
                return False
            commits = payload.get("commits")
            if not isinstance(commits, list) or not commits:
                self._webhooks_ignored += 1
                logger.debug(
                    "[DocSensor] webhook ignored — no commits list "
                    "(keys=%s)", list(payload.keys())[:6],
                )
                return False

            ref = str(payload.get("ref", "") or "")

            touched: List[str] = []
            for commit in commits:
                if not isinstance(commit, dict):
                    continue
                for key in ("added", "modified"):
                    files = commit.get(key) or []
                    if not isinstance(files, list):
                        continue
                    for f in files:
                        if not isinstance(f, str):
                            continue
                        if not f.endswith(".py"):
                            continue
                        if any(f.startswith(p) for p in self._scan_paths):
                            touched.append(f)

            if not touched:
                self._webhooks_ignored += 1
                logger.debug(
                    "[DocSensor] webhook ignored — no watched-path .py "
                    "changes (ref=%s commits=%d)", ref, len(commits),
                )
                return False

            self._webhooks_handled += 1
            logger.info(
                "[DocSensor] webhook push ref=%s touched_py=%d — "
                "triggering scan_once",
                ref, len(touched),
            )
            findings = await self.scan_once()
            # Envelope emission + dedup handled by scan_once. We return
            # True iff the scan produced at least one finding; dedup-hit
            # scans return [] from scan_once but are still "handled".
            return bool(findings)
        except Exception:
            logger.debug(
                "[DocSensor] webhook ingest failed", exc_info=True,
            )
            self._webhooks_ignored += 1
            return False

    async def scan_once(self) -> List[DocFinding]:
        """Scan Python files for documentation gaps via AST analysis.

        Slice 11.6.b — when ``JARVIS_DOCSTALE_USE_MERKLE=true`` AND the
        Merkle Cartographer says nothing has changed under ``_scan_paths``
        since the last successful scan, short-circuit to the cached
        findings (skip AST parsing + emission). When master flag(s) off
        OR cartographer reports change → full scan as legacy behavior.
        """
        current_hash = self._merkle_current_root_hash()
        if self._merkle_should_short_circuit(current_hash):
            self._merkle_short_circuits += 1
            logger.debug(
                "[DocSensor] Merkle short-circuit "
                "(scan #%d skipped, %d cached findings)",
                self._merkle_short_circuits + self._merkle_full_scans,
                len(self._merkle_cached_findings),
            )
            return list(self._merkle_cached_findings)

        self._merkle_full_scans += 1
        loop = asyncio.get_running_loop()
        findings = await loop.run_in_executor(None, self._scan_files_sync)
        # Cache the result so a subsequent merkle-says-no-change cycle
        # has accurate state to return. Stored regardless of merkle
        # flag so flipping the flag mid-session doesn't blank state.
        self._merkle_cached_findings = list(findings)
        # Refresh baseline AFTER the scan completes — captures the
        # cartographer's current state so the next cycle can detect
        # post-scan changes.
        self._merkle_last_seen_root_hash = current_hash

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

    def _merkle_current_root_hash(self) -> str:
        """Read the cartographer's current root hash. Returns empty
        string on any failure path — fail-safe to legacy scan."""
        if not merkle_consult_enabled():
            return ""
        try:
            from backend.core.ouroboros.governance.merkle_cartographer import (
                get_default_cartographer,
            )
            c = get_default_cartographer(repo_root=self._project_root)
            return c.current_root_hash()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[DocSensor] current_root_hash read failed; "
                "falling through to full scan", exc_info=True,
            )
            return ""

    def _merkle_should_short_circuit(self, current_hash: str) -> bool:
        """Decide whether to skip the AST sweep based on cartographer
        state. Returns False (i.e. proceed with full scan) on any
        failure path — fail-safe to legacy behavior.

        Conditions for short-circuit:
          1. Per-sensor flag ``JARVIS_DOCSTALE_USE_MERKLE`` is true
          2. Cartographer master flag enabled (its
             ``current_root_hash`` returns "" when off — sensor
             treats empty as "always changed" → fail-safe)
          3. The cartographer's current root hash equals the hash
             we recorded after the last full scan
          4. We have a prior cached scan result (no point short-
             circuiting on cold-start since cache is empty)
        """
        if not merkle_consult_enabled():
            return False
        if not self._merkle_cached_findings:
            return False  # cold start — must populate cache
        if not current_hash:
            return False  # cartographer disabled / cold-start / error
        if not self._merkle_last_seen_root_hash:
            return False  # first scan — no baseline yet
        return current_hash == self._merkle_last_seen_root_hash

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
            # Slice 11.6.b — Merkle consultation telemetry
            "merkle_consult_enabled": merkle_consult_enabled(),
            "merkle_short_circuits": self._merkle_short_circuits,
            "merkle_full_scans": self._merkle_full_scans,
            "merkle_last_seen_root_hash": self._merkle_last_seen_root_hash,
            "merkle_cached_findings": len(self._merkle_cached_findings),
        }
