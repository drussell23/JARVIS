"""Slice 12W — Ultimate Teardown Exorcism & Precision Telemetry.

bt-2026-05-23-201956 (Slice 12V validation soak) captured three
clear next-class signals via the new forensic substrate:

* **ShutdownWatchdog tombstone** pinpointed the exact interpreter
  wedge — ``threading._shutdown`` at ``threading.py:1590`` blocking
  on ``lock.acquire()`` for a non-daemon thread. Cross-referencing
  the full thread dump named two culprits: ``aiosqlite/core.py:99 in
  run`` (per-connection worker, default ``daemon=False``) and
  ``posthog/consumer.py:65 in run`` (PostHog analytics, pulled in
  transitively via ``chromadb.telemetry.product.posthog``).

* **Sidecar Profiler** correctly captured 8 in-progress MainThread
  frames — but 7/8 were ``selectors.py:566:select`` (the asyncio
  event loop's normal kqueue.control idle position). Only 1 was the
  real wedge.

* **WAL-first** wrote summary.json before cleanup, but the fixture
  op was still mid-cascade so ``operations[]`` landed empty even
  though stop_reason finally captured ``budget_exhausted`` instead
  of ``unknown``.

Slice 12W is the **tripartite ultimate teardown exorcism** — three
composable disciplines that close all three follow-on classes:

# Phase 1 — Rogue Thread Exorcism

NEW module ``battle_test/rogue_thread_exorcism.py``:

* ``apply_telemetry_env_defaults`` — sets ``ANONYMIZED_TELEMETRY=False``,
  ``POSTHOG_DISABLED=True``, ``OTEL_SDK_DISABLED=true`` via
  :func:`os.environ.setdefault` (preserves operator overrides). Must
  run BEFORE any subsystem imports chromadb/posthog.
* ``patch_aiosqlite_to_daemon`` — monkeypatches
  :class:`aiosqlite.core.Connection` so its per-connection worker
  thread spawns as ``daemon=True``. Connections still drain cleanly
  via async ``__aexit__``; daemon flag only matters at hard
  ``Py_FinalizeEx`` teardown.
* ``disable_posthog_if_loaded`` — defense-in-depth: if posthog was
  imported before our env vars (edge case), flips its module-level
  ``disabled`` flag.
* ``exorcise_at_boot`` — composite entry point called by
  ``BattleTestHarness.__init__`` as the VERY FIRST step (before
  session-dir setup, before any subsystem import).

Master switch ``JARVIS_ROGUE_THREAD_EXORCISM_ENABLED`` (default
TRUE). Idempotent.

# Phase 2 — Sidecar Idle-Frame Exclusion Registry

NEW closed registry ``_IDLE_FRAME_EXCLUSIONS`` in
``sidecar_profiler.py``:

* ``(selectors.py, select)`` — asyncio kqueue.control / epoll.poll
  idle (the bt-2026-05-23-201956 false-positive class)
* ``(base_events.py, run_forever / run_until_complete / _run_once)``
  — asyncio loop outer drivers
* ``(events.py, _run)`` — asyncio handle dispatcher (transient
  between-task position)
* ``(threading.py, wait)`` — Event/Condition.wait (legitimate
  blocked-on-signal position)

NEW pure function ``is_idle_frame(filename, function_name)`` — match
is endswith-on-filename + equals-on-function (avoids matching
user-defined ``select`` in unrelated modules). Wired into
``SidecarProfiler._poll_once`` between the threshold check and the
log-throttle, so excluded frames never reach the emission path.

# Phase 3 — WAL-Second Checkpoint

NEW write site in ``harness._shutdown_components`` AFTER step 4
(``GovernedLoopService.stop`` completes the ``_active_ops`` drain
+ final SessionRecorder writes) but BEFORE step 5+ (Oracle Chroma
client + network teardowns that can hang past the ShutdownWatchdog
30s deadline). Stamps ``stop_reason="in_flight_shutdown_wal_second"``.

Master switch reuses ``JARVIS_SHUTDOWN_WAL_FIRST_ENABLED`` (single
source of truth — operators don't tune WAL-first and WAL-second
separately; both fire under the same flag).
"""

from __future__ import annotations

import ast
import os
import sys
import threading
from pathlib import Path
from typing import Optional

import pytest

from backend.core.ouroboros.battle_test import rogue_thread_exorcism
from backend.core.ouroboros.battle_test.rogue_thread_exorcism import (
    ROGUE_THREAD_EXORCISM_ENABLED_ENV_VAR,
    _TELEMETRY_DEFAULTS,
    apply_telemetry_env_defaults,
    disable_posthog_if_loaded,
    exorcise_at_boot,
    exorcism_enabled,
    patch_aiosqlite_to_daemon,
)
from backend.core.ouroboros.governance import sidecar_profiler
from backend.core.ouroboros.governance.sidecar_profiler import (
    SidecarProfiler,
    _IDLE_FRAME_EXCLUSIONS,
    is_idle_frame,
)


# ──────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Slice 12W env knobs must not leak between tests."""
    for var in (
        ROGUE_THREAD_EXORCISM_ENABLED_ENV_VAR,
        "ANONYMIZED_TELEMETRY",
        "POSTHOG_DISABLED",
        "OTEL_SDK_DISABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ──────────────────────────────────────────────────────────────────────
# Phase 1 — Rogue Thread Exorcism
# ──────────────────────────────────────────────────────────────────────


class TestPhase1MasterSwitch:
    def test_default_is_true(self, monkeypatch):
        monkeypatch.delenv(
            ROGUE_THREAD_EXORCISM_ENABLED_ENV_VAR, raising=False,
        )
        assert exorcism_enabled() is True

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("true", True), ("1", True), ("on", True),
            ("yes", True),
            ("false", False), ("0", False), ("no", False),
            ("off", False),
        ],
    )
    def test_truthy_values(self, monkeypatch, raw, expected):
        monkeypatch.setenv(
            ROGUE_THREAD_EXORCISM_ENABLED_ENV_VAR, raw,
        )
        assert exorcism_enabled() is expected


class TestPhase1TelemetryEnv:
    def test_applies_all_defaults_on_clean_env(self, monkeypatch):
        for var, _ in _TELEMETRY_DEFAULTS:
            monkeypatch.delenv(var, raising=False)
        applied = apply_telemetry_env_defaults()
        assert len(applied) == len(_TELEMETRY_DEFAULTS)
        # Every entry now in env with the expected value.
        for var, value in _TELEMETRY_DEFAULTS:
            assert os.environ.get(var) == value

    def test_preserves_operator_overrides(self, monkeypatch):
        """setdefault semantics: operator-set values MUST survive
        — Slice 12W must NEVER clobber explicit operator config."""
        # Operator wants telemetry enabled for debugging.
        monkeypatch.setenv("ANONYMIZED_TELEMETRY", "True")
        applied = apply_telemetry_env_defaults()
        # ANONYMIZED_TELEMETRY should NOT be in applied (already set).
        applied_vars = [v for v, _ in applied]
        assert "ANONYMIZED_TELEMETRY" not in applied_vars
        # And the operator's value MUST survive.
        assert os.environ["ANONYMIZED_TELEMETRY"] == "True"

    def test_telemetry_defaults_table_covers_known_offenders(self):
        """Pin the closed table so adding a new known offender is
        a deliberate code edit (not silent regression). Current
        rogue threads observed in bt-2026-05-23-201956:
        posthog (via chromadb)."""
        vars_only = {v for v, _ in _TELEMETRY_DEFAULTS}
        assert "ANONYMIZED_TELEMETRY" in vars_only, (
            "Slice 12W must disable chromadb telemetry "
            "(transitive posthog spawner)"
        )
        assert "POSTHOG_DISABLED" in vars_only, (
            "Slice 12W must disable posthog directly "
            "(defense-in-depth)"
        )


class TestPhase1AiosqlitePatch:
    def test_patch_idempotent(self):
        """Calling the patch twice must be a no-op (latch
        prevents double-patching)."""
        # Reset the latch to test the first-apply path
        # cleanly. The patch may not actually be applied if
        # aiosqlite isn't installed — both outcomes are fine.
        rogue_thread_exorcism._AIOSQLITE_PATCHED = False
        first = patch_aiosqlite_to_daemon()
        second = patch_aiosqlite_to_daemon()
        # Both calls return the same value (True if installed,
        # False otherwise) — second call is idempotent because
        # of the latch.
        assert first == second

    def test_patch_never_raises_when_aiosqlite_missing(
        self, monkeypatch,
    ):
        """If aiosqlite isn't importable, patch returns False
        without raising — never blocks boot."""
        # Temporarily hide aiosqlite from sys.modules + block
        # re-import via meta_path.
        original_aiosqlite_core = sys.modules.pop(
            "aiosqlite.core", None,
        )
        rogue_thread_exorcism._AIOSQLITE_PATCHED = False
        try:
            class _BlockAiosqlite:
                def find_module(self, name, path=None):
                    if name == "aiosqlite.core":
                        return self

                def load_module(self, name):
                    raise ImportError(f"blocked: {name}")

            blocker = _BlockAiosqlite()
            sys.meta_path.insert(0, blocker)
            try:
                # MUST NOT raise.
                result = patch_aiosqlite_to_daemon()
                assert result is False
            finally:
                sys.meta_path.remove(blocker)
        finally:
            if original_aiosqlite_core is not None:
                sys.modules["aiosqlite.core"] = original_aiosqlite_core


class TestPhase1PosthogDisable:
    def test_returns_false_when_not_loaded(self, monkeypatch):
        """No posthog in sys.modules → returns False, never raises."""
        original = sys.modules.pop("posthog", None)
        try:
            assert disable_posthog_if_loaded() is False
        finally:
            if original is not None:
                sys.modules["posthog"] = original

    def test_flips_disabled_when_loaded(self):
        """Mock posthog into sys.modules + verify flag flips."""
        original = sys.modules.get("posthog")
        try:
            class _FakePosthog:
                disabled = False

            fake = _FakePosthog()
            sys.modules["posthog"] = fake  # type: ignore[assignment]
            result = disable_posthog_if_loaded()
            assert result is True
            assert fake.disabled is True
        finally:
            if original is None:
                sys.modules.pop("posthog", None)
            else:
                sys.modules["posthog"] = original


class TestPhase1CompositeEntry:
    def test_exorcise_at_boot_returns_report(self, monkeypatch):
        for var, _ in _TELEMETRY_DEFAULTS:
            monkeypatch.delenv(var, raising=False)
        report = exorcise_at_boot()
        assert report["enabled"] is True
        assert isinstance(report["env_defaults_applied"], list)
        assert "aiosqlite_patched" in report
        assert "posthog_disabled" in report

    def test_master_off_skips_everything(self, monkeypatch):
        monkeypatch.setenv(
            ROGUE_THREAD_EXORCISM_ENABLED_ENV_VAR, "false",
        )
        for var, _ in _TELEMETRY_DEFAULTS:
            monkeypatch.delenv(var, raising=False)
        report = exorcise_at_boot()
        assert report["enabled"] is False
        assert report["env_defaults_applied"] == []
        # Env vars must NOT have been set.
        for var, _ in _TELEMETRY_DEFAULTS:
            assert var not in os.environ


class TestPhase1HarnessWiring:
    def test_harness_calls_exorcise_at_boot_first(self):
        """The exorcism MUST be called in __init__ BEFORE any
        downstream subsystem init that could import chromadb."""
        src = Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()
        assert "exorcise_at_boot" in src, (
            "Slice 12W Phase 1 — harness does not wire "
            "exorcise_at_boot; rogue threads will spawn"
        )
        # Find the __init__ method via AST and verify the call
        # appears in its first ~30 lines (before any subsystem
        # construction).
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "BattleTestHarness"
            ):
                for child in node.body:
                    if (
                        isinstance(child, ast.FunctionDef)
                        and child.name == "__init__"
                    ):
                        body_text = ast.unparse(child)
                        # exorcise_at_boot must appear early —
                        # before "_session_dir = " line.
                        idx_exorcise = body_text.find(
                            "exorcise_at_boot",
                        )
                        idx_session_dir = body_text.find(
                            "_session_dir = config.session_dir",
                        )
                        assert idx_exorcise > 0, (
                            "exorcise_at_boot not called in "
                            "__init__"
                        )
                        assert idx_exorcise < idx_session_dir, (
                            "exorcise_at_boot called AFTER "
                            "session_dir setup — too late to "
                            "prevent module-load-time imports"
                        )
                        return
        pytest.fail(
            "Could not locate BattleTestHarness.__init__"
        )


# ──────────────────────────────────────────────────────────────────────
# Phase 2 — Sidecar Idle-Frame Exclusion
# ──────────────────────────────────────────────────────────────────────


class TestPhase2IdleFrameExclusions:
    def test_registry_includes_known_offenders(self):
        """Pin the exclusion entries that closed
        bt-2026-05-23-201956's false-positive class."""
        excl_set = set(_IDLE_FRAME_EXCLUSIONS)
        # The single biggest false-positive class.
        assert ("selectors.py", "select") in excl_set, (
            "selectors.py:select missing from exclusion list — "
            "asyncio idle false positives will return"
        )
        # asyncio loop drivers.
        assert ("base_events.py", "run_forever") in excl_set
        # Python's threading wait primitives.
        assert ("threading.py", "wait") in excl_set


class TestPhase2IsIdleFrame:
    def test_selectors_select_matches(self):
        """Most important case — asyncio kqueue/epoll idle."""
        assert is_idle_frame(
            "/usr/lib/python3.11/selectors.py", "select",
        )
        # Also matches when the path has different prefixes.
        assert is_idle_frame(
            "/some/other/path/selectors.py", "select",
        )

    def test_real_wedge_does_not_match(self):
        """The actual wedge from bt-2026-05-23-201956 was
        ``threading.py:1590 in _shutdown`` — this MUST NOT be
        excluded; it's the wedge we want attribution for."""
        assert not is_idle_frame(
            "/usr/lib/python3.11/threading.py", "_shutdown",
        )

    def test_user_defined_select_not_excluded(self):
        """Function-name match must be exact, not substring —
        a user-defined ``select`` in some other module MUST NOT
        be excluded."""
        assert not is_idle_frame(
            "/my/custom_module.py", "select",
        )

    def test_predictive_engine_fragility_not_excluded(self):
        """The Slice 12U-fixed wedge (predictive_engine._fragility
        rglob+read_text on the loop) was a REAL wedge. If it ever
        regresses, the sidecar must still catch it — confirm it's
        not accidentally in the exclusion list."""
        assert not is_idle_frame(
            "/backend/core/ouroboros/governance/predictive_engine.py",
            "_fragility",
        )


class TestPhase2SidecarSuppressesFalsePositives:
    """End-to-end: spawn a profiler + force MainThread into an
    excluded frame via threading.Event.wait — STUCK_FRAME must
    NOT fire."""

    def test_excluded_frame_suppressed(self, caplog):
        sp = SidecarProfiler(
            poll_interval_s=0.05,
            stuck_threshold_s=0.2,
            stuck_log_interval_s=30.0,
        )
        assert sp.start()
        try:
            with caplog.at_level(
                "CRITICAL", logger="Ouroboros.SidecarProfiler",
            ):
                # threading.Event.wait is in the exclusion list
                # — must NOT emit STUCK_FRAME even though we
                # park here for well over the threshold.
                _ev = threading.Event()
                _ev.wait(timeout=0.5)

            stuck_msgs = [
                r.getMessage() for r in caplog.records
                if "STUCK_FRAME" in r.getMessage()
            ]
            assert len(stuck_msgs) == 0, (
                f"Excluded frame emitted STUCK_FRAME "
                f"({len(stuck_msgs)} times) — Phase 2 exclusion "
                "registry not wired into _poll_once"
            )
        finally:
            sp.stop()

    def test_real_wedge_still_emits(self, caplog):
        """Sanity: the Slice 12V behavior (catch true wedges)
        must survive the Phase 2 tuning. time.sleep is NOT in
        the exclusion list, so MUST fire."""
        import time
        sp = SidecarProfiler(
            poll_interval_s=0.05,
            stuck_threshold_s=0.2,
            stuck_log_interval_s=30.0,
        )
        assert sp.start()
        try:
            with caplog.at_level(
                "CRITICAL", logger="Ouroboros.SidecarProfiler",
            ):
                time.sleep(0.6)
            stuck_msgs = [
                r.getMessage() for r in caplog.records
                if "STUCK_FRAME" in r.getMessage()
            ]
            assert len(stuck_msgs) >= 1, (
                "Phase 2 over-suppressed — real time.sleep wedge "
                "(not in exclusion list) no longer triggers "
                "STUCK_FRAME"
            )
        finally:
            sp.stop()


class TestPhase2ASTPin:
    def test_sidecar_calls_is_idle_frame_in_poll(self):
        """The exclusion check MUST be wired into _poll_once."""
        src = Path(
            "backend/core/ouroboros/governance/sidecar_profiler.py"
        ).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_poll_once"
            ):
                body_text = ast.unparse(node)
                assert "is_idle_frame" in body_text, (
                    "_poll_once does not call is_idle_frame — "
                    "Phase 2 exclusion never fires; false "
                    "positives return"
                )
                return
        pytest.fail("_poll_once not found in sidecar_profiler.py")


# ──────────────────────────────────────────────────────────────────────
# Phase 3 — WAL-Second Checkpoint
# ──────────────────────────────────────────────────────────────────────


class TestPhase3WALSecond:
    def test_harness_writes_wal_second_after_gls_stop(self):
        """The WAL-second write site MUST live in
        _shutdown_components AFTER GovernedLoopService.stop() but
        BEFORE step 5 (GovernanceStack.stop)."""
        src = Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()
        assert "Slice 12W Phase 3" in src, (
            "Slice 12W Phase 3 marker missing from harness.py"
        )
        assert "WAL-second" in src
        assert "in_flight_shutdown_wal_second" in src

        # Position check: WAL-second must appear AFTER the
        # "# 4. Governed loop service" comment + GLS.stop()
        # block, and BEFORE the "# 5. Governance stack" comment.
        idx_step4 = src.find("# 4. Governed loop service")
        idx_wal_second = src.find("Slice 12W Phase 3")
        idx_step5 = src.find("# 5. Governance stack")
        assert idx_step4 > 0
        assert idx_wal_second > 0
        assert idx_step5 > 0
        assert idx_step4 < idx_wal_second < idx_step5, (
            "Slice 12W WAL-second is in the wrong place — must "
            "fire AFTER GLS.stop drains _active_ops and BEFORE "
            "Oracle/network teardown"
        )

    def test_wal_second_uses_same_master_switch_as_wal_first(self):
        """Operators tune one knob, not two."""
        src = Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()
        # The WAL-second region must reference the WAL-first env
        # var (single source of truth).
        wal_second_idx = src.find("Slice 12W Phase 3")
        # Look for the env var name in the next ~3000 chars
        # (the WAL-second block size).
        block = src[wal_second_idx:wal_second_idx + 3000]
        assert "JARVIS_SHUTDOWN_WAL_FIRST_ENABLED" in block, (
            "WAL-second introduced a NEW master switch — must "
            "reuse WAL-first's knob for single-source-of-truth"
        )

    def test_wal_second_stamp_distinguishes_from_wal_first(self):
        """The two writes must use distinct session_outcome stamps
        so operators can grep which one persisted last."""
        src = Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()
        # WAL-first stamp (Slice 12V Phase 1).
        assert "in_flight_shutdown_wal" in src
        # WAL-second stamp (this slice).
        assert "in_flight_shutdown_wal_second" in src

    def test_wal_second_resets_summary_written_latch(self):
        """``_atexit_fallback_write`` has an early-return on
        ``_summary_written=True``. WAL-first sets the latch; if
        WAL-second doesn't reset it, the second write becomes a
        no-op and operations[] still lands empty.

        Slice 12Y extended the surrounding block with additional
        telemetry — search the broader window (8000 chars) to
        survive future expansions; the position-pin tests
        guarantee the reset is still INSIDE the WAL-second
        block (between Phase 3 marker and step 5)."""
        src = Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()
        wal_second_idx = src.find("Slice 12W Phase 3")
        step5_idx = src.find("# 5. Governance stack")
        block = src[wal_second_idx:step5_idx]
        assert "_summary_written = False" in block, (
            "WAL-second does not reset the _summary_written "
            "latch — the atexit fallback's early-return will "
            "skip it and operations[] stays empty"
        )


# ──────────────────────────────────────────────────────────────────────
# Cross-phase sanity
# ──────────────────────────────────────────────────────────────────────


class TestPublicSurface:
    def test_phase1_exports(self):
        for name in (
            "ROGUE_THREAD_EXORCISM_ENABLED_ENV_VAR",
            "_TELEMETRY_DEFAULTS",
            "apply_telemetry_env_defaults",
            "disable_posthog_if_loaded",
            "exorcise_at_boot",
            "exorcism_enabled",
            "patch_aiosqlite_to_daemon",
        ):
            assert hasattr(rogue_thread_exorcism, name), (
                f"rogue_thread_exorcism.{name} missing"
            )

    def test_phase2_exports(self):
        for name in (
            "_IDLE_FRAME_EXCLUSIONS",
            "is_idle_frame",
        ):
            assert hasattr(sidecar_profiler, name), (
                f"sidecar_profiler.{name} missing"
            )
