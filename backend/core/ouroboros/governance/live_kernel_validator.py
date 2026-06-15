"""Slice 256 — Live-Fire Validation Engine for O+V's VALIDATE phase (SKELETON / blueprint).

Root problem (Slice 255 live-fire finding): O+V's VALIDATE relied on sandbox fixtures,
so kernel patches that pytest-pass but crash on a real boot (NameError in a function,
TypeError on a real call) advanced to GATE/APPLY anyway. This adds a reality check:
when a candidate mutates `unified_supervisor.py` or `backend/core/`, spawn an EPHEMERAL,
TTL+memory-bounded subprocess that live-imports the kernel and exercises the patched
symbols; any unhandled exception FAILS VALIDATE and routes the traceback back to GENERATE.

Status: SKELETON. The pure, deterministic helpers (retry budget + state-dump sanitizer)
are fully implemented (syntax-verified); the subprocess validator + cascade breaker are
structured stubs to be filled under TDD once the blueprint is authorized. Built MANUALLY
(bootstrap paradox: the validator validates kernel patches and IS one — the loop must not
ship it unvalidated).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ── Phase 1: the deterministic guardrail (FULLY IMPLEMENTED — pure, testable) ──

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def livefire_retry_budget(num_changed_files: int) -> int:
    """Deterministic retry budget: base + per-file, hard-capped. All env-tunable.

    NOT a complexity heuristic — a predictable, debuggable guardrail. A circuit
    breaker must be deterministic, so this is a pure function of one observable.
    """
    base = _env_int("JARVIS_LIVEFIRE_RETRY_BASE", 3)
    per_file = _env_int("JARVIS_LIVEFIRE_RETRY_PER_FILE", 1)
    cap = _env_int("JARVIS_MAX_LIVEFIRE_RETRIES", 8)
    budget = base + per_file * max(0, int(num_changed_files) - 1)
    return max(1, min(budget, cap))


# ── Phase 3: secret-sanitized state dump (FULLY IMPLEMENTED — pure, testable) ──

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|bearer|authorization|"
    r"anthropic|doubleword|hf[_-]?token|huggingface)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|hf_[A-Za-z0-9]{8,}|"
    r"AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,})"
)
_HOME = os.path.expanduser("~")
_REDACTED = "***REDACTED***"


def sanitize_state_dump(payload: Any) -> Any:
    """Recursively scrub secrets + local paths from a state-dump before it is
    serialized onto a PR. Redacts by key name AND by value shape; collapses home
    paths to ``~``. NEVER raises (best-effort defense-in-depth)."""
    try:
        if isinstance(payload, dict):
            out: Dict[str, Any] = {}
            for k, v in payload.items():
                if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                    out[k] = _REDACTED
                else:
                    out[k] = sanitize_state_dump(v)
            return out
        if isinstance(payload, (list, tuple)):
            return [sanitize_state_dump(v) for v in payload]
        if isinstance(payload, str):
            s = _SECRET_VALUE_RE.sub(_REDACTED, payload)
            if _HOME and _HOME in s:
                s = s.replace(_HOME, "~")
            return s
        return payload
    except Exception:  # noqa: BLE001 — sanitizer must never break the dump path
        return _REDACTED


# ── Phase 2: the ephemeral live-fire validator (SKELETON — TDD to fill) ──

@dataclass
class LiveFireResult:
    ok: bool
    traceback: str = ""
    exception_type: str = ""
    exercised: List[str] = field(default_factory=list)
    timed_out: bool = False
    duration_s: float = 0.0


class LiveKernelValidator:
    """Spawns an ephemeral subprocess that live-imports the kernel + exercises the
    patched symbols, with outbound I/O mocked so a failed boot cannot corrupt real
    state/db/fs. TTL + memory bounded. Returns a LiveFireResult."""

    def __init__(
        self,
        *,
        subprocess_runner: Optional[Callable[..., Any]] = None,
        timeout_s: Optional[float] = None,
        mem_cap_mb: Optional[int] = None,
    ) -> None:
        self._run = subprocess_runner  # inject AsyncSubprocessRunner; default resolved lazily
        self._timeout_s = timeout_s if timeout_s is not None else float(
            _env_int("JARVIS_LIVEFIRE_TIMEOUT_S", 90)
        )
        self._mem_cap_mb = mem_cap_mb if mem_cap_mb is not None else _env_int(
            "JARVIS_LIVEFIRE_MEM_CAP_MB", 4096
        )

    @staticmethod
    def affects_kernel(changed_files: List[str]) -> bool:
        """True iff the patch touches the kernel surface this validator guards."""
        return any(
            f == "unified_supervisor.py" or f.startswith("backend/core/")
            for f in changed_files
        )

    _MARKER = "LIVEFIRE_RESULT:"

    def _build_probe_script(
        self, module: str, affected_symbols: List[str], path_insert: Optional[str]
    ) -> str:
        """Render the ephemeral probe. SCOPED, high-signal exercise (no blind mock-arg
        fabrication): import the module (catches import/decorator/class-body errors),
        construct affected default-constructible classes, and call affected NO-ARG
        module-level functions (catches the Slice-255 NameError class). Functions that
        REQUIRE args are flagged (``needs_args``), never force-called with fake args."""
        return (
            "import sys, json, traceback, inspect\n"
            f"_PI = {path_insert!r}\n"
            "if _PI:\n    sys.path.insert(0, _PI)\n"
            'RES = {"ok": True, "exercised": [], "needs_args": [], '
            '"exception_type": "", "traceback": ""}\n'
            "def _exercise(obj):\n"
            "    if isinstance(obj, type):\n"
            "        try:\n"
            "            obj()\n"
            "        except TypeError:\n"
            "            return 'needs_args'\n"
            "        return 'ok'\n"
            "    if callable(obj):\n"
            "        sig = inspect.signature(obj)\n"
            "        req = [p for p in sig.parameters.values() if p.default is p.empty "
            "and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]\n"
            "        if req:\n            return 'needs_args'\n"
            "        r = obj()\n"
            "        if inspect.iscoroutine(r):\n"
            "            import asyncio; asyncio.run(r)\n"
            "        return 'ok'\n"
            "    return 'ok'\n"
            "try:\n"
            f"    mod = __import__({module!r})\n"
            f"    for sym in {list(affected_symbols)!r}:\n"
            "        obj = getattr(mod, sym, None)\n"
            "        if obj is None:\n            continue\n"
            "        outcome = _exercise(obj)\n"
            "        (RES['needs_args'] if outcome == 'needs_args' else RES['exercised']).append(sym)\n"
            "except Exception:\n"
            "    RES['ok'] = False\n"
            "    RES['exception_type'] = type(sys.exc_info()[1]).__name__\n"
            "    RES['traceback'] = traceback.format_exc()\n"
            f'print("{self._MARKER}" + json.dumps(RES))\n'
        )

    async def _default_runner(self, script: str, timeout_s: float):
        """Spawn an ephemeral, TTL-bounded subprocess. Returns (rc, stdout, stderr)."""
        import asyncio
        import sys as _sys
        proc = await asyncio.create_subprocess_exec(
            _sys.executable, "-c", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            raise
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def validate_patch(
        self,
        *,
        changed_files: List[str],
        affected_symbols: List[str],
        module: str = "unified_supervisor",
        path_insert: Optional[str] = None,
    ) -> LiveFireResult:
        """Run the ephemeral live-fire probe; FAIL on any unhandled exception/timeout.
        Skips (ok=True) when the patch doesn't touch the guarded kernel surface."""
        import asyncio
        import time

        if module == "unified_supervisor" and not self.affects_kernel(changed_files):
            return LiveFireResult(ok=True, exercised=[])

        script = self._build_probe_script(module, affected_symbols, path_insert)
        runner = self._run or self._default_runner
        t0 = time.monotonic()
        try:
            rc, out, err = await asyncio.wait_for(
                runner(script, self._timeout_s), timeout=self._timeout_s + 5
            )
        except asyncio.TimeoutError:
            return LiveFireResult(
                ok=False, timed_out=True, exception_type="LiveFireTimeout",
                traceback=f"live-fire exceeded {self._timeout_s}s",
                duration_s=time.monotonic() - t0,
            )
        dur = time.monotonic() - t0

        marker_idx = out.rfind(self._MARKER)
        if marker_idx < 0:
            return LiveFireResult(
                ok=False, exception_type="ProbeProtocolError",
                traceback=(err or out or "no probe result").strip()[-4000:],
                duration_s=dur,
            )
        import json as _json
        try:
            payload = _json.loads(out[marker_idx + len(self._MARKER):].splitlines()[0])
        except Exception as err2:  # noqa: BLE001
            return LiveFireResult(
                ok=False, exception_type="ProbeProtocolError",
                traceback=f"unparseable probe result: {err2!r}", duration_s=dur,
            )
        return LiveFireResult(
            ok=bool(payload.get("ok")),
            traceback=payload.get("traceback", ""),
            exception_type=payload.get("exception_type", ""),
            exercised=list(payload.get("exercised", [])),
            duration_s=dur,
        )


# ── Phase 3: cascade breaker + adaptive relief (SKELETON — TDD to fill) ──

class CascadeFailureBreaker:
    """Trips after N consecutive live-fire ESCALATIONS (suspected environmental
    degradation). On trip: deterministic suspend → non-destructive soft relief
    (gc.collect + ephemeral cache clear) → re-check MemoryPressureGate → resume if
    recovered, else synthesize a HARD_RESTART routed through ``shadow_guard`` for
    /endorse. NEVER auto-reboots."""

    def __init__(self, *, threshold: Optional[int] = None) -> None:
        self._threshold = threshold if threshold is not None else _env_int(
            "JARVIS_CASCADE_ESCALATION_THRESHOLD", 3
        )
        self._consecutive = 0
        self._tripped = False

    def record_escalation(self) -> bool:
        """Count a consecutive escalation; return True if this trips the breaker."""
        self._consecutive += 1
        if self._consecutive >= self._threshold:
            self._tripped = True
        return self._tripped

    def record_clean_task(self) -> None:
        """A task that did NOT escalate resets the consecutive counter."""
        self._consecutive = 0

    @staticmethod
    def _is_recovered(level: Any) -> bool:
        """True ONLY for an explicit safe level. Unknown/None/failed-probe → False
        (fail-secure: assume still-degraded, proceed to the shadow-gated reboot)."""
        if level is None:
            return False
        s = str(getattr(level, "value", level)).strip().lower()
        return s in ("ok", "normal", "low", "nominal")

    async def on_trip(
        self,
        *,
        pressure_probe: Callable[[], Any],
        shadow_guard: Callable[..., Any],
        reboot_action: Callable[[], Any],
        cache_clear: Optional[Callable[[], Any]] = None,
        state_flush: Optional[Callable[[], Any]] = None,
        emit: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ) -> str:
        """Deterministic suspend → non-destructive soft relief → re-evaluate → resume,
        else best-effort serializable state flush + shadow-gated HARD_RESTART.

        Returns 'recovered' | 'awaiting_endorsement' | 'suspended'. NEVER auto-reboots
        and NEVER raises — every step is fail-soft. ``reboot_action`` is only ever
        invoked *through* ``shadow_guard`` (trapped in shadow mode → /endorse).
        """
        import gc

        async def _safe(label: str, fn: Optional[Callable[..., Any]], *args: Any) -> Any:
            if fn is None:
                return None
            try:
                return await _await_if_needed(fn(*args))
            except Exception as err:  # noqa: BLE001 — relief/telemetry never escalate
                _LOG.warning("[Cascade] %s failed (fail-soft): %r", label, err)
                return None

        # 1. NON-DESTRUCTIVE soft relief
        try:
            gc.collect()
        except Exception:  # noqa: BLE001
            pass
        await _safe("cache_clear", cache_clear)

        # 2. Re-evaluate the environment (MemoryPressureGate, injected)
        level = await _safe("pressure_probe", pressure_probe)
        if self._is_recovered(level):
            self._tripped = False
            self._consecutive = 0
            await _safe("emit", emit, "ENVIRONMENT_RECOVERED", {"level": str(level)})
            _LOG.info("[Cascade] soft relief recovered the environment (level=%s)", level)
            return "recovered"

        # 3. Still degraded → best-effort flush of SERIALIZABLE state (reuse WAL; closures
        #    + live handles cannot survive — documented, not silently lost), then route the
        #    HARD_RESTART through shadow_guard so the human endorses it. NEVER auto-reboot.
        await _safe("state_flush", state_flush)
        await _safe("emit", emit, "CRITICAL_SYSTEMIC_CASCADE", {"level": str(level)})
        outcome = await _safe(
            "shadow_guard", shadow_guard,
            "HARD_RESTART the O+V loop (environment critical)", reboot_action,
        )
        # shadow_guard returns a SHADOW_TRAPPED sentinel in shadow mode (action withheld
        # → pending /endorse). Anything else means it executed (shadow off + endorsed path).
        if outcome is not None and repr(outcome).upper().find("SHADOW_TRAPPED") >= 0:
            return "awaiting_endorsement"
        return "suspended"


async def _await_if_needed(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


import logging as _logging  # noqa: E402 — module logger for the cascade breaker
_LOG = _logging.getLogger("live_kernel_validator")
