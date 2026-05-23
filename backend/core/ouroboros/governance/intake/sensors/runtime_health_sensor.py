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
import enum
import importlib.metadata
import importlib.util
import logging
import os
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
# Slice 12M — non-executing dependency discovery
# ---------------------------------------------------------------------------
#
# Empirical context: bt-2026-05-23-004847 (Slice 12L verification soak)
# captured a ControlPlaneStarvation lag_ms=14267.9 immediately after
# RuntimeHealthSensor triggered a synchronous ``__import__("speechbrain")``
# whose top-level code pulled in torch + torio + FFmpeg extension lookups,
# blocking the asyncio loop for ~14s.
#
# Root fix: the sensor MUST NOT execute target-package top-level code
# to determine presence. Two stdlib primitives are sufficient:
#
#   * ``importlib.metadata.distribution(pkg_name)`` — returns the dist
#     object if the package was installed (e.g. via pip); raises
#     ``PackageNotFoundError`` otherwise. NEVER executes target code.
#
#   * ``importlib.util.find_spec(module_name)`` — returns the module's
#     ModuleSpec if found on sys.path; returns None if not. For
#     TOP-LEVEL modules (no dot in name) this is fully non-executing.
#     For DOTTED modules (e.g. ``google.api_core``) it DOES import the
#     parent package, so Slice 12M skips find_spec for dotted modules
#     and relies on distribution() alone for those.
#
# Optional subprocess deep-probe (env-gated, default OFF) handles the
# rare case where metadata + spec say "installed" but the actual import
# is broken (missing C extension, ABI mismatch). Uses
# ``asyncio.create_subprocess_exec`` with a bounded ``asyncio.wait_for``
# timeout — NEVER on the asyncio loop, NEVER blocks, NEVER raises.


class DependencyState(str, enum.Enum):
    """Closed taxonomy of dependency presence states from non-executing
    discovery. Slice 12M: replaces the prior ``__import__``-based
    detection that blocked the loop on heavy package top-level code."""

    INSTALLED_AND_IMPORTABLE = "installed_and_importable"
    MISSING_DISTRIBUTION = "missing_distribution"      # not pip-installed
    INSTALLED_BUT_NO_SPEC = "installed_but_no_spec"    # metadata present, spec missing
    UNKNOWN_ERROR = "unknown_error"                    # defensive bucket


class SubprocessProbeOutcome(str, enum.Enum):
    """Closed taxonomy of subprocess deep-probe results. Used only
    when the env-gated deep-probe path is enabled — the default
    Slice 12M behavior never reaches this code."""

    IMPORTED = "imported"
    IMPORT_ERROR = "import_error"
    TIMEOUT = "timeout"
    SUBPROCESS_FAILED = "subprocess_failed"
    REJECTED_UNSAFE_NAME = "rejected_unsafe_name"


@dataclass(frozen=True)
class SubprocessProbeResult:
    """Frozen result from the optional subprocess deep-probe. Carries
    the closed-taxonomy outcome plus the wall-clock duration so
    operators can detect slow probes via telemetry."""

    outcome: SubprocessProbeOutcome
    elapsed_s: float
    error_detail: str = ""


# PyPI name -> importable module name (deterministic mapping). Lifted
# from the body of _check_import_errors so it's testable + AST-walkable
# and not re-allocated per call.
_MODULE_MAP: Dict[str, str] = {
    "google-api-core": "google.api_core",
    "llama-cpp-python": "llama_cpp",
    "scikit-learn": "sklearn",
}


# Identifier-only regex for subprocess probe input safety. Module names
# must match standard Python identifier rules (letters, digits,
# underscores, dots between identifiers) — defensive against any future
# refactor that passes operator-controlled strings to the probe.
_SAFE_MODULE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


# Slice 12M env knobs
_DEEP_PROBE_ENABLED_ENV = "JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_ENABLED"
_DEEP_PROBE_TIMEOUT_S_ENV = "JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_TIMEOUT_S"
_DEEP_PROBE_PYTHON_BIN_ENV = "JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_PYTHON_BIN"

_DEFAULT_DEEP_PROBE_TIMEOUT_S = 30.0
_DEEP_PROBE_TIMEOUT_FLOOR_S = 1.0
_DEEP_PROBE_TIMEOUT_CEIL_S = 300.0


def _resolve_dependency_state(
    pkg_name: str,
    module_name: str,
) -> Tuple[DependencyState, str]:
    """Pure non-executing dependency presence check.

    Returns ``(state, detail)``. NEVER imports the target module —
    no ``__import__``, no ``importlib.import_module``. NEVER raises;
    any unexpected exception maps to ``UNKNOWN_ERROR``.

    Two-layer discovery:

      Layer 1 — ``importlib.metadata.distribution(pkg_name)``:
        Returns the dist object if pip-installed. Raises
        ``PackageNotFoundError`` otherwise. Pure metadata read.

      Layer 2 — ``importlib.util.find_spec(module_name)``:
        Returns the ModuleSpec if found on sys.path; None
        otherwise. For TOP-LEVEL modules (no dot) this is fully
        non-executing. DOTTED modules trigger parent-package
        ``__init__.py`` execution as a side effect, so Slice 12M
        skips Layer 2 for dotted module names.

    Decision matrix (preserves the prior ``__import__`` semantics
    while adding PYTHONPATH/namespace-package tolerance):

      dist  spec    →  state
      ──────────────────────────────────────────────────────────
      yes   yes     →  INSTALLED_AND_IMPORTABLE   (normal pip install)
      yes   no      →  INSTALLED_BUT_NO_SPEC      (broken install)
      yes   N/A     →  INSTALLED_AND_IMPORTABLE   (dotted module trusted)
      no    yes     →  INSTALLED_AND_IMPORTABLE   (PYTHONPATH/namespace pkg)
      no    no      →  MISSING_DISTRIBUTION       (truly absent)
      no    N/A     →  MISSING_DISTRIBUTION       (dotted, no dist-info)
      ──────────────────────────────────────────────────────────
    """
    # Layer 1: distribution metadata
    dist_present: Optional[bool] = None
    try:
        importlib.metadata.distribution(pkg_name)
        dist_present = True
    except importlib.metadata.PackageNotFoundError:
        dist_present = False
    except Exception as exc:  # noqa: BLE001 — defensive
        return (DependencyState.UNKNOWN_ERROR, f"metadata: {exc}")

    # Layer 2: spec presence (top-level modules only).
    # ``find_spec("torch")`` only searches sys.path for torch/__init__.py;
    # it does NOT execute it. But ``find_spec("google.api_core")`` DOES
    # import the parent ``google``, which can trigger side effects. So
    # we skip the spec check for dotted modules and trust the
    # distribution() result alone for those.
    spec_present: Optional[bool] = None  # None means "not checked"
    spec_detail = ""
    if "." not in module_name:
        try:
            spec = importlib.util.find_spec(module_name)
            spec_present = spec is not None
            if not spec_present:
                spec_detail = (
                    f"importlib.util.find_spec({module_name!r}) returned None"
                )
        except (ModuleNotFoundError, ImportError, ValueError) as exc:
            spec_present = False
            spec_detail = f"find_spec raised: {exc}"
        except Exception as exc:  # noqa: BLE001
            return (DependencyState.UNKNOWN_ERROR, f"find_spec: {exc}")

    # Decision matrix — see docstring for the table form.
    if dist_present:
        if spec_present is False:
            return (DependencyState.INSTALLED_BUT_NO_SPEC, spec_detail)
        return (DependencyState.INSTALLED_AND_IMPORTABLE, "")

    # dist_present is False
    if spec_present is True:
        # Edge case: module is importable but not pip-installed
        # (PYTHONPATH-side install, namespace package, stdlib).
        # The module IS available — do not emit missing_dependency.
        return (
            DependencyState.INSTALLED_AND_IMPORTABLE,
            "no dist-info but spec present (PYTHONPATH or namespace)",
        )

    return (
        DependencyState.MISSING_DISTRIBUTION,
        f"importlib.metadata.distribution({pkg_name!r}) raised "
        f"PackageNotFoundError",
    )


async def _subprocess_import_probe(
    module_name: str,
    *,
    timeout_s: float,
    python_bin: str,
) -> SubprocessProbeResult:
    """Run ``<python> -c "import <module>"`` in a bounded subprocess.

    Slice 12M opt-in deep-probe path. Uses ``asyncio.create_subprocess_exec``
    + ``asyncio.wait_for`` — fully loop-safe, never blocks. NEVER raises;
    every failure shape maps to a closed taxonomy value. Refuses
    unsafe module names (anything not matching the standard Python
    identifier regex) so a future refactor passing operator-controlled
    strings to this probe cannot be turned into shell injection or
    arbitrary-code execution.

    Subprocess isolation means the heavy package's top-level code
    executes in a SEPARATE process — never in the watchdog'd asyncio
    loop process. The probe's elapsed time is wall-clock; the loop
    only awaits ``wait_for`` (cheap).
    """
    if not _SAFE_MODULE_NAME_RE.match(module_name):
        return SubprocessProbeResult(
            outcome=SubprocessProbeOutcome.REJECTED_UNSAFE_NAME,
            elapsed_s=0.0,
            error_detail=f"module name {module_name!r} not a valid Python identifier",
        )
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            python_bin,
            "-c",
            f"import {module_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:  # noqa: BLE001 — never raise into sensor
        return SubprocessProbeResult(
            outcome=SubprocessProbeOutcome.SUBPROCESS_FAILED,
            elapsed_s=time.monotonic() - t0,
            error_detail=f"create_subprocess_exec: {exc}",
        )
    try:
        _stdout, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        return SubprocessProbeResult(
            outcome=SubprocessProbeOutcome.TIMEOUT,
            elapsed_s=time.monotonic() - t0,
            error_detail=f"probe exceeded timeout={timeout_s:.1f}s",
        )
    except Exception as exc:  # noqa: BLE001
        return SubprocessProbeResult(
            outcome=SubprocessProbeOutcome.SUBPROCESS_FAILED,
            elapsed_s=time.monotonic() - t0,
            error_detail=f"communicate: {exc}",
        )
    elapsed = time.monotonic() - t0
    if proc.returncode == 0:
        return SubprocessProbeResult(
            outcome=SubprocessProbeOutcome.IMPORTED,
            elapsed_s=elapsed,
        )
    stderr_text = stderr_bytes.decode("utf-8", errors="replace") \
        if stderr_bytes else ""
    # Truncate stderr to keep findings/log lines bounded.
    return SubprocessProbeResult(
        outcome=SubprocessProbeOutcome.IMPORT_ERROR,
        elapsed_s=elapsed,
        error_detail=stderr_text[-512:].strip(),
    )

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

        # 4. Import error detection — Slice 12M: non-executing
        #    discovery via importlib.metadata + importlib.util. Async
        #    because the optional subprocess deep-probe path uses
        #    asyncio.create_subprocess_exec.
        import_errors = await self._check_import_errors()
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

    async def _check_import_errors(self) -> List[HealthFinding]:
        """Slice 12M — non-executing dependency presence detection.

        Replaces the prior ``__import__``-based detection that
        triggered a 14s ControlPlaneStarvation wedge in bt-2026-05-
        23-004847 by executing heavy package top-level code (torch
        + speechbrain + torio + FFmpeg lookup) on the asyncio loop.

        Default behavior NEVER executes target package code:
          * ``importlib.metadata.distribution`` for pip-installed?
          * ``importlib.util.find_spec`` for top-level module spec
            (skipped for dotted modules to avoid parent-package
            import side effects).

        Optional opt-in deep-probe path (env-gated, default OFF):
          * ``JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_ENABLED=true``
          * Per-package subprocess: ``<python> -c "import <module>"``
          * Bounded ``asyncio.wait_for(timeout_s)``; default 30s,
            tunable via env knob (clamped to [1.0, 300.0])
          * Subprocess heavy-load NEVER touches the loop process
          * No package-specific hardcoding — uniform across the
            tracked-package list

        Preserves Slice 11A finding categories: ``missing_dependency``
        when distribution absent, ``broken_dependency`` when spec
        missing or deep-probe import fails. ``requires_human_ack``
        and routing surfaces unchanged.
        """
        findings: List[HealthFinding] = []
        deep_probe_enabled = self._resolve_deep_probe_enabled()
        deep_timeout_s = self._resolve_deep_probe_timeout_s()
        deep_python_bin = self._resolve_deep_probe_python_bin()

        for pkg_name in _TRACKED_PACKAGES:
            module_name = _MODULE_MAP.get(
                pkg_name, pkg_name.replace("-", "_"),
            )
            state, detail = _resolve_dependency_state(pkg_name, module_name)

            if state == DependencyState.MISSING_DISTRIBUTION:
                findings.append(HealthFinding(
                    category="missing_dependency",
                    severity="high",
                    summary=(
                        f"Missing dependency: '{module_name}' "
                        f"(PyPI: {pkg_name}) is not installed. "
                        f"Add to requirements.txt and run pip install."
                    ),
                    details={
                        "package": pkg_name,
                        "module": module_name,
                        "error_type": "MissingDistribution",
                        "discovery": "importlib.metadata.distribution",
                        "resolution": "dependency_install",
                        "detail": detail,
                    },
                    target_files=("requirements.txt",),
                ))
                continue

            if state == DependencyState.INSTALLED_BUT_NO_SPEC:
                findings.append(HealthFinding(
                    category="broken_dependency",
                    severity="high",
                    summary=(
                        f"Broken dependency: '{module_name}' "
                        f"(PyPI: {pkg_name}) is installed but the "
                        f"module spec is missing. May need reinstall "
                        f"or namespace-package fix."
                    ),
                    details={
                        "package": pkg_name,
                        "module": module_name,
                        "error_type": "MissingSpec",
                        "discovery": "importlib.util.find_spec",
                        "resolution": "dependency_reinstall",
                        "detail": detail,
                    },
                    target_files=("requirements.txt",),
                ))
                continue

            if state == DependencyState.UNKNOWN_ERROR:
                # Defensive: log but do not emit a finding — preserves
                # the prior "code problem, not a pip problem" semantics
                # of catching unexpected exceptions.
                logger.debug(
                    "[RuntimeHealthSensor] dependency state UNKNOWN_ERROR "
                    "for pkg=%s module=%s detail=%s",
                    pkg_name, module_name, detail,
                )
                continue

            # INSTALLED_AND_IMPORTABLE: by default we trust this.
            # Optional deep-probe runs the actual import in a
            # subprocess to catch the rare "spec-present-but-actually-
            # broken" case (missing C extension, ABI mismatch). Heavy
            # package top-level code executes in the CHILD process —
            # the asyncio loop only awaits the bounded wait_for.
            if deep_probe_enabled:
                probe = await _subprocess_import_probe(
                    module_name,
                    timeout_s=deep_timeout_s,
                    python_bin=deep_python_bin,
                )
                if probe.outcome == SubprocessProbeOutcome.IMPORT_ERROR:
                    findings.append(HealthFinding(
                        category="broken_dependency",
                        severity="high",
                        summary=(
                            f"Broken dependency: '{module_name}' "
                            f"(PyPI: {pkg_name}) subprocess import "
                            f"failed in {probe.elapsed_s:.1f}s. "
                            f"May need reinstall or version change."
                        ),
                        details={
                            "package": pkg_name,
                            "module": module_name,
                            "error_type": "SubprocessImportError",
                            "discovery": "subprocess_deep_probe",
                            "elapsed_s": probe.elapsed_s,
                            "stderr_tail": probe.error_detail,
                            "resolution": "dependency_reinstall",
                        },
                        target_files=("requirements.txt",),
                    ))
                # IMPORTED → no finding (the goal of the probe).
                # TIMEOUT / SUBPROCESS_FAILED / REJECTED_UNSAFE_NAME
                # → no finding (instrumentation failure, not a
                # dependency finding — log at DEBUG only).
                elif probe.outcome != SubprocessProbeOutcome.IMPORTED:
                    logger.debug(
                        "[RuntimeHealthSensor] deep-probe non-actionable "
                        "for pkg=%s module=%s outcome=%s elapsed=%.1fs "
                        "detail=%s",
                        pkg_name, module_name, probe.outcome.value,
                        probe.elapsed_s, probe.error_detail,
                    )

        return findings

    # ---- Slice 12M — deep-probe env resolvers ----

    def _resolve_deep_probe_enabled(self) -> bool:
        """``JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_ENABLED`` — opt-in
        gate for the subprocess deep-probe path. Default FALSE. Any
        truthy value (``"1"`` / ``"true"`` / ``"yes"`` / ``"on"``,
        case-insensitive) opts in. NEVER raises."""
        try:
            raw = os.environ.get(_DEEP_PROBE_ENABLED_ENV, "").strip().lower()
        except Exception:  # noqa: BLE001
            return False
        return raw in ("1", "true", "yes", "on")

    def _resolve_deep_probe_timeout_s(self) -> float:
        """``JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_TIMEOUT_S`` —
        per-package subprocess timeout. Default 30s. Clamped to
        ``[1.0, 300.0]`` so a typo can't disable the bound. Invalid
        values fall back to default. NEVER raises."""
        try:
            raw = os.environ.get(_DEEP_PROBE_TIMEOUT_S_ENV, "").strip()
            if not raw:
                return _DEFAULT_DEEP_PROBE_TIMEOUT_S
            v = float(raw)
        except (TypeError, ValueError):
            return _DEFAULT_DEEP_PROBE_TIMEOUT_S
        return max(_DEEP_PROBE_TIMEOUT_FLOOR_S,
                   min(_DEEP_PROBE_TIMEOUT_CEIL_S, v))

    def _resolve_deep_probe_python_bin(self) -> str:
        """``JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_PYTHON_BIN`` —
        which Python interpreter the subprocess probe spawns.
        Default ``sys.executable`` (matches the running interpreter,
        same site-packages). NEVER raises."""
        try:
            raw = os.environ.get(_DEEP_PROBE_PYTHON_BIN_ENV, "").strip()
        except Exception:  # noqa: BLE001
            raw = ""
        return raw if raw else sys.executable

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
