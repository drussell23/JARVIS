"""
WebIntelligenceSensor — Proactive security and dependency advisory monitoring.

P1 Gap: If a CVE drops for aiohttp and nobody files an issue, Ouroboros
never knows. This sensor polls external advisory sources and emits
IntentEnvelopes for actionable findings.

Boundary Principle:
  Deterministic: HTTP fetch, JSON parse, version comparison, advisory matching.
  Agentic: Remediation (patching requirements.txt) routed through Ouroboros pipeline.

Sources:
  1. PyPI JSON API — per-package vulnerability data (no auth needed)
  2. GitHub Advisory Database — ecosystem-wide advisories (public API)

Follows the implicit sensor protocol: start(), stop(), scan_once().
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_POLL_INTERVAL_S = float(
    os.environ.get("JARVIS_WEB_INTEL_INTERVAL_S", "86400")  # Daily
)
_PYPI_TIMEOUT_S = float(os.environ.get("JARVIS_WEB_INTEL_PYPI_TIMEOUT_S", "15"))
_GITHUB_TIMEOUT_S = float(os.environ.get("JARVIS_WEB_INTEL_GITHUB_TIMEOUT_S", "30"))
_MAX_PACKAGES_PER_SCAN = int(os.environ.get("JARVIS_WEB_INTEL_MAX_PACKAGES", "50"))

# Packages to monitor — read from requirements.txt at runtime
# plus these critical-path packages always checked
_ALWAYS_CHECK: Tuple[str, ...] = (
    "anthropic", "aiohttp", "fastapi", "cryptography", "torch",
    "numpy", "uvicorn", "websockets", "pydantic", "httpx",
    "google-api-core", "grpcio", "protobuf", "redis",
)


@dataclass
class AdvisoryFinding:
    """One detected security advisory."""
    package: str
    installed_version: str
    advisory_id: str
    summary: str
    severity: str          # "critical", "high", "normal", "low"
    fixed_in: str          # Version that fixes the issue, or "" if unknown
    source: str            # "pypi" or "github"
    details: Dict[str, Any] = field(default_factory=dict)


class WebIntelligenceSensor:
    """Proactive security advisory sensor for the Ouroboros intake layer.

    Polls PyPI vulnerability data for installed packages and emits
    IntentEnvelopes when actionable advisories are found.

    Follows the implicit sensor protocol:
      - async start() — spawn background poll loop
      - stop()        — signal exit
      - scan_once()   — one detection pass
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        poll_interval_s: float = _POLL_INTERVAL_S,
        requirements_path: Optional[Path] = None,
        project_root: Optional[Path] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._requirements_path = requirements_path or Path("requirements.txt")
        self._project_root = project_root or Path(".")
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seen_advisories: set[str] = set()  # dedup by advisory_id+package
        self._session: Optional[Any] = None

    async def start(self) -> None:
        """Spawn background polling loop."""
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"web_intel_sensor_{self._repo}"
        )
        logger.info(
            "[WebIntelSensor] Started for repo=%s poll_interval=%ds",
            self._repo, self._poll_interval_s,
        )

    def stop(self) -> None:
        """Signal polling loop to exit."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        if self._session and not self._session.closed:
            asyncio.get_event_loop().create_task(self._session.close())
        logger.info("[WebIntelSensor] Stopped for repo=%s", self._repo)

    async def _poll_loop(self) -> None:
        """Main polling loop — initial delay, then at interval."""
        # Delay initial scan to let boot complete
        await asyncio.sleep(60.0)

        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[WebIntelSensor] Poll error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break

    async def _get_session(self) -> Any:
        """Lazy-init persistent aiohttp session."""
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_PYPI_TIMEOUT_S),
            )
        return self._session

    async def scan_once(self) -> List[AdvisoryFinding]:
        """Run one full advisory scan. Emits envelopes for new findings."""
        findings: List[AdvisoryFinding] = []

        # Build package list from requirements.txt + always-check list
        packages = self._get_installed_packages()

        # Check PyPI vulnerability data for each package
        for pkg_name, installed_version in packages[:_MAX_PACKAGES_PER_SCAN]:
            try:
                pkg_findings = await asyncio.wait_for(
                    self._check_pypi_advisories(pkg_name, installed_version),
                    timeout=_PYPI_TIMEOUT_S,
                )
                findings.extend(pkg_findings)
            except (asyncio.TimeoutError, OSError):
                logger.debug("[WebIntelSensor] Timeout checking %s", pkg_name)
            except asyncio.CancelledError:
                raise
            except Exception as _exc:
                logger.debug(
                    "[WebIntelSensor] Error checking %s: %s", pkg_name, type(_exc).__name__
                )

        # Emit envelopes for new findings
        emitted = 0
        for finding in findings:
            dedup_key = f"{finding.advisory_id}:{finding.package}"
            if dedup_key in self._seen_advisories:
                continue
            self._seen_advisories.add(dedup_key)

            try:
                severity = finding.severity
                envelope = make_envelope(
                    source="security_advisory",
                    description=(
                        f"Security advisory {finding.advisory_id} for "
                        f"{finding.package}=={finding.installed_version}: "
                        f"{finding.summary}"
                    ),
                    target_files=("requirements.txt",),
                    repo=self._repo,
                    confidence=0.95,
                    urgency=severity,
                    evidence={
                        "category": "security_advisory",
                        "package": finding.package,
                        "installed_version": finding.installed_version,
                        "advisory_id": finding.advisory_id,
                        "fixed_in": finding.fixed_in,
                        "source": finding.source,
                        "sensor": "WebIntelligenceSensor",
                    },
                    requires_human_ack=severity in ("critical",),
                )
                result = await self._router.ingest(envelope)
                if result == "enqueued":
                    emitted += 1
                    logger.info(
                        "[WebIntelSensor] Advisory: %s %s %s -> %s",
                        finding.advisory_id, finding.package,
                        finding.severity, result,
                    )
            except Exception:
                logger.exception(
                    "[WebIntelSensor] Failed to emit: %s", finding.advisory_id
                )

        if findings:
            logger.info(
                "[WebIntelSensor] Scan complete: %d advisories found, %d emitted",
                len(findings), emitted,
            )
        return findings

    # ------------------------------------------------------------------
    # Package discovery (deterministic)
    # ------------------------------------------------------------------

    def _get_installed_packages(self) -> List[Tuple[str, str]]:
        """Get list of (package_name, installed_version) to check.

        Combines requirements.txt parsing with importlib.metadata for
        actual installed versions. Deterministic — no inference.
        """
        packages: Dict[str, str] = {}

        # Always-check packages
        for pkg in _ALWAYS_CHECK:
            try:
                from importlib.metadata import version as get_version
                packages[pkg] = get_version(pkg)
            except Exception:
                pass

        # Parse requirements.txt for additional packages
        req_path = self._project_root / self._requirements_path
        if req_path.exists():
            try:
                for line in req_path.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("-"):
                        continue
                    # Parse "package==version" or "package>=version"
                    match = re.match(r"^([a-zA-Z0-9_.-]+)", line)
                    if match:
                        pkg = match.group(1).lower()
                        if pkg not in packages:
                            try:
                                from importlib.metadata import version as get_version
                                packages[pkg] = get_version(pkg)
                            except Exception:
                                pass
            except Exception:
                pass

        return list(packages.items())

    # ------------------------------------------------------------------
    # PyPI advisory check (deterministic — HTTP + JSON + version compare)
    # ------------------------------------------------------------------

    async def _check_pypi_advisories(
        self, pkg_name: str, installed_version: str,
    ) -> List[AdvisoryFinding]:
        """Check PyPI JSON API for vulnerabilities affecting installed version.

        PyPI endpoint: https://pypi.org/pypi/{package}/json
        Response includes 'vulnerabilities' array (PEP 691).
        """
        findings = []
        session = await self._get_session()

        try:
            import aiohttp as _aio
            url = f"https://pypi.org/pypi/{pkg_name}/json"
            async with session.get(url, timeout=_aio.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)

            vulns = data.get("vulnerabilities", [])
            if not vulns:
                return []

            for vuln in vulns:
                vuln_id = vuln.get("id", "")
                aliases = vuln.get("aliases", [])
                summary = vuln.get("summary", vuln.get("details", ""))[:200]

                # Check if our installed version is affected
                affected = self._is_version_affected(
                    installed_version, vuln.get("fixed_in", []),
                    vuln.get("affected", []),
                )
                if not affected:
                    continue

                # Determine severity from the advisory
                severity = self._classify_severity(vuln)

                # Find fix version
                fixed_in_versions = vuln.get("fixed_in", [])
                fixed_in = fixed_in_versions[0] if fixed_in_versions else ""

                findings.append(AdvisoryFinding(
                    package=pkg_name,
                    installed_version=installed_version,
                    advisory_id=vuln_id or (aliases[0] if aliases else "unknown"),
                    summary=summary,
                    severity=severity,
                    fixed_in=fixed_in,
                    source="pypi",
                    details={
                        "aliases": aliases[:5],
                        "fixed_in_versions": fixed_in_versions[:3],
                        "link": vuln.get("link", ""),
                    },
                ))

        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, OSError):
            # Network timeout or DNS failure — clean one-liner, no traceback
            logger.debug("[WebIntelSensor] Timeout checking %s", pkg_name)
        except Exception:
            logger.debug(
                "[WebIntelSensor] PyPI check failed for %s: %s",
                pkg_name, type(Exception).__name__,
            )

        return findings

    def _is_version_affected(
        self,
        installed: str,
        fixed_in: List[str],
        affected_ranges: List[Any],
    ) -> bool:
        """Check if installed version is affected by the advisory.

        Simple version comparison — if fixed_in exists and installed
        is less than any fix version, we're affected. Deterministic.
        """
        if not fixed_in and not affected_ranges:
            # No version info — conservatively assume affected
            return True

        if fixed_in:
            try:
                from packaging.version import Version
                inst = Version(installed)
                for fix_ver in fixed_in:
                    try:
                        if inst < Version(fix_ver):
                            return True
                    except Exception:
                        continue
                return False
            except Exception:
                # packaging not available — fall back to string compare
                return installed < fixed_in[0] if fixed_in else True

        return True  # Affected ranges present but unparsed — assume affected

    def _classify_severity(self, vuln: Dict[str, Any]) -> str:
        """Classify advisory severity from PyPI vulnerability data."""
        # Check for explicit severity field
        severity = vuln.get("severity", "")
        if isinstance(severity, str):
            s = severity.lower()
            if "critical" in s:
                return "critical"
            if "high" in s:
                return "high"
            if "low" in s:
                return "low"

        # Check aliases for CVE pattern (CVEs are typically high/critical)
        aliases = vuln.get("aliases", [])
        has_cve = any(a.startswith("CVE-") for a in aliases)
        has_ghsa = any(a.startswith("GHSA-") for a in aliases)

        if has_cve or has_ghsa:
            return "high"
        return "normal"

    def health(self) -> Dict[str, Any]:
        """Health check for observability."""
        return {
            "sensor": "WebIntelligenceSensor",
            "repo": self._repo,
            "running": self._running,
            "advisories_seen": len(self._seen_advisories),
            "poll_interval_s": self._poll_interval_s,
        }
