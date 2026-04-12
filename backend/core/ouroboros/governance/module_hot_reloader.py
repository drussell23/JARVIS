"""ModuleHotReloader — Manifesto §6 RSI loop closer.

Allows the running Ouroboros engine to reload its own pure-function modules
in-process after a successful self-modification, without a process restart.

Without this, an engine fix to (e.g.) verify_gate.py only takes effect on the
NEXT process boot — breaking the neuroplasticity loop. With it, the very next
op uses the freshly-fixed code.

## Constraints
- Only modules in DEFAULT_SAFE_MODULES are eligible.
- Modules in DEFAULT_QUARANTINE are NEVER reloaded (defense-in-depth).
- Modules with mutable module-level state (semaphores, caches, registries) MUST NOT
  be added to the safe set without first hoisting that state to a never-reloaded
  module — reload re-initializes module-level objects, orphaning any waiters.
- Callers of safe modules MUST use lazy/deferred imports inside method bodies, not
  module-level `from X import Y` (which captures a stale reference at orchestrator
  import time and never re-runs).

## Safety properties
- Atomic preflight: validate every candidate before reloading any.
- Hash verification: confirm post-reload that the on-disk source matches.
- Function identity probe: where a probe is registered, confirm
  `id(old_probe) != id(new_probe)` to prove the function object was actually
  replaced (catches "reload silently failed" cases).
- Self-quarantine: this module cannot reload itself.
- Lock-protected: concurrent reload attempts are serialized.
- Restart fallback: on partial failure, queue restart so the harness respawns.

## Anti-patterns this prevents
- Reloading the orchestrator while ops are in flight.
- Reloading a module that holds a live asyncio.Semaphore (orphans waiters).
- Reloading a module whose stale references are held module-level by callers.

## Adding a module to the safe set
1. Verify it has NO module-level mutable state (semaphores, locks, caches,
   registries, asyncio primitives).
2. Verify all callers use lazy/deferred imports (`from X import Y` inside
   methods, not at module top).
3. Add to DEFAULT_SAFE_MODULES below.
4. Register a probe symbol in PROBES (a stable public function or class).
5. Add a regression test in tests/test_ouroboros_governance/test_module_hot_reloader.py.
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Modules safe to hot-reload. Each must satisfy the constraints in the
# module docstring above. Curate this list deliberately — adding a stateful
# module here will cause runtime corruption.
DEFAULT_SAFE_MODULES: FrozenSet[str] = frozenset({
    "backend.core.ouroboros.governance.verify_gate",
    "backend.core.ouroboros.governance.patch_benchmarker",
    "backend.core.ouroboros.governance.semantic_triage",
    "backend.core.ouroboros.governance.plan_generator",
    "backend.core.ouroboros.governance.strategic_direction",
})

# Modules that are NEVER reloaded — even if they appear in a safe set by
# accident. Defense-in-depth. Anything holding in-flight FSM state, network
# clients, asyncio primitives, live observers, or budget state belongs here.
DEFAULT_QUARANTINE: FrozenSet[str] = frozenset({
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.governed_loop_service",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.doubleword_provider",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.urgency_router",
    "backend.core.ouroboros.governance.module_hot_reloader",
    "backend.core.ouroboros.governance.background_agent_pool",
    "backend.core.ouroboros.governance.tool_executor",
    "backend.core.ouroboros.governance.repair_engine",
    "backend.core.ouroboros.governance.auto_committer",
    "backend.core.ouroboros.governance.comm_protocol",
    "backend.core.ouroboros.governance._process_singletons",
    "backend.core.ouroboros.battle_test.harness",
    "backend.core.ouroboros.battle_test.serpent_flow",
    "backend.core.ouroboros.battle_test.live_dashboard",
})

# Probe symbols: a known-stable public attribute per safe module. After
# reload, we look up the symbol again and verify `id(old) != id(new)` to
# prove the attribute was replaced. Catches the case where importlib.reload
# returned without raising but the module dict was not actually swapped.
PROBES: Dict[str, str] = {
    "backend.core.ouroboros.governance.verify_gate": "enforce_verify_thresholds",
    "backend.core.ouroboros.governance.patch_benchmarker": "_filter_python_files",
    "backend.core.ouroboros.governance.semantic_triage": "SemanticTriage",
    "backend.core.ouroboros.governance.plan_generator": "PlanGenerator",
    "backend.core.ouroboros.governance.strategic_direction": "StrategicDirectionService",
}


# Sentinel exit code: when the harness exits with this, the wrapper script
# re-execs the process with the same argv. Distinct from 0 (clean), 1
# (general error), 75 maps to BSD's EX_TEMPFAIL convention.
RESTART_EXIT_CODE = 75


@dataclass(frozen=True)
class ModuleSnapshot:
    """Captured state of a loaded module's source file at a point in time."""

    module_name: str
    file_path: str
    source_sha256: str
    file_size: int
    mtime_ns: int
    captured_at_ns: int


@dataclass(frozen=True)
class ReloadOutcome:
    """Result of attempting to reload a single module."""

    module_name: str
    # "reloaded"      — code was different and reload + verification succeeded
    # "no_change"     — on-disk content matched snapshot, no reload needed
    # "failed"        — reload raised or could not read source
    # "verify_failed" — reload succeeded but post-verify failed (hash or probe)
    status: str
    old_sha: Optional[str]
    new_sha: Optional[str]
    duration_ms: float
    error: Optional[str] = None
    probe_id_changed: Optional[bool] = None


@dataclass(frozen=True)
class ReloadDecision:
    """Routing decision for a target_files batch — pure function, no side effects."""

    # "HOT_RELOAD" — every safe candidate is loaded and eligible
    # "RESTART"    — at least one in-scope module is quarantined or unsafe
    # "NO_OP"      — no in-scope (.py under backend.core.ouroboros) modules
    action: str
    reason: str
    safe_modules: Tuple[str, ...]
    quarantined: Tuple[str, ...]
    out_of_scope: Tuple[str, ...]


@dataclass(frozen=True)
class ReloadBatch:
    """Aggregate result of one reload_for_op call."""

    op_id: str
    started_at_ns: int
    finished_at_ns: int
    decision: ReloadDecision
    outcomes: Tuple[ReloadOutcome, ...]
    # "success"          — at least one module reloaded, all verified
    # "no_change"        — all candidates matched on-disk, nothing reloaded
    # "preflight_failed" — could not snapshot a candidate; nothing reloaded
    # "reload_failed"    — at least one reload raised or failed verification
    # "skipped"          — NO_OP or RESTART decision; nothing reloaded
    overall_status: str
    restart_required: bool
    restart_reason: Optional[str]


class ModuleHotReloader:
    """In-process hot-reloader for safe Ouroboros pure-function modules.

    Thread-safe via internal RLock. Reload is synchronous (no event loop
    yields), so it must only be invoked between operations, not during one.
    The orchestrator calls `reload_for_op` from the post-VERIFY hook.
    """

    def __init__(
        self,
        project_root: Path,
        safe_modules: Optional[FrozenSet[str]] = None,
        quarantine: Optional[FrozenSet[str]] = None,
        event_emitter: Optional[Callable[[dict], None]] = None,
        in_scope_prefix: str = "backend.core.ouroboros.",
    ) -> None:
        self._root = Path(project_root).resolve()
        self._safe = frozenset(
            safe_modules if safe_modules is not None else DEFAULT_SAFE_MODULES
        )
        # Always include __name__ in quarantine — never reload self.
        base_quarantine = quarantine if quarantine is not None else DEFAULT_QUARANTINE
        self._quarantine = frozenset(base_quarantine | {__name__})
        self._emit = event_emitter
        self._in_scope_prefix = in_scope_prefix
        self._lock = threading.RLock()
        self._restart_reason: Optional[str] = None
        self._reload_count: int = 0
        self._last_batch: Optional[ReloadBatch] = None
        # Per-module "imported hash" — the disk hash captured at the moment we
        # first observed the module loaded. As long as the reloader is created
        # before any safe module is mutated on disk, these faithfully represent
        # the bytecode currently live in sys.modules. We refuse to derive
        # change-detection from a fresh disk read because the file may have
        # already been rewritten by O+V's APPLY phase before reload_for_op runs.
        self._loaded_hashes: Dict[str, str] = {}
        self._prime_loaded_hashes()

    @property
    def safe_modules(self) -> FrozenSet[str]:
        return self._safe

    @property
    def quarantine(self) -> FrozenSet[str]:
        return self._quarantine

    @property
    def reload_count(self) -> int:
        return self._reload_count

    @property
    def restart_pending(self) -> Optional[str]:
        """Reason string if a restart has been queued, else None."""
        return self._restart_reason

    @property
    def last_batch(self) -> Optional[ReloadBatch]:
        return self._last_batch

    def queue_restart(self, reason: str) -> None:
        """Mark process for graceful restart. First caller wins."""
        with self._lock:
            if self._restart_reason is None:
                self._restart_reason = reason
                logger.warning("[HotReload] Restart queued: %s", reason)
                self._emit_event({
                    "type": "hot_reload.restart_queued",
                    "ts_ns": time.time_ns(),
                    "reason": reason,
                })

    def clear_restart(self) -> None:
        """Test-only: clear the restart-pending flag."""
        with self._lock:
            self._restart_reason = None

    def _prime_loaded_hashes(self) -> None:
        """Snapshot the on-disk hash of every currently-loaded safe module.

        Called once at construction time. Captures the 'imported hash' for
        each safe module that is already in sys.modules. Modules not yet
        loaded are silently skipped — they'll be picked up the first time
        `reload_for_op` sees them via the lazy-prime fallback in
        `_loaded_hash_for`.
        """
        for mod_name in self._safe:
            if mod_name in sys.modules:
                snap = self.snapshot(mod_name)
                if snap is not None:
                    self._loaded_hashes[mod_name] = snap.source_sha256

    def _loaded_hash_for(self, mod_name: str, fallback: str) -> str:
        """Return the cached imported-hash for a module, lazily seeding it.

        If the module was loaded after construction (and so missed the eager
        prime), seed the cache with the provided fallback hash. The fallback
        is the current disk hash; this is best-effort and means the very
        first reload after such a lazy import cannot detect a change. In
        practice this is fine because deferred imports run during early
        pipeline phases (PLAN/GENERATE) before APPLY mutates the file.
        """
        with self._lock:
            cached = self._loaded_hashes.get(mod_name)
            if cached is None:
                self._loaded_hashes[mod_name] = fallback
                return fallback
            return cached

    def stats(self) -> dict:
        """Lightweight observability snapshot."""
        with self._lock:
            return {
                "reload_count": self._reload_count,
                "restart_pending": self._restart_reason,
                "safe_modules": sorted(self._safe),
                "last_batch_status": (
                    self._last_batch.overall_status if self._last_batch else None
                ),
            }

    def path_to_module(self, rel_path: str) -> Optional[str]:
        """Convert a relative .py path to a fully-qualified module name.

        Returns None if the path doesn't look like an importable Python source
        under the project root, or if it points to a package __init__ file
        (which would require special package-reload handling).
        """
        if not rel_path or not rel_path.endswith(".py"):
            return None
        if rel_path.endswith("__init__.py"):
            return None
        cleaned = rel_path.replace("\\", "/")
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        if cleaned.startswith("/") or ".." in cleaned.split("/"):
            return None
        cleaned = cleaned[:-3]  # strip .py
        return cleaned.replace("/", ".")

    def classify(self, target_files: Iterable[str]) -> ReloadDecision:
        """Map target_files to module names and decide reload action.

        - HOT_RELOAD: every in-scope file maps to a loaded, safe, non-quarantined module
        - RESTART:    at least one in-scope file is quarantined or not in safe set
        - NO_OP:      no in-scope files (no .py under backend.core.ouroboros)
        """
        safe: List[str] = []
        quarantined: List[str] = []
        out_of_scope: List[str] = []

        for f in target_files:
            mod = self.path_to_module(str(f))
            if mod is None:
                out_of_scope.append(str(f))
                continue
            if not mod.startswith(self._in_scope_prefix):
                out_of_scope.append(str(f))
                continue
            if mod in self._quarantine:
                quarantined.append(mod)
                continue
            if mod not in self._safe:
                quarantined.append(mod)
                continue
            if mod not in sys.modules:
                # Not currently loaded — fresh import will get the new code anyway
                out_of_scope.append(mod)
                continue
            safe.append(mod)

        if quarantined:
            return ReloadDecision(
                action="RESTART",
                reason=f"unsafe/quarantined modules: {sorted(set(quarantined))}",
                safe_modules=tuple(sorted(set(safe))),
                quarantined=tuple(sorted(set(quarantined))),
                out_of_scope=tuple(out_of_scope),
            )

        if not safe:
            return ReloadDecision(
                action="NO_OP",
                reason="no in-scope modules to reload",
                safe_modules=(),
                quarantined=(),
                out_of_scope=tuple(out_of_scope),
            )

        return ReloadDecision(
            action="HOT_RELOAD",
            reason=f"all {len(safe)} module(s) in safe set",
            safe_modules=tuple(sorted(set(safe))),
            quarantined=(),
            out_of_scope=tuple(out_of_scope),
        )

    def snapshot(self, module_name: str) -> Optional[ModuleSnapshot]:
        """Capture current source hash + mtime for a loaded module.

        Returns None if the module is not loaded, has no __file__, or its
        source cannot be read.
        """
        mod = sys.modules.get(module_name)
        if mod is None:
            return None
        file_path = getattr(mod, "__file__", None)
        if not file_path:
            return None
        try:
            p = Path(file_path)
            content = p.read_bytes()
            stat = p.stat()
        except OSError as exc:
            logger.warning(
                "[HotReload] Snapshot failed for %s: %s", module_name, exc
            )
            return None
        return ModuleSnapshot(
            module_name=module_name,
            file_path=str(p),
            source_sha256=hashlib.sha256(content).hexdigest(),
            file_size=len(content),
            mtime_ns=stat.st_mtime_ns,
            captured_at_ns=time.time_ns(),
        )

    def reload_for_op(
        self,
        op_id: str,
        target_files: Iterable[str],
    ) -> ReloadBatch:
        """Main entry point: classify → preflight → reload → verify → emit.

        Always returns a ReloadBatch — never raises. Restart-required outcomes
        set both `batch.restart_required=True` and queue the restart reason
        on the reloader instance for the harness to observe.
        """
        with self._lock:
            return self._reload_locked(op_id, list(target_files))

    def _reload_locked(self, op_id: str, target_files: List[str]) -> ReloadBatch:
        started = time.time_ns()
        decision = self.classify(target_files)

        if decision.action == "NO_OP":
            return self._finalize_batch(
                op_id, started, decision, (),
                overall_status="skipped",
                restart_required=False,
                restart_reason=None,
            )

        if decision.action == "RESTART":
            self.queue_restart(decision.reason)
            return self._finalize_batch(
                op_id, started, decision, (),
                overall_status="skipped",
                restart_required=True,
                restart_reason=decision.reason,
            )

        # HOT_RELOAD path
        # PREFLIGHT: snapshot every candidate and capture probe ids. If any
        # candidate cannot be snapshotted, abort the entire batch — atomicity
        # over partial progress.
        #
        # NOTE on the snapshot's source_sha256: snapshot() reads disk, but the
        # disk may already reflect APPLY's mutation. The TRUE "before" hash is
        # the imported-hash captured at reloader construction (or lazy-seeded
        # on first observation). We rebuild each snapshot with the cached
        # imported-hash so change detection compares in-memory vs disk, not
        # disk vs disk.
        before_snapshots: Dict[str, ModuleSnapshot] = {}
        before_probe_ids: Dict[str, Optional[int]] = {}
        for mod in decision.safe_modules:
            snap = self.snapshot(mod)
            if snap is None:
                reason = f"preflight: cannot snapshot {mod}"
                self.queue_restart(reason)
                return self._finalize_batch(
                    op_id, started, decision, (),
                    overall_status="preflight_failed",
                    restart_required=True,
                    restart_reason=reason,
                )
            imported_sha = self._loaded_hash_for(mod, snap.source_sha256)
            if imported_sha != snap.source_sha256:
                snap = replace(snap, source_sha256=imported_sha)
            before_snapshots[mod] = snap
            before_probe_ids[mod] = self._probe_id(mod)

        # Force importlib to re-read finder caches before reload, defending
        # against bytecode-cache staleness.
        importlib.invalidate_caches()

        outcomes: List[ReloadOutcome] = []
        any_failed = False
        for mod in decision.safe_modules:
            outcome = self._reload_one(
                mod, before_snapshots[mod], before_probe_ids[mod]
            )
            outcomes.append(outcome)
            if outcome.status in ("failed", "verify_failed"):
                any_failed = True
            elif outcome.status == "reloaded" and outcome.new_sha:
                # Promote the new disk hash to the imported-hash cache so the
                # next reload_for_op compares against the freshly-loaded code,
                # not the previous generation.
                self._loaded_hashes[mod] = outcome.new_sha
            elif outcome.status == "no_change" and outcome.new_sha:
                # Keep cache aligned with disk for no-op rounds too.
                self._loaded_hashes[mod] = outcome.new_sha

        if any_failed:
            failed_mods = [
                o.module_name for o in outcomes
                if o.status in ("failed", "verify_failed")
            ]
            reason = f"hot-reload partial failure on {failed_mods}"
            self.queue_restart(reason)
            return self._finalize_batch(
                op_id, started, decision, tuple(outcomes),
                overall_status="reload_failed",
                restart_required=True,
                restart_reason=reason,
            )

        if all(o.status == "no_change" for o in outcomes):
            return self._finalize_batch(
                op_id, started, decision, tuple(outcomes),
                overall_status="no_change",
                restart_required=False,
                restart_reason=None,
            )

        self._reload_count += 1
        return self._finalize_batch(
            op_id, started, decision, tuple(outcomes),
            overall_status="success",
            restart_required=False,
            restart_reason=None,
        )

    def _reload_one(
        self,
        mod_name: str,
        before: ModuleSnapshot,
        before_probe_id: Optional[int],
    ) -> ReloadOutcome:
        t0 = time.perf_counter()

        try:
            disk_content = Path(before.file_path).read_bytes()
        except OSError as exc:
            return ReloadOutcome(
                module_name=mod_name,
                status="failed",
                old_sha=before.source_sha256,
                new_sha=None,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                error=f"read disk: {exc}",
            )
        disk_sha = hashlib.sha256(disk_content).hexdigest()

        if disk_sha == before.source_sha256:
            return ReloadOutcome(
                module_name=mod_name,
                status="no_change",
                old_sha=before.source_sha256,
                new_sha=disk_sha,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )

        mod = sys.modules.get(mod_name)
        if mod is None:
            return ReloadOutcome(
                module_name=mod_name,
                status="failed",
                old_sha=before.source_sha256,
                new_sha=disk_sha,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                error="module no longer in sys.modules",
            )

        try:
            importlib.reload(mod)
        except Exception as exc:
            logger.error(
                "[HotReload] importlib.reload(%s) raised: %s",
                mod_name, exc, exc_info=True,
            )
            return ReloadOutcome(
                module_name=mod_name,
                status="failed",
                old_sha=before.source_sha256,
                new_sha=disk_sha,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                error=f"reload: {type(exc).__name__}: {exc}",
            )

        # POST-VERIFY: re-snapshot and confirm the in-process module's source
        # file hashes match what we just read from disk. Catches the case
        # where the file was further mutated after the read but before reload.
        after = self.snapshot(mod_name)
        if after is None:
            return ReloadOutcome(
                module_name=mod_name,
                status="verify_failed",
                old_sha=before.source_sha256,
                new_sha=None,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                error="post-reload snapshot failed",
            )
        if after.source_sha256 != disk_sha:
            return ReloadOutcome(
                module_name=mod_name,
                status="verify_failed",
                old_sha=before.source_sha256,
                new_sha=after.source_sha256,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                error="post-reload hash drift (file mutated mid-reload)",
            )

        # FUNCTION IDENTITY PROBE: prove the module dict was actually swapped.
        probe_changed: Optional[bool] = None
        after_probe_id = self._probe_id(mod_name)
        if before_probe_id is not None and after_probe_id is not None:
            probe_changed = before_probe_id != after_probe_id
            if not probe_changed:
                return ReloadOutcome(
                    module_name=mod_name,
                    status="verify_failed",
                    old_sha=before.source_sha256,
                    new_sha=disk_sha,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    error=f"probe identity unchanged: {PROBES.get(mod_name)}",
                    probe_id_changed=False,
                )

        return ReloadOutcome(
            module_name=mod_name,
            status="reloaded",
            old_sha=before.source_sha256,
            new_sha=disk_sha,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            probe_id_changed=probe_changed,
        )

    def _probe_id(self, module_name: str) -> Optional[int]:
        probe_attr = PROBES.get(module_name)
        if probe_attr is None:
            return None
        mod = sys.modules.get(module_name)
        if mod is None:
            return None
        sym = getattr(mod, probe_attr, None)
        if sym is None:
            return None
        return id(sym)

    def _finalize_batch(
        self,
        op_id: str,
        started: int,
        decision: ReloadDecision,
        outcomes: Tuple[ReloadOutcome, ...],
        overall_status: str,
        restart_required: bool,
        restart_reason: Optional[str],
    ) -> ReloadBatch:
        batch = ReloadBatch(
            op_id=op_id,
            started_at_ns=started,
            finished_at_ns=time.time_ns(),
            decision=decision,
            outcomes=outcomes,
            overall_status=overall_status,
            restart_required=restart_required,
            restart_reason=restart_reason,
        )
        self._last_batch = batch
        self._emit_batch(batch)
        return batch

    def _emit_event(self, event: dict) -> None:
        if self._emit is None:
            return
        try:
            self._emit(event)
        except Exception as exc:
            logger.warning("[HotReload] event emitter raised: %s", exc)

    def _emit_batch(self, batch: ReloadBatch) -> None:
        if self._emit is None:
            return
        event = {
            "type": "hot_reload.batch",
            "op_id": batch.op_id,
            "started_at_ns": batch.started_at_ns,
            "finished_at_ns": batch.finished_at_ns,
            "duration_ms": (batch.finished_at_ns - batch.started_at_ns) / 1_000_000,
            "overall_status": batch.overall_status,
            "restart_required": batch.restart_required,
            "restart_reason": batch.restart_reason,
            "decision": {
                "action": batch.decision.action,
                "reason": batch.decision.reason,
                "safe_modules": list(batch.decision.safe_modules),
                "quarantined": list(batch.decision.quarantined),
                "out_of_scope": list(batch.decision.out_of_scope),
            },
            "outcomes": [
                {
                    "module": o.module_name,
                    "status": o.status,
                    "old_sha": o.old_sha[:12] if o.old_sha else None,
                    "new_sha": o.new_sha[:12] if o.new_sha else None,
                    "duration_ms": round(o.duration_ms, 2),
                    "error": o.error,
                    "probe_id_changed": o.probe_id_changed,
                }
                for o in batch.outcomes
            ],
        }
        self._emit_event(event)
