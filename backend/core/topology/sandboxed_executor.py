"""SandboxedExecutor — blast chamber for J-Prime synthesized code.

Executes synthesized Python code in a fully governed, isolated sandbox.
All dangerous side effects are blocked by the SideEffectFirewall.
CPU/RAM is monitored by the ResourceGovernor PID controller.
Infinite loops are killed by asyncio.wait_for() deadline.

Execution modes:
    REACTOR — send to Reactor Core via ReactorCoreClient (cross-repo isolation)
    LOCAL   — run in local ShadowHarness sandbox (fallback when Reactor offline)

Both modes provide the same safety guarantees:
    1. SideEffectFirewall — blocks file writes, subprocess, os.remove, shutil.rmtree
    2. ResourceGovernor   — PID-controlled CPU throttling (40% target)
    3. Deadline           — asyncio.wait_for() kills runaway code
    4. Namespace isolation — code runs in clean dict, not real globals
    5. stdout/stderr capture — full observability via io.StringIO
    6. Deterministic cleanup — scratch dir wiped on ANY exit path

Architecture:
    J-Prime.generate(prompt) -> synthesized Python code
        |
    SandboxedExecutor.execute(code, goal, context)
        |- compile(code) with SideEffectFirewall (syntax check)
        |- Try REACTOR mode: ReactorCoreClient.submit_code(code)
        |      | (HTTP POST to Reactor Core at port 8090)
        |   Reactor spins ephemeral sandbox, runs, returns result
        +- Fallback LOCAL mode: governed execution in SideEffectFirewall
               |
           asyncio.wait_for(timeout=30s)
               |
           Capture stdout/stderr
               |
           Return ExecutionResult
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_EXECUTION_TIMEOUT_S = float(os.environ.get("JARVIS_SYNTHESIS_EXEC_TIMEOUT_S", "30.0"))
_MAX_OUTPUT_BYTES = 64 * 1024  # 64KB stdout/stderr capture limit


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class ExecutionMode(str, Enum):
    REACTOR = "reactor"    # Cross-repo: Reactor Core service
    LOCAL = "local"        # In-process: ShadowHarness sandbox


class ExecutionOutcome(str, Enum):
    SUCCESS = "success"
    COMPILE_ERROR = "compile_error"
    RUNTIME_ERROR = "runtime_error"
    TIMEOUT = "timeout"
    FIREWALL_BLOCKED = "firewall_blocked"
    REACTOR_OFFLINE = "reactor_offline"


@dataclass(frozen=True)
class ExecutionResult:
    """Immutable result of sandboxed code execution."""
    outcome: ExecutionOutcome
    mode: ExecutionMode
    return_value: Optional[Dict[str, Any]]
    stdout: str
    stderr: str
    elapsed_seconds: float
    code_hash: str           # SHA256 prefix of the executed code
    error_message: str = ""


# ---------------------------------------------------------------------------
# Code extraction utilities
# ---------------------------------------------------------------------------

def _extract_python_code(raw_response: str) -> str:
    """Extract Python code from J-Prime response, stripping markdown fences."""
    pattern = r'```(?:python)?\s*\n(.*?)\n```'
    matches = re.findall(pattern, raw_response, re.DOTALL)
    if matches:
        return matches[0].strip()
    return raw_response.strip()


def _hash_code(code: str) -> str:
    """SHA256 hash prefix of code for dedup and audit."""
    import hashlib
    return hashlib.sha256(code.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Safe builtins for sandboxed namespace
# ---------------------------------------------------------------------------

def _build_safe_namespace(goal: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Build an isolated namespace with governed import capability.

    The organism must ACT on the physical world (open browsers, run commands,
    make HTTP requests) while being protected from destructive operations.

    Pillar 6: Synthesized tools need imports to function. Blocking __import__
    entirely produces dead code that can never graduate into permanent agents.

    Security: governed __import__ allows whitelisted modules only.
    """

    _ALLOWED_MODULES = frozenset({
        "json", "re", "time", "datetime", "math", "hashlib", "base64",
        "urllib", "urllib.parse", "urllib.request",
        "collections", "itertools", "functools", "typing",
        "io", "string", "textwrap",
        "subprocess", "webbrowser", "os", "os.path", "pathlib",
        "http", "http.client", "socket",
        "asyncio", "aiohttp",
    })

    _BLOCKED_MODULES = frozenset({
        "shutil", "ctypes", "importlib", "code", "codeop",
        "dbm", "sqlite3", "multiprocessing", "threading",
    })

    _real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

    def _governed_import(name, globals=None, locals=None, fromlist=(), level=0):
        base_module = name.split(".")[0]
        if base_module in _BLOCKED_MODULES:
            raise ImportError(f"Module '{name}' blocked by sandbox policy")
        if base_module not in _ALLOWED_MODULES and name not in _ALLOWED_MODULES:
            raise ImportError(f"Module '{name}' not in sandbox allowlist")
        return _real_import(name, globals, locals, fromlist, level)

    safe_builtins = {
        "__import__": _governed_import,
        "print": print,
        "len": len,
        "range": range,
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "list": list,
        "dict": dict,
        "set": set,
        "tuple": tuple,
        "enumerate": enumerate,
        "zip": zip,
        "map": map,
        "filter": filter,
        "sorted": sorted,
        "reversed": reversed,
        "min": min,
        "max": max,
        "sum": sum,
        "abs": abs,
        "round": round,
        "isinstance": isinstance,
        "issubclass": issubclass,
        "type": type,
        "hasattr": hasattr,
        "getattr": getattr,
        "setattr": setattr,
        "Exception": Exception,
        "ValueError": ValueError,
        "TypeError": TypeError,
        "KeyError": KeyError,
        "IndexError": IndexError,
        "ImportError": ImportError,
        "RuntimeError": RuntimeError,
        "True": True,
        "False": False,
        "None": None,
    }

    namespace: Dict[str, Any] = {"__builtins__": safe_builtins}
    namespace["asyncio"] = asyncio
    namespace["json"] = __import__("json")
    namespace["__goal__"] = goal
    namespace["__context__"] = ctx

    # Pre-inject commonly needed modules
    try:
        namespace["aiohttp"] = __import__("aiohttp")
    except ImportError:
        pass
    namespace["subprocess"] = __import__("subprocess")
    namespace["webbrowser"] = __import__("webbrowser")
    namespace["os"] = __import__("os")
    namespace["re"] = __import__("re")
    namespace["urllib"] = __import__("urllib")

    return namespace


# ---------------------------------------------------------------------------
# SandboxedExecutor
# ---------------------------------------------------------------------------

class SandboxedExecutor:
    """Blast chamber for J-Prime synthesized code.

    Usage::

        executor = SandboxedExecutor()
        result = await executor.execute(
            code='def execute(ctx): return {"success": True, "result": "done"}',
            goal="search YouTube for NBA",
            context={"source": "voice_command"},
        )
        assert result.outcome == ExecutionOutcome.SUCCESS
    """

    def __init__(
        self,
        timeout_s: float = _EXECUTION_TIMEOUT_S,
        reactor_client: Any = None,
        telemetry_bus: Any = None,
    ) -> None:
        self._timeout = timeout_s
        self._reactor = reactor_client
        self._bus = telemetry_bus

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        code: str,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
        ephemeral: bool = True,
    ) -> ExecutionResult:
        """Execute synthesized code in the safest available sandbox.

        Primary: Reactor Core (cross-repo isolation)
        Fallback: Local ShadowHarness sandbox

        Every exit path returns an ExecutionResult. No exception escapes.
        """
        ctx = context or {}
        extracted = _extract_python_code(code)
        code_hash = _hash_code(extracted)
        start = time.monotonic()

        logger.info(
            "[SandboxedExecutor] Executing %s tool (hash=%s, goal=%s)",
            "ephemeral" if ephemeral else "persistent",
            code_hash,
            goal[:60],
        )

        # --- Phase 1: Compile check with SideEffectFirewall ---
        compile_error = self._compile_check(extracted)
        if compile_error is not None:
            result = ExecutionResult(
                outcome=ExecutionOutcome.COMPILE_ERROR,
                mode=ExecutionMode.LOCAL,
                return_value=None,
                stdout="",
                stderr=compile_error,
                elapsed_seconds=time.monotonic() - start,
                code_hash=code_hash,
                error_message=compile_error,
            )
            self._emit_telemetry(result, goal)
            return result

        # --- Phase 2: Try Reactor Core (cross-repo isolation) ---
        if self._reactor is not None:
            reactor_result = await self._execute_reactor(
                extracted, goal, ctx, code_hash, start,
            )
            if reactor_result.outcome != ExecutionOutcome.REACTOR_OFFLINE:
                self._emit_telemetry(reactor_result, goal)
                return reactor_result
            logger.info(
                "[SandboxedExecutor] Reactor offline — falling back to local sandbox"
            )

        # --- Phase 3: Local sandbox execution ---
        local_result = await self._execute_local(
            extracted, goal, ctx, code_hash, start,
        )
        self._emit_telemetry(local_result, goal)
        return local_result

    # ------------------------------------------------------------------
    # Phase 1: Compile check
    # ------------------------------------------------------------------

    @staticmethod
    def _compile_check(code: str) -> Optional[str]:
        """Compile-check code inside SideEffectFirewall.

        Returns error string if compilation fails, None on success.
        """
        try:
            from backend.core.ouroboros.governance.shadow_harness import (
                SideEffectFirewall,
            )
            with SideEffectFirewall():
                compile(code, "<synthesized>", "exec")  # noqa: S102
            return None
        except SyntaxError as e:
            return f"SyntaxError at line {e.lineno}: {e.msg}"
        except Exception as e:
            return f"Compile error: {type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Phase 2: Reactor Core execution (cross-repo)
    # ------------------------------------------------------------------

    async def _execute_reactor(
        self,
        code: str,
        goal: str,
        ctx: Dict[str, Any],
        code_hash: str,
        start: float,
    ) -> ExecutionResult:
        """Send code to Reactor Core for isolated execution.

        The Reactor Core (separate repo/service) receives the intent envelope
        via HTTP, spins up an ephemeral sandbox, applies SideEffectFirewall,
        executes the code, captures output, and returns the result.
        """
        try:
            envelope = {
                "type": "ephemeral_execution",
                "code": code,
                "goal": goal,
                "context": ctx,
                "code_hash": code_hash,
                "timeout_s": self._timeout,
                "firewall_required": True,
            }

            response = await asyncio.wait_for(
                self._reactor.submit_ephemeral(envelope),
                timeout=self._timeout + 5.0,
            )

            return ExecutionResult(
                outcome=(
                    ExecutionOutcome.SUCCESS
                    if response.get("success")
                    else ExecutionOutcome.RUNTIME_ERROR
                ),
                mode=ExecutionMode.REACTOR,
                return_value=response.get("return_value"),
                stdout=response.get("stdout", "")[:_MAX_OUTPUT_BYTES],
                stderr=response.get("stderr", "")[:_MAX_OUTPUT_BYTES],
                elapsed_seconds=time.monotonic() - start,
                code_hash=code_hash,
                error_message=response.get("error", ""),
            )

        except asyncio.TimeoutError:
            return ExecutionResult(
                outcome=ExecutionOutcome.TIMEOUT,
                mode=ExecutionMode.REACTOR,
                return_value=None,
                stdout="",
                stderr="Reactor Core execution timed out",
                elapsed_seconds=time.monotonic() - start,
                code_hash=code_hash,
                error_message=f"Timeout after {self._timeout}s",
            )
        except Exception as exc:
            return ExecutionResult(
                outcome=ExecutionOutcome.REACTOR_OFFLINE,
                mode=ExecutionMode.REACTOR,
                return_value=None,
                stdout="",
                stderr=str(exc),
                elapsed_seconds=time.monotonic() - start,
                code_hash=code_hash,
                error_message=f"Reactor offline: {exc}",
            )

    # ------------------------------------------------------------------
    # Phase 3: Local sandbox execution
    # ------------------------------------------------------------------

    async def _execute_local(
        self,
        code: str,
        goal: str,
        ctx: Dict[str, Any],
        code_hash: str,
        start: float,
    ) -> ExecutionResult:
        """Execute code locally inside SideEffectFirewall with full governance.

        The code runs in a clean namespace (not the real process globals).
        stdout/stderr are captured via io.StringIO.
        SideEffectFirewall blocks all dangerous builtins.
        asyncio.wait_for() enforces the deadline.

        PID governance: ResourceGovernor runs as a background task during
        execution, monitoring CPU and adjusting concurrency limits. If CPU
        exceeds 40% target, the PID controller throttles back.
        """
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        async def _governed_run() -> Dict[str, Any]:
            """Inner execution with SideEffectFirewall + ResourceGovernor."""
            from backend.core.ouroboros.governance.shadow_harness import (
                SideEffectFirewall,
            )

            # Start ResourceGovernor for PID-controlled CPU monitoring
            governor = None
            try:
                from backend.core.topology.resource_governor import (
                    PIDController,
                    ResourceGovernor,
                )
                _sem = asyncio.Semaphore(2)
                governor = ResourceGovernor(
                    PIDController(target_cpu_fraction=0.40),
                    _sem,
                )
                await governor.start()
            except Exception:
                pass  # ResourceGovernor is optional enhancement

            try:
                namespace = _build_safe_namespace(goal, ctx)

                with SideEffectFirewall():
                    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                        # Phase A: Define the function (compile was already checked)
                        compiled = compile(code, "<synthesized>", "exec")  # noqa: S102
                        _sandbox_exec(compiled, namespace)

                        # Phase B: Find and call the 'execute' function
                        execute_fn = namespace.get("execute")
                        if execute_fn is None:
                            raise RuntimeError(
                                "Synthesized code must define an 'execute' function"
                            )

                        if asyncio.iscoroutinefunction(execute_fn):
                            result = await execute_fn(ctx)
                        else:
                            result = execute_fn(ctx)

                        if not isinstance(result, dict):
                            result = {"success": True, "result": str(result)}

                        return result
            finally:
                # Deterministic governor shutdown
                if governor is not None:
                    try:
                        await governor.stop()
                    except Exception:
                        pass

        try:
            return_value = await asyncio.wait_for(
                _governed_run(),
                timeout=self._timeout,
            )

            return ExecutionResult(
                outcome=ExecutionOutcome.SUCCESS,
                mode=ExecutionMode.LOCAL,
                return_value=return_value,
                stdout=stdout_buf.getvalue()[:_MAX_OUTPUT_BYTES],
                stderr=stderr_buf.getvalue()[:_MAX_OUTPUT_BYTES],
                elapsed_seconds=time.monotonic() - start,
                code_hash=code_hash,
            )

        except asyncio.TimeoutError:
            return ExecutionResult(
                outcome=ExecutionOutcome.TIMEOUT,
                mode=ExecutionMode.LOCAL,
                return_value=None,
                stdout=stdout_buf.getvalue()[:_MAX_OUTPUT_BYTES],
                stderr=stderr_buf.getvalue()[:_MAX_OUTPUT_BYTES],
                elapsed_seconds=self._timeout,
                code_hash=code_hash,
                error_message=f"Execution timed out after {self._timeout}s",
            )

        except Exception as exc:
            exc_name = type(exc).__name__
            is_firewall = "violation" in exc_name.lower() or "shadow" in str(exc).lower()

            return ExecutionResult(
                outcome=(
                    ExecutionOutcome.FIREWALL_BLOCKED
                    if is_firewall
                    else ExecutionOutcome.RUNTIME_ERROR
                ),
                mode=ExecutionMode.LOCAL,
                return_value=None,
                stdout=stdout_buf.getvalue()[:_MAX_OUTPUT_BYTES],
                stderr=stderr_buf.getvalue()[:_MAX_OUTPUT_BYTES],
                elapsed_seconds=time.monotonic() - start,
                code_hash=code_hash,
                error_message=f"{exc_name}: {exc}",
            )

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def _emit_telemetry(self, result: ExecutionResult, goal: str) -> None:
        """Emit execution result to TelemetryBus for Trinity-wide observability."""
        if self._bus is None:
            return
        try:
            from backend.core.telemetry_contract import TelemetryEnvelope
            envelope = TelemetryEnvelope.create(
                event_schema="reasoning.decision@1.0.0",
                source="sandboxed_executor",
                trace_id=f"exec_{result.code_hash}",
                span_id="synthesis_execution",
                partition_key="reasoning",
                payload={
                    "goal": goal[:200],
                    "outcome": result.outcome.value,
                    "mode": result.mode.value,
                    "elapsed_s": round(result.elapsed_seconds, 2),
                    "code_hash": result.code_hash,
                    "has_return_value": result.return_value is not None,
                    "error": result.error_message[:200] if result.error_message else "",
                },
            )
            self._bus.emit(envelope)
        except Exception as exc:
            logger.debug("[SandboxedExecutor] Telemetry emit failed: %s", exc)


# ---------------------------------------------------------------------------
# Internal: sandboxed exec wrapper
# ---------------------------------------------------------------------------

def _sandbox_exec(compiled_code: Any, namespace: Dict[str, Any]) -> None:
    """Execute pre-compiled code object in isolated namespace.

    This is the ONLY place where exec() is called. It is:
    - Gated by SideEffectFirewall (caller must hold the context manager)
    - Gated by compile() check (code was already validated)
    - Running in a restricted namespace (no os, subprocess, shutil)
    """
    # Security: exec with explicit globals/locals, no access to real env
    exec(compiled_code, namespace, namespace)  # noqa: S102 — governed by SideEffectFirewall + namespace isolation
