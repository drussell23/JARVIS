"""isomorphic_a1_local.py -- Full-chain local A1 E2E driver (Task 6).

Composes the Isomorphic Local Sandbox Tasks 1-5 into a single runnable proof:

  T1  IsomorphicEnv             -- path/env/policy parity with the GCP soak node
  T2  repo_root injection fix   -- already in GovernedLoopConfig (no new code)
  T3  SyntheticAdversary        -- deterministic provider chaos via env-URL swap
  T4  failover trigger fix      -- already in candidate_generator (no new code)
  T5  capture_failure_telemetry -- fail-soft FSM/memory/causal dump on any failure

Run-#12 fix (post-boot chaos injection)
---------------------------------------
OLD: inject -> boot soak -> detect  [TestWatcher ran full pytest tests/]
NEW: boot soak -> [READY] -> inject -> touch(chaos_file) -> detect [scoped pytest]

The pre-soak injection was why chaos was never detected: the TestWatcher was cold
(not yet subscribed to fs.changed.*) when the mutation landed. By injecting AFTER
boot and then touching the mutated file to fire fs.changed.modified, the
TestFailureSensor picks up exactly that file and runs the scoped pytest target
(e.g. tests/core/test_foo.py) instead of the full tests/ suite.

Run-#13 fix (intervention-lock lineage scoping)
-----------------------------------------------
Already complete in a1_graduation_auditor.py -- CONFIRMED PRE-EXISTING.
An unrelated APPROVAL_REQUIRED op (e.g. OpportunityMiner hitting the Immutable
Orange guard) does NOT trip the Absolute Intervention-Lock; only a human-gate
on an op in the chaos-repair causal subtree does.  Verified by the new test
suite in tests/integration/test_isomorphic_a1_e2e.py.

Usage::

    python3 scripts/isomorphic_a1_local.py --stub-soak              # wiring proof
    python3 scripts/isomorphic_a1_local.py --stub-soak --mode container
    python3 scripts/isomorphic_a1_local.py                           # live soak
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths -- no hardcoding; always derived from this file's location
# ---------------------------------------------------------------------------
_SCRIPTS_DIR: str = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT: str = os.path.dirname(_SCRIPTS_DIR)

_HARNESS_SCRIPT: str = os.path.join(_SCRIPTS_DIR, "a1_live_fire_chaos_harness.py")
_AUDITOR_SCRIPT: str = os.path.join(_SCRIPTS_DIR, "a1_graduation_auditor.py")
_ADVERSARY_SCRIPT: str = os.path.join(_SCRIPTS_DIR, "synthetic_adversary.py")

# Marker emitted by TestWatcher when it has successfully subscribed to
# fs.changed.* on the TrinityEventBus.  Used by _await_soak_boot().
_TESTWATCHER_READY_MARKER: str = "[TestWatcher] READY subscribed=fs.changed.*"


# ---------------------------------------------------------------------------
# Lazy module loaders (same pattern as a1_live_fire_chaos_harness.py)
# ---------------------------------------------------------------------------

def _load_module(name: str, path: str) -> Any:
    """Load a script-module by path; return the cached version if already loaded.

    Uses a cache-first strategy so every caller (driver + tests) gets the SAME
    object -- essential for ``patch.object`` to work across call sites.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ImportError("Cannot load %s from %s" % (name, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # store BEFORE exec to handle circular refs
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _harness() -> Any:
    """Return the a1_live_fire_chaos_harness module (lazy-cached)."""
    return sys.modules.get("a1_live_fire_chaos_harness") or _load_module(
        "a1_live_fire_chaos_harness", _HARNESS_SCRIPT
    )


def _auditor() -> Any:
    """Return the a1_graduation_auditor module (lazy-cached)."""
    return sys.modules.get("a1_graduation_auditor") or _load_module(
        "a1_graduation_auditor", _AUDITOR_SCRIPT
    )


def _adversary_mod() -> Any:
    """Return the synthetic_adversary module (lazy-cached)."""
    return sys.modules.get("synthetic_adversary") or _load_module(
        "synthetic_adversary", _ADVERSARY_SCRIPT
    )


def _ensure_backend_on_path() -> None:
    """Add repo root and backend dir to sys.path so absolute backend imports work
    regardless of the process cwd (IsomorphicEnv changes cwd to <tmpdir>/app)."""
    for entry in (_REPO_ROOT, os.path.join(_REPO_ROOT, "backend")):
        if entry not in sys.path:
            sys.path.insert(0, entry)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print("[IsoA1] %s" % (msg,), flush=True)


def _truthy(val: Optional[str]) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Signal handler -- chaos-revert-always on SIGINT / SIGTERM
# ---------------------------------------------------------------------------

_ACTIVE_CHAOS: List[Any] = []


def _install_revert_signal_handlers() -> None:
    """Install SIGINT/SIGTERM handlers that revert any active chaos before exit.
    The repo must NEVER be left broken on any exit path."""
    def _handler(signum: int, _frame: Any) -> None:
        _log("signal %d received -- reverting chaos before exit" % (signum,))
        for chaos in list(_ACTIVE_CHAOS):
            try:
                chaos.revert()
            except Exception:  # noqa: BLE001
                pass
        try:
            signal.signal(signum, signal.SIG_DFL)
        except Exception:  # noqa: BLE001
            pass
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # not in main thread (e.g. pytest workers) -- skip silently


# ---------------------------------------------------------------------------
# Run-#12 helpers: touch chaos files + derive scoped test targets
# ---------------------------------------------------------------------------

def _touch_chaos_files(chaos_files: List[str], repo_root: str) -> List[str]:
    """Touch each chaos target file to update its mtime (run-#12 fix).

    In a live O+V soak the FileSystemEventBridge watches the filesystem; a
    mtime change fires ``fs.changed.modified`` on the TrinityEventBus, which
    wakes the TestFailureSensor's dynamic subscription and triggers a SCOPED
    pytest run (just the affected test file) instead of the full ``tests/``
    suite.

    In a stub/dry-run soak the touch is still performed: it proves the
    sequencing logic is correct and leaves an auditable mtime trail.

    Returns the list of absolute paths that were successfully touched.
    """
    touched: List[str] = []
    for cf in chaos_files:
        abs_cf = cf if os.path.isabs(cf) else os.path.join(repo_root, cf)
        if not os.path.exists(abs_cf):
            _log("touch skip (not found): %s" % abs_cf)
            continue
        try:
            Path(abs_cf).touch()
            touched.append(abs_cf)
            _log("run-#12 fix: touched %s (fires fs.changed.modified)" % abs_cf)
        except OSError as exc:
            _log("touch warning %s: %r" % (abs_cf, exc))
    return touched


def _derive_scoped_test_targets(chaos_files: List[str], repo_root: str) -> List[str]:
    """Heuristic derivation of scoped pytest targets from chaos source files.

    The authoritative implementation is ``TestFailureSensor._resolve_scoped_targets``
    (async, requires a live sensor context).  This local approximation proves the
    "scoped, not full-suite" invariant without booting the organism.

    Returns a sorted, de-duped list of matching test file paths.  An EMPTY list
    means no scoped targets were found locally -- this is NOT a fallback to
    ``tests/``.  The driver NEVER expands to the full test suite.
    """
    tests_root = Path(repo_root) / "tests"
    targets: List[str] = []
    for cf in chaos_files:
        stem = Path(cf).stem
        # Pattern 1: test_<stem>.py anywhere under tests/
        for tf in tests_root.rglob("test_%s.py" % stem):
            targets.append(str(tf))
        # Pattern 2: <stem>_test.py anywhere under tests/
        for tf in tests_root.rglob("%s_test.py" % stem):
            targets.append(str(tf))
    return sorted(set(targets))


def _await_soak_boot(
    proc: Any,
    debug_log: str,
    timeout_s: float = 60.0,
) -> bool:
    """Poll the soak's debug.log for the TestWatcher READY marker.

    Returns True when the marker is found within *timeout_s*, False on timeout
    or premature process exit.  Stub-soak callers pass ``proc=None`` and this
    returns immediately (no real boot to await).
    """
    if proc is None:
        return True  # stub soak -- no real O+V boot
    deadline = time.monotonic() + timeout_s
    seen_lines: int = 0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _log("soak exited prematurely (rc=%d)" % proc.poll())
            return False
        try:
            with open(debug_log, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    seen_lines += 1
                    if _TESTWATCHER_READY_MARKER in line:
                        _log("soak boot READY (%d lines scanned)" % seen_lines)
                        return True
        except OSError:
            pass
        time.sleep(0.5)
    _log("soak boot TIMEOUT after %.0fs (%d lines scanned)" % (timeout_s, seen_lines))
    return False  # timeout -- proceed anyway; sensor may still start


# ---------------------------------------------------------------------------
# Adversary fault scheduling
# ---------------------------------------------------------------------------

def _schedule_adversary_fault(
    adversary: Any, adv_mod: Any, fault: str
) -> None:
    """Schedule a deterministic DW provider fault via the SyntheticAdversary.

    *fault* is one of: ``http5xx`` | ``transport`` | ``timeout`` | ``parse_error``.
    Silently skips if FailureSource cannot be imported (dev boxes lacking topology deps).
    """
    if not fault or fault == "none":
        return
    fault_map: Dict[str, str] = {
        "http5xx": "live_http_5xx",
        "transport": "live_transport",
        "timeout": "live_stream_stall",
        "parse_error": "live_parse_error",
        "http429": "live_http_429",
    }
    fault_value = fault_map.get(fault, fault)
    try:
        # FailureSource lives in topology_sentinel; the adversary module
        # already re-exports it (or sets it to None on import failure).
        fs_cls = getattr(adv_mod, "FailureSource", None)
        if fs_cls is None:
            _log("adversary fault skipped (FailureSource unavailable): %s" % fault)
            return
        # Look up by value (the string stored in the enum).
        matching = [e for e in fs_cls if e.value == fault_value]
        if not matching:
            _log("adversary fault %r not in FailureSource enum -- skipping" % fault_value)
            return
        adversary.schedule(
            route="doubleword",
            endpoint="/chat/completions",
            fault=matching[0],
            count=None,
        )
        _log("adversary fault scheduled: %s" % matching[0])
    except Exception as exc:  # noqa: BLE001
        _log("adversary fault schedule warning: %r" % (exc,))


# ---------------------------------------------------------------------------
# IsomorphicA1Driver -- the full-chain local E2E driver
# ---------------------------------------------------------------------------

class IsomorphicA1Driver:
    """Full-chain local A1 E2E driver under IsomorphicEnv + SyntheticAdversary.

    Key differences from ``HarnessRun.execute()``:

    1. **Runs inside IsomorphicEnv** (T1): forces live ``/opt/trinity/jarvis``
       path + cwd mismatch + restricted sandbox prefix policy.
    2. **SyntheticAdversary** (T3): env-URL swap replaces real DW/Prime URLs
       with a localhost proxy that serves deterministic failure responses.
    3. **Post-boot chaos injection** (run-#12 fix): the soak is BOOTED first;
       only after the TestWatcher logs its READY marker does the driver inject
       chaos and touch the mutated file to fire ``fs.changed.modified``.
    4. **capture_failure_telemetry** (T5): called on any non-proven verdict.
    """

    def __init__(
        self,
        *,
        repo_root: Optional[str] = None,
        mode: str = "process",
        seed: int = 0,
        stub_soak: bool = True,
        strict: bool = True,
        sse_base: str = "http://127.0.0.1:7778",
        run_root: Optional[str] = None,
        adversary_fault: Optional[str] = None,
        verbose: bool = False,
        # Injection seam for tests: a zero-arg callable that returns an adversary
        # instance.  None -> use the real SyntheticAdversary from adversary_mod.
        _adversary_factory: Optional[Any] = None,
    ) -> None:
        self.repo_root: str = repo_root or _REPO_ROOT
        self.mode: str = mode
        self.seed: int = seed
        self.stub_soak: bool = stub_soak
        self.strict: bool = strict
        self.sse_base: str = sse_base
        self.run_root: str = run_root or os.path.join(os.getcwd(), "a1_iso_runs")
        self.adversary_fault: Optional[str] = adversary_fault
        self.verbose: bool = verbose
        self._adversary_factory: Optional[Any] = _adversary_factory

    async def run(self) -> int:
        """Execute the full chain.  Returns 0 iff A1_DISPATCH_PROVEN."""
        _ensure_backend_on_path()

        harness_mod = _harness()
        auditor_mod = _auditor()
        adv_mod = _adversary_mod()

        # T1: IsomorphicEnv + T5: capture_failure_telemetry (imported inside
        # run() so the lazy sys.path extension is in effect before import).
        from backend.core.ouroboros.battle_test.isomorphic_env import IsomorphicEnv
        from backend.core.ouroboros.battle_test.failure_telemetry import (
            capture_failure_telemetry,
        )

        run_id = time.strftime("iso-a1-%Y%m%d-%H%M%S")
        run_dir = os.path.join(self.run_root, run_id)
        os.makedirs(run_dir, exist_ok=True)

        _log("run_id=%s mode=%s stub_soak=%s seed=%d" % (
            run_id, self.mode, self.stub_soak, self.seed))

        # T3: SyntheticAdversary -- start BEFORE IsomorphicEnv so the server
        # binds on the host-network port that the soak env will point at.
        if self._adversary_factory is not None:
            adversary = self._adversary_factory()
        else:
            adversary = adv_mod.SyntheticAdversary()
        if self.adversary_fault:
            _schedule_adversary_fault(adversary, adv_mod, self.adversary_fault)
        adversary_urls: Dict[str, str] = await adversary.start()
        _log("adversary started: dw=%s prime=%s" % (
            adversary_urls.get("doubleword", "?"), adversary_urls.get("prime", "?")))

        verdict: Dict[str, Any] = {"proven": False, "failure_locus": "not_run"}
        injected: bool = False
        chaos: Any = None

        try:
            # T1: enter the isomorphic process/container environment.
            with IsomorphicEnv(Path(self.repo_root), mode=self.mode) as env_ctx:
                _log("IsomorphicEnv: root=%s cwd=%s" % (env_ctx.root, os.getcwd()))

                # Compose env: node vars (from IsomorphicEnv via os.environ) +
                # cognitive flags ON (from CADENCE_POLICY) + adversary overrides.
                env: Dict[str, str] = harness_mod.compose_env()
                env.update(adversary.env_overrides())
                _log("env composed: %d keys total, adversary overrides applied" % len(env))

                chaos = harness_mod.ChaosController(
                    repo_root=self.repo_root,
                    test_timeout_s=60.0,
                )
                _ACTIVE_CHAOS.append(chaos)

                try:
                    # ── a. PREFLIGHT ─────────────────────────────────────────
                    _log("STEP preflight: chaos status")
                    st = chaos.status()
                    if st.get("active"):
                        _log("ABORT: active chaos manifest already exists "
                             "(run --revert first)")
                        verdict = {"proven": False,
                                   "failure_locus": "preflight:active_manifest"}
                        return 1

                    # ── b. BOOT SOAK FIRST (run-#12 fix) ─────────────────────
                    #
                    # This is the critical ordering change: the O+V organism is
                    # booted BEFORE chaos is injected.  The TestFailureSensor
                    # subscribes to fs.changed.* during boot; only then does the
                    # injection + touch trigger a scoped pytest run (not the full
                    # tests/ suite).
                    verdict_out = os.path.join(run_dir, "a1_verdict.json")
                    soak_proc: Any = None
                    debug_log: str = ""

                    if self.stub_soak:
                        _log("STEP soak: STUB -- post-boot chaos sequencing (run-#12)")
                        debug_log = os.path.join(run_dir, "stub_debug.log")
                        harness_mod.write_stub_soak_log(
                            debug_log, goal_id="GOAL-ISO-A1")
                        soak_proc = None
                    else:
                        _log("STEP soak: launching production O+V (pre-inject boot)")
                        soak_runner = harness_mod.SoakRunner(
                            repo_root=self.repo_root,
                            cost_cap=0.0,
                            wall_seconds=300,
                        )
                        handle = soak_runner.launch(env, run_dir)
                        debug_log = handle.debug_log
                        soak_proc = handle.proc
                        _log("STEP await boot READY (TestWatcher fs.changed.* sub)")
                        _await_soak_boot(soak_proc, debug_log, timeout_s=90.0)

                    _log("STEP soak boot OK: debug_log=%s" % debug_log)

                    # ── c. INJECT CHAOS POST-BOOT (run-#12 fix) ───────────────
                    _log("STEP inject POST-BOOT: seed=%d" % self.seed)
                    red = chaos.inject(self.seed)
                    injected = True

                    if not red and not self.stub_soak:
                        _log("ABORT: test did not go RED post-injection")
                        verdict = {"proven": False, "failure_locus": "inject:not_red"}
                        return 1
                    _log("STEP inject OK (red=%s stub=%s)" % (red, self.stub_soak))

                    # ── d. TOUCH chaos files → fire fs.changed.* (run-#12) ────
                    manifest_path = os.path.join(
                        self.repo_root, ".jarvis", "chaos_manifest.json")
                    chaos_files = auditor_mod.load_chaos_target_files(manifest_path)
                    if chaos_files:
                        touched = _touch_chaos_files(chaos_files, self.repo_root)
                        scoped = _derive_scoped_test_targets(
                            chaos_files, self.repo_root)
                        _log("run-#12: %d file(s) touched; scoped pytest: %s"
                             % (len(touched),
                                scoped[0] if scoped else "<none found locally>"))
                    else:
                        _log("run-#12: no chaos files in manifest (stub mode?)")

                    # ── e. LAUNCH AUDITOR ────────────────────────────────────
                    _log("STEP audit: sse=%s log=%s" % (self.sse_base, debug_log))
                    if self.stub_soak:
                        aud_runner = harness_mod.StubAuditorRunner(
                            strict=self.strict, goal_id="GOAL-ISO-A1")
                    else:
                        aud_runner = harness_mod.AuditorRunner(strict=self.strict)

                    verdict = aud_runner.watch(
                        base=self.sse_base,
                        log_file=debug_log,
                        timeout_s=120.0,
                        verdict_out=verdict_out,
                    )

                    proven = bool(verdict.get("proven"))
                    _log("STEP audit VERDICT: %s"
                         % ("A1_DISPATCH_PROVEN" if proven else "FAILED"))

                    # ── f. FAILURE PATH: T5 telemetry + local autopsy ────────
                    if not proven:
                        _log("STEP telemetry: capturing failure artifacts (T5)")
                        try:
                            capture_failure_telemetry(
                                output_dir=Path(run_dir) / "telemetry",
                                reason="a1_iso_not_proven:%s"
                                % verdict.get("failure_locus", ""),
                            )
                        except Exception as exc:  # noqa: BLE001
                            _log("telemetry warning: %r" % (exc,))
                        try:
                            harness_mod.local_autopsy(
                                run_id=run_id,
                                autopsy_root=os.path.join(
                                    self.run_root, "autopsy"),
                                debug_log=debug_log,
                                verdict=verdict,
                                chaos_manifest=manifest_path,
                            )
                        except Exception as exc:  # noqa: BLE001
                            _log("autopsy warning: %r" % (exc,))

                    _log("run complete: %s -> %s"
                         % (run_id, "PROVEN" if proven else "FAILED"))
                    return 0 if proven else 1

                except Exception as exc:  # noqa: BLE001
                    _log("orchestration error: %r" % (exc,))
                    verdict = {
                        "proven": False,
                        "failure_locus": "orchestration_error:%s" % type(exc).__name__,
                    }
                    try:
                        capture_failure_telemetry(
                            output_dir=Path(run_dir) / "telemetry",
                            reason="orchestration_error:%s" % type(exc).__name__,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return 1

                finally:
                    # CHAOS-REVERT-ALWAYS: repo must never be left broken.
                    if injected and chaos is not None:
                        _log("STEP revert: restoring chaos (always)")
                        try:
                            chaos.revert()
                        except Exception as exc:  # noqa: BLE001
                            _log("revert warning: %r" % (exc,))
                    if chaos in _ACTIVE_CHAOS:
                        _ACTIVE_CHAOS.remove(chaos)

        finally:
            try:
                await adversary.stop()
                _log("adversary stopped")
            except Exception as exc:  # noqa: BLE001
                _log("adversary stop warning: %r" % (exc,))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="isomorphic_a1_local.py",
        description=(
            "Isomorphic A1 Local E2E driver (Task 6). "
            "Full O+V A1 chain under GCP-node-identical conditions for $0. "
            "Post-boot chaos injection fires fs.changed dynamically (run-#12 fix). "
            "Lineage-scoped intervention lock (run-#13 fix) is in the auditor."
        ),
    )
    p.add_argument(
        "--mode", choices=["process", "container"], default="process",
        help="Isomorphic env mode: process (symlink + env-patch, default) or "
             "container (docker run --network none).",
    )
    p.add_argument(
        "--stub-soak", action="store_true",
        help="Stub soak: write a synthetic debug.log (no real O+V process, $0). "
             "Proves the wiring without a live soak.",
    )
    p.add_argument(
        "--seed", type=int,
        default=int(os.environ.get("JARVIS_A1_CHAOS_SEED", "0")),
        help="Chaos injector seed for deterministic target selection.",
    )
    p.add_argument(
        "--base",
        default=os.environ.get("JARVIS_A1_SSE_BASE", "http://127.0.0.1:7778"),
        help="SSE observability base URL.",
    )
    p.add_argument(
        "--strict", action="store_true", default=True,
        help="Strict auditor mode (UNVERIFIABLE -> FAIL; default).",
    )
    p.add_argument(
        "--lenient", action="store_false", dest="strict",
        help="Lenient auditor mode (UNVERIFIABLE -> WARN).",
    )
    p.add_argument("--run-root", default=None,
                   help="Root directory for run artifacts (default: ./a1_iso_runs).")
    p.add_argument(
        "--adversary-fault",
        default=os.environ.get("JARVIS_ISO_ADVERSARY_FAULT", "none"),
        choices=["none", "http5xx", "transport", "timeout", "parse_error", "http429"],
        help="Deterministic fault to inject into the DW provider via the "
             "SyntheticAdversary (default: none = transparent passthrough).",
    )
    p.add_argument("--verbose", action="store_true", help="Verbose output.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _install_revert_signal_handlers()
    driver = IsomorphicA1Driver(
        mode=args.mode,
        seed=args.seed,
        stub_soak=args.stub_soak,
        strict=args.strict,
        sse_base=args.base,
        run_root=args.run_root,
        adversary_fault=(
            args.adversary_fault if args.adversary_fault != "none" else None
        ),
        verbose=args.verbose,
    )
    return asyncio.run(driver.run())


if __name__ == "__main__":
    sys.exit(main())
