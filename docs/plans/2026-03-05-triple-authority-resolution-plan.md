# Triple Authority Resolution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Resolve the Triple Authority Problem by establishing `unified_supervisor.py` as the single Root Authority with explicit managed-mode contracts, active crash detection, and managed-mode demotion in Prime and Reactor.

**Architecture:** Thin `backend/core/root_authority.py` module owns policy + state machine + verdicts. Existing `ProcessOrchestrator` becomes pure executor via `VerdictExecutor` protocol. Prime/Reactor get minimal managed-mode conformance (~120 lines each). Staged rollout with shadow mode first.

**Tech Stack:** Python 3.9+, asyncio, aiohttp, FastAPI, dataclasses, HMAC-SHA256, structured JSON logging

**Design Doc:** `docs/plans/2026-03-05-triple-authority-resolution-design.md`

---

## Wave 0: Foundation Types & Contract (Tasks 1-4)

### Task 1: Root Authority Types Module

**Files:**
- Create: `backend/core/root_authority_types.py`
- Test: `tests/unit/core/test_root_authority_types.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/test_root_authority_types.py
"""Tests for root authority contract types."""
import pytest
from enum import Enum


class TestLifecycleAction:
    def test_enum_values(self):
        from backend.core.root_authority_types import LifecycleAction
        assert LifecycleAction.DRAIN.value == "drain"
        assert LifecycleAction.TERM.value == "term"
        assert LifecycleAction.GROUP_KILL.value == "group_kill"
        assert LifecycleAction.RESTART.value == "restart"
        assert LifecycleAction.ESCALATE_OPERATOR.value == "escalate_operator"

    def test_all_actions_present(self):
        from backend.core.root_authority_types import LifecycleAction
        assert len(LifecycleAction) == 5


class TestSubsystemState:
    def test_enum_values(self):
        from backend.core.root_authority_types import SubsystemState
        expected = {"STARTING", "HANDSHAKE", "ALIVE", "READY", "DEGRADED",
                    "DRAINING", "STOPPED", "CRASHED", "REJECTED"}
        assert {s.name for s in SubsystemState} == expected

    def test_terminal_states(self):
        from backend.core.root_authority_types import SubsystemState
        terminals = {SubsystemState.STOPPED, SubsystemState.CRASHED, SubsystemState.REJECTED}
        for s in SubsystemState:
            if s in terminals:
                assert s.is_terminal
            else:
                assert not s.is_terminal


class TestProcessIdentity:
    def test_creation(self):
        from backend.core.root_authority_types import ProcessIdentity
        pi = ProcessIdentity(pid=123, start_time_ns=999, session_id="abc", exec_fingerprint="sha256:def")
        assert pi.pid == 123
        assert pi.session_id == "abc"

    def test_frozen(self):
        from backend.core.root_authority_types import ProcessIdentity
        pi = ProcessIdentity(pid=1, start_time_ns=0, session_id="x", exec_fingerprint="y")
        with pytest.raises(AttributeError):
            pi.pid = 2

    def test_matches(self):
        from backend.core.root_authority_types import ProcessIdentity
        a = ProcessIdentity(pid=1, start_time_ns=100, session_id="s1", exec_fingerprint="fp1")
        b = ProcessIdentity(pid=1, start_time_ns=100, session_id="s1", exec_fingerprint="fp1")
        c = ProcessIdentity(pid=2, start_time_ns=100, session_id="s1", exec_fingerprint="fp1")
        assert a == b
        assert a != c


class TestLifecycleVerdict:
    def test_creation(self):
        from backend.core.root_authority_types import (
            LifecycleVerdict, LifecycleAction, ProcessIdentity
        )
        identity = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f")
        v = LifecycleVerdict(
            subsystem="jarvis-prime",
            identity=identity,
            action=LifecycleAction.DRAIN,
            reason="health timeout",
            reason_code="health_timeout",
            correlation_id="corr-1",
            incident_id="inc-1",
            exit_code=None,
            observed_at_ns=12345,
            wall_time_utc="2026-03-05T00:00:00Z",
        )
        assert v.action == LifecycleAction.DRAIN
        assert v.subsystem == "jarvis-prime"

    def test_frozen(self):
        from backend.core.root_authority_types import (
            LifecycleVerdict, LifecycleAction, ProcessIdentity
        )
        identity = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f")
        v = LifecycleVerdict(
            subsystem="x", identity=identity, action=LifecycleAction.TERM,
            reason="r", reason_code="rc", correlation_id="c", incident_id="i",
            exit_code=None, observed_at_ns=0, wall_time_utc="t"
        )
        with pytest.raises(AttributeError):
            v.action = LifecycleAction.RESTART


class TestExecutionResult:
    def test_creation(self):
        from backend.core.root_authority_types import ExecutionResult
        r = ExecutionResult(
            accepted=True, executed=True, result="success",
            new_identity=None, error_code=None, correlation_id="c1"
        )
        assert r.accepted
        assert r.result == "success"


class TestTimeoutPolicy:
    def test_defaults(self):
        from backend.core.root_authority_types import TimeoutPolicy
        tp = TimeoutPolicy()
        assert tp.startup_grace_s == 120.0
        assert tp.health_timeout_s == 5.0
        assert tp.health_poll_interval_s == 5.0
        assert tp.drain_timeout_s == 30.0
        assert tp.term_timeout_s == 10.0
        assert tp.degraded_tolerance_s == 60.0
        assert tp.degraded_recovery_check_s == 10.0


class TestRestartPolicy:
    def test_defaults(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy()
        assert rp.max_restarts == 3
        assert rp.window_s == 300.0
        assert rp.jitter_factor == 0.3
        assert 0 in rp.no_restart_exit_codes
        assert 100 in rp.no_restart_exit_codes
        assert 200 in rp.retry_exit_codes
        assert 300 not in rp.no_restart_exit_codes

    def test_compute_delay_with_jitter(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy()
        delay = rp.compute_delay(attempt=0)
        assert 0 < delay <= rp.base_delay_s * (1 + rp.jitter_factor)

    def test_delay_increases_with_attempts(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy(jitter_factor=0.0)  # no jitter for determinism
        d0 = rp.compute_delay(attempt=0)
        d1 = rp.compute_delay(attempt=1)
        d2 = rp.compute_delay(attempt=2)
        assert d0 < d1 < d2

    def test_delay_capped_at_max(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy(jitter_factor=0.0)
        d = rp.compute_delay(attempt=100)
        assert d <= rp.max_delay_s

    def test_should_restart_by_exit_code(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy()
        assert not rp.should_restart(exit_code=0)       # clean shutdown
        assert not rp.should_restart(exit_code=101)      # config error
        assert rp.should_restart(exit_code=200)          # dependency failure
        assert rp.should_restart(exit_code=300)          # runtime fatal
        assert rp.should_restart(exit_code=1)            # unknown crash


class TestContractGate:
    def test_schema_version_compatibility(self):
        from backend.core.root_authority_types import ContractGate
        gate = ContractGate(
            subsystem="jarvis-prime",
            expected_schema_version="1.0.0",
            expected_capability_hash=None,
            required_health_fields=frozenset({"liveness", "readiness", "session_id"}),
            required_endpoints=frozenset({"/health", "/lifecycle/drain"}),
        )
        assert gate.is_schema_compatible("1.0.0")
        assert gate.is_schema_compatible("1.0.1")  # patch ok
        assert not gate.is_schema_compatible("2.0.0")  # major mismatch
        assert not gate.is_schema_compatible("0.8.0")  # too old


class TestLifecycleEvent:
    def test_creation(self):
        from backend.core.root_authority_types import LifecycleEvent
        e = LifecycleEvent(
            event_type="state_transition",
            subsystem="jarvis-prime",
            correlation_id="c1",
            session_id="s1",
            identity=None,
            from_state="READY",
            to_state="DEGRADED",
            verdict_action=None,
            reason_code="health_timeout",
            exit_code=None,
            observed_at_ns=0,
            wall_time_utc="t",
            policy_source="root_authority",
        )
        assert e.event_type == "state_transition"
        assert e.policy_source == "root_authority"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_root_authority_types.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.root_authority_types'`

**Step 3: Write minimal implementation**

```python
# backend/core/root_authority_types.py
"""
Root Authority Contract Types v1.0.0
=====================================
Shared types for the Triple Authority Resolution.
Defines the contract between RootAuthorityWatcher (policy),
ProcessOrchestrator (execution), and managed subsystems (Prime/Reactor).

This module has ZERO imports from orchestrator or USP.
"""

import hashlib
import json
import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional, Tuple

SCHEMA_VERSION = "1.0.0"


# =========================================================================
# ENUMS
# =========================================================================

class LifecycleAction(Enum):
    """Strongly typed verdict actions."""
    DRAIN = "drain"
    TERM = "term"
    GROUP_KILL = "group_kill"
    RESTART = "restart"
    ESCALATE_OPERATOR = "escalate_operator"


class SubsystemState(Enum):
    """Per-subsystem lifecycle states."""
    STARTING = "starting"
    HANDSHAKE = "handshake"
    ALIVE = "alive"
    READY = "ready"
    DEGRADED = "degraded"
    DRAINING = "draining"
    STOPPED = "stopped"
    CRASHED = "crashed"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (SubsystemState.STOPPED, SubsystemState.CRASHED, SubsystemState.REJECTED)


# =========================================================================
# IDENTITY & VERDICTS
# =========================================================================

@dataclass(frozen=True)
class ProcessIdentity:
    """4-tuple uniquely identifying a managed process across PID reuse."""
    pid: int
    start_time_ns: int           # monotonic, captured at process boot
    session_id: str              # JARVIS_ROOT_SESSION_ID
    exec_fingerprint: str        # sha256 of binary path + cmdline


@dataclass(frozen=True)
class LifecycleVerdict:
    """Structured decision emitted by watcher, consumed by executor."""
    subsystem: str
    identity: ProcessIdentity
    action: LifecycleAction
    reason: str                  # human-readable
    reason_code: str             # machine: "health_timeout", "crash_exit_300", etc.
    correlation_id: str          # groups events for one incident
    incident_id: str             # dedup key: sha256(subsystem+identity+reason_code+time_bucket)
    exit_code: Optional[int]
    observed_at_ns: int          # monotonic
    wall_time_utc: str           # audit only


@dataclass(frozen=True)
class ExecutionResult:
    """Acknowledgment envelope from executor back to watcher."""
    accepted: bool
    executed: bool
    result: str                  # "success", "timeout", "stale_identity", "error"
    new_identity: Optional[ProcessIdentity]  # if restart
    error_code: Optional[str]
    correlation_id: str


# =========================================================================
# POLICIES
# =========================================================================

@dataclass
class TimeoutPolicy:
    """Timeout classes per lifecycle phase. All use monotonic clock."""
    startup_grace_s: float = 120.0
    health_timeout_s: float = 5.0
    health_poll_interval_s: float = 5.0
    drain_timeout_s: float = 30.0
    term_timeout_s: float = 10.0
    degraded_tolerance_s: float = 60.0
    degraded_recovery_check_s: float = 10.0


@dataclass
class RestartPolicy:
    """Exit-code-aware restart policy with jittered backoff."""
    max_restarts: int = 3
    window_s: float = 300.0
    base_delay_s: float = 2.0
    max_delay_s: float = 60.0
    jitter_factor: float = 0.3

    no_restart_exit_codes: Tuple[int, ...] = (0, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109)
    retry_exit_codes: Tuple[int, ...] = (200, 201, 202, 203, 204, 205, 206, 207, 208, 209)

    def compute_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter."""
        base = min(self.base_delay_s * (2 ** attempt), self.max_delay_s)
        jitter = base * self.jitter_factor * random.uniform(-1, 1)
        return max(0.1, base + jitter)

    def should_restart(self, exit_code: int) -> bool:
        """Whether this exit code warrants a restart."""
        if exit_code in self.no_restart_exit_codes:
            return False
        return True  # unknown codes default to restart-once


# =========================================================================
# CONTRACT GATING
# =========================================================================

@dataclass(frozen=True)
class ContractGate:
    """Defines what root expects from a subsystem at handshake."""
    subsystem: str
    expected_schema_version: str
    expected_capability_hash: Optional[str]
    required_health_fields: FrozenSet[str]
    required_endpoints: FrozenSet[str]

    def is_schema_compatible(self, actual: str) -> bool:
        """N/N-1 minor version compatibility. Major must match."""
        try:
            exp_parts = [int(x) for x in self.expected_schema_version.split(".")]
            act_parts = [int(x) for x in actual.split(".")]
        except (ValueError, IndexError):
            return False
        if exp_parts[0] != act_parts[0]:
            return False
        if len(exp_parts) > 1 and len(act_parts) > 1:
            if exp_parts[1] - act_parts[1] > 1:
                return False
        return True


# =========================================================================
# OBSERVABILITY
# =========================================================================

@dataclass(frozen=True)
class LifecycleEvent:
    """Structured lifecycle event for observability."""
    event_type: str              # "spawn", "health_check", "verdict_emitted", etc.
    subsystem: str
    correlation_id: str
    session_id: str
    identity: Optional[ProcessIdentity]
    from_state: Optional[str]
    to_state: Optional[str]
    verdict_action: Optional[str]
    reason_code: Optional[str]
    exit_code: Optional[int]
    observed_at_ns: int          # monotonic
    wall_time_utc: str           # audit
    policy_source: str           # "root_authority" or "orchestrator"


# =========================================================================
# UTILITIES
# =========================================================================

def compute_exec_fingerprint(binary_path: str, cmdline: str) -> str:
    """sha256 of binary path + cmdline, truncated to 16 hex chars."""
    digest = hashlib.sha256(f"{binary_path}:{cmdline}".encode()).hexdigest()[:16]
    return f"sha256:{digest}"


def compute_capability_hash(capabilities: dict) -> str:
    """Deterministic hash of capability declaration."""
    canonical = json.dumps(capabilities, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"sha256:{digest}"


def compute_incident_id(subsystem: str, identity: ProcessIdentity,
                        reason_code: str, time_ns: int) -> str:
    """Dedup key: collapses duplicate verdicts within 60s bucket."""
    bucket = time_ns // (60 * 10**9)  # 60-second buckets
    raw = f"{subsystem}:{identity.pid}:{identity.session_id}:{reason_code}:{bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_root_authority_types.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/root_authority_types.py tests/unit/core/test_root_authority_types.py
git commit -m "feat(authority): add root authority contract types (Task 1)"
```

---

### Task 2: Managed-Mode Utilities (Subsystem Side)

**Files:**
- Create: `backend/core/managed_mode.py`
- Test: `tests/unit/core/test_managed_mode.py`

**Context:** This is the JARVIS-AI-Agent copy. Identical copies go to Prime and Reactor in Tasks 9-10. Tested here first.

**Step 1: Write the failing test**

```python
# tests/unit/core/test_managed_mode.py
"""Tests for managed-mode contract utilities (subsystem side)."""
import hashlib
import hmac
import os
import time
import pytest


class TestManagedModeFlags:
    def test_root_managed_default_false(self):
        from backend.core.managed_mode import is_root_managed
        # Without env var set, should be False
        old = os.environ.pop("JARVIS_ROOT_MANAGED", None)
        try:
            assert not is_root_managed()
        finally:
            if old is not None:
                os.environ["JARVIS_ROOT_MANAGED"] = old

    def test_root_managed_true(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_MANAGED", "true")
        from backend.core.managed_mode import is_root_managed
        assert is_root_managed()

    def test_root_managed_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_MANAGED", "TRUE")
        from backend.core.managed_mode import is_root_managed
        assert is_root_managed()


class TestExitCodes:
    def test_exit_code_constants(self):
        from backend.core.managed_mode import (
            EXIT_CLEAN, EXIT_CONFIG_ERROR, EXIT_CONTRACT_MISMATCH,
            EXIT_DEPENDENCY_FAILURE, EXIT_RUNTIME_FATAL
        )
        assert EXIT_CLEAN == 0
        assert EXIT_CONFIG_ERROR == 100
        assert EXIT_CONTRACT_MISMATCH == 101
        assert EXIT_DEPENDENCY_FAILURE == 200
        assert EXIT_RUNTIME_FATAL == 300


class TestExecFingerprint:
    def test_deterministic(self):
        from backend.core.managed_mode import compute_exec_fingerprint
        a = compute_exec_fingerprint("/usr/bin/python3", "run_server.py --port 8000")
        b = compute_exec_fingerprint("/usr/bin/python3", "run_server.py --port 8000")
        assert a == b
        assert a.startswith("sha256:")

    def test_different_inputs(self):
        from backend.core.managed_mode import compute_exec_fingerprint
        a = compute_exec_fingerprint("/usr/bin/python3", "run_server.py --port 8000")
        b = compute_exec_fingerprint("/usr/bin/python3", "run_server.py --port 9000")
        assert a != b


class TestCapabilityHash:
    def test_deterministic(self):
        from backend.core.managed_mode import compute_capability_hash
        caps = {"endpoints": ["/health", "/inference"], "models": ["llama"]}
        a = compute_capability_hash(caps)
        b = compute_capability_hash(caps)
        assert a == b
        assert a.startswith("sha256:")

    def test_order_independent(self):
        from backend.core.managed_mode import compute_capability_hash
        a = compute_capability_hash({"b": 2, "a": 1})
        b = compute_capability_hash({"a": 1, "b": 2})
        assert a == b


class TestHMACAuth:
    def test_build_and_verify(self):
        from backend.core.managed_mode import build_hmac_auth, verify_hmac_auth
        secret = "test-secret-abc123"
        session_id = "session-uuid"
        header = build_hmac_auth(session_id, secret)
        assert verify_hmac_auth(header, session_id, secret, tolerance_s=30.0)

    def test_reject_wrong_secret(self):
        from backend.core.managed_mode import build_hmac_auth, verify_hmac_auth
        header = build_hmac_auth("session-1", "secret-a")
        assert not verify_hmac_auth(header, "session-1", "secret-b", tolerance_s=30.0)

    def test_reject_wrong_session(self):
        from backend.core.managed_mode import build_hmac_auth, verify_hmac_auth
        header = build_hmac_auth("session-1", "secret-a")
        assert not verify_hmac_auth(header, "session-2", "secret-a", tolerance_s=30.0)

    def test_reject_expired(self):
        from backend.core.managed_mode import verify_hmac_auth
        # Manually craft an expired header
        import time as _time
        ts = str(int(_time.time()) - 60)  # 60 seconds ago
        nonce = "nonce123"
        msg = f"session-1:{ts}:{nonce}"
        sig = hmac.new(b"secret", msg.encode(), hashlib.sha256).hexdigest()
        header = f"{ts}:{nonce}:{sig}"
        assert not verify_hmac_auth(header, "session-1", "secret", tolerance_s=30.0)


class TestHealthEnvelope:
    def test_build_envelope(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "sess-123")
        monkeypatch.setenv("JARVIS_SUBSYSTEM_ROLE", "jarvis-prime")
        from backend.core.managed_mode import build_health_envelope
        base = {"status": "healthy", "model_loaded": True}
        result = build_health_envelope(base, readiness="ready")
        assert result["liveness"] == "up"
        assert result["readiness"] == "ready"
        assert result["session_id"] == "sess-123"
        assert result["subsystem_role"] == "jarvis-prime"
        assert result["schema_version"] == "1.0.0"
        assert "pid" in result
        assert "start_time_ns" in result
        assert "observed_at_ns" in result
        assert "wall_time_utc" in result
        # Original fields preserved
        assert result["status"] == "healthy"
        assert result["model_loaded"] is True

    def test_drain_id_included(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "sess-123")
        monkeypatch.setenv("JARVIS_SUBSYSTEM_ROLE", "test")
        from backend.core.managed_mode import build_health_envelope
        result = build_health_envelope({}, readiness="draining", drain_id="drain-abc")
        assert result["drain_id"] == "drain-abc"

    def test_no_enrichment_without_session(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ROOT_SESSION_ID", raising=False)
        from backend.core.managed_mode import build_health_envelope
        base = {"status": "ok"}
        result = build_health_envelope(base, readiness="ready")
        # Without session ID, returns base unchanged
        assert result == {"status": "ok"}


SCHEMA_VERSION = "1.0.0"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_managed_mode.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/core/managed_mode.py
"""
Managed-Mode Contract Utilities v1.0.0
=======================================
Subsystem-side utilities for the Root Authority managed-mode contract.
Used by Prime and Reactor when JARVIS_ROOT_MANAGED=true.

This module is duplicated in jarvis-prime and reactor-core repos.
SCHEMA_VERSION must match root_authority_types.py exactly.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

SCHEMA_VERSION = "1.0.0"

# Exit code constants
EXIT_CLEAN = 0
EXIT_CONFIG_ERROR = 100
EXIT_CONTRACT_MISMATCH = 101
EXIT_DEPENDENCY_FAILURE = 200
EXIT_RUNTIME_FATAL = 300

# Captured once at module load — never resets on hot reload
_BOOT_TIME_NS = time.monotonic_ns()
_PID = os.getpid()
_EXEC_FINGERPRINT: Optional[str] = None


def is_root_managed() -> bool:
    """Check if this subsystem is running under root authority."""
    return os.environ.get("JARVIS_ROOT_MANAGED", "").lower() == "true"


def get_session_id() -> str:
    return os.environ.get("JARVIS_ROOT_SESSION_ID", "")


def get_subsystem_role() -> str:
    return os.environ.get("JARVIS_SUBSYSTEM_ROLE", "")


def get_control_plane_secret() -> str:
    return os.environ.get("JARVIS_CONTROL_PLANE_SECRET", "")


def compute_exec_fingerprint(binary_path: str, cmdline: str) -> str:
    """sha256 of binary path + cmdline, truncated to 16 hex chars."""
    digest = hashlib.sha256(f"{binary_path}:{cmdline}".encode()).hexdigest()[:16]
    return f"sha256:{digest}"


def get_exec_fingerprint() -> str:
    """Get or compute the exec fingerprint for this process."""
    global _EXEC_FINGERPRINT
    if _EXEC_FINGERPRINT is None:
        cmdline = " ".join(sys.argv)
        binary = sys.executable or "unknown"
        _EXEC_FINGERPRINT = compute_exec_fingerprint(binary, cmdline)
    return _EXEC_FINGERPRINT


def compute_capability_hash(capabilities: dict) -> str:
    """Deterministic hash of capability declaration."""
    canonical = json.dumps(capabilities, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"sha256:{digest}"


def build_hmac_auth(session_id: str, secret: str) -> str:
    """Build HMAC auth header value for control-plane requests."""
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex[:12]
    msg = f"{session_id}:{ts}:{nonce}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{ts}:{nonce}:{sig}"


def verify_hmac_auth(header: str, session_id: str, secret: str,
                     tolerance_s: float = 30.0) -> bool:
    """Verify HMAC auth header. Returns False on any failure."""
    try:
        parts = header.split(":")
        if len(parts) != 3:
            return False
        ts_str, nonce, sig = parts
        ts = int(ts_str)
        if abs(time.time() - ts) > tolerance_s:
            return False
        msg = f"{session_id}:{ts_str}:{nonce}"
        expected = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except (ValueError, TypeError):
        return False


def build_health_envelope(base_response: dict, readiness: str,
                          drain_id: Optional[str] = None,
                          capability_hash: Optional[str] = None) -> dict:
    """Enrich health response with managed-mode fields.

    If JARVIS_ROOT_SESSION_ID is not set, returns base_response unchanged.
    """
    session_id = get_session_id()
    if not session_id:
        return base_response

    result = dict(base_response)
    result.update({
        "liveness": "up",
        "readiness": readiness,
        "session_id": session_id,
        "pid": _PID,
        "start_time_ns": _BOOT_TIME_NS,
        "exec_fingerprint": get_exec_fingerprint(),
        "subsystem_role": get_subsystem_role(),
        "schema_version": SCHEMA_VERSION,
        "capability_hash": capability_hash or "",
        "observed_at_ns": time.monotonic_ns(),
        "wall_time_utc": datetime.now(timezone.utc).isoformat(),
    })
    if drain_id:
        result["drain_id"] = drain_id
    return result
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_managed_mode.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/managed_mode.py tests/unit/core/test_managed_mode.py
git commit -m "feat(authority): add managed-mode contract utilities (Task 2)"
```

---

### Task 3: Contract Conformance Tests (Golden Tests)

**Files:**
- Create: `tests/unit/core/test_managed_mode_contract.py`

**Context:** These are the "golden" contract tests that will be duplicated identically in Prime and Reactor. They validate the contract shape, not implementation details.

**Step 1: Write the test file**

```python
# tests/unit/core/test_managed_mode_contract.py
"""
Golden Contract Conformance Tests v1.0.0
==========================================
These tests validate the managed-mode contract shape.
IDENTICAL copies must exist in:
  - JARVIS-AI-Agent/tests/unit/core/test_managed_mode_contract.py
  - jarvis-prime/tests/test_managed_mode_contract.py
  - reactor-core/tests/test_managed_mode_contract.py

CI drift check: compare file hashes across repos.
"""
import pytest

EXPECTED_SCHEMA_VERSION = "1.0.0"

REQUIRED_HEALTH_FIELDS = {
    "liveness", "readiness", "session_id", "pid", "start_time_ns",
    "exec_fingerprint", "subsystem_role", "schema_version",
    "capability_hash", "observed_at_ns", "wall_time_utc",
}

VALID_LIVENESS = {"up", "down"}
VALID_READINESS = {"ready", "not_ready", "degraded", "draining"}

EXIT_CODE_RANGES = {
    "clean": (0,),
    "config_contract": tuple(range(100, 110)),
    "dependency": tuple(range(200, 210)),
    "runtime_fatal": tuple(range(300, 310)),
}


class TestContractShape:
    """Validates the contract field names and value domains."""

    def test_schema_version_matches(self):
        from backend.core.managed_mode import SCHEMA_VERSION
        assert SCHEMA_VERSION == EXPECTED_SCHEMA_VERSION

    def test_health_envelope_has_required_fields(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "test-session")
        monkeypatch.setenv("JARVIS_SUBSYSTEM_ROLE", "test-role")
        from backend.core.managed_mode import build_health_envelope
        result = build_health_envelope({}, readiness="ready")
        missing = REQUIRED_HEALTH_FIELDS - set(result.keys())
        assert not missing, f"Missing required fields: {missing}"

    def test_liveness_values(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "test")
        monkeypatch.setenv("JARVIS_SUBSYSTEM_ROLE", "test")
        from backend.core.managed_mode import build_health_envelope
        result = build_health_envelope({}, readiness="ready")
        assert result["liveness"] in VALID_LIVENESS

    def test_readiness_values(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "test")
        monkeypatch.setenv("JARVIS_SUBSYSTEM_ROLE", "test")
        from backend.core.managed_mode import build_health_envelope
        for r in VALID_READINESS:
            result = build_health_envelope({}, readiness=r)
            assert result["readiness"] in VALID_READINESS


class TestExitCodeContract:
    """Validates exit code constants match the contract."""

    def test_clean_exit(self):
        from backend.core.managed_mode import EXIT_CLEAN
        assert EXIT_CLEAN in EXIT_CODE_RANGES["clean"]

    def test_config_error_exit(self):
        from backend.core.managed_mode import EXIT_CONFIG_ERROR, EXIT_CONTRACT_MISMATCH
        assert EXIT_CONFIG_ERROR in EXIT_CODE_RANGES["config_contract"]
        assert EXIT_CONTRACT_MISMATCH in EXIT_CODE_RANGES["config_contract"]

    def test_dependency_failure_exit(self):
        from backend.core.managed_mode import EXIT_DEPENDENCY_FAILURE
        assert EXIT_DEPENDENCY_FAILURE in EXIT_CODE_RANGES["dependency"]

    def test_runtime_fatal_exit(self):
        from backend.core.managed_mode import EXIT_RUNTIME_FATAL
        assert EXIT_RUNTIME_FATAL in EXIT_CODE_RANGES["runtime_fatal"]


class TestHMACContract:
    """Validates HMAC auth round-trip."""

    def test_build_verify_roundtrip(self):
        from backend.core.managed_mode import build_hmac_auth, verify_hmac_auth
        header = build_hmac_auth("sess-1", "secret-abc")
        assert verify_hmac_auth(header, "sess-1", "secret-abc")

    def test_session_mismatch_rejected(self):
        from backend.core.managed_mode import build_hmac_auth, verify_hmac_auth
        header = build_hmac_auth("sess-1", "secret-abc")
        assert not verify_hmac_auth(header, "sess-2", "secret-abc")
```

**Step 2: Run test**

Run: `python3 -m pytest tests/unit/core/test_managed_mode_contract.py -v`
Expected: ALL PASS (uses already-implemented managed_mode.py)

**Step 3: Commit**

```bash
git add tests/unit/core/test_managed_mode_contract.py
git commit -m "test(authority): add golden contract conformance tests (Task 3)"
```

---

### Task 4: Wave 0 Go/No-Go Gate

**Validation checklist:**
1. Run all Wave 0 tests: `python3 -m pytest tests/unit/core/test_root_authority_types.py tests/unit/core/test_managed_mode.py tests/unit/core/test_managed_mode_contract.py -v`
2. Verify zero import dependencies on orchestrator or USP: `python3 -c "from backend.core.root_authority_types import *; from backend.core.managed_mode import *; print('Clean imports')"`
3. All tests pass, no regressions

---

## Wave 1: Root Authority Watcher (Tasks 5-8)

### Task 5: Watcher State Machine Core

**Files:**
- Create: `backend/core/root_authority.py`
- Test: `tests/unit/core/test_root_authority_watcher.py`

**Context:** The watcher owns the lifecycle state machine. It observes process health and emits verdicts. It does NOT kill or spawn processes. It has ZERO imports from orchestrator or USP.

**Step 1: Write the failing test**

```python
# tests/unit/core/test_root_authority_watcher.py
"""Tests for RootAuthorityWatcher state machine and verdict emission."""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.core.root_authority_types import (
    ProcessIdentity, LifecycleAction, SubsystemState,
    TimeoutPolicy, RestartPolicy, LifecycleVerdict,
)


@pytest.fixture
def sample_identity():
    return ProcessIdentity(
        pid=1234, start_time_ns=time.monotonic_ns(),
        session_id="test-session", exec_fingerprint="sha256:abc123"
    )


@pytest.fixture
def sample_policy():
    return TimeoutPolicy(
        startup_grace_s=5.0, health_timeout_s=1.0,
        health_poll_interval_s=0.5, drain_timeout_s=5.0,
        term_timeout_s=2.0, degraded_tolerance_s=3.0,
        degraded_recovery_check_s=1.0,
    )


@pytest.fixture
def sample_restart_policy():
    return RestartPolicy(max_restarts=2, window_s=60.0, jitter_factor=0.0)


class TestWatcherStateTransitions:
    @pytest.mark.asyncio
    async def test_initial_state_is_starting(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        assert watcher.get_state("jarvis-prime") == SubsystemState.STARTING

    @pytest.mark.asyncio
    async def test_transition_to_alive_on_health_up(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        # Skip handshake for this test (contract_gate=None)
        watcher.process_health_response("jarvis-prime", {
            "liveness": "up", "readiness": "not_ready",
            "session_id": "test-session", "pid": 1234,
            "start_time_ns": sample_identity.start_time_ns,
            "exec_fingerprint": "sha256:abc123",
            "schema_version": "1.0.0",
        })
        state = watcher.get_state("jarvis-prime")
        assert state in (SubsystemState.ALIVE, SubsystemState.HANDSHAKE)

    @pytest.mark.asyncio
    async def test_transition_to_ready(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        watcher.process_health_response("jarvis-prime", {
            "liveness": "up", "readiness": "ready",
            "session_id": "test-session", "pid": 1234,
            "start_time_ns": sample_identity.start_time_ns,
            "exec_fingerprint": "sha256:abc123",
            "schema_version": "1.0.0",
        })
        # May need multiple transitions depending on handshake
        assert watcher.get_state("jarvis-prime") in (
            SubsystemState.READY, SubsystemState.ALIVE, SubsystemState.HANDSHAKE
        )

    @pytest.mark.asyncio
    async def test_crash_detection_emits_verdict(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        verdict = watcher.process_crash("jarvis-prime", exit_code=300)
        assert verdict is not None
        assert verdict.action == LifecycleAction.RESTART
        assert verdict.reason_code == "crash_exit_300"
        assert watcher.get_state("jarvis-prime") == SubsystemState.CRASHED

    @pytest.mark.asyncio
    async def test_clean_exit_no_restart(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        verdict = watcher.process_crash("jarvis-prime", exit_code=0)
        assert verdict is None or verdict.action == LifecycleAction.ESCALATE_OPERATOR
        assert watcher.get_state("jarvis-prime") == SubsystemState.STOPPED

    @pytest.mark.asyncio
    async def test_config_error_no_restart(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        verdict = watcher.process_crash("jarvis-prime", exit_code=101)
        assert verdict is not None
        assert verdict.action == LifecycleAction.ESCALATE_OPERATOR

    @pytest.mark.asyncio
    async def test_identity_mismatch_ignored(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        # Health from wrong PID
        watcher.process_health_response("jarvis-prime", {
            "liveness": "up", "readiness": "ready",
            "session_id": "test-session", "pid": 9999,
            "start_time_ns": sample_identity.start_time_ns,
            "exec_fingerprint": "sha256:abc123",
            "schema_version": "1.0.0",
        })
        # Should NOT transition — identity mismatch
        assert watcher.get_state("jarvis-prime") == SubsystemState.STARTING

    @pytest.mark.asyncio
    async def test_max_restarts_escalates(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,  # max_restarts=2
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        # Crash 3 times (exceeds max_restarts=2)
        watcher.process_crash("jarvis-prime", exit_code=300)
        watcher.register_subsystem("jarvis-prime", sample_identity)  # re-register after restart
        watcher.process_crash("jarvis-prime", exit_code=300)
        watcher.register_subsystem("jarvis-prime", sample_identity)
        verdict = watcher.process_crash("jarvis-prime", exit_code=300)
        assert verdict is not None
        assert verdict.action == LifecycleAction.ESCALATE_OPERATOR


class TestVerdictDeduplication:
    @pytest.mark.asyncio
    async def test_duplicate_crash_verdicts_coalesced(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        v1 = watcher.process_crash("jarvis-prime", exit_code=300)
        # Second crash for same incident (same subsystem, same reason, same time bucket)
        v2 = watcher.process_crash("jarvis-prime", exit_code=300)
        # Second should be None (deduplicated)
        assert v1 is not None
        assert v2 is None


class TestWatcherObservability:
    @pytest.mark.asyncio
    async def test_events_emitted_on_state_change(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        events = []
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
            event_sink=lambda e: events.append(e),
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        watcher.process_crash("jarvis-prime", exit_code=300)
        # Should have at least: state_transition to CRASHED + verdict_emitted
        assert len(events) >= 2
        event_types = {e.event_type for e in events}
        assert "state_transition" in event_types
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_root_authority_watcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.root_authority'`

**Step 3: Write minimal implementation**

```python
# backend/core/root_authority.py
"""
Root Authority Watcher v1.0.0
==============================
Lifecycle state machine for managed subsystems.
Observes health, detects crashes, emits verdicts.
Does NOT execute verdicts (that's ProcessOrchestrator's job).

ZERO imports from orchestrator or USP.
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from backend.core.root_authority_types import (
    ContractGate,
    ExecutionResult,
    LifecycleAction,
    LifecycleEvent,
    LifecycleVerdict,
    ProcessIdentity,
    RestartPolicy,
    SubsystemState,
    TimeoutPolicy,
    compute_incident_id,
)

logger = logging.getLogger(__name__)


class _SubsystemTracker:
    """Internal per-subsystem state tracking."""

    def __init__(self, name: str, identity: ProcessIdentity,
                 restart_policy: RestartPolicy):
        self.name = name
        self.identity = identity
        self.state = SubsystemState.STARTING
        self.restart_count = 0
        self.restart_timestamps: List[float] = []
        self.consecutive_health_failures = 0
        self.degraded_since_ns: Optional[int] = None
        self.draining_since_ns: Optional[int] = None
        self.drain_id: Optional[str] = None
        self.last_health_ns: Optional[int] = None
        self._restart_policy = restart_policy

    def can_restart(self) -> bool:
        """Check if restart budget allows another attempt."""
        now = time.monotonic()
        window = self._restart_policy.window_s
        recent = [t for t in self.restart_timestamps if now - t < window]
        self.restart_timestamps = recent
        return len(recent) < self._restart_policy.max_restarts

    def record_restart(self):
        self.restart_timestamps.append(time.monotonic())
        self.restart_count += 1


class RootAuthorityWatcher:
    """Lifecycle state machine for managed subsystems.

    Observes health responses and process exits.
    Emits LifecycleVerdicts — never executes them directly.
    """

    def __init__(
        self,
        session_id: str,
        timeout_policy: TimeoutPolicy,
        restart_policy: RestartPolicy,
        contract_gates: Optional[Dict[str, ContractGate]] = None,
        event_sink: Optional[Callable[[LifecycleEvent], None]] = None,
    ):
        self._session_id = session_id
        self._timeout = timeout_policy
        self._restart_policy = restart_policy
        self._contract_gates = contract_gates or {}
        self._event_sink = event_sink
        self._trackers: Dict[str, _SubsystemTracker] = {}
        self._recent_incidents: Set[str] = set()
        self._incident_timestamps: Dict[str, int] = {}

        # Telemetry counters
        self.verdicts_coalesced_total = 0
        self.verdicts_dropped_total = 0

    def register_subsystem(self, name: str, identity: ProcessIdentity):
        """Register a subsystem for monitoring."""
        self._trackers[name] = _SubsystemTracker(
            name, identity, self._restart_policy
        )
        self._emit_event(
            event_type="spawn",
            subsystem=name,
            identity=identity,
            to_state=SubsystemState.STARTING.value,
        )

    def get_state(self, name: str) -> SubsystemState:
        tracker = self._trackers.get(name)
        if tracker is None:
            raise KeyError(f"Unknown subsystem: {name}")
        return tracker.state

    def get_identity(self, name: str) -> Optional[ProcessIdentity]:
        tracker = self._trackers.get(name)
        return tracker.identity if tracker else None

    def process_health_response(self, name: str, data: dict) -> Optional[LifecycleVerdict]:
        """Process a health check response. Returns verdict if action needed."""
        tracker = self._trackers.get(name)
        if tracker is None:
            return None

        # Identity validation
        if not self._validate_identity(tracker, data):
            return None

        tracker.last_health_ns = time.monotonic_ns()
        tracker.consecutive_health_failures = 0

        liveness = data.get("liveness", "down")
        readiness = data.get("readiness", "not_ready")

        if liveness != "up":
            return None

        old_state = tracker.state

        # State transitions based on health
        if tracker.state == SubsystemState.STARTING:
            # Check handshake if contract gate exists
            gate = self._contract_gates.get(name)
            if gate:
                tracker.state = SubsystemState.HANDSHAKE
                if not self._check_handshake(name, gate, data):
                    tracker.state = SubsystemState.REJECTED
                    self._emit_transition(name, old_state, tracker.state, tracker.identity)
                    return self._make_verdict(
                        name, tracker, LifecycleAction.ESCALATE_OPERATOR,
                        "Contract handshake failed", "handshake_failed"
                    )
                # Handshake passed
                tracker.state = SubsystemState.ALIVE
            else:
                tracker.state = SubsystemState.ALIVE

        if tracker.state in (SubsystemState.ALIVE, SubsystemState.HANDSHAKE):
            if readiness == "ready":
                tracker.state = SubsystemState.READY
                tracker.degraded_since_ns = None
            elif readiness == "degraded":
                tracker.state = SubsystemState.DEGRADED
                tracker.degraded_since_ns = time.monotonic_ns()

        elif tracker.state == SubsystemState.READY:
            if readiness == "degraded":
                tracker.state = SubsystemState.DEGRADED
                tracker.degraded_since_ns = time.monotonic_ns()
            elif readiness == "not_ready":
                tracker.state = SubsystemState.ALIVE

        elif tracker.state == SubsystemState.DEGRADED:
            if readiness == "ready":
                tracker.state = SubsystemState.READY
                tracker.degraded_since_ns = None
            elif readiness == "not_ready":
                tracker.state = SubsystemState.ALIVE
                tracker.degraded_since_ns = None
            else:
                # Still degraded — check SLO window
                if tracker.degraded_since_ns:
                    elapsed_ns = time.monotonic_ns() - tracker.degraded_since_ns
                    elapsed_s = elapsed_ns / 1e9
                    if elapsed_s >= self._timeout.degraded_tolerance_s:
                        return self._make_verdict(
                            name, tracker, LifecycleAction.DRAIN,
                            f"Degraded for {elapsed_s:.1f}s (limit {self._timeout.degraded_tolerance_s}s)",
                            "degraded_slo_exceeded"
                        )

        if tracker.state != old_state:
            self._emit_transition(name, old_state, tracker.state, tracker.identity)

        return None

    def process_health_failure(self, name: str) -> Optional[LifecycleVerdict]:
        """Process a health check failure (timeout or connection error)."""
        tracker = self._trackers.get(name)
        if tracker is None:
            return None

        # Don't count failures during startup grace
        if tracker.state == SubsystemState.STARTING:
            return None

        tracker.consecutive_health_failures += 1
        n = tracker.consecutive_health_failures

        if n == 1:
            logger.warning(f"Health check miss for {name} (1 consecutive)")
        elif n == 2:
            old = tracker.state
            tracker.state = SubsystemState.DEGRADED
            tracker.degraded_since_ns = time.monotonic_ns()
            self._emit_transition(name, old, tracker.state, tracker.identity)
        elif n == 3:
            return self._make_verdict(
                name, tracker, LifecycleAction.DRAIN,
                f"{n} consecutive health failures", "health_timeout"
            )
        elif n >= 5:
            return self._make_verdict(
                name, tracker, LifecycleAction.GROUP_KILL,
                f"{n} consecutive health failures, drain likely stuck", "health_timeout_critical"
            )

        return None

    def process_crash(self, name: str, exit_code: int) -> Optional[LifecycleVerdict]:
        """Process an unexpected process exit."""
        tracker = self._trackers.get(name)
        if tracker is None:
            return None

        old_state = tracker.state

        # Clean shutdown
        if exit_code == 0:
            tracker.state = SubsystemState.STOPPED
            self._emit_transition(name, old_state, tracker.state, tracker.identity)
            return None

        # Abnormal exit
        tracker.state = SubsystemState.CRASHED
        self._emit_transition(name, old_state, tracker.state, tracker.identity)

        # Dedup check
        incident_id = compute_incident_id(
            name, tracker.identity, f"crash_exit_{exit_code}", time.monotonic_ns()
        )
        if incident_id in self._recent_incidents:
            self.verdicts_coalesced_total += 1
            return None
        self._recent_incidents.add(incident_id)

        # Should we restart?
        if not self._restart_policy.should_restart(exit_code):
            return self._make_verdict(
                name, tracker, LifecycleAction.ESCALATE_OPERATOR,
                f"Exit code {exit_code} is non-restartable", f"crash_exit_{exit_code}",
                exit_code=exit_code, incident_id=incident_id,
            )

        if not tracker.can_restart():
            return self._make_verdict(
                name, tracker, LifecycleAction.ESCALATE_OPERATOR,
                f"Max restarts exceeded ({self._restart_policy.max_restarts} in {self._restart_policy.window_s}s)",
                "max_restarts_exceeded",
                exit_code=exit_code, incident_id=incident_id,
            )

        tracker.record_restart()
        return self._make_verdict(
            name, tracker, LifecycleAction.RESTART,
            f"Crash with exit code {exit_code}", f"crash_exit_{exit_code}",
            exit_code=exit_code, incident_id=incident_id,
        )

    def _validate_identity(self, tracker: _SubsystemTracker, data: dict) -> bool:
        """Validate that health response matches expected process identity."""
        pid = data.get("pid")
        start_time_ns = data.get("start_time_ns")
        session_id = data.get("session_id")
        fingerprint = data.get("exec_fingerprint")

        if pid is not None and pid != tracker.identity.pid:
            logger.warning(f"PID mismatch for {tracker.name}: expected {tracker.identity.pid}, got {pid}")
            return False
        if session_id and session_id != tracker.identity.session_id:
            logger.warning(f"Session mismatch for {tracker.name}")
            return False
        if start_time_ns is not None and start_time_ns != tracker.identity.start_time_ns:
            logger.warning(f"Start time mismatch for {tracker.name}")
            return False
        if fingerprint and fingerprint != tracker.identity.exec_fingerprint:
            logger.warning(f"Exec fingerprint mismatch for {tracker.name}")
            return False
        return True

    def _check_handshake(self, name: str, gate: ContractGate, data: dict) -> bool:
        """Validate contract gate at handshake."""
        schema = data.get("schema_version", "")
        if not gate.is_schema_compatible(schema):
            logger.error(f"Schema incompatible for {name}: expected ~{gate.expected_schema_version}, got {schema}")
            return False

        missing = gate.required_health_fields - set(data.keys())
        if missing:
            logger.error(f"Missing required health fields for {name}: {missing}")
            return False

        return True

    def _make_verdict(
        self, name: str, tracker: _SubsystemTracker,
        action: LifecycleAction, reason: str, reason_code: str,
        exit_code: Optional[int] = None, incident_id: Optional[str] = None,
    ) -> LifecycleVerdict:
        now_ns = time.monotonic_ns()
        if incident_id is None:
            incident_id = compute_incident_id(
                name, tracker.identity, reason_code, now_ns
            )

        verdict = LifecycleVerdict(
            subsystem=name,
            identity=tracker.identity,
            action=action,
            reason=reason,
            reason_code=reason_code,
            correlation_id=str(uuid.uuid4()),
            incident_id=incident_id,
            exit_code=exit_code,
            observed_at_ns=now_ns,
            wall_time_utc=datetime.now(timezone.utc).isoformat(),
        )

        self._emit_event(
            event_type="verdict_emitted",
            subsystem=name,
            identity=tracker.identity,
            verdict_action=action.value,
            reason_code=reason_code,
            exit_code=exit_code,
        )

        return verdict

    def _emit_transition(self, name: str, old: SubsystemState,
                         new: SubsystemState, identity: ProcessIdentity):
        self._emit_event(
            event_type="state_transition",
            subsystem=name,
            identity=identity,
            from_state=old.value,
            to_state=new.value,
        )

    def _emit_event(self, event_type: str, subsystem: str, **kwargs):
        if self._event_sink is None:
            return
        identity = kwargs.pop("identity", None)
        event = LifecycleEvent(
            event_type=event_type,
            subsystem=subsystem,
            correlation_id=kwargs.pop("correlation_id", str(uuid.uuid4())),
            session_id=self._session_id,
            identity=identity,
            from_state=kwargs.get("from_state"),
            to_state=kwargs.get("to_state"),
            verdict_action=kwargs.get("verdict_action"),
            reason_code=kwargs.get("reason_code"),
            exit_code=kwargs.get("exit_code"),
            observed_at_ns=time.monotonic_ns(),
            wall_time_utc=datetime.now(timezone.utc).isoformat(),
            policy_source="root_authority",
        )
        self._event_sink(event)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_root_authority_watcher.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/root_authority.py tests/unit/core/test_root_authority_watcher.py
git commit -m "feat(authority): add RootAuthorityWatcher state machine (Task 5)"
```

---

### Task 6: VerdictExecutor Protocol

**Files:**
- Modify: `backend/core/root_authority.py` (add protocol at top)
- Test: `tests/unit/core/test_verdict_executor.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/test_verdict_executor.py
"""Tests for VerdictExecutor protocol compliance."""
import asyncio
import pytest
from unittest.mock import AsyncMock

from backend.core.root_authority_types import ProcessIdentity, ExecutionResult


class TestVerdictExecutorProtocol:
    def test_protocol_importable(self):
        from backend.core.root_authority import VerdictExecutor
        assert hasattr(VerdictExecutor, 'execute_drain')
        assert hasattr(VerdictExecutor, 'execute_term')
        assert hasattr(VerdictExecutor, 'execute_group_kill')
        assert hasattr(VerdictExecutor, 'execute_restart')
        assert hasattr(VerdictExecutor, 'get_current_identity')

    @pytest.mark.asyncio
    async def test_mock_executor_satisfies_protocol(self):
        from backend.core.root_authority import VerdictExecutor
        identity = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f")

        class MockExecutor:
            async def execute_drain(self, subsystem, identity, drain_timeout_s):
                return ExecutionResult(True, True, "success", None, None, "c1")
            async def execute_term(self, subsystem, identity, term_timeout_s):
                return ExecutionResult(True, True, "success", None, None, "c1")
            async def execute_group_kill(self, subsystem, identity):
                return ExecutionResult(True, True, "success", None, None, "c1")
            async def execute_restart(self, subsystem, delay_s):
                return ExecutionResult(True, True, "success", identity, None, "c1")
            def get_current_identity(self, subsystem):
                return identity

        executor = MockExecutor()
        result = await executor.execute_drain("test", identity, 30.0)
        assert result.accepted
        assert result.result == "success"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_verdict_executor.py -v`
Expected: FAIL (VerdictExecutor not defined)

**Step 3: Add VerdictExecutor protocol to root_authority.py**

Add this near the top of `backend/core/root_authority.py`, after imports:

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class VerdictExecutor(Protocol):
    """Interface that ProcessOrchestrator must implement.

    The watcher decides WHAT to do. The executor decides HOW.
    """
    async def execute_drain(self, subsystem: str, identity: ProcessIdentity,
                            drain_timeout_s: float) -> ExecutionResult: ...
    async def execute_term(self, subsystem: str, identity: ProcessIdentity,
                           term_timeout_s: float) -> ExecutionResult: ...
    async def execute_group_kill(self, subsystem: str,
                                 identity: ProcessIdentity) -> ExecutionResult: ...
    async def execute_restart(self, subsystem: str,
                              delay_s: float) -> ExecutionResult: ...
    def get_current_identity(self, subsystem: str) -> Optional[ProcessIdentity]: ...
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_verdict_executor.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/root_authority.py tests/unit/core/test_verdict_executor.py
git commit -m "feat(authority): add VerdictExecutor protocol (Task 6)"
```

---

### Task 7: Kill Escalation Engine

**Files:**
- Modify: `backend/core/root_authority.py` (add escalation engine)
- Test: `tests/unit/core/test_kill_escalation.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/test_kill_escalation.py
"""Tests for kill escalation ladder: drain -> term -> group_kill."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.core.root_authority_types import (
    ProcessIdentity, LifecycleAction, LifecycleVerdict, ExecutionResult,
    TimeoutPolicy, RestartPolicy,
)


@pytest.fixture
def identity():
    return ProcessIdentity(pid=100, start_time_ns=0, session_id="s1", exec_fingerprint="f1")


@pytest.fixture
def success_result():
    return ExecutionResult(True, True, "success", None, None, "c1")


@pytest.fixture
def timeout_result():
    return ExecutionResult(True, True, "timeout", None, None, "c1")


class TestEscalationEngine:
    @pytest.mark.asyncio
    async def test_drain_success_stops_escalation(self, identity, success_result):
        from backend.core.root_authority import EscalationEngine
        executor = AsyncMock()
        executor.execute_drain = AsyncMock(return_value=success_result)
        executor.get_current_identity = MagicMock(return_value=identity)

        engine = EscalationEngine(TimeoutPolicy(drain_timeout_s=5.0, term_timeout_s=2.0))
        result = await engine.escalate(
            "jarvis-prime", identity, executor, correlation_id="c1"
        )
        assert result == "drain_success"
        executor.execute_drain.assert_called_once()
        executor.execute_term.assert_not_called()

    @pytest.mark.asyncio
    async def test_drain_timeout_escalates_to_term(self, identity, timeout_result, success_result):
        from backend.core.root_authority import EscalationEngine
        executor = AsyncMock()
        executor.execute_drain = AsyncMock(return_value=timeout_result)
        executor.execute_term = AsyncMock(return_value=success_result)
        executor.get_current_identity = MagicMock(return_value=identity)

        engine = EscalationEngine(TimeoutPolicy(drain_timeout_s=5.0, term_timeout_s=2.0))
        result = await engine.escalate(
            "jarvis-prime", identity, executor, correlation_id="c1"
        )
        assert result == "term_success"
        executor.execute_drain.assert_called_once()
        executor.execute_term.assert_called_once()

    @pytest.mark.asyncio
    async def test_full_escalation_to_group_kill(self, identity, timeout_result, success_result):
        from backend.core.root_authority import EscalationEngine
        executor = AsyncMock()
        executor.execute_drain = AsyncMock(return_value=timeout_result)
        executor.execute_term = AsyncMock(return_value=timeout_result)
        executor.execute_group_kill = AsyncMock(return_value=success_result)
        executor.get_current_identity = MagicMock(return_value=identity)

        engine = EscalationEngine(TimeoutPolicy(drain_timeout_s=5.0, term_timeout_s=2.0))
        result = await engine.escalate(
            "jarvis-prime", identity, executor, correlation_id="c1"
        )
        assert result == "group_kill_success"

    @pytest.mark.asyncio
    async def test_stale_identity_aborts(self, identity, success_result):
        from backend.core.root_authority import EscalationEngine
        executor = AsyncMock()
        # Identity changed between verdict and execution
        different = ProcessIdentity(pid=200, start_time_ns=0, session_id="s1", exec_fingerprint="f1")
        executor.get_current_identity = MagicMock(return_value=different)

        engine = EscalationEngine(TimeoutPolicy())
        result = await engine.escalate(
            "jarvis-prime", identity, executor, correlation_id="c1"
        )
        assert result == "stale_identity"
        executor.execute_drain.assert_not_called()
```

**Step 2: Run test, verify fail, implement, verify pass**

Add `EscalationEngine` to `backend/core/root_authority.py`:

```python
class EscalationEngine:
    """Executes the kill escalation ladder: drain -> term -> group_kill.

    Race-safe: re-checks identity before each step.
    """
    def __init__(self, timeout_policy: TimeoutPolicy):
        self._timeout = timeout_policy

    async def escalate(
        self, subsystem: str, identity: ProcessIdentity,
        executor: VerdictExecutor, correlation_id: str,
    ) -> str:
        """Run escalation ladder. Returns result string."""
        # Race-safe check
        current = executor.get_current_identity(subsystem)
        if current != identity:
            return "stale_identity"

        # Step 1: Drain
        result = await executor.execute_drain(
            subsystem, identity, self._timeout.drain_timeout_s
        )
        if result.result == "success":
            return "drain_success"

        # Step 2: SIGTERM
        current = executor.get_current_identity(subsystem)
        if current != identity:
            return "stale_identity"
        result = await executor.execute_term(
            subsystem, identity, self._timeout.term_timeout_s
        )
        if result.result == "success":
            return "term_success"

        # Step 3: Process group kill
        current = executor.get_current_identity(subsystem)
        if current != identity:
            return "stale_identity"
        result = await executor.execute_group_kill(subsystem, identity)
        if result.result == "success":
            return "group_kill_success"

        return "escalation_failed"
```

**Step 3: Commit**

```bash
git add backend/core/root_authority.py tests/unit/core/test_kill_escalation.py
git commit -m "feat(authority): add kill escalation engine (Task 7)"
```

---

### Task 8: Wave 1 Go/No-Go Gate

**Validation:**
1. Run all Wave 0+1 tests: `python3 -m pytest tests/unit/core/test_root_authority_types.py tests/unit/core/test_managed_mode.py tests/unit/core/test_managed_mode_contract.py tests/unit/core/test_root_authority_watcher.py tests/unit/core/test_verdict_executor.py tests/unit/core/test_kill_escalation.py -v`
2. Verify zero imports from orchestrator/USP: `python3 -c "from backend.core.root_authority import RootAuthorityWatcher, VerdictExecutor, EscalationEngine; print('Clean imports')"`
3. All tests pass, watcher state machine transitions are correct, verdicts are strongly typed

---

## Wave 2: ProcessOrchestrator Integration (Tasks 9-12)

### Task 9: Copy `managed_mode.py` to Prime

**Files:**
- Create: `/Users/djrussell23/Documents/repos/jarvis-prime/managed_mode.py` (copy from `backend/core/managed_mode.py`)
- Create: `/Users/djrussell23/Documents/repos/jarvis-prime/tests/test_managed_mode_contract.py` (copy golden tests, adjust import path)

**Step 1:** Copy `backend/core/managed_mode.py` to `/Users/djrussell23/Documents/repos/jarvis-prime/managed_mode.py`

**Step 2:** Copy golden contract tests, adjusting import:
```python
# In jarvis-prime, imports change from:
#   from backend.core.managed_mode import ...
# To:
#   from managed_mode import ...
```

**Step 3:** Run: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_managed_mode_contract.py -v`
Expected: ALL PASS

**Step 4:** Commit in Prime repo:
```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
git add managed_mode.py tests/test_managed_mode_contract.py
git commit -m "feat(authority): add managed-mode contract utilities + golden tests (Task 9)"
```

---

### Task 10: Copy `managed_mode.py` to Reactor

**Files:**
- Create: `/Users/djrussell23/Documents/repos/reactor-core/managed_mode.py`
- Create: `/Users/djrussell23/Documents/repos/reactor-core/tests/test_managed_mode_contract.py`

Same pattern as Task 9. Adjust imports from `backend.core.managed_mode` to `managed_mode`.

**Commit in Reactor repo:**
```bash
cd /Users/djrussell23/Documents/repos/reactor-core
git add managed_mode.py tests/test_managed_mode_contract.py
git commit -m "feat(authority): add managed-mode contract utilities + golden tests (Task 10)"
```

---

### Task 11: Prime Managed-Mode Conformance

**Files:**
- Modify: `/Users/djrussell23/Documents/repos/jarvis-prime/run_supervisor.py:205-207,1013-1040`
- Modify: `/Users/djrussell23/Documents/repos/jarvis-prime/run_server.py:1110-1154,3891-3901`
- Test: `/Users/djrussell23/Documents/repos/jarvis-prime/tests/test_managed_mode_behavior.py`

**Step 1: Write the failing test**

```python
# /Users/djrussell23/Documents/repos/jarvis-prime/tests/test_managed_mode_behavior.py
"""Tests for Prime managed-mode behavior."""
import os
import pytest


class TestManagedModeRestart:
    def test_auto_restart_disabled_when_managed(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_MANAGED", "true")
        # Must re-import to pick up env
        import importlib
        # Test that the managed mode flag is respected
        from managed_mode import is_root_managed
        assert is_root_managed()

    def test_auto_restart_enabled_when_not_managed(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ROOT_MANAGED", raising=False)
        from managed_mode import is_root_managed
        assert not is_root_managed()
```

**Step 2: Modify `run_supervisor.py`**

At line 205, change:
```python
auto_restart: bool = True
```
To:
```python
auto_restart: bool = not os.environ.get("JARVIS_ROOT_MANAGED", "").lower() == "true"
```

Add import at top: `import os` (if not already present)

In the health monitor (line 1013-1040), after the `if not healthy and manager.config.auto_restart:` block, add:
```python
elif not healthy and os.environ.get("JARVIS_ROOT_MANAGED", "").lower() == "true":
    from managed_mode import EXIT_RUNTIME_FATAL
    logger.critical(
        "Component unhealthy in managed mode - exiting for root restart",
        extra={"event": "fatal", "exit_code": EXIT_RUNTIME_FATAL,
               "component": name,
               "session_id": os.environ.get("JARVIS_ROOT_SESSION_ID", "")}
    )
    # Signal controlled shutdown instead of sys.exit()
    self._shutdown_event.set()
    self._managed_exit_code = EXIT_RUNTIME_FATAL
    return
```

**Step 3: Modify `run_server.py`**

At `/health` endpoint (line 1110), enrich the response:
```python
@app.get("/health")
async def health_check():
    # ... existing logic ...
    status = _startup_state.get_status() if _startup_state else {"status": "starting"}

    # Managed-mode enrichment
    session_id = os.environ.get("JARVIS_ROOT_SESSION_ID", "")
    if session_id:
        from managed_mode import build_health_envelope
        readiness = "ready" if status.get("phase") == "ready" else "not_ready"
        if status.get("status") == "error":
            readiness = "degraded"
        status = build_health_envelope(status, readiness=readiness)

    return status
```

Add `/lifecycle/drain` endpoint (after `/health`):
```python
_draining = False
_drain_id = None
_shutdown_event = None  # set from startup

@app.post("/lifecycle/drain")
async def lifecycle_drain(request: Request):
    global _draining, _drain_id
    body = await request.json()
    session_id = os.environ.get("JARVIS_ROOT_SESSION_ID", "")

    # Session gating
    if body.get("session_id") != session_id:
        return JSONResponse(status_code=409, content={"error": "session_id mismatch"})

    # HMAC auth
    from managed_mode import verify_hmac_auth, get_control_plane_secret
    auth_header = request.headers.get("X-Root-Auth", "")
    if not verify_hmac_auth(auth_header, session_id, get_control_plane_secret()):
        return JSONResponse(status_code=403, content={"error": "auth failed"})

    # Idempotent
    if _draining:
        return JSONResponse(status_code=202, content={
            "drain_id": _drain_id, "session_id": session_id, "status": "already_draining"
        })

    import uuid
    _draining = True
    _drain_id = str(uuid.uuid4())

    # Trigger graceful shutdown
    asyncio.create_task(_drain_and_exit_prime())

    return JSONResponse(status_code=202, content={
        "drain_id": _drain_id, "session_id": session_id, "status": "draining"
    })

async def _drain_and_exit_prime():
    """Controlled drain: stop accepting, flush, signal exit."""
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Drain initiated: {_drain_id}")
    await asyncio.sleep(5)  # allow in-flight to complete
    logger.info("Drain complete, signaling shutdown")
    if _shutdown_event:
        _shutdown_event.set()
```

**Step 4: Run tests, commit in Prime repo**

```bash
cd /Users/djrussell23/Documents/repos/jarvis-prime
python3 -m pytest tests/test_managed_mode_behavior.py tests/test_managed_mode_contract.py -v
git add run_supervisor.py run_server.py tests/test_managed_mode_behavior.py
git commit -m "feat(authority): Prime managed-mode conformance (Task 11)"
```

---

### Task 12: Reactor Managed-Mode Conformance

**Files:**
- Modify: `/Users/djrussell23/Documents/repos/reactor-core/run_supervisor.py:746-770`
- Modify: `/Users/djrussell23/Documents/repos/reactor-core/reactor_core/api/server.py:1039-1083`
- Test: `/Users/djrussell23/Documents/repos/reactor-core/tests/test_managed_mode_behavior.py`

Same pattern as Task 11:

1. In `run_supervisor.py` `should_restart()` (line 746): return `(False, 0.0)` when `JARVIS_ROOT_MANAGED=true`
2. In `reactor_core/api/server.py` `/health` (line 1039): enrich with `build_health_envelope()`
3. Add `/lifecycle/drain` endpoint to server.py
4. Run tests, commit

```bash
cd /Users/djrussell23/Documents/repos/reactor-core
git add run_supervisor.py reactor_core/api/server.py tests/test_managed_mode_behavior.py
git commit -m "feat(authority): Reactor managed-mode conformance (Task 12)"
```

---

### Task 13: Wave 2 Go/No-Go Gate

**Validation across all 3 repos:**
1. JARVIS: `python3 -m pytest tests/unit/core/test_root_authority_types.py tests/unit/core/test_managed_mode.py tests/unit/core/test_managed_mode_contract.py tests/unit/core/test_root_authority_watcher.py tests/unit/core/test_verdict_executor.py tests/unit/core/test_kill_escalation.py -v`
2. Prime: `cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/test_managed_mode_contract.py tests/test_managed_mode_behavior.py -v`
3. Reactor: `cd /Users/djrussell23/Documents/repos/reactor-core && python3 -m pytest tests/test_managed_mode_contract.py tests/test_managed_mode_behavior.py -v`
4. Verify managed_mode.py hash matches across repos: `md5 backend/core/managed_mode.py ../jarvis-prime/managed_mode.py ../reactor-core/managed_mode.py`

---

## Wave 3: USP Wiring & Shadow Mode (Tasks 14-17)

### Task 14: ProcessOrchestrator VerdictExecutor Adapter

**Files:**
- Modify: `backend/supervisor/cross_repo_startup_orchestrator.py`
- Test: `tests/unit/supervisor/test_orchestrator_executor.py`

**Context:** Add VerdictExecutor methods to ProcessOrchestrator. Do NOT remove existing health monitoring yet (that happens after shadow mode validates).

**Key changes to ProcessOrchestrator (line 7885):**
1. Add `_verdict_executor_mode: bool = False` field
2. Implement `execute_drain()`, `execute_term()`, `execute_group_kill()`, `execute_restart()`, `get_current_identity()`
3. Add `set_verdict_executor_mode(enabled: bool)` method
4. Add `_build_hmac_auth()` helper
5. Add `_verify_group_dead()` helper
6. Add `_get_process_identity()` to construct ProcessIdentity from ManagedProcess

This is a large task -- the implementer should read the design doc Section 3 carefully for the exact interface contract.

**Commit:**
```bash
git add backend/supervisor/cross_repo_startup_orchestrator.py tests/unit/supervisor/test_orchestrator_executor.py
git commit -m "feat(authority): add VerdictExecutor adapter to ProcessOrchestrator (Task 14)"
```

---

### Task 15: USP Wiring (Shadow Mode)

**Files:**
- Modify: `unified_supervisor.py` (in the kernel startup sequence)
- Test: `tests/unit/supervisor/test_root_authority_wiring.py`

**Context:** Wire RootAuthorityWatcher into the kernel boot sequence. Default to shadow mode (`JARVIS_ROOT_AUTHORITY_MODE=shadow`).

**Key changes:**
1. Import `RootAuthorityWatcher`, `EscalationEngine` from `backend.core.root_authority`
2. In `JarvisSystemKernel.__init__()` or startup: create watcher if `JARVIS_ROOT_AUTHORITY_MODE` is set
3. After `ProcessOrchestrator` spawns each subsystem, register it with watcher
4. In shadow mode: watcher observes and logs verdicts but does NOT execute them
5. Add `JARVIS_ROOT_AUTHORITY_MODE` (shadow|active) and `JARVIS_ROOT_AUTHORITY_SUBSYSTEMS` env vars

**Commit:**
```bash
git add unified_supervisor.py tests/unit/supervisor/test_root_authority_wiring.py
git commit -m "feat(authority): wire RootAuthorityWatcher into USP kernel (Task 15)"
```

---

### Task 16: Active `process.wait()` Crash Detection

**Files:**
- Modify: `backend/core/root_authority.py` (add async monitoring methods)
- Test: `tests/unit/core/test_active_crash_detection.py`

**Context:** Add `async def watch_process()` and `async def poll_health()` methods to RootAuthorityWatcher that run as background tasks.

**Key additions to RootAuthorityWatcher:**
```python
async def watch_process(self, name: str, proc: asyncio.subprocess.Process):
    """Active crash detection -- fires within ms of process exit."""
    exit_code = await proc.wait()
    verdict = self.process_crash(name, exit_code)
    if verdict:
        await self._verdict_queue.put(verdict)

async def poll_health(self, name: str, health_url: str, session: aiohttp.ClientSession):
    """Passive health polling with jitter."""
    while True:
        interval = self._timeout.health_poll_interval_s * (1 + random.uniform(-0.2, 0.2))
        await asyncio.sleep(interval)
        try:
            async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=self._timeout.health_timeout_s)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    verdict = self.process_health_response(name, data)
                else:
                    verdict = self.process_health_failure(name)
        except Exception:
            verdict = self.process_health_failure(name)
        if verdict:
            await self._verdict_queue.put(verdict)
```

**Commit:**
```bash
git add backend/core/root_authority.py tests/unit/core/test_active_crash_detection.py
git commit -m "feat(authority): add active crash detection + health polling (Task 16)"
```

---

### Task 17: Wave 3 Go/No-Go Gate

**Validation:**
1. Run full test suite: all JARVIS tests from Waves 0-3
2. Verify shadow mode: start system with `JARVIS_ROOT_AUTHORITY_MODE=shadow`, confirm watcher logs verdicts without executing
3. Verify active crash detection: kill a subsystem, confirm watcher detects within 1s (Gate G1)

---

## Wave 4: Activation & Hardening (Tasks 18-21)

### Task 18: Active Mode with Per-Subsystem Kill Switch

**Files:**
- Modify: `unified_supervisor.py` (switch from shadow to active mode)
- Modify: `backend/core/root_authority.py` (verdict dispatch loop)

**Context:** When `JARVIS_ROOT_AUTHORITY_MODE=active`, the watcher's verdict queue is consumed by the orchestrator. Per-subsystem activation via `JARVIS_ROOT_AUTHORITY_SUBSYSTEMS=reactor-core,jarvis-prime`.

**Commit:**
```bash
git commit -m "feat(authority): enable active mode with per-subsystem kill switch (Task 18)"
```

---

### Task 19: Contract Hash Gating at Boot

**Files:**
- Modify: `backend/core/root_authority.py` (handshake validation)
- Test: `tests/unit/core/test_contract_gating.py`

**Context:** Add `HANDSHAKE` state to boot flow. Validate schema_version (N/N-1), capability_hash, and required fields. Emergency bypass via `JARVIS_CONTRACT_BYPASS=<subsystem>`.

**Commit:**
```bash
git commit -m "feat(authority): add contract hash gating at boot handshake (Task 19)"
```

---

### Task 20: Surgical Policy Removal from ProcessOrchestrator

**Files:**
- Modify: `backend/supervisor/cross_repo_startup_orchestrator.py`

**Context:** When `_verdict_executor_mode=True`:
1. `_health_monitor_loop()` delegates to watcher instead of making its own decisions
2. Restart decision logic defers to watcher
3. `calculate_backoff()` deferred to watcher's RestartPolicy
4. Circuit breaker trip decisions deferred to watcher

This is the "remove policy brain from orchestrator" step. The fallback path (when root authority is off) keeps existing behavior.

**Commit:**
```bash
git commit -m "refactor(authority): surgical policy removal from ProcessOrchestrator (Task 20)"
```

---

### Task 21: Wave 4 Go/No-Go — Final Validation

**Validation across all repos:**
1. Full JARVIS test suite passes
2. Prime contract tests pass
3. Reactor contract tests pass
4. Gates G1-G6 validated:
   - G1: Crash detection <1s
   - G2: Shadow verdict parity (manual check if not 48h soak)
   - G3: Drain + no corruption
   - G4: Group kill + no orphans
   - G5: No restart storms
   - G6: Contract mismatch blocks READY

---

## Summary

| Wave | Tasks | Scope | Go/No-Go |
|------|-------|-------|----------|
| 0 | 1-4 | Foundation types, managed_mode, golden tests | Clean imports, all tests pass |
| 1 | 5-8 | Watcher state machine, VerdictExecutor, escalation | State transitions correct, zero orchestrator imports |
| 2 | 9-13 | Prime/Reactor conformance, managed_mode copies | All 3 repos pass contract tests, hash parity |
| 3 | 14-17 | Orchestrator adapter, USP wiring, shadow mode | Shadow mode works, crash detection <1s |
| 4 | 18-21 | Active mode, contract gating, policy removal | Gates G1-G6, no restart storms |

**Total:** 21 tasks across 5 waves, 3 repos.
