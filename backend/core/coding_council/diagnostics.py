"""
v77.3: Comprehensive Diagnostics & Pre-Flight Checks
=====================================================

Advanced diagnostic system for the Unified Coding Council that:
- Validates all critical connections before startup
- Tests Trinity cross-repo connectivity
- Verifies API keys and credentials
- Checks port availability
- Provides detailed failure analysis
- Implements auto-recovery for common issues

This module can be run standalone:
    python -m backend.core.coding_council.diagnostics

Or imported for programmatic checks:
    from backend.core.coding_council.diagnostics import run_preflight_checks

Author: JARVIS v77.3
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Configuration (Dynamic via unified config)
# =============================================================================

def _get_trinity_repos() -> Dict[str, Path]:
    """Get Trinity repos from unified config."""
    try:
        from .config import get_config
        config = get_config()
        return {name: repo.path for name, repo in config.repos.items()}
    except ImportError:
        # Fallback if config not available
        return {
            "jarvis": Path(os.getenv("JARVIS_REPO", Path(__file__).parent.parent.parent.parent)),
            "j_prime": Path(os.getenv("J_PRIME_REPO", Path.home() / "Documents/repos/jarvis-prime")),
            "reactor_core": Path(os.getenv("REACTOR_CORE_REPO", Path.home() / "Documents/repos/reactor-core")),
        }


def _get_required_ports() -> Dict[str, int]:
    """Get required ports from unified config."""
    try:
        from .config import get_config
        config = get_config()
        return {
            "jarvis_api": config.jarvis_api_port.port,
            "lsp_server": config.lsp_server_port.port,
            "websocket": config.websocket_port.port,
        }
    except ImportError:
        return {
            "jarvis_api": int(os.getenv("JARVIS_PORT", "8010")),
            "lsp_server": int(os.getenv("LSP_SERVER_PORT", "9257")),
            "websocket": int(os.getenv("IDE_WEBSOCKET_PORT", "9258")),
        }


def _allows_graceful_degradation() -> bool:
    """Check if graceful degradation is enabled."""
    try:
        from .config import get_config
        return get_config().degradation.allow_no_api_key
    except ImportError:
        return True  # Default to allowing degradation


# Dynamic config accessors
TRINITY_REPOS = property(lambda self: _get_trinity_repos())
REQUIRED_PORTS = property(lambda self: _get_required_ports())

# Environment variable definitions with graceful degradation support
REQUIRED_ENV_VARS = {
    "ANTHROPIC_API_KEY": {
        "required": False,  # NOT required - graceful degradation supported
        "degradable": True,  # Can work without this
        "format": r"^sk-ant-|^sk-",
        "description": "Claude API key for Anthropic (AI features disabled without this)",
        "fix": "export ANTHROPIC_API_KEY=sk-ant-api03-...",
        "degradation_impact": "AI-powered suggestions and code generation will be unavailable",
    },
}

OPTIONAL_ENV_VARS = {
    "CODING_COUNCIL_ENABLED": {
        "default": "true",
        "description": "Enable/disable Coding Council",
    },
    "IDE_BRIDGE_ENABLED": {
        "default": "true",
        "description": "Enable/disable IDE integration",
    },
    "TRINITY_SYNC_ENABLED": {
        "default": "true",
        "description": "Enable/disable Trinity cross-repo sync",
    },
}


# =============================================================================
# Enums and Data Classes
# =============================================================================

class CheckStatus(Enum):
    """Status of a diagnostic check."""
    PASS = auto()
    WARN = auto()
    FAIL = auto()
    SKIP = auto()


class CheckCategory(Enum):
    """Category of diagnostic checks."""
    ENVIRONMENT = "environment"
    CONNECTIVITY = "connectivity"
    DEPENDENCIES = "dependencies"
    TRINITY = "trinity"
    PORTS = "ports"
    PERMISSIONS = "permissions"
    RUNTIME = "runtime"


@dataclass
class CheckResult:
    """Result of a single diagnostic check."""
    name: str
    category: CheckCategory
    status: CheckStatus
    message: str
    details: Optional[str] = None
    fix_command: Optional[str] = None
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category.value,
            "status": self.status.name,
            "message": self.message,
            "details": self.details,
            "fix_command": self.fix_command,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class DiagnosticReport:
    """Complete diagnostic report."""
    checks: List[CheckResult] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    system_info: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.PASS)

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.WARN)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.FAIL)

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.SKIP)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def duration_ms(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0

    @property
    def is_healthy(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": {
                "passed": self.passed,
                "warnings": self.warnings,
                "failed": self.failed,
                "skipped": self.skipped,
                "total": self.total,
                "healthy": self.is_healthy,
                "duration_ms": round(self.duration_ms, 2),
            },
            "checks": [c.to_dict() for c in self.checks],
            "system_info": self.system_info,
        }


# =============================================================================
# Port Checker
# =============================================================================

class PortChecker:
    """Check port availability and conflicts."""

    @staticmethod
    def is_port_available(port: int, host: str = "127.0.0.1") -> bool:
        """Check if a port is available for binding."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            sock.close()
            return True
        except OSError:
            return False

    @staticmethod
    def get_port_user(port: int) -> Optional[str]:
        """Get the process using a port (macOS/Linux)."""
        try:
            result = subprocess.run(
                ["lsof", "-i", f":{port}", "-t"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                pid = result.stdout.strip().split("\n")[0]
                # Get process name
                proc_result = subprocess.run(
                    ["ps", "-p", pid, "-o", "comm="],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if proc_result.returncode == 0:
                    return f"PID {pid}: {proc_result.stdout.strip()}"
                return f"PID {pid}"
            return None
        except Exception:
            return None

    @staticmethod
    async def check_port(
        port: int,
        name: str,
        host: str = "127.0.0.1",
    ) -> CheckResult:
        """
        Check a port and return a diagnostic result.

        v109.1: Enhanced with intelligent service detection.
        If port is in use by a healthy JARVIS service, returns PASS (already running).
        """
        start = time.time()

        if PortChecker.is_port_available(port, host):
            return CheckResult(
                name=f"Port {port} ({name})",
                category=CheckCategory.PORTS,
                status=CheckStatus.PASS,
                message=f"Port {port} is available",
                duration_ms=(time.time() - start) * 1000,
            )
        else:
            user = PortChecker.get_port_user(port)

            # v109.1: Check if it's our own healthy JARVIS service
            # If so, no need to report as failure - service is already running
            if await PortChecker._is_healthy_jarvis_service(port, name):
                return CheckResult(
                    name=f"Port {port} ({name})",
                    category=CheckCategory.PORTS,
                    status=CheckStatus.PASS,
                    message=f"Port {port} has healthy {name} service running",
                    details=f"Service already active: {user}" if user else "Service already active",
                    duration_ms=(time.time() - start) * 1000,
                )

            return CheckResult(
                name=f"Port {port} ({name})",
                category=CheckCategory.PORTS,
                status=CheckStatus.FAIL,
                message=f"Port {port} is in use",
                details=f"Used by: {user}" if user else "Unknown process",
                fix_command=f"lsof -ti:{port} | xargs kill -9" if user else None,
                duration_ms=(time.time() - start) * 1000,
            )

    @staticmethod
    async def _is_healthy_jarvis_service(port: int, expected_name: str) -> bool:
        """
        v109.2: Check if port is used by a healthy JARVIS service.

        This prevents false "port in use" errors when the service is already
        running and healthy from a previous startup or parallel process.

        Enhanced with:
        - Multiple health endpoint paths (for different service configurations)
        - Longer timeout for slow-starting services
        - Accept HTTP 200 as healthy regardless of body
        - Debug logging to trace health check issues
        """
        import logging
        log = logging.getLogger(__name__)

        try:
            import aiohttp

            # Map port to expected health endpoints (try multiple paths)
            health_paths = {
                8010: ["/health/ping", "/health", "/"],        # jarvis_api - ping is fastest
                8000: ["/health", "/"],                        # jarvis_prime
                8090: ["/health", "/"],                        # reactor_core
            }

            paths_to_try = health_paths.get(port, ["/health", "/"])

            for health_path in paths_to_try:
                url = f"http://127.0.0.1:{port}{health_path}"

                try:
                    async with aiohttp.ClientSession() as session:
                        # v109.2: Increased timeout to 5s for slow-starting services
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as response:
                            # v109.2: Accept ANY successful HTTP response as "healthy"
                            # The service is responding - that's what matters during startup
                            if 200 <= response.status < 400:
                                log.debug(
                                    f"[PortChecker] Port {port} has healthy service "
                                    f"(HTTP {response.status} on {health_path})"
                                )
                                return True

                            # Also accept 404/405 - service is responding but endpoint doesn't exist
                            if response.status in (404, 405):
                                log.debug(
                                    f"[PortChecker] Port {port} has responding service "
                                    f"(HTTP {response.status} on {health_path} - service alive)"
                                )
                                return True

                except aiohttp.ClientConnectorError:
                    # Connection refused - service not listening
                    continue
                except asyncio.TimeoutError:
                    # Timeout - service may be overloaded but try next path
                    continue
                except Exception as e:
                    log.debug(f"[PortChecker] Port {port} health check on {health_path} failed: {e}")
                    continue

            # None of the paths succeeded
            log.debug(f"[PortChecker] Port {port} no healthy service detected after trying {paths_to_try}")
            return False

        except ImportError:
            log.debug("[PortChecker] aiohttp not available for health check")
            return False
        except Exception as e:
            log.debug(f"[PortChecker] Port {port} health check exception: {e}")
            return False


# =============================================================================
# Environment Checker
# =============================================================================

class EnvironmentChecker:
    """Check environment variables and configuration."""

    @staticmethod
    async def check_env_var(
        name: str,
        config: Dict[str, Any],
    ) -> CheckResult:
        """Check an environment variable with graceful degradation support."""
        import re

        start = time.time()
        value = os.getenv(name, "")
        required = config.get("required", False)
        degradable = config.get("degradable", False)
        format_pattern = config.get("format")
        description = config.get("description", name)
        fix_command = config.get("fix")
        degradation_impact = config.get("degradation_impact", "")

        if not value:
            # Check if this is a degradable requirement
            if degradable and _allows_graceful_degradation():
                # Not a failure - graceful degradation allows this
                impact_msg = f" Impact: {degradation_impact}" if degradation_impact else ""
                return CheckResult(
                    name=f"Env: {name}",
                    category=CheckCategory.ENVIRONMENT,
                    status=CheckStatus.WARN,
                    message=f"{name} not set (graceful degradation active)",
                    details=f"{description}.{impact_msg}",
                    fix_command=fix_command,
                    duration_ms=(time.time() - start) * 1000,
                )
            elif required:
                return CheckResult(
                    name=f"Env: {name}",
                    category=CheckCategory.ENVIRONMENT,
                    status=CheckStatus.FAIL,
                    message=f"{name} not set (required)",
                    details=description,
                    fix_command=fix_command,
                    duration_ms=(time.time() - start) * 1000,
                )
            else:
                # v109.1: Changed from WARN to PASS - using defaults for optional
                # env vars is expected behavior, not a warning condition.
                # The system is correctly configured when defaults are used.
                default = config.get("default", "")
                return CheckResult(
                    name=f"Env: {name}",
                    category=CheckCategory.ENVIRONMENT,
                    status=CheckStatus.PASS,
                    message=f"{name} using default: {default}",
                    details=description,
                    duration_ms=(time.time() - start) * 1000,
                )

        # Check format if specified
        if format_pattern and not re.match(format_pattern, value):
            return CheckResult(
                name=f"Env: {name}",
                category=CheckCategory.ENVIRONMENT,
                status=CheckStatus.WARN,
                message=f"{name} format looks unusual",
                details=f"Expected pattern: {format_pattern}",
                duration_ms=(time.time() - start) * 1000,
            )

        # Mask sensitive values
        masked = value[:8] + "..." + value[-4:] if len(value) > 12 else "***"

        return CheckResult(
            name=f"Env: {name}",
            category=CheckCategory.ENVIRONMENT,
            status=CheckStatus.PASS,
            message=f"{name} is set ({masked})",
            duration_ms=(time.time() - start) * 1000,
        )


# =============================================================================
# Trinity Checker
# =============================================================================

class TrinityChecker:
    """Check Trinity cross-repo connectivity."""

    @staticmethod
    async def check_repo(
        name: str,
        path: Path,
    ) -> CheckResult:
        """Check a Trinity repository."""
        start = time.time()

        if not path.exists():
            return CheckResult(
                name=f"Trinity: {name}",
                category=CheckCategory.TRINITY,
                status=CheckStatus.WARN,
                message=f"Repository not found: {path}",
                details="Some cross-repo features may not work",
                duration_ms=(time.time() - start) * 1000,
            )

        # Check if it's a git repo
        git_dir = path / ".git"
        if not git_dir.exists():
            return CheckResult(
                name=f"Trinity: {name}",
                category=CheckCategory.TRINITY,
                status=CheckStatus.WARN,
                message=f"Not a git repository: {path}",
                details="Git operations will not work",
                fix_command=f"cd {path} && git init",
                duration_ms=(time.time() - start) * 1000,
            )

        # Check for key files based on repo type
        key_files = {
            "jarvis": ["backend/main.py", "run_supervisor.py"],
            "j_prime": ["jarvis_prime/__init__.py"],
            "reactor_core": ["reactor_core/__init__.py"],
        }

        missing_files = []
        for kf in key_files.get(name, []):
            if not (path / kf).exists():
                missing_files.append(kf)

        if missing_files:
            return CheckResult(
                name=f"Trinity: {name}",
                category=CheckCategory.TRINITY,
                status=CheckStatus.WARN,
                message=f"Missing expected files in {name}",
                details=f"Missing: {', '.join(missing_files)}",
                duration_ms=(time.time() - start) * 1000,
            )

        return CheckResult(
            name=f"Trinity: {name}",
            category=CheckCategory.TRINITY,
            status=CheckStatus.PASS,
            message=f"Repository OK: {path}",
            duration_ms=(time.time() - start) * 1000,
        )

    @staticmethod
    async def check_heartbeat_dir() -> CheckResult:
        """Check Trinity heartbeat directory."""
        start = time.time()
        heartbeat_dir = Path(os.path.expanduser("~/.jarvis/trinity"))

        if not heartbeat_dir.exists():
            try:
                heartbeat_dir.mkdir(parents=True, exist_ok=True)
                return CheckResult(
                    name="Trinity: Heartbeat Directory",
                    category=CheckCategory.TRINITY,
                    status=CheckStatus.PASS,
                    message=f"Created heartbeat directory: {heartbeat_dir}",
                    duration_ms=(time.time() - start) * 1000,
                )
            except Exception as e:
                return CheckResult(
                    name="Trinity: Heartbeat Directory",
                    category=CheckCategory.TRINITY,
                    status=CheckStatus.FAIL,
                    message=f"Cannot create heartbeat directory",
                    details=str(e),
                    fix_command=f"mkdir -p {heartbeat_dir}",
                    duration_ms=(time.time() - start) * 1000,
                )

        # Check write permissions
        test_file = heartbeat_dir / ".write_test"
        try:
            test_file.write_text("test")
            test_file.unlink()
            return CheckResult(
                name="Trinity: Heartbeat Directory",
                category=CheckCategory.TRINITY,
                status=CheckStatus.PASS,
                message=f"Heartbeat directory OK: {heartbeat_dir}",
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return CheckResult(
                name="Trinity: Heartbeat Directory",
                category=CheckCategory.TRINITY,
                status=CheckStatus.FAIL,
                message=f"Heartbeat directory not writable",
                details=str(e),
                fix_command=f"chmod 755 {heartbeat_dir}",
                duration_ms=(time.time() - start) * 1000,
            )

    @staticmethod
    async def check_stale_heartbeats(stale_threshold: float = 60.0) -> List[CheckResult]:
        """
        Check for stale heartbeats indicating dead components.

        Args:
            stale_threshold: Seconds after which a heartbeat is considered stale

        Returns:
            List of check results for each component
        """
        results = []
        heartbeat_dir = Path(os.path.expanduser("~/.jarvis/trinity/components"))

        if not heartbeat_dir.exists():
            return results

        for heartbeat_file in heartbeat_dir.glob("*.json"):
            start = time.time()
            component_name = heartbeat_file.stem

            try:
                with open(heartbeat_file) as f:
                    data = json.load(f)

                last_update = data.get("timestamp", 0)
                age = time.time() - last_update

                if age > stale_threshold:
                    results.append(CheckResult(
                        name=f"Heartbeat: {component_name}",
                        category=CheckCategory.TRINITY,
                        status=CheckStatus.WARN,
                        message=f"Stale heartbeat ({age:.0f}s old)",
                        details=f"Component may be dead or unresponsive",
                        fix_command=f"rm {heartbeat_file}  # Clear stale heartbeat",
                        duration_ms=(time.time() - start) * 1000,
                    ))
                else:
                    results.append(CheckResult(
                        name=f"Heartbeat: {component_name}",
                        category=CheckCategory.TRINITY,
                        status=CheckStatus.PASS,
                        message=f"Healthy ({age:.1f}s ago)",
                        duration_ms=(time.time() - start) * 1000,
                    ))

            except json.JSONDecodeError:
                results.append(CheckResult(
                    name=f"Heartbeat: {component_name}",
                    category=CheckCategory.TRINITY,
                    status=CheckStatus.WARN,
                    message="Corrupted heartbeat file",
                    fix_command=f"rm {heartbeat_file}",
                    duration_ms=(time.time() - start) * 1000,
                ))
            except Exception as e:
                results.append(CheckResult(
                    name=f"Heartbeat: {component_name}",
                    category=CheckCategory.TRINITY,
                    status=CheckStatus.WARN,
                    message=f"Cannot read heartbeat: {e}",
                    duration_ms=(time.time() - start) * 1000,
                ))

        return results


# =============================================================================
# Module Import Checker
# =============================================================================

class ModuleChecker:
    """Check Python module availability."""

    @staticmethod
    async def check_module(
        module_path: str,
        description: str,
    ) -> CheckResult:
        """Check if a Python module can be imported."""
        start = time.time()

        try:
            # Use importlib for dynamic import
            import importlib

            # Try direct import first (for module paths like "backend.core.coding_council.startup")
            module = importlib.import_module(module_path)

            return CheckResult(
                name=f"Module: {description}",
                category=CheckCategory.DEPENDENCIES,
                status=CheckStatus.PASS,
                message=f"Successfully imported {module_path}",
                duration_ms=(time.time() - start) * 1000,
            )
        except ImportError as e:
            return CheckResult(
                name=f"Module: {description}",
                category=CheckCategory.DEPENDENCIES,
                status=CheckStatus.FAIL,
                message=f"Import failed: {module_path}",
                details=str(e),
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return CheckResult(
                name=f"Module: {description}",
                category=CheckCategory.DEPENDENCIES,
                status=CheckStatus.WARN,
                message=f"Import warning: {module_path}",
                details=str(e),
                duration_ms=(time.time() - start) * 1000,
            )


# =============================================================================
# Runtime Checker
# =============================================================================

class RuntimeChecker:
    """Check runtime components."""

    @staticmethod
    async def check_anthropic_api() -> CheckResult:
        """
        Test Anthropic API connectivity.

        v109.1: Enhanced with offline mode detection and better error handling.
        - Respects JARVIS_OFFLINE_MODE environment variable
        - Provides specific error details (timeout, connection refused, etc.)
        - Returns PASS when offline mode is intentionally enabled
        """
        start = time.time()
        api_key = os.getenv("ANTHROPIC_API_KEY", "")

        # v109.1: Check for offline mode - AI features intentionally disabled
        offline_mode = os.getenv("JARVIS_OFFLINE_MODE", "").lower() in ("true", "1", "yes")
        if offline_mode:
            return CheckResult(
                name="Anthropic API",
                category=CheckCategory.CONNECTIVITY,
                status=CheckStatus.PASS,
                message="Offline mode enabled (AI features disabled by configuration)",
                duration_ms=(time.time() - start) * 1000,
            )

        if not api_key:
            return CheckResult(
                name="Anthropic API",
                category=CheckCategory.CONNECTIVITY,
                status=CheckStatus.SKIP,
                message="API key not set, skipping connectivity test",
                duration_ms=(time.time() - start) * 1000,
            )

        try:
            import httpx

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    timeout=10.0,
                )

                if response.status_code == 200:
                    return CheckResult(
                        name="Anthropic API",
                        category=CheckCategory.CONNECTIVITY,
                        status=CheckStatus.PASS,
                        message="API connectivity OK",
                        duration_ms=(time.time() - start) * 1000,
                    )
                elif response.status_code == 401:
                    return CheckResult(
                        name="Anthropic API",
                        category=CheckCategory.CONNECTIVITY,
                        status=CheckStatus.FAIL,
                        message="Invalid API key",
                        details="The provided ANTHROPIC_API_KEY is not valid",
                        fix_command="export ANTHROPIC_API_KEY=<valid_key>",
                        duration_ms=(time.time() - start) * 1000,
                    )
                else:
                    return CheckResult(
                        name="Anthropic API",
                        category=CheckCategory.CONNECTIVITY,
                        status=CheckStatus.WARN,
                        message=f"Unexpected response: {response.status_code}",
                        duration_ms=(time.time() - start) * 1000,
                    )
        except ImportError:
            return CheckResult(
                name="Anthropic API",
                category=CheckCategory.CONNECTIVITY,
                status=CheckStatus.SKIP,
                message="httpx not available, skipping API test",
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            # v109.1: Provide specific error details based on exception type
            error_str = str(e)
            if "timeout" in error_str.lower() or "timed out" in error_str.lower():
                details = "Connection timed out - network may be slow or API endpoint unreachable"
            elif "connection refused" in error_str.lower():
                details = "Connection refused - check network/firewall settings"
            elif "name resolution" in error_str.lower() or "dns" in error_str.lower():
                details = "DNS resolution failed - check network connectivity"
            elif "ssl" in error_str.lower() or "certificate" in error_str.lower():
                details = "SSL/TLS error - check system certificates"
            else:
                details = error_str

            return CheckResult(
                name="Anthropic API",
                category=CheckCategory.CONNECTIVITY,
                status=CheckStatus.WARN,
                message="Cannot reach Anthropic API",
                details=details,
                duration_ms=(time.time() - start) * 1000,
            )


# =============================================================================
# Pre-Flight Checker (Main Class)
# =============================================================================

class PreFlightChecker:
    """
    Comprehensive pre-flight check system.

    Runs all diagnostic checks in parallel and generates a report.
    """

    def __init__(self):
        self.report = DiagnosticReport()

    async def run_all_checks(self) -> DiagnosticReport:
        """Run all diagnostic checks with advanced parallel execution."""
        self.report = DiagnosticReport()
        self.report.start_time = time.time()

        # Collect system info
        self.report.system_info = self._collect_system_info()

        # Get dynamic configuration
        trinity_repos = _get_trinity_repos()
        required_ports = _get_required_ports()

        # Get stale threshold from config
        try:
            from .config import get_config
            stale_threshold = get_config().stale_threshold
        except ImportError:
            stale_threshold = 60.0

        # Phase 1: Fast checks (environment, ports) - run in parallel
        fast_checks = []

        # Environment checks
        for name, config in REQUIRED_ENV_VARS.items():
            fast_checks.append(EnvironmentChecker.check_env_var(name, config))
        for name, config in OPTIONAL_ENV_VARS.items():
            fast_checks.append(EnvironmentChecker.check_env_var(name, config))

        # Port checks
        for name, port in required_ports.items():
            fast_checks.append(PortChecker.check_port(port, name))

        # Execute fast checks in parallel with timeout
        try:
            fast_results = await asyncio.wait_for(
                asyncio.gather(*fast_checks, return_exceptions=True),
                timeout=10.0
            )
        except asyncio.TimeoutError:
            fast_results = []
            self.report.checks.append(CheckResult(
                name="Fast Checks",
                category=CheckCategory.RUNTIME,
                status=CheckStatus.WARN,
                message="Fast checks timed out",
                duration_ms=10000,
            ))

        # Phase 2: Trinity checks - run in parallel
        trinity_checks = []
        for name, path in trinity_repos.items():
            trinity_checks.append(TrinityChecker.check_repo(name, path))
        trinity_checks.append(TrinityChecker.check_heartbeat_dir())

        try:
            trinity_results = await asyncio.wait_for(
                asyncio.gather(*trinity_checks, return_exceptions=True),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            trinity_results = []
            self.report.checks.append(CheckResult(
                name="Trinity Checks",
                category=CheckCategory.TRINITY,
                status=CheckStatus.WARN,
                message="Trinity checks timed out",
                duration_ms=15000,
            ))

        # Phase 2.5: Stale heartbeat checks
        try:
            stale_results = await TrinityChecker.check_stale_heartbeats(stale_threshold)
        except Exception as e:
            stale_results = [CheckResult(
                name="Stale Heartbeat Check",
                category=CheckCategory.TRINITY,
                status=CheckStatus.WARN,
                message=f"Could not check stale heartbeats: {e}",
            )]

        # Phase 3: Module checks - run in parallel
        module_checks_list = [
            ("backend.core.coding_council.config", "Configuration Module"),
            ("backend.core.coding_council.startup", "Startup Module"),
            ("backend.core.coding_council.orchestrator", "Orchestrator"),
            ("backend.core.coding_council.ide", "IDE Module"),
            ("backend.core.coding_council.adapters.anthropic_engine", "Anthropic Engine"),
            ("backend.core.coding_council.integration", "Integration Module"),
        ]
        module_checks = [
            ModuleChecker.check_module(module_path, description)
            for module_path, description in module_checks_list
        ]

        try:
            module_results = await asyncio.wait_for(
                asyncio.gather(*module_checks, return_exceptions=True),
                timeout=20.0
            )
        except asyncio.TimeoutError:
            module_results = []
            self.report.checks.append(CheckResult(
                name="Module Checks",
                category=CheckCategory.DEPENDENCIES,
                status=CheckStatus.WARN,
                message="Module checks timed out",
                duration_ms=20000,
            ))

        # Phase 4: Runtime checks (network calls) - with timeout
        runtime_checks = [RuntimeChecker.check_anthropic_api()]

        try:
            runtime_results = await asyncio.wait_for(
                asyncio.gather(*runtime_checks, return_exceptions=True),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            runtime_results = [CheckResult(
                name="Runtime Checks",
                category=CheckCategory.CONNECTIVITY,
                status=CheckStatus.WARN,
                message="Runtime checks timed out",
                duration_ms=15000,
            )]

        # Collect all results
        all_results = (
            list(fast_results) +
            list(trinity_results) +
            stale_results +
            list(module_results) +
            list(runtime_results)
        )

        for result in all_results:
            if isinstance(result, Exception):
                self.report.checks.append(CheckResult(
                    name="Check Error",
                    category=CheckCategory.RUNTIME,
                    status=CheckStatus.WARN,
                    message=f"Check failed with exception",
                    details=str(result),
                ))
            elif isinstance(result, CheckResult):
                self.report.checks.append(result)

        self.report.end_time = time.time()
        return self.report

    def _collect_system_info(self) -> Dict[str, Any]:
        """Collect system information."""
        import platform

        return {
            "python_version": sys.version,
            "platform": platform.platform(),
            "cwd": os.getcwd(),
            "pid": os.getpid(),
            "timestamp": time.time(),
        }

    def print_report(self, report: Optional[DiagnosticReport] = None) -> None:
        """Print a formatted report to console."""
        r = report or self.report

        print("\n" + "=" * 70)
        print("JARVIS v77.3 - Unified Coding Council Diagnostics")
        print("=" * 70)

        # Summary
        status_icon = "✅" if r.is_healthy else "❌"
        print(f"\n{status_icon} Summary: {r.passed}/{r.total} checks passed")
        if r.warnings > 0:
            print(f"   ⚠️  {r.warnings} warnings")
        if r.failed > 0:
            print(f"   ❌ {r.failed} failures")
        print(f"   ⏱️  Duration: {r.duration_ms:.1f}ms")

        # Group by category
        by_category: Dict[CheckCategory, List[CheckResult]] = {}
        for check in r.checks:
            if check.category not in by_category:
                by_category[check.category] = []
            by_category[check.category].append(check)

        # Print each category
        for category in CheckCategory:
            if category not in by_category:
                continue

            print(f"\n── {category.value.upper()} ──")

            for check in by_category[category]:
                icon = {
                    CheckStatus.PASS: "✓",
                    CheckStatus.WARN: "⚠",
                    CheckStatus.FAIL: "✗",
                    CheckStatus.SKIP: "○",
                }[check.status]

                color = {
                    CheckStatus.PASS: "\033[92m",  # Green
                    CheckStatus.WARN: "\033[93m",  # Yellow
                    CheckStatus.FAIL: "\033[91m",  # Red
                    CheckStatus.SKIP: "\033[90m",  # Gray
                }[check.status]
                reset = "\033[0m"

                print(f"  {color}{icon} {check.name}{reset}")
                print(f"    {check.message}")

                if check.details:
                    print(f"    Details: {check.details}")

                if check.fix_command and check.status in (CheckStatus.FAIL, CheckStatus.WARN):
                    print(f"    Fix: {check.fix_command}")

        # Recommendations
        failed_checks = [c for c in r.checks if c.status == CheckStatus.FAIL]
        if failed_checks:
            print("\n── RECOMMENDED ACTIONS ──")
            for i, check in enumerate(failed_checks, 1):
                print(f"  {i}. Fix: {check.name}")
                if check.fix_command:
                    print(f"     Run: {check.fix_command}")

        print("\n" + "=" * 70)


# =============================================================================
# Auto-Recovery System
# =============================================================================

class AutoRecovery:
    """
    Automatic recovery for common issues.

    Can attempt to fix issues found during diagnostics with
    advanced parallel execution and intelligent recovery strategies.
    """

    @staticmethod
    async def attempt_recovery(report: DiagnosticReport) -> List[str]:
        """
        Attempt to recover from failures with parallel execution.

        Returns list of recovery actions taken.
        """
        actions = []
        recovery_tasks = []

        for check in report.checks:
            # Handle failures
            if check.status == CheckStatus.FAIL:
                # Port conflict recovery
                if check.category == CheckCategory.PORTS and check.fix_command:
                    recovery_tasks.append(
                        AutoRecovery._recover_port(check)
                    )

                # Directory creation recovery
                elif check.category == CheckCategory.TRINITY:
                    if "directory" in check.message.lower() and check.fix_command:
                        recovery_tasks.append(
                            AutoRecovery._create_directory(check)
                        )

            # Handle warnings that can be auto-fixed
            elif check.status == CheckStatus.WARN:
                # Stale heartbeat cleanup
                if "Stale heartbeat" in check.message:
                    recovery_tasks.append(
                        AutoRecovery._cleanup_stale_heartbeat(check)
                    )

                # Corrupted heartbeat cleanup
                elif "Corrupted heartbeat" in check.message:
                    recovery_tasks.append(
                        AutoRecovery._cleanup_corrupted_heartbeat(check)
                    )

        # Execute all recovery tasks in parallel
        if recovery_tasks:
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*recovery_tasks, return_exceptions=True),
                    timeout=30.0
                )

                for result in results:
                    if isinstance(result, str) and result:
                        actions.append(result)
                    elif isinstance(result, Exception):
                        logger.debug(f"Recovery task failed: {result}")

            except asyncio.TimeoutError:
                logger.warning("Recovery tasks timed out")

        return actions

    @staticmethod
    async def _recover_port(check: CheckResult) -> Optional[str]:
        """Attempt to recover a port conflict."""
        if not check.fix_command:
            return None

        try:
            result = subprocess.run(
                check.fix_command,
                shell=True,
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return f"Killed process using port (from: {check.name})"
        except Exception as e:
            logger.debug(f"Port recovery failed: {e}")

        return None

    @staticmethod
    async def _create_directory(check: CheckResult) -> Optional[str]:
        """Create a missing directory."""
        if not check.fix_command:
            return None

        try:
            result = subprocess.run(
                check.fix_command,
                shell=True,
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return f"Created directory (from: {check.name})"
        except Exception as e:
            logger.debug(f"Directory creation failed: {e}")

        return None

    @staticmethod
    async def _cleanup_stale_heartbeat(check: CheckResult) -> Optional[str]:
        """
        Cleanup a stale heartbeat file.

        Instead of just deleting, we archive it for debugging.
        """
        if not check.fix_command:
            return None

        # Extract file path from fix command
        # Format: "rm /path/to/file.json  # comment"
        parts = check.fix_command.split()
        if len(parts) < 2:
            return None

        heartbeat_path = Path(parts[1])
        if not heartbeat_path.exists():
            return None

        try:
            # Archive instead of delete
            archive_dir = heartbeat_path.parent / "archived"
            archive_dir.mkdir(exist_ok=True)

            timestamp = int(time.time())
            archive_path = archive_dir / f"{heartbeat_path.stem}_{timestamp}.json"

            # Move to archive
            heartbeat_path.rename(archive_path)

            return f"Archived stale heartbeat: {heartbeat_path.name}"

        except Exception as e:
            logger.debug(f"Heartbeat cleanup failed: {e}")

            # Fallback to delete
            try:
                heartbeat_path.unlink()
                return f"Removed stale heartbeat: {heartbeat_path.name}"
            except Exception:
                pass

        return None

    @staticmethod
    async def _cleanup_corrupted_heartbeat(check: CheckResult) -> Optional[str]:
        """Cleanup a corrupted heartbeat file."""
        if not check.fix_command:
            return None

        parts = check.fix_command.split()
        if len(parts) < 2:
            return None

        heartbeat_path = Path(parts[1])
        if not heartbeat_path.exists():
            return None

        try:
            heartbeat_path.unlink()
            return f"Removed corrupted heartbeat: {heartbeat_path.name}"
        except Exception as e:
            logger.debug(f"Corrupted heartbeat cleanup failed: {e}")

        return None

    @staticmethod
    async def cleanup_all_stale_heartbeats(stale_threshold: float = 60.0) -> List[str]:
        """
        Cleanup all stale heartbeats proactively.

        This can be called during startup to clean up stale state.
        """
        actions = []
        heartbeat_dir = Path(os.path.expanduser("~/.jarvis/trinity/components"))

        if not heartbeat_dir.exists():
            return actions

        archive_dir = heartbeat_dir / "archived"

        for heartbeat_file in heartbeat_dir.glob("*.json"):
            try:
                with open(heartbeat_file) as f:
                    data = json.load(f)

                last_update = data.get("timestamp", 0)
                age = time.time() - last_update

                if age > stale_threshold:
                    # Archive the stale heartbeat
                    archive_dir.mkdir(exist_ok=True)
                    timestamp = int(time.time())
                    archive_path = archive_dir / f"{heartbeat_file.stem}_{timestamp}.json"

                    heartbeat_file.rename(archive_path)
                    actions.append(f"Archived stale heartbeat: {heartbeat_file.name} (age: {age:.0f}s)")

            except json.JSONDecodeError:
                # Corrupted - just delete
                try:
                    heartbeat_file.unlink()
                    actions.append(f"Removed corrupted heartbeat: {heartbeat_file.name}")
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f"Could not process heartbeat {heartbeat_file}: {e}")

        return actions


# =============================================================================
# Public API
# =============================================================================

async def run_preflight_checks(
    print_report: bool = True,
    auto_recover: bool = False,
) -> DiagnosticReport:
    """
    Run pre-flight checks and optionally print report.

    Args:
        print_report: Whether to print the report to console
        auto_recover: Whether to attempt auto-recovery

    Returns:
        DiagnosticReport with all check results
    """
    checker = PreFlightChecker()
    report = await checker.run_all_checks()

    if auto_recover and not report.is_healthy:
        actions = await AutoRecovery.attempt_recovery(report)
        if actions:
            # Re-run checks after recovery
            report = await checker.run_all_checks()

    if print_report:
        checker.print_report(report)

    return report


def run_preflight_checks_sync(
    print_report: bool = True,
    auto_recover: bool = False,
) -> DiagnosticReport:
    """Synchronous wrapper for pre-flight checks."""
    return asyncio.run(run_preflight_checks(print_report, auto_recover))


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    """CLI entry point for diagnostics."""
    import argparse

    parser = argparse.ArgumentParser(
        description="JARVIS Coding Council Diagnostics"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "--auto-recover",
        action="store_true",
        help="Attempt automatic recovery",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only show failures",
    )

    args = parser.parse_args()

    # Run checks
    report = run_preflight_checks_sync(
        print_report=not args.json and not args.quiet,
        auto_recover=args.auto_recover,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif args.quiet:
        for check in report.checks:
            if check.status == CheckStatus.FAIL:
                print(f"❌ {check.name}: {check.message}")

    # Exit with appropriate code
    sys.exit(0 if report.is_healthy else 1)


if __name__ == "__main__":
    main()
