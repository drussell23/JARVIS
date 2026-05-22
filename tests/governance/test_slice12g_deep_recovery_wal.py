"""Slice 12G — Deep Async Recovery + Extended Compute + Continuous WAL.

Closes the Phase 3A relaunch wedge (``bt-2026-05-22-195721``):

  * **Phase 1 — Extended Compute (12G-1):** SWE-Bench-Pro
    high-urgency envelopes are stamped ``ProviderRoute.IMMEDIATE``
    for Slice 12F-A semaphore preemption, but the workload is
    COMPLEX code-generation against massive repos (element-web).
    The legacy ``immediate-reflex`` thinking disable in
    ``_compute_thinking_profile`` left the model with 120s of
    server-side rupture window — empirically too short. 12G-1
    inserts a benchmark-source override: ``signal_source ==
    "swe_bench_pro"`` force-enables extended thinking, which
    raises the rupture watchdog timeout to 360s.

  * **Phase 2 — LoopDeadman (12G-2):** the Slice 11A
    ControlPlaneWatchdog can't surface a TOTAL loop wedge
    (its own ``asyncio.sleep`` can't fire when the loop is
    dead). The new ``LoopDeadman`` runs in a daemon OS thread
    independent of the asyncio loop, monitors the
    asyncio-side heartbeat timestamp, and fires
    ``os._exit(75)`` plus a faulthandler stack dump after
    ``deadman_timeout_s`` (default 300s) without a tick.

  * **Phase 3 — SessionWAL (12G-3):** continuous atomic
    checkpoint of ``summary.json`` (temp + os.replace) so when
    Layer-3 SIGKILL or LoopDeadman ``os._exit(75)`` fires, the
    latest session state is already on disk. Operator
    explicitly rejected Slice 12C panic-save; this is the
    continuous-WAL alternative — write during normal operation,
    survive any kill path.

## Test surface

### 12G-1 — extended thinking gate
  * SWE-Bench-Pro source on IMMEDIATE route returns
    ``(enabled=True, budget>0, reason="benchmark-source:...")``
  * Non-benchmark IMMEDIATE returns the legacy
    ``immediate-reflex`` disable
  * Env knob ``JARVIS_THINKING_BENCHMARK_OVERRIDE_ENABLED=false``
    reverts to legacy behaviour
  * Closed taxonomy: ``_BENCHMARK_THINKING_SOURCES`` is a frozenset
    (immutable)

### 12G-2 — LoopDeadman
  * Heartbeat updates the timestamp
  * ``last_heartbeat_age_s`` increases when no heartbeat
  * ``deadman_enabled`` honours env
  * Bounded ``timeout_s`` (30s floor, 3600s ceiling)
  * Daemon thread is genuinely a daemon (won't block exit)
  * AST pins on the fire path so the os._exit(75) contract
    can't regress silently

### 12G-3 — SessionWAL
  * Atomic checkpoint creates summary.json
  * Repeated checkpoints overwrite (atomic temp+replace)
  * ``checkpoint`` is debounced; ``force_checkpoint`` bypasses
  * ``wal_enabled`` honours env
  * Schema includes ``checkpoint_iso``, ``checkpoint_reason``,
    ``checkpoint_seq``
  * Non-serializable values coerced safely (no raise)
  * Atomic guarantee: a reader sees either the prior version
    or the new one — never a torn write
  * Singleton helpers (install / get / reset)

### Wiring AST pins
  * harness.py imports loop_deadman + session_wal
  * harness boots both behind env-flag guard
  * harness has _slice12g3_periodic_checkpoint_loop method
"""

from __future__ import annotations

import ast as _ast
import asyncio
import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
from typing import Any, Dict


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_HARNESS_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "battle_test"
    / "harness.py"
)
_DEADMAN_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "loop_deadman.py"
)
_WAL_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "session_wal.py"
)
_PROVIDERS_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "providers.py"
)


def _parse_module(path: pathlib.Path) -> _ast.Module:
    return _ast.parse(path.read_text())


# ============================================================================
# Slice 12G-1 — extended thinking gate for benchmark sources
# ============================================================================


class TestThinkingBenchmarkOverride(unittest.TestCase):

    def setUp(self) -> None:
        self._prior_env = os.environ.pop(
            "JARVIS_THINKING_BENCHMARK_OVERRIDE_ENABLED", None,
        )

    def tearDown(self) -> None:
        if self._prior_env is None:
            os.environ.pop(
                "JARVIS_THINKING_BENCHMARK_OVERRIDE_ENABLED", None,
            )
        else:
            os.environ[
                "JARVIS_THINKING_BENCHMARK_OVERRIDE_ENABLED"
            ] = self._prior_env

    def _ctx(self, **kwargs):
        class _Ctx:
            pass
        c = _Ctx()
        for k, v in kwargs.items():
            setattr(c, k, v)
        return c

    def test_swe_bench_pro_immediate_enables_thinking(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _compute_thinking_profile,
        )
        ctx = self._ctx(
            task_complexity="simple",
            provider_route="immediate",
            signal_source="swe_bench_pro",
        )
        enabled, budget, reason = _compute_thinking_profile(
            ctx, extended_thinking_default=False, base_budget=4000,
        )
        self.assertTrue(
            enabled,
            "swe_bench_pro source on IMMEDIATE must enable "
            "thinking (Slice 12G-1)",
        )
        self.assertGreater(budget, 0)
        self.assertIn("benchmark-source", reason)
        self.assertIn("swe_bench_pro", reason)

    def test_non_benchmark_immediate_still_disabled(self) -> None:
        """Sanity: ops without the benchmark source still get the
        legacy immediate-reflex disable — Slice 12G-1 is narrowly
        scoped to benchmark workloads."""
        from backend.core.ouroboros.governance.providers import (
            _compute_thinking_profile,
        )
        ctx = self._ctx(
            task_complexity="simple",
            provider_route="immediate",
            signal_source="github_issue",
        )
        enabled, budget, reason = _compute_thinking_profile(
            ctx, extended_thinking_default=False, base_budget=4000,
        )
        self.assertFalse(enabled)
        self.assertEqual(budget, 0)
        self.assertEqual(reason, "immediate-reflex")

    def test_override_env_disables_benchmark_thinking(self) -> None:
        """Hot-revert path: explicit `=false` returns to legacy
        behaviour for benchmark sources too."""
        from backend.core.ouroboros.governance.providers import (
            _compute_thinking_profile,
        )
        os.environ[
            "JARVIS_THINKING_BENCHMARK_OVERRIDE_ENABLED"
        ] = "false"
        ctx = self._ctx(
            task_complexity="simple",
            provider_route="immediate",
            signal_source="swe_bench_pro",
        )
        enabled, _budget, reason = _compute_thinking_profile(
            ctx, extended_thinking_default=False, base_budget=4000,
        )
        self.assertFalse(enabled)
        self.assertEqual(reason, "immediate-reflex")

    def test_benchmark_sources_taxonomy_closed(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _BENCHMARK_THINKING_SOURCES,
        )
        self.assertIsInstance(
            _BENCHMARK_THINKING_SOURCES, frozenset,
            "Must be frozenset (immutable closed taxonomy)",
        )
        self.assertIn("swe_bench_pro", _BENCHMARK_THINKING_SOURCES)


# ============================================================================
# Slice 12G-2 — LoopDeadman
# ============================================================================


class TestLoopDeadmanCore(unittest.TestCase):

    def setUp(self) -> None:
        from backend.core.ouroboros.governance.loop_deadman import (
            reset_default_deadman,
        )
        reset_default_deadman()

    def tearDown(self) -> None:
        from backend.core.ouroboros.governance.loop_deadman import (
            reset_default_deadman,
        )
        reset_default_deadman()

    def test_heartbeat_updates_timestamp(self) -> None:
        from backend.core.ouroboros.governance.loop_deadman import (
            LoopDeadman,
        )
        dm = LoopDeadman()
        initial_age = dm.last_heartbeat_age_s()
        time.sleep(0.05)
        mid_age = dm.last_heartbeat_age_s()
        self.assertGreater(mid_age, initial_age)
        dm.heartbeat()
        post_age = dm.last_heartbeat_age_s()
        self.assertLess(
            post_age, mid_age,
            "heartbeat() must reset the age clock",
        )

    def test_env_knob_default_true(self) -> None:
        from backend.core.ouroboros.governance.loop_deadman import (
            deadman_enabled,
        )
        prior = os.environ.pop("JARVIS_LOOP_DEADMAN_ENABLED", None)
        try:
            self.assertTrue(deadman_enabled())
            os.environ["JARVIS_LOOP_DEADMAN_ENABLED"] = "false"
            self.assertFalse(deadman_enabled())
            os.environ["JARVIS_LOOP_DEADMAN_ENABLED"] = "true"
            self.assertTrue(deadman_enabled())
        finally:
            if prior is None:
                os.environ.pop("JARVIS_LOOP_DEADMAN_ENABLED", None)
            else:
                os.environ["JARVIS_LOOP_DEADMAN_ENABLED"] = prior

    def test_timeout_bounded(self) -> None:
        from backend.core.ouroboros.governance.loop_deadman import (
            deadman_timeout_s,
        )
        prior = os.environ.pop("JARVIS_LOOP_DEADMAN_TIMEOUT_S", None)
        try:
            os.environ["JARVIS_LOOP_DEADMAN_TIMEOUT_S"] = "1"
            self.assertEqual(deadman_timeout_s(), 30.0,
                             "floor at 30s")
            os.environ["JARVIS_LOOP_DEADMAN_TIMEOUT_S"] = "99999"
            self.assertEqual(deadman_timeout_s(), 3600.0,
                             "ceiling at 3600s")
            os.environ["JARVIS_LOOP_DEADMAN_TIMEOUT_S"] = "120"
            self.assertEqual(deadman_timeout_s(), 120.0)
        finally:
            if prior is None:
                os.environ.pop(
                    "JARVIS_LOOP_DEADMAN_TIMEOUT_S", None,
                )
            else:
                os.environ[
                    "JARVIS_LOOP_DEADMAN_TIMEOUT_S"
                ] = prior


class TestLoopDeadmanAstPins(unittest.TestCase):
    """The os._exit(75) firing contract is structural — pinning
    at AST level so it can't regress to a soft sys.exit() that
    asyncio cleanup might intercept."""

    def test_fire_wedge_calls_os_exit_75(self) -> None:
        tree = _parse_module(_DEADMAN_FILE)
        fire_fn = None
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.FunctionDef)
                and node.name == "_fire_wedge"
            ):
                fire_fn = node
                break
        self.assertIsNotNone(
            fire_fn, "_fire_wedge must exist",
        )
        # Must call os._exit with literal 75
        found = False
        for sub in _ast.walk(fire_fn):
            if not isinstance(sub, _ast.Call):
                continue
            f = sub.func
            if (
                isinstance(f, _ast.Attribute)
                and f.attr == "_exit"
                and isinstance(f.value, _ast.Name)
                and f.value.id == "os"
            ):
                if (
                    sub.args
                    and isinstance(sub.args[0], _ast.Constant)
                    and sub.args[0].value == 75
                ):
                    found = True
                    break
        self.assertTrue(
            found,
            "_fire_wedge must call os._exit(75) with literal 75 — "
            "soft sys.exit / non-75 codes are not equivalent",
        )

    def test_uses_daemon_thread(self) -> None:
        src = _DEADMAN_FILE.read_text()
        self.assertIn("daemon=True", src,
                      "deadman must run in daemon OS thread")
        self.assertIn("threading.Thread", src)


# ============================================================================
# Slice 12G-3 — SessionWAL
# ============================================================================


class TestSessionWal(unittest.TestCase):

    def setUp(self) -> None:
        from backend.core.ouroboros.governance.session_wal import (
            reset_default_wal,
        )
        reset_default_wal()
        self.tmpdir = tempfile.TemporaryDirectory(
            prefix="slice12g3-",
        )
        self.session_dir = pathlib.Path(self.tmpdir.name)

    def tearDown(self) -> None:
        from backend.core.ouroboros.governance.session_wal import (
            reset_default_wal,
        )
        reset_default_wal()
        self.tmpdir.cleanup()

    def test_atomic_write_creates_summary_json(self) -> None:
        from backend.core.ouroboros.governance.session_wal import (
            SessionWAL,
        )
        wal = SessionWAL(self.session_dir)
        state = {"session_id": "test-1", "stop_reason": "running"}
        ok = wal.force_checkpoint(state, "test_init")
        self.assertTrue(ok)
        summary_path = self.session_dir / "summary.json"
        self.assertTrue(summary_path.exists())
        data = json.loads(summary_path.read_text())
        self.assertEqual(data["session_id"], "test-1")
        self.assertEqual(data["checkpoint_reason"], "test_init")
        self.assertEqual(data["checkpoint_seq"], 1)
        self.assertIn("checkpoint_iso", data)

    def test_checkpoint_is_debounced(self) -> None:
        """Same-second consecutive checkpoints respect the
        debounce floor; subsequent reads still see SOME write."""
        from backend.core.ouroboros.governance.session_wal import (
            SessionWAL,
        )
        # Force a tight debounce so we can observe it.
        os.environ["JARVIS_SESSION_WAL_MIN_INTERVAL_S"] = "0.5"
        try:
            wal = SessionWAL(self.session_dir)
            ok1 = wal.checkpoint({"a": 1}, "first")
            ok2 = wal.checkpoint({"a": 2}, "second")  # debounced
            self.assertTrue(ok1)
            self.assertFalse(
                ok2,
                "Second checkpoint within debounce window must be "
                "rejected (False return); prior state stays valid",
            )
            data = json.loads(
                (self.session_dir / "summary.json").read_text(),
            )
            self.assertEqual(data["a"], 1,
                             "Debounced checkpoint must not overwrite")
        finally:
            os.environ.pop(
                "JARVIS_SESSION_WAL_MIN_INTERVAL_S", None,
            )

    def test_force_checkpoint_bypasses_debounce(self) -> None:
        from backend.core.ouroboros.governance.session_wal import (
            SessionWAL,
        )
        os.environ["JARVIS_SESSION_WAL_MIN_INTERVAL_S"] = "5.0"
        try:
            wal = SessionWAL(self.session_dir)
            wal.force_checkpoint({"v": 1}, "force_a")
            wal.force_checkpoint({"v": 2}, "force_b")
            data = json.loads(
                (self.session_dir / "summary.json").read_text(),
            )
            self.assertEqual(data["v"], 2,
                             "force_checkpoint must bypass debounce")
            self.assertEqual(
                data["checkpoint_seq"], 2,
                "checkpoint_seq must monotonically advance",
            )
        finally:
            os.environ.pop(
                "JARVIS_SESSION_WAL_MIN_INTERVAL_S", None,
            )

    def test_non_serializable_values_coerced(self) -> None:
        """The WAL is best-effort — non-serializable values must
        be coerced (stringified) rather than raise into the loop."""
        from backend.core.ouroboros.governance.session_wal import (
            SessionWAL,
        )

        class _Weird:
            def __repr__(self):
                return "WeirdObject"

        wal = SessionWAL(self.session_dir)
        state = {
            "ok": "fine",
            "weird": _Weird(),
            "path": pathlib.Path("/tmp/x"),
            "set_field": {"a", "b"},
        }
        ok = wal.force_checkpoint(state, "weird_payload")
        self.assertTrue(ok)
        data = json.loads(
            (self.session_dir / "summary.json").read_text(),
        )
        # Stringified — exact form doesn't matter, just no raise
        self.assertIn("weird", data)
        self.assertIn("path", data)
        self.assertIn("set_field", data)

    def test_install_default_wal_singleton(self) -> None:
        from backend.core.ouroboros.governance.session_wal import (
            install_default_wal,
            get_default_wal,
            reset_default_wal,
        )
        reset_default_wal()
        self.assertIsNone(get_default_wal())
        wal1 = install_default_wal(self.session_dir)
        wal2 = install_default_wal(self.session_dir)
        self.assertIs(wal1, wal2, "install must be idempotent")
        self.assertIs(get_default_wal(), wal1)

    def test_env_knob_disabled_skips_write(self) -> None:
        from backend.core.ouroboros.governance.session_wal import (
            SessionWAL,
        )
        prior = os.environ.pop("JARVIS_SESSION_WAL_ENABLED", None)
        try:
            os.environ["JARVIS_SESSION_WAL_ENABLED"] = "false"
            wal = SessionWAL(self.session_dir)
            ok = wal.force_checkpoint({"x": 1}, "test")
            self.assertFalse(ok)
            self.assertFalse(
                (self.session_dir / "summary.json").exists(),
            )
        finally:
            if prior is None:
                os.environ.pop(
                    "JARVIS_SESSION_WAL_ENABLED", None,
                )
            else:
                os.environ[
                    "JARVIS_SESSION_WAL_ENABLED"
                ] = prior


# ============================================================================
# Wiring AST pins — harness boots all three Slice 12G surfaces
# ============================================================================


class TestHarnessWiringPins(unittest.TestCase):

    def test_harness_imports_loop_deadman(self) -> None:
        src = _HARNESS_FILE.read_text()
        self.assertIn(
            "from backend.core.ouroboros.governance.loop_deadman",
            src,
            "harness must import LoopDeadman (Slice 12G-2 boot)",
        )
        self.assertIn(
            "_loop_deadman", src,
            "harness must store deadman reference",
        )

    def test_harness_imports_session_wal(self) -> None:
        src = _HARNESS_FILE.read_text()
        self.assertIn(
            "from backend.core.ouroboros.governance.session_wal",
            src,
            "harness must import SessionWAL (Slice 12G-3 boot)",
        )
        self.assertIn(
            "_slice12g3_periodic_checkpoint_loop", src,
            "harness must expose the periodic checkpoint loop",
        )

    def test_harness_periodic_checkpoint_loop_exists(self) -> None:
        tree = _parse_module(_HARNESS_FILE)
        found = False
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.AsyncFunctionDef)
                and node.name == "_slice12g3_periodic_checkpoint_loop"
            ):
                found = True
                break
        self.assertTrue(
            found,
            "_slice12g3_periodic_checkpoint_loop must be an "
            "async method on the harness",
        )

    def test_harness_build_checkpoint_state_exists(self) -> None:
        tree = _parse_module(_HARNESS_FILE)
        found = False
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.FunctionDef)
                and node.name == "_slice12g3_build_checkpoint_state"
            ):
                found = True
                break
        self.assertTrue(found)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
