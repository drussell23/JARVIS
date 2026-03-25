"""
RuntimeHealthSensor — Autonomous runtime and dependency health monitoring.

Pillar 6 (Neuroplasticity) + Boundary Principle:
  Deterministic: Version comparison, EOL date lookup, deprecation detection.
  Agentic: Risk assessment for upgrade timing routed through Ouroboros pipeline.

Detects:
  - Python runtime version drift (current vs. latest stable)
  - Key dependency staleness (pinned version vs. latest compatible)
  - Deprecation warnings captured during boot
  - Python 3.9 compat shims still active (signals upgrade was needed)
  - Security advisories for installed packages (via pip audit)

Emits IntentEnvelopes to Ouroboros when action thresholds are met.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deterministic constants — Tier 0 (known, no model inference needed)
# ---------------------------------------------------------------------------

# Python EOL dates (source: https://devguide.python.org/versions/)
_PYTHON_EOL: Dict[str, str] = {
    "3.9":  "2025-10",
    "3.10": "2026-10",
    "3.11": "2027-10",
    "3.12": "2028-10",
    "3.13": "2029-10",
    "3.14": "2030-10",
}

# Packages whose staleness matters for security or compatibility
_TRACKED_PACKAGES: Tuple[str, ...] = (
    "torch",
    "transformers",
    "numpy",
    "anthropic",
    "aiohttp",
    "fastapi",
    "cryptography",
    "google-api-core",
    "llama-cpp-python",
    "speechbrain",
    "chromadb",
)

# How many minor versions behind before we emit
_STALENESS_THRESHOLD_MINOR = 3

# How many days past EOL before urgency escalates to "high"
_EOL_GRACE_DAYS = 90


@dataclass
class HealthFinding:
    """One detected health issue."""
    category: str          # "python_eol", "package_stale", "deprecation", "security"
    severity: str          # "critical", "high", "normal", "low"
    summary: str           # Human-readable one-liner
    details: Dict[str, Any] = field(default_factory=dict)
    target_files: Tuple[str, ...] = ("requirements.txt",)


class RuntimeHealthSensor:
    """Ouroboros intake sensor for runtime and dependency health.

    Follows the implicit sensor protocol:
      - async start() — spawn background poll loop
      - stop()        — signal exit
      - scan_once()   — one detection pass
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        poll_interval_s: float = 86400.0,  # Default: daily
        requirements_path: Optional[Path] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._requirements_path = requirements_path or Path("requirements.txt")
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seen_findings: set[str] = set()  # dedup by summary
        self._boot_scan_done = False

    async def start(self) -> None:
        """Spawn background polling loop."""
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"runtime_health_sensor_{self._repo}"
        )
        logger.info(
            "[RuntimeHealthSensor] Started for repo=%s poll_interval=%ds",
            self._repo, self._poll_interval_s,
        )

    def stop(self) -> None:
        """Signal polling loop to exit."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("[RuntimeHealthSensor] Stopped for repo=%s", self._repo)

    async def _poll_loop(self) -> None:
        """Main polling loop — scan on startup, then at interval."""
        # Initial scan shortly after boot (give system time to stabilize)
        if not self._boot_scan_done:
            await asyncio.sleep(30.0)  # Let other zones finish booting
            self._boot_scan_done = True

        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[RuntimeHealthSensor] Poll error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break

    async def scan_once(self) -> List[HealthFinding]:
        """Run one full health scan. Emits envelopes for actionable findings."""
        findings: List[HealthFinding] = []

        # 1. Python runtime version check (deterministic)
        findings.extend(self._check_python_version())

        # 2. Package staleness check (deterministic — pip index lookup)
        stale = await self._check_package_staleness()
        findings.extend(stale)

        # 3. Security audit (deterministic — pip audit)
        security = await self._check_security_audit()
        findings.extend(security)

        # 4. Import error detection (deterministic — catch ModuleNotFoundError)
        import_errors = self._check_import_errors()
        findings.extend(import_errors)

        # 5. Compat shim detection (deterministic — grep for legacy patterns)
        shims = self._check_legacy_shims()
        findings.extend(shims)

        # Emit envelopes for new findings
        emitted = 0
        for finding in findings:
            if finding.summary in self._seen_findings:
                continue
            self._seen_findings.add(finding.summary)

            try:
                envelope = make_envelope(
                    source="runtime_health",
                    description=finding.summary,
                    target_files=finding.target_files,
                    repo=self._repo,
                    confidence=0.95,  # High — these are factual version comparisons
                    urgency=finding.severity,
                    evidence={
                        "category": finding.category,
                        "details": finding.details,
                        "python_version": platform.python_version(),
                        "sensor": "RuntimeHealthSensor",
                    },
                    requires_human_ack=False,  # Fully autonomous — Ouroboros handles upgrades without human gate
                )
                result = await self._router.ingest(envelope)
                if result == "enqueued":
                    emitted += 1
                    logger.info(
                        "[RuntimeHealthSensor] Emitted: %s (%s) -> %s",
                        finding.category, finding.severity, result,
                    )
            except Exception:
                logger.exception(
                    "[RuntimeHealthSensor] Failed to emit finding: %s",
                    finding.summary,
                )

        if findings:
            logger.info(
                "[RuntimeHealthSensor] Scan complete: %d findings, %d emitted",
                len(findings), emitted,
            )
        return findings

    # ------------------------------------------------------------------
    # Detection methods (all deterministic — Tier 0)
    # ------------------------------------------------------------------

    def _check_python_version(self) -> List[HealthFinding]:
        """Check if Python version is approaching or past EOL."""
        findings = []
        ver = platform.python_version()
        minor_key = f"{sys.version_info.major}.{sys.version_info.minor}"
        eol_str = _PYTHON_EOL.get(minor_key)

        if eol_str:
            # Parse EOL month
            eol_year, eol_month = map(int, eol_str.split("-"))
            now = time.gmtime()
            months_until_eol = (eol_year - now.tm_year) * 12 + (eol_month - now.tm_mon)

            if months_until_eol < 0:
                findings.append(HealthFinding(
                    category="python_eol",
                    severity="critical",
                    summary=f"Python {ver} is PAST end-of-life (EOL: {eol_str}). "
                            f"No security patches. Upgrade required.",
                    details={
                        "current_version": ver,
                        "eol_date": eol_str,
                        "months_past_eol": abs(months_until_eol),
                    },
                ))
            elif months_until_eol <= 6:
                findings.append(HealthFinding(
                    category="python_eol",
                    severity="high",
                    summary=f"Python {ver} reaches EOL in {months_until_eol} months "
                            f"({eol_str}). Plan upgrade.",
                    details={
                        "current_version": ver,
                        "eol_date": eol_str,
                        "months_until_eol": months_until_eol,
                    },
                ))
            elif months_until_eol <= 12:
                findings.append(HealthFinding(
                    category="python_eol",
                    severity="normal",
                    summary=f"Python {ver} EOL in {months_until_eol} months ({eol_str}). "
                            f"Consider upgrade path.",
                    details={
                        "current_version": ver,
                        "eol_date": eol_str,
                        "months_until_eol": months_until_eol,
                    },
                ))

        return findings

    async def _check_package_staleness(self) -> List[HealthFinding]:
        """Check tracked packages against latest available versions via pip index."""
        findings = []

        for pkg_name in _TRACKED_PACKAGES:
            try:
                result = await asyncio.wait_for(
                    self._get_installed_and_latest(pkg_name),
                    timeout=15.0,
                )
                if result is None:
                    continue

                installed, latest = result
                if installed == latest:
                    continue

                staleness = self._compute_staleness(installed, latest)
                if staleness >= _STALENESS_THRESHOLD_MINOR:
                    severity = "high" if staleness >= 6 else "normal"
                    findings.append(HealthFinding(
                        category="package_stale",
                        severity=severity,
                        summary=f"{pkg_name} is {staleness} minor versions behind "
                                f"(installed: {installed}, latest: {latest})",
                        details={
                            "package": pkg_name,
                            "installed": installed,
                            "latest": latest,
                            "minor_versions_behind": staleness,
                        },
                    ))
            except asyncio.TimeoutError:
                logger.debug("[RuntimeHealthSensor] Timeout checking %s", pkg_name)
            except Exception:
                logger.debug(
                    "[RuntimeHealthSensor] Error checking %s", pkg_name, exc_info=True
                )

        return findings

    async def _get_installed_and_latest(
        self, pkg_name: str
    ) -> Optional[Tuple[str, str]]:
        """Get installed and latest version for a package."""
        # Get installed version
        try:
            from importlib.metadata import version as get_version
            installed = get_version(pkg_name)
        except Exception:
            return None

        # Get latest from pip index (subprocess — non-blocking)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "index", "versions", pkg_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            return None

        # Parse "package_name (x.y.z)" from first line
        first_line = stdout.decode().strip().split("\n")[0]
        match = re.search(r"\(([^)]+)\)", first_line)
        if not match:
            return None

        latest = match.group(1)
        return (installed, latest)

    def _compute_staleness(self, installed: str, latest: str) -> int:
        """Compute how many minor versions behind (rough heuristic)."""
        try:
            i_parts = [int(x) for x in installed.split(".")[:3]]
            l_parts = [int(x) for x in latest.split(".")[:3]]

            # Pad to 3 elements
            while len(i_parts) < 3:
                i_parts.append(0)
            while len(l_parts) < 3:
                l_parts.append(0)

            # Major version difference counts as many minors
            if l_parts[0] > i_parts[0]:
                return (l_parts[0] - i_parts[0]) * 10 + l_parts[1]
            return max(0, l_parts[1] - i_parts[1])
        except (ValueError, IndexError):
            return 0

    async def _check_security_audit(self) -> List[HealthFinding]:
        """Run pip audit to detect known vulnerabilities."""
        findings = []
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip_audit", "--format=json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)

            if proc.returncode != 0 and stdout:
                import json
                try:
                    data = json.loads(stdout.decode())
                    vulns = data.get("vulnerabilities", [])
                    if vulns:
                        by_pkg: Dict[str, List[str]] = {}
                        for v in vulns:
                            pkg = v.get("name", "unknown")
                            vid = v.get("id", "unknown")
                            by_pkg.setdefault(pkg, []).append(vid)

                        for pkg, ids in by_pkg.items():
                            findings.append(HealthFinding(
                                category="security",
                                severity="critical",
                                summary=f"Security vulnerability in {pkg}: "
                                        f"{len(ids)} advisory/ies "
                                        f"({', '.join(ids[:3])})",
                                details={
                                    "package": pkg,
                                    "advisory_ids": ids,
                                    "count": len(ids),
                                },
                            ))
                except (json.JSONDecodeError, KeyError):
                    pass
        except (asyncio.TimeoutError, FileNotFoundError):
            # pip-audit not installed or timed out — not critical
            pass
        except Exception:
            logger.debug("[RuntimeHealthSensor] pip audit error", exc_info=True)

        return findings

    def _check_import_errors(self) -> List[HealthFinding]:
        """Detect missing Python packages by attempting tracked imports.

        Distinguishes dependency errors (ModuleNotFoundError, ImportError) from
        syntax errors or runtime errors. Only dependency errors route to the
        dependency-resolution prompt path in the Ouroboros pipeline.
        """
        findings = []

        # PyPI name -> importable module name (deterministic mapping)
        _MODULE_MAP: Dict[str, str] = {
            "google-api-core": "google.api_core",
            "llama-cpp-python": "llama_cpp",
            "scikit-learn": "sklearn",
        }

        for pkg_name in _TRACKED_PACKAGES:
            module_name = _MODULE_MAP.get(
                pkg_name, pkg_name.replace("-", "_")
            )
            try:
                __import__(module_name)
            except ModuleNotFoundError:
                findings.append(HealthFinding(
                    category="missing_dependency",
                    severity="high",
                    summary=(
                        f"ModuleNotFoundError: '{module_name}' "
                        f"(PyPI: {pkg_name}) is not installed. "
                        f"Add to requirements.txt and run pip install."
                    ),
                    details={
                        "package": pkg_name,
                        "module": module_name,
                        "error_type": "ModuleNotFoundError",
                        "resolution": "dependency_install",
                    },
                    target_files=("requirements.txt",),
                ))
            except ImportError as exc:
                # ImportError with a message (e.g., missing C extension,
                # incompatible version). Different from ModuleNotFoundError —
                # the package exists but can't load.
                findings.append(HealthFinding(
                    category="broken_dependency",
                    severity="high",
                    summary=(
                        f"ImportError for '{module_name}' "
                        f"(PyPI: {pkg_name}): {exc}. "
                        f"May need reinstall or version change."
                    ),
                    details={
                        "package": pkg_name,
                        "module": module_name,
                        "error_type": "ImportError",
                        "error_message": str(exc),
                        "resolution": "dependency_reinstall",
                    },
                    target_files=("requirements.txt",),
                ))
            except Exception:
                # SyntaxError, RuntimeError, etc. — not a dependency issue.
                # Don't emit a finding; this is a code problem, not a pip problem.
                pass

        return findings

    def _check_legacy_shims(self) -> List[HealthFinding]:
        """Detect Python 3.9 compatibility shims that are no longer needed."""
        findings = []

        # Check if we're running 3.11+ where shims are unnecessary
        if sys.version_info >= (3, 11):
            try:
                import importlib.util
                spec = importlib.util.find_spec("backend.utils.python39_compat")
                if spec is not None:
                    findings.append(HealthFinding(
                        category="legacy_shim",
                        severity="low",
                        summary=(
                            f"Legacy Python 3.9 compat shims still present. "
                            f"Can be removed on Python "
                            f"{platform.python_version()}."
                        ),
                        details={
                            "shim_module": "backend.utils.python39_compat",
                            "current_python": platform.python_version(),
                            "min_python_needed": "3.11",
                        },
                        target_files=(
                            "backend/utils/python39_compat.py",
                            "unified_supervisor.py",
                            "start_system.py",
                        ),
                    ))
            except Exception:
                pass

        return findings

    def health(self) -> Dict[str, Any]:
        """Health check for observability."""
        return {
            "sensor": "RuntimeHealthSensor",
            "repo": self._repo,
            "running": self._running,
            "boot_scan_done": self._boot_scan_done,
            "findings_seen": len(self._seen_findings),
            "python_version": platform.python_version(),
            "poll_interval_s": self._poll_interval_s,
        }
