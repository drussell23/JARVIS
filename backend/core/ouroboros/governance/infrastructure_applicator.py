"""
InfrastructureApplicator — Deterministic post-APPLY hook for infrastructure operations.

Boundary Principle:
  The agentic layer discovers WHAT needs to change (writes requirements.txt).
  This module deterministically executes the KNOWN consequence (pip install).
  No model inference. No routing decisions. Pure deterministic skeleton.

Trigger: When the APPLY phase successfully modifies a file that has a known
infrastructure consequence (e.g., requirements.txt -> pip install).

State Integrity: The Ouroboros cycle does NOT advance to COMPLETE until the
infrastructure operation returns exit code 0. Failure rolls back the file
change and marks the operation as FAILED.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deterministic file->command mappings — Tier 0 (no inference needed)
# ---------------------------------------------------------------------------
# Each entry: (file pattern, command factory key, description)
# This is a declarative mapping of known infrastructure consequences.
# Adding a new mapping is a config change, not a code change.

_INFRA_MAPPINGS: Tuple[Tuple[str, str, str], ...] = (
    (
        "requirements.txt",
        "pip_install",
        "Python dependency installation",
    ),
    (
        "requirements-dev.txt",
        "pip_install",
        "Python dev dependency installation",
    ),
    (
        "package.json",
        "npm_install",
        "Node.js dependency installation",
    ),
    (
        ".env",
        "env_reload",
        "Environment variable reload",
    ),
    (
        "backend/.env",
        "env_reload",
        "Backend environment variable reload",
    ),
)

# Environment-driven configuration
_PIP_TIMEOUT_S = float(os.environ.get("JARVIS_PIP_INSTALL_TIMEOUT_S", "300"))
_NPM_TIMEOUT_S = float(os.environ.get("JARVIS_NPM_INSTALL_TIMEOUT_S", "300"))
_MAX_OUTPUT_BYTES = int(os.environ.get("JARVIS_INFRA_MAX_OUTPUT_BYTES", "262144"))


@dataclass(frozen=True)
class InfraResult:
    """Outcome of an infrastructure operation."""
    success: bool
    command: str
    exit_code: int
    duration_s: float
    stdout_tail: str
    stderr_tail: str
    file_trigger: str


# Slice 160 — environment-aware manifest resolution. The agentic layer may write a
# generic "requirements.txt", but the live environment's ACTIVE manifest can differ
# (the soak container ships requirements-soak*.txt, not requirements.txt). Resolve the
# manifest actually present in the repo root by priority instead of hardcoding one.
_DEFAULT_MANIFEST_PRIORITY: Tuple[str, ...] = (
    "requirements-soak-oracle.txt",  # Oracle-capable soak (most specific)
    "requirements-soak.txt",          # lean soak
    "requirements.txt",               # full host / standard
)


def _manifest_priority() -> Tuple[str, ...]:
    """Manifest resolution priority (env-tunable, no hardcode in logic). Highest
    priority first. Invalid/empty env → the default order. NEVER raises."""
    raw = (os.environ.get("JARVIS_INFRA_MANIFEST_PRIORITY", "") or "").strip()
    if raw:
        parts = tuple(p.strip() for p in raw.split(",") if p.strip())
        if parts:
            return parts
    return _DEFAULT_MANIFEST_PRIORITY


def _resolve_pip_manifest(project_root: Path, requested_file: str) -> Optional[str]:
    """Slice 160 — resolve the active pip manifest. The exact requested file wins if it
    is actually present; otherwise the highest-priority manifest present in the repo
    root; otherwise None (nothing to install → caller skips, never a hard fail).
    Environment-aware, no hardcoded single-file assumption. NEVER raises."""
    try:
        if requested_file and (project_root / requested_file).is_file():
            return requested_file
        for cand in _manifest_priority():
            if (project_root / cand).is_file():
                return cand
    except Exception:  # noqa: BLE001 — defensive
        pass
    return None


def _build_pip_argv(project_root: Path, file_path: str) -> List[str]:
    """Build pip install argv using the project's venv Python, targeting the
    dynamically-resolved active manifest (Slice 160). Falls back to the requested
    file when nothing resolvable is present (the fail-soft layer then catches it)."""
    target = _resolve_pip_manifest(project_root, file_path) or file_path
    venv_pip = project_root / "venv" / "bin" / "pip"
    if venv_pip.exists():
        return [str(venv_pip), "install", "-r", target]
    return [sys.executable, "-m", "pip", "install", "-r", target]


def infra_fail_soft_enabled() -> bool:
    """Slice 160 — master for fail-soft infra. Default **TRUE** (failure-path-only: an
    infra hook failure flags INFRA_WARNING and the op continues to the governance floor
    instead of terminally dying; =0 reverts to legacy terminal-FAILED). NEVER raises."""
    return os.environ.get("JARVIS_INFRA_FAIL_SOFT_ENABLED", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def summarize_infra_failures(results: List["InfraResult"]) -> str:
    """Compact INFRA_WARNING text from failed infra results — surfaced in the op
    context + the Discord #governance-gates embed so the operator can decide."""
    parts = []
    for r in results:
        if not getattr(r, "success", True):
            tail = (getattr(r, "stderr_tail", "") or "").strip().replace("\n", " ")
            parts.append(
                f"{getattr(r, 'file_trigger', '?')} (exit={getattr(r, 'exit_code', -1)}): "
                f"{tail[:200]}"
            )
    return " | ".join(parts) if parts else ""


def _build_npm_argv(project_root: Path, file_path: str) -> List[str]:
    """Build npm install argv."""
    return ["npm", "install", "--prefix", str(project_root / Path(file_path).parent)]


_ENV_RELOAD_TIMEOUT_S = 5.0  # env reload is instant, but cap it

_COMMAND_BUILDERS: Dict[str, Tuple[Any, float]] = {
    "pip_install": (_build_pip_argv, _PIP_TIMEOUT_S),
    "npm_install": (_build_npm_argv, _NPM_TIMEOUT_S),
}

# In-process operations (no subprocess — deterministic reload)
_INPROCESS_OPS: Dict[str, str] = {
    "env_reload": "env_reload",
}


class InfrastructureApplicator:
    """Deterministic post-APPLY hook for infrastructure operations.

    Called by the orchestrator AFTER a successful file write. Checks if any
    modified file has a known infrastructure consequence and executes it.

    Pure deterministic skeleton — no model inference, no routing decisions.
    The agentic layer already decided WHAT to write. This module executes
    the KNOWN consequence.
    """

    def __init__(
        self,
        project_root: Path,
        enabled: bool = True,
    ) -> None:
        self._project_root = project_root
        self._enabled = enabled and os.environ.get(
            "JARVIS_INFRA_APPLICATOR_ENABLED", "true"
        ).lower() in ("true", "1", "yes")

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def detect_infra_operations(
        self, modified_files: Tuple[str, ...]
    ) -> List[Tuple[str, str, str]]:
        """Detect which modified files trigger infrastructure operations.

        Returns list of (file_path, command_key, description) tuples.
        Deterministic — no inference, no ambiguity.
        """
        operations = []
        for file_path in modified_files:
            basename = Path(file_path).name
            for pattern, cmd_key, description in _INFRA_MAPPINGS:
                if basename == pattern:
                    operations.append((file_path, cmd_key, description))
                    break
        return operations

    async def execute_post_apply(
        self,
        modified_files: Tuple[str, ...],
        op_id: str = "",
    ) -> List[InfraResult]:
        """Execute all infrastructure operations triggered by modified files.

        Called by the orchestrator between APPLY and VERIFY.
        Blocks until all operations complete (or fail).

        Returns list of InfraResult. Caller checks all_succeeded() before
        advancing to COMPLETE.
        """
        if not self._enabled:
            return []

        operations = self.detect_infra_operations(modified_files)
        if not operations:
            return []

        results = []
        for file_path, cmd_key, description in operations:
            logger.info(
                "[InfraApplicator] Triggered by %s: %s (op=%s)",
                file_path, description, op_id,
            )
            result = await self._execute_one(file_path, cmd_key, description)
            results.append(result)

            if not result.success:
                logger.error(
                    "[InfraApplicator] FAILED: %s (exit=%d, op=%s)\n"
                    "stderr: %s",
                    description, result.exit_code, op_id,
                    result.stderr_tail[:500],
                )
                break
            else:
                logger.info(
                    "[InfraApplicator] SUCCESS: %s in %.1fs (op=%s)",
                    description, result.duration_s, op_id,
                )

        return results

    async def _execute_one(
        self,
        file_path: str,
        cmd_key: str,
        _description: str,
    ) -> InfraResult:
        """Execute a single infrastructure operation.

        Routes to subprocess execution (pip, npm) or in-process
        operation (env reload) based on command key.
        """
        # In-process operations — no subprocess needed
        if cmd_key in _INPROCESS_OPS:
            return await self._execute_env_reload(file_path)

        builder, timeout_s = _COMMAND_BUILDERS[cmd_key]
        argv = builder(self._project_root, file_path)
        cmd_str = " ".join(argv)

        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._project_root),
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                elapsed = time.monotonic() - t0
                return InfraResult(
                    success=False,
                    command=cmd_str,
                    exit_code=-1,
                    duration_s=elapsed,
                    stdout_tail="",
                    stderr_tail=f"TIMEOUT after {timeout_s}s",
                    file_trigger=file_path,
                )

            elapsed = time.monotonic() - t0
            exit_code = proc.returncode or 0

            stdout_tail = stdout_bytes[-_MAX_OUTPUT_BYTES:].decode(
                errors="replace"
            ) if stdout_bytes else ""
            stderr_tail = stderr_bytes[-_MAX_OUTPUT_BYTES:].decode(
                errors="replace"
            ) if stderr_bytes else ""

            return InfraResult(
                success=(exit_code == 0),
                command=cmd_str,
                exit_code=exit_code,
                duration_s=elapsed,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                file_trigger=file_path,
            )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed = time.monotonic() - t0
            return InfraResult(
                success=False,
                command=cmd_str,
                exit_code=-2,
                duration_s=elapsed,
                stdout_tail="",
                stderr_tail=f"Exception: {exc}",
                file_trigger=file_path,
            )

    async def _execute_env_reload(self, file_path: str) -> InfraResult:
        """Reload environment variables from a .env file.

        Deterministic in-process operation — reads the file, updates
        os.environ for changed/new keys. Does NOT remove existing keys
        (additive merge only — safe for running system).

        Uses python-dotenv if available, falls back to manual parsing.
        """
        t0 = time.monotonic()
        env_path = self._project_root / file_path
        loaded_count = 0

        try:
            if not env_path.exists():
                return InfraResult(
                    success=False, command=f"env_reload({file_path})",
                    exit_code=1, duration_s=time.monotonic() - t0,
                    stdout_tail="", stderr_tail=f"File not found: {env_path}",
                    file_trigger=file_path,
                )

            # Try python-dotenv first (handles quoting, comments, exports)
            try:
                from dotenv import dotenv_values
                new_vars = dotenv_values(env_path)
                for key, value in new_vars.items():
                    if value is not None:
                        os.environ[key] = value
                        loaded_count += 1
            except ImportError:
                # Manual parsing fallback
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key:
                            os.environ[key] = value
                            loaded_count += 1

            elapsed = time.monotonic() - t0
            logger.info(
                "[InfraApplicator] Env reload: %d vars loaded from %s in %.3fs",
                loaded_count, file_path, elapsed,
            )
            return InfraResult(
                success=True,
                command=f"env_reload({file_path})",
                exit_code=0,
                duration_s=elapsed,
                stdout_tail=f"Loaded {loaded_count} environment variables",
                stderr_tail="",
                file_trigger=file_path,
            )

        except Exception as exc:
            elapsed = time.monotonic() - t0
            return InfraResult(
                success=False,
                command=f"env_reload({file_path})",
                exit_code=-2,
                duration_s=elapsed,
                stdout_tail="",
                stderr_tail=f"Exception: {exc}",
                file_trigger=file_path,
            )

    @staticmethod
    def all_succeeded(results: List[InfraResult]) -> bool:
        """Check if all infrastructure operations succeeded."""
        return all(r.success for r in results)
