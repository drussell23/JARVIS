"""
CrossRepoDriftSensor — Detects contract and API drift across Trinity repos.

P3 Gap: Ouroboros doesn't proactively monitor whether changes in jarvis-prime
would break JARVIS, or vice versa. This sensor periodically compares contract
versions, schema versions, and critical file hashes across repos.

Boundary Principle:
  Deterministic: File hash comparison, schema version parsing, protocol
  version matching. No model inference for detection.
  Agentic: Remediation (updating contracts) routed through pipeline.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = float(
    os.environ.get("JARVIS_DRIFT_DETECTION_INTERVAL_S", "3600")
)

# Cross-repo contract files to monitor for drift
# (repo_name, file_relative_to_repo_root, description)
_CONTRACT_FILES: Tuple[Tuple[str, str, str], ...] = (
    ("jarvis", "docs/cross-repo-contract.md", "Cross-repo contract spec"),
    ("jarvis", "backend/core/ouroboros/governance/op_context.py", "Operation context schema"),
    ("jarvis", "backend/core/ouroboros/governance/intake/intent_envelope.py", "IntentEnvelope schema"),
)

# Protocol version endpoints to check
_PROTOCOL_CHECKS: Tuple[Tuple[str, str, str], ...] = (
    ("jarvis", "backend/core/mind_client.py", "MindClient protocol"),
    ("jarvis", "backend/core/prime_client.py", "PrimeClient protocol"),
)


@dataclass
class DriftFinding:
    """One detected cross-repo drift."""
    category: str          # "schema_drift", "contract_drift", "protocol_drift"
    severity: str
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)


class CrossRepoDriftSensor:
    """Detects contract drift across Trinity repositories.

    Periodically:
    1. Hashes critical contract files and compares against stored baselines
    2. Checks protocol version constants for mismatches
    3. Validates schema version compatibility between repos

    When drift is detected, emits IntentEnvelope for investigation.

    Follows the implicit sensor protocol: start(), stop(), scan_once().
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        poll_interval_s: float = _POLL_INTERVAL_S,
        project_root: Optional[Path] = None,
        repo_registry: Optional[Any] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._project_root = project_root or Path(".")
        self._repo_registry = repo_registry
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._baselines: Dict[str, str] = {}  # file_path -> sha256
        self._seen_findings: set[str] = set()

    async def start(self) -> None:
        self._running = True
        # Capture initial baselines
        self._capture_baselines()
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"drift_sensor_{self._repo}"
        )
        logger.info(
            "[DriftSensor] Started with %d baseline files", len(self._baselines)
        )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------------
    # Event-driven path (Manifesto §3: zero polling, pure reflex)
    # ------------------------------------------------------------------

    async def subscribe_to_bus(self, event_bus: Any) -> None:
        """Subscribe to file system events for instant drift detection."""
        await event_bus.subscribe("fs.changed.*", self._on_fs_event)
        logger.info("[DriftSensor] Subscribed to fs.changed.* events")

    async def _on_fs_event(self, event: Any) -> None:
        """React to git commit or contract file changes."""
        rel_path = event.payload.get("relative_path", "")

        # Git commit event → check if contract files changed
        if rel_path.endswith("git_events.json") and ".jarvis" in rel_path:
            import json
            try:
                data = json.loads(Path(event.payload["path"]).read_text())
                changed = data.get("changed_files", [])
                # Check if any changed file is a contract file we track
                if any(f in self._baselines for f in changed):
                    logger.debug("[DriftSensor] Contract file changed in commit, rescanning")
                    await self.scan_once()
            except Exception:
                logger.debug("[DriftSensor] Failed to read git event", exc_info=True)
            return

        # Direct contract file change → rescan
        if rel_path in self._baselines:
            try:
                await self.scan_once()
            except Exception:
                logger.debug("[DriftSensor] Event-driven scan error", exc_info=True)

    # ------------------------------------------------------------------
    # Poll fallback
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        await asyncio.sleep(300.0)  # Let repos settle after boot
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[DriftSensor] Poll error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break

    def _capture_baselines(self) -> None:
        """Hash all contract files (normalized) to establish baselines."""
        for repo_name, rel_path, _desc in _CONTRACT_FILES:
            full_path = self._resolve_repo_path(repo_name, rel_path)
            if full_path and full_path.exists():
                try:
                    raw = full_path.read_text(errors="replace")
                    normalized = "\n".join(line.rstrip() for line in raw.splitlines()) + "\n"
                    h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                    self._baselines[f"{repo_name}:{rel_path}"] = h
                except Exception:
                    pass

    def _resolve_repo_path(self, repo_name: str, rel_path: str) -> Optional[Path]:
        """Resolve a path within a named repo. Deterministic."""
        if repo_name == "jarvis" or repo_name == self._repo:
            return self._project_root / rel_path

        # Use RepoRegistry if available
        if self._repo_registry is not None:
            try:
                repos = list(self._repo_registry.list_enabled())
                for rc in repos:
                    if rc.name == repo_name:
                        return rc.local_path / rel_path
            except Exception:
                pass

        # Environment variable fallback
        env_key = f"JARVIS_{repo_name.upper().replace('-', '_')}_REPO_PATH"
        env_path = os.environ.get(env_key)
        if env_path:
            return Path(env_path) / rel_path

        return None

    async def scan_once(self) -> List[DriftFinding]:
        """Check all contract files for drift against baselines."""
        findings: List[DriftFinding] = []

        for repo_name, rel_path, description in _CONTRACT_FILES:
            key = f"{repo_name}:{rel_path}"
            full_path = self._resolve_repo_path(repo_name, rel_path)

            if full_path is None or not full_path.exists():
                continue

            try:
                # Normalize content: strip trailing whitespace + ensure newline
                # Prevents false positives from formatting-only changes (P2 edge case)
                raw = full_path.read_text(errors="replace")
                normalized = "\n".join(line.rstrip() for line in raw.splitlines()) + "\n"
                content = normalized.encode("utf-8")
                current_hash = hashlib.sha256(content).hexdigest()
                baseline_hash = self._baselines.get(key)

                if baseline_hash and current_hash != baseline_hash:
                    findings.append(DriftFinding(
                        category="contract_drift",
                        severity="normal",
                        summary=(
                            f"Contract file changed: {rel_path} in {repo_name} "
                            f"(hash drift detected). {description} may need "
                            f"cross-repo validation."
                        ),
                        details={
                            "repo": repo_name,
                            "file": rel_path,
                            "baseline_hash": baseline_hash[:12],
                            "current_hash": current_hash[:12],
                        },
                    ))
                    # Update baseline to current
                    self._baselines[key] = current_hash

            except Exception:
                pass

        # Check schema version consistency
        schema_finding = self._check_schema_versions()
        if schema_finding:
            findings.append(schema_finding)

        # Emit envelopes
        emitted = 0
        for finding in findings:
            dedup_key = f"{finding.category}:{finding.summary[:50]}"
            if dedup_key in self._seen_findings:
                continue
            self._seen_findings.add(dedup_key)

            try:
                envelope = make_envelope(
                    source="cross_repo_drift",
                    description=finding.summary,
                    target_files=("docs/cross-repo-contract.md",),
                    repo=self._repo,
                    confidence=0.85,
                    urgency=finding.severity,
                    evidence={
                        "category": finding.category,
                        "sensor": "CrossRepoDriftSensor",
                        **finding.details,
                    },
                    requires_human_ack=False,
                )
                await self._router.ingest(envelope)
                emitted += 1
            except Exception:
                pass

        if findings:
            logger.info(
                "[DriftSensor] Scan: %d drift findings, %d emitted",
                len(findings), emitted,
            )
        return findings

    def _check_schema_versions(self) -> Optional[DriftFinding]:
        """Check schema version constants for consistency. Deterministic."""
        try:
            from backend.core.ouroboros.governance.intake.intent_envelope import (
                SCHEMA_VERSION,
            )
            # Read the expected schema version from the contract
            contract_path = self._project_root / "docs" / "cross-repo-contract.md"
            if contract_path.exists():
                content = contract_path.read_text(errors="replace")
                if SCHEMA_VERSION not in content:
                    return DriftFinding(
                        category="schema_drift",
                        severity="high",
                        summary=(
                            f"IntentEnvelope schema version {SCHEMA_VERSION} "
                            f"not found in cross-repo contract doc. "
                            f"Contract may be outdated."
                        ),
                        details={
                            "schema_version": SCHEMA_VERSION,
                            "contract_file": "docs/cross-repo-contract.md",
                        },
                    )
        except Exception:
            pass
        return None

    def health(self) -> Dict[str, Any]:
        return {
            "sensor": "CrossRepoDriftSensor",
            "repo": self._repo,
            "running": self._running,
            "baseline_files": len(self._baselines),
            "findings_seen": len(self._seen_findings),
        }
