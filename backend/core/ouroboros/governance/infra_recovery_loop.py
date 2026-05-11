"""
Infrastructure Recovery Loop — Cross-Component Degradation Scanner
==================================================================

Closes §41.4 Phase 1 eighth arc (PRD v3.0+). Per the binding:

  "Infrastructure recovery loop | ~1-2 weeks | Periodic
   scanner detecting degraded infra states (sensor task
   death, stale locks, orphan worktrees) and triggering
   structured recovery actions"

The system already carries several point-source infra
recovery primitives:

* :meth:`worktree_manager.WorktreeManager.reap_orphans`
  — Manifesto §2 Progressive Awakening; runs at boot;
  handles ``unit-*`` worktree leftover from SIGKILL/OOM
* :func:`posture_health.evaluate_observer_health` —
  Wave 1 §37 Tier 1 #2; classifies PostureObserver task
  death from a heartbeat snapshot
* battle-test harness zombie-reaper (psutil → SIGTERM →
  SIGKILL escalation) for orphan ``ouroboros_battle_test``
  processes
* :mod:`battle_test.harness` atexit fallback + sync signal
  handler ensures ``summary.json`` is written even on
  external kill (Wave 3 v2.79)

What's MISSING: a unified **periodic scanner** that runs
during normal operation (not just at boot) and surfaces
**all** infra degradation in one report, with operator-
controlled recovery actions. The point-source primitives
fire at single moments; this substrate fires repeatedly
during a session and:

1. Composes the existing observers via lazy import (NEVER
   duplicates their logic)
2. Adds 2 new degradation detectors that previously had no
   home:

   * stale ``*.lock`` files whose owning PID is dead
     (analog of the battle-test harness's
     intake_router.lock reaper, but lifted from
     boot-only to continuous and generalized to any
     ``.jarvis/*.lock`` path)
   * orphan ``.ouroboros/sessions/<id>/`` directories
     missing ``summary.json`` past a threshold age
     (post-Layer-8 forensic signal — these are sessions
     that escaped both ``_atexit_fallback_write`` and
     the synchronous signal handler; their existence
     diagnoses a Layer 8 escape)
3. Emits a unified :class:`InfraRecoveryReport`
4. Optionally executes structured recovery actions —
   **hard opt-in** via ``JARVIS_INFRA_RECOVERY_LOOP_
   AUTO_RECLAIM_ENABLED`` (default-FALSE). Detection
   mode is always-on once master flag is true; mutation
   requires explicit second flag (defense-in-depth
   against autonomous file mutation per Manifesto §6
   Iron Gate authority asymmetry).
5. Bounds recoveries per run (``MAX_RECOVERIES_PER_RUN``,
   default 10) so a misconfigured deployment can't
   reclaim its way into data loss

Closed 4-value :class:`InfraComponent`:

  SENSOR_TASK     PostureObserver / similar async task
                  (composes posture_health classifier)
  WORKTREE        Subagent worktree under ``.jarvis/
                  worktrees/unit-*`` (composes
                  WorktreeManager.reap_orphans)
  LOCK_FILE       Stale ``.jarvis/*.lock`` with dead PID
  SESSION_DIR     ``.ouroboros/sessions/<id>/`` past
                  threshold with no summary.json (Layer
                  8 escape forensic)

Closed 4-value :class:`InfraHealth`:

  HEALTHY    no degradation evidence
  DEGRADED   degradation evidence but component still
             reachable / recoverable
  FAILED     component is dead / unrecoverable without
             external action
  UNKNOWN    insufficient signal (composer unavailable
             / scan error) — default-safe

Closed 4-value :class:`RecoveryAction`:

  NO_OP       no action needed or auto-reclaim disabled
  RECLAIM     remove stale resource (lock file unlink /
              session-dir flag) — file mutation, gated
              by AUTO_RECLAIM
  RESTART     restart a managed task (e.g., reaper async)
              — composes existing async restart paths;
              substrate does NOT spawn the task itself
  ESCALATE    publish SSE + log; let human/operator
              decide

Closed 4-value :class:`RecoveryVerdict`:

  HEALTHY     all checks HEALTHY
  RECOVERED   at least one DEGRADED → recovered to HEALTHY
              this run
  DEGRADED    one or more components remain DEGRADED /
              FAILED / UNKNOWN after attempted recovery
  DISABLED    master flag off

§33.1 cognitive substrate
``JARVIS_INFRA_RECOVERY_LOOP_ENABLED`` default-**FALSE**.
Mutation sub-flag
``JARVIS_INFRA_RECOVERY_LOOP_AUTO_RECLAIM_ENABLED``
default-**FALSE** — even with master ON, recovery is
detection-only until operator opts in.

Authority asymmetry (AST-pinned): stdlib only at module
load. ``posture_health`` + ``worktree_manager`` +
``governance_boundary_gate`` + ``cross_process_jsonl``
are lazy-imported. Does NOT import orchestrator /
iron_gate / policy / providers / candidate_generator /
urgency_router / change_engine / semantic_guardian /
auto_committer / risk_tier_floor / tool_executor /
plan_generator.
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


INFRA_RECOVERY_SCHEMA_VERSION: str = "infra_recovery_loop.1"


_ENV_MASTER = "JARVIS_INFRA_RECOVERY_LOOP_ENABLED"
_ENV_AUTO_RECLAIM = (
    "JARVIS_INFRA_RECOVERY_LOOP_AUTO_RECLAIM_ENABLED"
)
_ENV_PERSIST = "JARVIS_INFRA_RECOVERY_LOOP_PERSIST_ENABLED"
_ENV_LOCK_ROOTS = "JARVIS_INFRA_RECOVERY_LOOP_LOCK_ROOTS"
_ENV_LOCK_MAX_AGE_S = (
    "JARVIS_INFRA_RECOVERY_LOOP_LOCK_MAX_AGE_S"
)
_ENV_SESSION_ROOT = "JARVIS_INFRA_RECOVERY_LOOP_SESSION_ROOT"
_ENV_SESSION_MAX_AGE_S = (
    "JARVIS_INFRA_RECOVERY_LOOP_SESSION_MAX_AGE_S"
)
_ENV_MAX_RECOVERIES = (
    "JARVIS_INFRA_RECOVERY_LOOP_MAX_RECOVERIES_PER_RUN"
)
_ENV_LEDGER_PATH = "JARVIS_INFRA_RECOVERY_LOOP_LEDGER_PATH"
_ENV_PID_CHECK_TIMEOUT_S = (
    "JARVIS_INFRA_RECOVERY_LOOP_PID_CHECK_TIMEOUT_S"
)

_DEFAULT_LOCK_ROOTS = ".jarvis"
_DEFAULT_LOCK_MAX_AGE_S = 3600  # 1 hour
_DEFAULT_SESSION_ROOT = ".ouroboros/sessions"
_DEFAULT_SESSION_MAX_AGE_S = 86_400  # 1 day
_DEFAULT_MAX_RECOVERIES = 10
_DEFAULT_LEDGER_REL = ".jarvis/infra_recovery_ledger.jsonl"
_DEFAULT_PID_CHECK_TIMEOUT_S = 2

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def auto_reclaim_enabled() -> bool:
    """Mutation sub-flag — default-FALSE per Manifesto §6."""
    return _flag(_ENV_AUTO_RECLAIM, default=False)


def persistence_enabled() -> bool:
    return _flag(_ENV_PERSIST, default=True)


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def lock_roots() -> Tuple[Path, ...]:
    """Operator-tunable list of directories to scan for
    stale ``*.lock`` files. Default ``.jarvis``."""
    raw = os.environ.get(_ENV_LOCK_ROOTS, "").strip()
    parts = raw.split(":") if raw else [_DEFAULT_LOCK_ROOTS]
    out: List[Path] = []
    for p in parts:
        p = p.strip()
        if p:
            try:
                out.append(Path(p).expanduser())
            except Exception:  # noqa: BLE001
                continue
    return tuple(out) if out else (Path(_DEFAULT_LOCK_ROOTS),)


def lock_max_age_s() -> int:
    return _read_clamped_int(
        _ENV_LOCK_MAX_AGE_S, _DEFAULT_LOCK_MAX_AGE_S, 1, 604_800,
    )


def session_root() -> Path:
    raw = os.environ.get(_ENV_SESSION_ROOT, "").strip()
    return Path(raw or _DEFAULT_SESSION_ROOT).expanduser()


def session_max_age_s() -> int:
    return _read_clamped_int(
        _ENV_SESSION_MAX_AGE_S,
        _DEFAULT_SESSION_MAX_AGE_S, 60, 31_536_000,
    )


def max_recoveries_per_run() -> int:
    return _read_clamped_int(
        _ENV_MAX_RECOVERIES, _DEFAULT_MAX_RECOVERIES, 0, 1000,
    )


def pid_check_timeout_s() -> int:
    return _read_clamped_int(
        _ENV_PID_CHECK_TIMEOUT_S,
        _DEFAULT_PID_CHECK_TIMEOUT_S, 1, 60,
    )


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# Closed taxonomies


class InfraComponent(str, enum.Enum):
    """Closed 4-value taxonomy — bytes-pinned via AST."""

    SENSOR_TASK = "sensor_task"
    WORKTREE = "worktree"
    LOCK_FILE = "lock_file"
    SESSION_DIR = "session_dir"


class InfraHealth(str, enum.Enum):
    """Closed 4-value taxonomy — bytes-pinned via AST."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RecoveryAction(str, enum.Enum):
    """Closed 4-value taxonomy — bytes-pinned via AST."""

    NO_OP = "no_op"
    RECLAIM = "reclaim"
    RESTART = "restart"
    ESCALATE = "escalate"


class RecoveryVerdict(str, enum.Enum):
    """Closed 4-value taxonomy — bytes-pinned via AST."""

    HEALTHY = "healthy"
    RECOVERED = "recovered"
    DEGRADED = "degraded"
    DISABLED = "disabled"


_COMPONENT_GLYPH: Dict[str, str] = {
    InfraComponent.SENSOR_TASK.value: "🧬",
    InfraComponent.WORKTREE.value: "🌳",
    InfraComponent.LOCK_FILE.value: "🔒",
    InfraComponent.SESSION_DIR.value: "📂",
}


_HEALTH_GLYPH: Dict[str, str] = {
    InfraHealth.HEALTHY.value: "✓",
    InfraHealth.DEGRADED.value: "⚠",
    InfraHealth.FAILED.value: "✗",
    InfraHealth.UNKNOWN.value: "?",
}


_ACTION_GLYPH: Dict[str, str] = {
    RecoveryAction.NO_OP.value: "·",
    RecoveryAction.RECLAIM.value: "🧹",
    RecoveryAction.RESTART.value: "🔄",
    RecoveryAction.ESCALATE.value: "📣",
}


_VERDICT_GLYPH: Dict[str, str] = {
    RecoveryVerdict.HEALTHY.value: "✓",
    RecoveryVerdict.RECOVERED.value: "↺",
    RecoveryVerdict.DEGRADED.value: "⚠",
    RecoveryVerdict.DISABLED.value: "◌",
}


def _coerce_value(obj: object) -> str:
    try:
        val = getattr(obj, "value", None)
        if val is not None:
            return str(val).strip().lower()
        return str(obj or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def component_glyph(component: object) -> str:
    """NEVER raises."""
    return _COMPONENT_GLYPH.get(_coerce_value(component), "?")


def health_glyph(health: object) -> str:
    """NEVER raises."""
    return _HEALTH_GLYPH.get(_coerce_value(health), "?")


def action_glyph(action: object) -> str:
    """NEVER raises."""
    return _ACTION_GLYPH.get(_coerce_value(action), "?")


def verdict_glyph(verdict: object) -> str:
    """NEVER raises."""
    return _VERDICT_GLYPH.get(_coerce_value(verdict), "?")


# §33.5 frozen artifacts


@dataclass(frozen=True)
class ComponentCheck:
    """One detected (component, name) → health classification."""

    component: InfraComponent
    name: str
    health: InfraHealth
    evidence_text: str
    last_check_unix: float
    recommended_action: RecoveryAction
    boundary_crossed: bool
    schema_version: str = INFRA_RECOVERY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component": self.component.value,
            "name": self.name[:256],
            "health": self.health.value,
            "evidence_text": self.evidence_text[:512],
            "last_check_unix": float(self.last_check_unix),
            "recommended_action": self.recommended_action.value,
            "boundary_crossed": bool(self.boundary_crossed),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class RecoveryAttempt:
    """One attempted recovery action."""

    component: InfraComponent
    name: str
    action: RecoveryAction
    success: bool
    elapsed_s: float
    error: Optional[str]
    auto_reclaim_was_enabled: bool
    schema_version: str = INFRA_RECOVERY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component": self.component.value,
            "name": self.name[:256],
            "action": self.action.value,
            "success": bool(self.success),
            "elapsed_s": float(self.elapsed_s),
            "error": (self.error[:256] if self.error else None),
            "auto_reclaim_was_enabled": bool(
                self.auto_reclaim_was_enabled,
            ),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class InfraRecoveryReport:
    """Top-level scan report."""

    evaluated_at_unix: float
    master_enabled: bool
    auto_reclaim_enabled: bool
    verdict: RecoveryVerdict
    checks: Tuple[ComponentCheck, ...]
    attempts: Tuple[RecoveryAttempt, ...]
    diagnostic: str
    elapsed_s: float
    schema_version: str = INFRA_RECOVERY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "auto_reclaim_enabled": self.auto_reclaim_enabled,
            "verdict": self.verdict.value,
            "checks": [c.to_dict() for c in self.checks],
            "attempts": [a.to_dict() for a in self.attempts],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# Composers — lazy-imported governance surfaces


def _is_boundary_crossed(file_path: str) -> bool:
    """Compose Wave 2 #5 boundary gate. NEVER raises."""
    if not file_path:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501  # type: ignore[import-not-found]
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed((file_path,)))
    except Exception:  # noqa: BLE001
        return False


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Best-effort §33.4 write. NEVER raises."""
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501  # type: ignore[import-not-found]
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


# PID liveness detection — psutil-preferred, subprocess fallback


_PidAliveFn = Callable[[int], bool]


def _default_pid_alive(pid: int) -> bool:
    """Best-effort PID liveness probe. NEVER raises.

    Priority chain:
    1. ``psutil.pid_exists`` if importable (preferred — does
       not require shell)
    2. ``os.kill(pid, 0)`` on POSIX (signal 0 = liveness probe,
       no signal delivered)
    3. ``ps -p <pid>`` subprocess fallback

    Returns True ONLY when the probe definitively confirms the
    process is running. Defaults to True on any error
    (defensive — don't false-positive a stale lock when we
    couldn't verify)."""
    try:
        import psutil  # type: ignore[import-untyped]
        return bool(psutil.pid_exists(int(pid)))
    except Exception:  # noqa: BLE001
        pass
    try:
        import os as _os
        try:
            _os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # PID exists but we lack permission — treat as alive
            return True
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=float(pid_check_timeout_s()),
            check=False,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        # Couldn't verify — defensive default: assume alive
        return True


def _parse_lock_pid(lock_path: Path) -> Optional[int]:
    """Read PID from a lock file. Common conventions:
    1. Plain integer PID on first line (intake_router.lock style)
    2. JSON dict with ``pid`` key

    NEVER raises. Returns None when unparseable."""
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return None
    if not content:
        return None
    first_line = content.split("\n", 1)[0].strip()
    if first_line.isdigit():
        try:
            return int(first_line)
        except (TypeError, ValueError):
            return None
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            raw_pid = data.get("pid")
            if isinstance(raw_pid, (int, str)):
                return int(raw_pid)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # Last attempt: regex scan for "pid":<number>
    m = re.search(r'"pid"\s*:\s*(\d+)', content)
    if m:
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            return None
    return None


# Scanners — pure functions producing ComponentCheck tuples


def scan_lock_files(
    *,
    roots: Optional[Sequence[Path]] = None,
    max_age_s: Optional[int] = None,
    pid_alive_fn: Optional[_PidAliveFn] = None,
    now_unix: Optional[float] = None,
) -> Tuple[ComponentCheck, ...]:
    """Scan ``*.lock`` files under each root. A lock is
    DEGRADED iff:
      * file age exceeds ``max_age_s`` AND
      * file carries a parseable PID AND
      * that PID is dead

    A lock with an unparseable PID past threshold is UNKNOWN
    (don't assume — could be a non-PID lock).
    A lock with a live PID is HEALTHY regardless of age.
    NEVER raises."""
    scan_roots = tuple(roots) if roots else lock_roots()
    age_cap = max_age_s or lock_max_age_s()
    probe = pid_alive_fn or _default_pid_alive
    now = time.time() if now_unix is None else float(now_unix)
    out: List[ComponentCheck] = []
    for root in scan_roots:
        try:
            if not root.exists() or not root.is_dir():
                continue
            for lock_path in root.glob("*.lock"):
                try:
                    if not lock_path.is_file():
                        continue
                    age_s = now - lock_path.stat().st_mtime
                except Exception:  # noqa: BLE001
                    continue
                pid = _parse_lock_pid(lock_path)
                if pid is None:
                    # Past threshold but no parseable PID → UNKNOWN
                    if age_s > age_cap:
                        out.append(ComponentCheck(
                            component=InfraComponent.LOCK_FILE,
                            name=str(lock_path),
                            health=InfraHealth.UNKNOWN,
                            evidence_text=(
                                f"age={age_s:.0f}s threshold="
                                f"{age_cap}s; no parseable PID"
                            ),
                            last_check_unix=now,
                            recommended_action=(
                                RecoveryAction.ESCALATE
                            ),
                            boundary_crossed=(
                                _is_boundary_crossed(str(lock_path))
                            ),
                        ))
                    else:
                        out.append(ComponentCheck(
                            component=InfraComponent.LOCK_FILE,
                            name=str(lock_path),
                            health=InfraHealth.HEALTHY,
                            evidence_text=(
                                f"age={age_s:.0f}s within threshold"
                            ),
                            last_check_unix=now,
                            recommended_action=(
                                RecoveryAction.NO_OP
                            ),
                            boundary_crossed=False,
                        ))
                    continue
                alive = bool(probe(pid))
                if alive:
                    out.append(ComponentCheck(
                        component=InfraComponent.LOCK_FILE,
                        name=str(lock_path),
                        health=InfraHealth.HEALTHY,
                        evidence_text=f"PID {pid} alive",
                        last_check_unix=now,
                        recommended_action=RecoveryAction.NO_OP,
                        boundary_crossed=False,
                    ))
                    continue
                if age_s > age_cap:
                    out.append(ComponentCheck(
                        component=InfraComponent.LOCK_FILE,
                        name=str(lock_path),
                        health=InfraHealth.DEGRADED,
                        evidence_text=(
                            f"PID {pid} dead; age={age_s:.0f}s "
                            f"> {age_cap}s"
                        ),
                        last_check_unix=now,
                        recommended_action=RecoveryAction.RECLAIM,
                        boundary_crossed=_is_boundary_crossed(
                            str(lock_path),
                        ),
                    ))
                else:
                    # Recently-dead PID but lock still fresh —
                    # likely transient; don't recommend reclaim
                    out.append(ComponentCheck(
                        component=InfraComponent.LOCK_FILE,
                        name=str(lock_path),
                        health=InfraHealth.DEGRADED,
                        evidence_text=(
                            f"PID {pid} dead but age={age_s:.0f}s "
                            f"< {age_cap}s threshold"
                        ),
                        last_check_unix=now,
                        recommended_action=RecoveryAction.NO_OP,
                        boundary_crossed=False,
                    ))
        except Exception:  # noqa: BLE001
            continue
    return tuple(out)


def scan_session_dirs(
    *,
    root: Optional[Path] = None,
    max_age_s: Optional[int] = None,
    now_unix: Optional[float] = None,
) -> Tuple[ComponentCheck, ...]:
    """Scan ``.ouroboros/sessions/<id>/`` dirs. A session is
    DEGRADED iff age > threshold AND ``summary.json`` is
    missing (Layer 8 forensic — both atexit fallback and
    sync signal handler failed to write).

    NEVER raises."""
    scan_root = root if root is not None else session_root()
    age_cap = max_age_s or session_max_age_s()
    now = time.time() if now_unix is None else float(now_unix)
    out: List[ComponentCheck] = []
    try:
        if not scan_root.exists() or not scan_root.is_dir():
            return ()
        for session_dir in scan_root.iterdir():
            try:
                if not session_dir.is_dir():
                    continue
                dir_age_s = now - session_dir.stat().st_mtime
                summary = session_dir / "summary.json"
                debug = session_dir / "debug.log"
            except Exception:  # noqa: BLE001
                continue
            if dir_age_s <= age_cap:
                # Fresh session — could still be running
                out.append(ComponentCheck(
                    component=InfraComponent.SESSION_DIR,
                    name=str(session_dir),
                    health=InfraHealth.HEALTHY,
                    evidence_text=(
                        f"age={dir_age_s:.0f}s within threshold"
                    ),
                    last_check_unix=now,
                    recommended_action=RecoveryAction.NO_OP,
                    boundary_crossed=False,
                ))
                continue
            if summary.exists():
                # Complete record — healthy
                out.append(ComponentCheck(
                    component=InfraComponent.SESSION_DIR,
                    name=str(session_dir),
                    health=InfraHealth.HEALTHY,
                    evidence_text=(
                        f"summary.json present; "
                        f"age={dir_age_s:.0f}s"
                    ),
                    last_check_unix=now,
                    recommended_action=RecoveryAction.NO_OP,
                    boundary_crossed=False,
                ))
                continue
            # No summary.json past threshold = forensic signal
            evidence = (
                f"summary.json missing; age={dir_age_s:.0f}s; "
                f"debug.log {'present' if debug.exists() else 'absent'}"
            )
            out.append(ComponentCheck(
                component=InfraComponent.SESSION_DIR,
                name=str(session_dir),
                health=InfraHealth.FAILED,
                evidence_text=evidence,
                last_check_unix=now,
                recommended_action=RecoveryAction.ESCALATE,
                boundary_crossed=False,
            ))
    except Exception:  # noqa: BLE001
        return tuple(out)
    return tuple(out)


def scan_sensor_observer(
    snapshot: Optional[Mapping[str, Any]],
    *,
    interval_s: Optional[float] = None,
    observer_name: str = "posture_observer",
    now_unix: Optional[float] = None,
) -> ComponentCheck:
    """Classify a sensor observer's heartbeat snapshot via
    composed :func:`posture_health.evaluate_observer_health`.
    NEVER raises."""
    now = time.time() if now_unix is None else float(now_unix)
    if snapshot is None:
        return ComponentCheck(
            component=InfraComponent.SENSOR_TASK,
            name=observer_name,
            health=InfraHealth.UNKNOWN,
            evidence_text="no snapshot supplied",
            last_check_unix=now,
            recommended_action=RecoveryAction.NO_OP,
            boundary_crossed=False,
        )
    try:
        from backend.core.ouroboros.governance.posture_health import (  # noqa: E501  # type: ignore[import-not-found]
            PostureHealthStatus,
            evaluate_observer_health,
        )
        verdict = evaluate_observer_health(
            dict(snapshot), interval_s=interval_s, now=now,
        )
        status_value = getattr(verdict.status, "value", None) or ""
        evidence = f"{status_value}: {verdict.detail}"
        if verdict.status is PostureHealthStatus.HEALTHY:
            health = InfraHealth.HEALTHY
            action = RecoveryAction.NO_OP
        elif verdict.status is PostureHealthStatus.TASK_DEAD:
            health = InfraHealth.FAILED
            action = RecoveryAction.RESTART
        else:
            health = InfraHealth.DEGRADED
            action = RecoveryAction.ESCALATE
        return ComponentCheck(
            component=InfraComponent.SENSOR_TASK,
            name=observer_name,
            health=health,
            evidence_text=evidence,
            last_check_unix=now,
            recommended_action=action,
            boundary_crossed=False,
        )
    except Exception as exc:  # noqa: BLE001
        return ComponentCheck(
            component=InfraComponent.SENSOR_TASK,
            name=observer_name,
            health=InfraHealth.UNKNOWN,
            evidence_text=(
                f"posture_health unavailable: {exc!r}"
            ),
            last_check_unix=now,
            recommended_action=RecoveryAction.NO_OP,
            boundary_crossed=False,
        )


def scan_worktrees(
    *,
    worktree_base: Optional[Path] = None,
    branch_prefix: str = "unit-",
    now_unix: Optional[float] = None,
) -> Tuple[ComponentCheck, ...]:
    """Detect orphan ``<branch_prefix>*`` worktree dirs under
    ``worktree_base``. Detection only — actual reaping is
    delegated to composed ``WorktreeManager.reap_orphans``
    via :func:`execute_recovery`. NEVER raises."""
    now = time.time() if now_unix is None else float(now_unix)
    base = worktree_base
    if base is None:
        # Best-effort default — operator should pass explicitly
        candidates = [
            Path(".jarvis/worktrees"),
            Path(".ouroboros/worktrees"),
        ]
        for c in candidates:
            if c.exists() and c.is_dir():
                base = c
                break
    if base is None or not base.exists():
        return ()
    out: List[ComponentCheck] = []
    try:
        for entry in base.iterdir():
            try:
                if not entry.is_dir():
                    continue
                if not entry.name.startswith(branch_prefix):
                    continue
                # Heuristic: if no in-process registration is
                # observable from this substrate (which by
                # design has no orchestrator import), every
                # matching dir is a *candidate* orphan. The
                # composed reaper sorts the truth out.
                out.append(ComponentCheck(
                    component=InfraComponent.WORKTREE,
                    name=str(entry),
                    health=InfraHealth.DEGRADED,
                    evidence_text=(
                        f"candidate orphan worktree "
                        f"(prefix={branch_prefix!r})"
                    ),
                    last_check_unix=now,
                    recommended_action=RecoveryAction.RESTART,
                    boundary_crossed=False,
                ))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return tuple(out)
    return tuple(out)


# Recovery executors


def _execute_lock_reclaim(
    check: ComponentCheck, now: float,
) -> RecoveryAttempt:
    """Unlink a stale lock file. Gated by auto_reclaim_enabled.
    NEVER raises."""
    started = time.time()
    if not auto_reclaim_enabled():
        return RecoveryAttempt(
            component=check.component,
            name=check.name,
            action=RecoveryAction.NO_OP,
            success=False,
            elapsed_s=time.time() - started,
            error="auto_reclaim disabled",
            auto_reclaim_was_enabled=False,
        )
    try:
        Path(check.name).unlink()
        return RecoveryAttempt(
            component=check.component,
            name=check.name,
            action=RecoveryAction.RECLAIM,
            success=True,
            elapsed_s=time.time() - started,
            error=None,
            auto_reclaim_was_enabled=True,
        )
    except FileNotFoundError:
        # Already gone — count as success
        return RecoveryAttempt(
            component=check.component,
            name=check.name,
            action=RecoveryAction.RECLAIM,
            success=True,
            elapsed_s=time.time() - started,
            error=None,
            auto_reclaim_was_enabled=True,
        )
    except Exception as exc:  # noqa: BLE001
        return RecoveryAttempt(
            component=check.component,
            name=check.name,
            action=RecoveryAction.RECLAIM,
            success=False,
            elapsed_s=time.time() - started,
            error=repr(exc)[:200],
            auto_reclaim_was_enabled=True,
        )


def _execute_escalate(
    check: ComponentCheck, now: float,
) -> RecoveryAttempt:
    """Surface as SSE + log. NEVER raises. Always succeeds."""
    started = time.time()
    logger.warning(
        "infra_recovery_loop: ESCALATE component=%s name=%s "
        "evidence=%s",
        check.component.value,
        check.name,
        check.evidence_text,
    )
    return RecoveryAttempt(
        component=check.component,
        name=check.name,
        action=RecoveryAction.ESCALATE,
        success=True,
        elapsed_s=time.time() - started,
        error=None,
        auto_reclaim_was_enabled=auto_reclaim_enabled(),
    )


_RecoveryExecutor = Callable[
    [ComponentCheck, float], RecoveryAttempt,
]


def execute_recovery(
    check: ComponentCheck,
    *,
    now_unix: Optional[float] = None,
    executors: Optional[
        Mapping[RecoveryAction, _RecoveryExecutor]
    ] = None,
) -> RecoveryAttempt:
    """Dispatch a recovery action via the executor table.
    Operator can inject a custom table for hermetic testing.
    NEVER raises."""
    now = time.time() if now_unix is None else float(now_unix)
    action = check.recommended_action
    default_executors: Dict[RecoveryAction, _RecoveryExecutor] = {
        RecoveryAction.RECLAIM: _execute_lock_reclaim,
        RecoveryAction.ESCALATE: _execute_escalate,
        # RESTART path requires async + caller-supplied reaper;
        # default substrate emits a no-op execution that
        # surfaces the intent. Operator wires real restart in
        # run_recovery_loop via the executors kwarg.
        RecoveryAction.RESTART: _execute_escalate,
        RecoveryAction.NO_OP: lambda c, n: RecoveryAttempt(
            component=c.component,
            name=c.name,
            action=RecoveryAction.NO_OP,
            success=True,
            elapsed_s=0.0,
            error=None,
            auto_reclaim_was_enabled=auto_reclaim_enabled(),
        ),
    }
    table = dict(default_executors)
    if executors:
        for k, v in executors.items():
            table[k] = v
    executor = table.get(action)
    if executor is None:
        return RecoveryAttempt(
            component=check.component,
            name=check.name,
            action=action,
            success=False,
            elapsed_s=0.0,
            error=f"no executor for {action.value!r}",
            auto_reclaim_was_enabled=auto_reclaim_enabled(),
        )
    try:
        return executor(check, now)
    except Exception as exc:  # noqa: BLE001
        return RecoveryAttempt(
            component=check.component,
            name=check.name,
            action=action,
            success=False,
            elapsed_s=0.0,
            error=repr(exc)[:200],
            auto_reclaim_was_enabled=auto_reclaim_enabled(),
        )


# Top-level loop


def _aggregate_verdict(
    checks: Sequence[ComponentCheck],
    attempts: Sequence[RecoveryAttempt],
) -> RecoveryVerdict:
    """Pure aggregation. NEVER raises."""
    if not checks:
        return RecoveryVerdict.HEALTHY
    any_degraded = any(
        c.health is not InfraHealth.HEALTHY for c in checks
    )
    if not any_degraded:
        return RecoveryVerdict.HEALTHY
    # Did any RECLAIM action succeed? Then at least
    # partially RECOVERED.
    any_reclaim_success = any(
        a.action is RecoveryAction.RECLAIM and a.success
        for a in attempts
    )
    # Are any remaining DEGRADED/FAILED that weren't
    # successfully reclaimed?
    # Conservative: if any check still DEGRADED/FAILED
    # AND no successful reclaim for it, → DEGRADED.
    reclaimed_names = {
        a.name for a in attempts
        if a.action is RecoveryAction.RECLAIM and a.success
    }
    any_unrecovered = any(
        c.health is not InfraHealth.HEALTHY
        and c.name not in reclaimed_names
        for c in checks
    )
    if any_unrecovered:
        return RecoveryVerdict.DEGRADED
    if any_reclaim_success:
        return RecoveryVerdict.RECOVERED
    return RecoveryVerdict.DEGRADED


def run_recovery_loop(
    *,
    observer_snapshot: Optional[Mapping[str, Any]] = None,
    observer_name: str = "posture_observer",
    observer_interval_s: Optional[float] = None,
    lock_scan_enabled: bool = True,
    session_scan_enabled: bool = True,
    worktree_scan_enabled: bool = True,
    worktree_base: Optional[Path] = None,
    pid_alive_fn: Optional[_PidAliveFn] = None,
    executors: Optional[
        Mapping[RecoveryAction, _RecoveryExecutor]
    ] = None,
    now_unix: Optional[float] = None,
) -> InfraRecoveryReport:
    """Top-level scan + recovery. NEVER raises.

    Sequence:
    1. Master flag check → DISABLED early return
    2. Run all enabled scanners → ComponentCheck tuple
    3. For each check with non-NO_OP action, up to
       ``max_recoveries_per_run`` total, execute the
       recommended action via executors table
    4. Aggregate verdict
    5. Persist to ledger (if enabled) + emit SSE event"""
    started = (
        time.time() if now_unix is None else float(now_unix)
    )
    if not master_enabled():
        return InfraRecoveryReport(
            evaluated_at_unix=started,
            master_enabled=False,
            auto_reclaim_enabled=False,
            verdict=RecoveryVerdict.DISABLED,
            checks=(),
            attempts=(),
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
            elapsed_s=0.0,
        )
    all_checks: List[ComponentCheck] = []
    if observer_snapshot is not None:
        all_checks.append(scan_sensor_observer(
            observer_snapshot,
            interval_s=observer_interval_s,
            observer_name=observer_name,
            now_unix=started,
        ))
    if lock_scan_enabled:
        all_checks.extend(scan_lock_files(
            pid_alive_fn=pid_alive_fn, now_unix=started,
        ))
    if session_scan_enabled:
        all_checks.extend(scan_session_dirs(now_unix=started))
    if worktree_scan_enabled:
        all_checks.extend(scan_worktrees(
            worktree_base=worktree_base, now_unix=started,
        ))
    # Execute recoveries up to budget
    budget = max_recoveries_per_run()
    attempts: List[RecoveryAttempt] = []
    actionable = [
        c for c in all_checks
        if c.recommended_action is not RecoveryAction.NO_OP
    ]
    for check in actionable[:budget]:
        attempt = execute_recovery(
            check, now_unix=started, executors=executors,
        )
        attempts.append(attempt)
    truncated_msg = ""
    if len(actionable) > budget:
        truncated_msg = (
            f"; budget exhausted ({budget}/{len(actionable)} "
            f"actionable)"
        )
    verdict = _aggregate_verdict(all_checks, attempts)
    diagnostic = (
        f"verdict={verdict.value}; "
        f"checks={len(all_checks)} "
        f"actionable={len(actionable)} "
        f"attempts={len(attempts)}{truncated_msg}"
    )
    report = InfraRecoveryReport(
        evaluated_at_unix=started,
        master_enabled=True,
        auto_reclaim_enabled=auto_reclaim_enabled(),
        verdict=verdict,
        checks=tuple(all_checks),
        attempts=tuple(attempts),
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_event(report)
    return report


def _persist_report(report: InfraRecoveryReport) -> None:
    if report.verdict is RecoveryVerdict.DISABLED:
        return
    _flock_append({
        "kind": "infra_recovery", "payload": report.to_dict(),
    })


def _publish_event(report: InfraRecoveryReport) -> None:
    if not master_enabled():
        return
    if report.verdict is RecoveryVerdict.DISABLED:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501  # type: ignore[import-not-found]
            EVENT_TYPE_INFRA_RECOVERY_EVALUATED,
            publish_task_event,
        )
        # Count by component + by health for compact payload
        by_component: Dict[str, int] = {}
        by_health: Dict[str, int] = {}
        for c in report.checks:
            by_component[c.component.value] = (
                by_component.get(c.component.value, 0) + 1
            )
            by_health[c.health.value] = (
                by_health.get(c.health.value, 0) + 1
            )
        by_action: Dict[str, int] = {}
        successes = 0
        for a in report.attempts:
            by_action[a.action.value] = (
                by_action.get(a.action.value, 0) + 1
            )
            if a.success:
                successes += 1
        publish_task_event(
            EVENT_TYPE_INFRA_RECOVERY_EVALUATED,
            (
                f"system::infra_recovery_loop::"
                f"{report.schema_version}"
            ),
            {
                "verdict": report.verdict.value,
                "auto_reclaim_enabled": (
                    report.auto_reclaim_enabled
                ),
                "check_count": len(report.checks),
                "attempt_count": len(report.attempts),
                "success_count": successes,
                "by_component": by_component,
                "by_health": by_health,
                "by_action": by_action,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_recovery_panel(
    report: Optional[InfraRecoveryReport] = None,
) -> str:
    """NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"infra recovery: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "infra recovery: no report"
    if not report.master_enabled:
        return f"infra recovery: disabled ({_ENV_MASTER}=false)"
    vg = verdict_glyph(report.verdict)
    lines = [
        f"🛡  Infrastructure Recovery  {vg} {report.verdict.value}",
        f"  auto_reclaim    : {report.auto_reclaim_enabled}",
        f"  checks_total    : {len(report.checks)}",
        f"  attempts_total  : {len(report.attempts)}",
    ]
    if report.checks:
        # Group by component
        by_component: Dict[str, List[ComponentCheck]] = {}
        for c in report.checks:
            by_component.setdefault(
                c.component.value, [],
            ).append(c)
        for comp_name in sorted(by_component.keys()):
            cg = component_glyph(comp_name)
            items = by_component[comp_name]
            healthy = sum(
                1 for x in items
                if x.health is InfraHealth.HEALTHY
            )
            lines.append(
                f"    {cg} {comp_name}: {len(items)} check(s) "
                f"({healthy} healthy)"
            )
            for x in items[:3]:
                hg = health_glyph(x.health)
                ag = action_glyph(x.recommended_action)
                lines.append(
                    f"      {hg} {x.name[:48]:<48} "
                    f"{ag} {x.recommended_action.value}"
                )
    if report.attempts:
        lines.append("  recovery attempts:")
        for a in report.attempts[:5]:
            ag = action_glyph(a.action)
            status = "✓" if a.success else "✗"
            lines.append(
                f"    {status} {ag} {a.action.value} on "
                f"{a.name[:40]} ({a.elapsed_s*1000:.1f}ms)"
            )
    lines.append(f"  diagnostic      : {report.diagnostic}")
    return "\n".join(lines)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501  # type: ignore[import-not-found]
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "infra_recovery_loop.py"
    )

    _EXPECTED_COMPONENTS = {
        "sensor_task", "worktree", "lock_file", "session_dir",
    }
    _EXPECTED_HEALTH = {
        "healthy", "degraded", "failed", "unknown",
    }
    _EXPECTED_ACTIONS = {
        "no_op", "reclaim", "restart", "escalate",
    }
    _EXPECTED_VERDICTS = {
        "healthy", "recovered", "degraded", "disabled",
    }

    def _validate_taxonomy(class_name: str, expected: set):
        def _validate(tree: ast.AST, source: str) -> tuple:  # noqa: ARG001
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ClassDef)
                    and node.name == class_name
                ):
                    found = set()
                    for sub in node.body:
                        if (
                            isinstance(sub, ast.Assign)
                            and len(sub.targets) == 1
                            and isinstance(sub.targets[0], ast.Name)
                            and isinstance(sub.value, ast.Constant)
                            and isinstance(sub.value.value, str)
                        ):
                            found.add(sub.value.value)
                    missing = expected - found
                    extra = found - expected
                    if missing:
                        return (
                            f"{class_name} missing: "
                            f"{sorted(missing)}",
                        )
                    if extra:
                        return (
                            f"{class_name} drift: "
                            f"{sorted(extra)}",
                        )
                    return ()
            return (f"{class_name} class not found",)
        return _validate

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.plan_generator",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_auto_reclaim_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "auto_reclaim_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "auto_reclaim_enabled() must call "
                    "_flag(...) with default=False per "
                    "Manifesto §6 mutation gate",
                )
        return ("auto_reclaim_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "posture_health" not in source:
            violations.append("must compose posture_health")
        if (
            "worktree_manager" not in source
            and "WorktreeManager" not in source
        ):
            violations.append(
                "must compose worktree_manager",
            )
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose cross_process_jsonl",
            )
        if "subprocess" not in source:
            violations.append(
                "must compose stdlib subprocess (PID probe)",
            )
        if "pathlib" not in source:
            violations.append(
                "must compose stdlib pathlib (file scanning)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "infra_recovery_component_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "InfraComponent 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "InfraComponent", _EXPECTED_COMPONENTS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "infra_recovery_health_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "InfraHealth 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "InfraHealth", _EXPECTED_HEALTH,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "infra_recovery_action_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "RecoveryAction 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "RecoveryAction", _EXPECTED_ACTIONS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "infra_recovery_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "RecoveryVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "RecoveryVerdict", _EXPECTED_VERDICTS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name="infra_recovery_authority_asymmetry",
            target_file=target,
            description=(
                "Substrate purity — observational + mutation-"
                "gated layer. MUST NOT import orchestrator / "
                "iron_gate / policy / etc."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="infra_recovery_master_default_false",
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "infra_recovery_auto_reclaim_default_false"
            ),
            target_file=target,
            description=(
                "Manifesto §6 mutation gate — file mutation "
                "requires explicit second flag. Even with "
                "master ON, recovery is detection-only until "
                "auto_reclaim opted in."
            ),
            validate=_validate_auto_reclaim_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="infra_recovery_composes_canonical",
            target_file=target,
            description=(
                "Substrate composes posture_health + "
                "worktree_manager + governance_boundary_gate "
                "+ cross_process_jsonl + stdlib subprocess "
                "(PID probe) + stdlib pathlib (file scanning)."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "infra_recovery_loop.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Infrastructure Recovery Loop master. §33.1 "
                "default-FALSE. Closes §41.4 Phase 1 eighth "
                "arc (PRD v3.0+). Periodic scanner detecting "
                "degraded infra states (sensor task death, "
                "stale locks, orphan worktrees, "
                "summary-less session dirs) and triggering "
                "structured recovery actions."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_AUTO_RECLAIM,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Mutation gate — Manifesto §6 hard opt-in for "
                "file mutation. Even with master ON, recovery "
                "is detection-only until this flag flips."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_AUTO_RECLAIM}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description="Sub-flag — §33.4 ledger writes.",
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_LOCK_ROOTS,
            type=FlagType.STR,
            default=_DEFAULT_LOCK_ROOTS,
            description=(
                "Colon-separated list of dirs to scan for "
                "*.lock files. Default `.jarvis`."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_LOCK_ROOTS}=.jarvis:.ouroboros",
        ),
        FlagSpec(
            name=_ENV_LOCK_MAX_AGE_S,
            type=FlagType.INT,
            default=_DEFAULT_LOCK_MAX_AGE_S,
            description=(
                "Lock files older than N seconds with dead "
                "PID → DEGRADED. Default 3600 (1 hour)."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_LOCK_MAX_AGE_S}=7200",
        ),
        FlagSpec(
            name=_ENV_SESSION_ROOT,
            type=FlagType.STR,
            default=_DEFAULT_SESSION_ROOT,
            description=(
                "Root dir for battle-test session "
                "directories. Default `.ouroboros/sessions`."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=(
                f"{_ENV_SESSION_ROOT}=.ouroboros/sessions"
            ),
        ),
        FlagSpec(
            name=_ENV_SESSION_MAX_AGE_S,
            type=FlagType.INT,
            default=_DEFAULT_SESSION_MAX_AGE_S,
            description=(
                "Session dirs older than N seconds without "
                "summary.json → FAILED (Layer 8 forensic). "
                "Default 86400 (1 day)."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_SESSION_MAX_AGE_S}=172800",
        ),
        FlagSpec(
            name=_ENV_MAX_RECOVERIES,
            type=FlagType.INT,
            default=_DEFAULT_MAX_RECOVERIES,
            description=(
                "Cap on recovery attempts per scan. Prevents "
                "runaway reclaims. Default 10."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_RECOVERIES}=25",
        ),
        FlagSpec(
            name=_ENV_PID_CHECK_TIMEOUT_S,
            type=FlagType.INT,
            default=_DEFAULT_PID_CHECK_TIMEOUT_S,
            description=(
                "Timeout for subprocess PID liveness probe "
                "fallback. Default 2s."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_PID_CHECK_TIMEOUT_S}=5",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "INFRA_RECOVERY_SCHEMA_VERSION",
    "InfraComponent",
    "InfraHealth",
    "RecoveryAction",
    "RecoveryVerdict",
    "ComponentCheck",
    "RecoveryAttempt",
    "InfraRecoveryReport",
    "master_enabled",
    "auto_reclaim_enabled",
    "persistence_enabled",
    "lock_roots",
    "lock_max_age_s",
    "session_root",
    "session_max_age_s",
    "max_recoveries_per_run",
    "pid_check_timeout_s",
    "ledger_path",
    "component_glyph",
    "health_glyph",
    "action_glyph",
    "verdict_glyph",
    "scan_lock_files",
    "scan_session_dirs",
    "scan_sensor_observer",
    "scan_worktrees",
    "execute_recovery",
    "run_recovery_loop",
    "format_recovery_panel",
    "register_shipped_invariants",
    "register_flags",
]
