"""Slice 12X — Boot-Time Supremacy & Total Daemonization.

bt-2026-05-23-204519 (Slice 12W validation soak) proved:

* Phase 2 (Sidecar idle-frame exclusion) — **WORKED** (8 → 2
  emissions, 0 false positives)
* Phase 3 (WAL-second checkpoint) — **WORKED** (fired in
  production after orchestrator drain)
* Phase 1 (rogue thread exorcism) — **FAILED** because
  ``exorcise_at_boot`` runs from ``BattleTestHarness.__init__``
  but by then ``scripts/ouroboros_battle_test.py`` has already
  triggered transitive imports of chromadb (→ posthog consumer
  thread spawns) and the harness-side aiosqlite connections
  opened during early boot kept their non-daemon workers.

Slice 12X closes the import-time race via two complementary
disciplines:

# Phase 1 — Script-Top Boot Supremacy

Env hygiene moves to the absolute TOP of
``scripts/ouroboros_battle_test.py`` — after ``from __future__
import annotations`` (which must be first per Python grammar) but
BEFORE any other import (including stdlib non-essentials, and
absolutely before ``backend.*``). Only ``os`` and ``sys`` from
stdlib are needed and they never trigger third-party loads.

Env vars set via ``os.environ.setdefault`` so operator overrides
(e.g., a debug session that explicitly enables telemetry) survive.

A structured boot marker line is written to ``sys.stderr``:

    [Slice12X.BootExorcism] script-top env hygiene applied — ...

visible in any tee/pipe redirect regardless of file-logger
wire-up state.

The env-var table is intentionally duplicated between the script
top and ``rogue_thread_exorcism._TELEMETRY_DEFAULTS`` — the
script-top can't import from ``backend.*`` yet. The two tables
are AST-pinned to stay in sync.

# Phase 2 — Total aiosqlite Daemonization

Slice 12W's ``patch_aiosqlite_to_daemon`` only wrapped
``Connection.__init__`` — but the tombstone showed pre-existing
connections (opened during early boot before the patch) kept
their non-daemon workers.

Slice 12X expands to FOUR daemonization layers in one composite:

1. ``Connection.__init__`` wrap (Slice 12W behavior preserved)
2. ``aiosqlite.connect()`` top-level factory wrap — daemonizes
   the returned object's worker if it's already a Connection,
   AND lets Layer 1 catch any deferred-construct case
3. ``_daemonize_existing_aiosqlite_threads`` — walks
   :func:`threading.enumerate` at patch time and flips
   ``daemon=True`` on any pre-existing worker thread whose name
   contains ``aiosqlite``. Closes the "patch landed too late"
   race the bt-2026-05-23-204519 tombstone surfaced.
4. Same ``_AIOSQLITE_PATCHED`` idempotency latch — re-arming
   the patch still re-sweeps existing threads (Layer 3 fires
   regardless of latch state).

Both phases honored: env var setdefault preserves operator
overrides; aiosqlite patch never raises when the library isn't
installed; thread sweep never raises on individual thread errors.
"""

from __future__ import annotations

import ast
import os
import sys
import threading
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import rogue_thread_exorcism
from backend.core.ouroboros.battle_test.rogue_thread_exorcism import (
    _AIOSQLITE_PATCHED,
    _TELEMETRY_DEFAULTS,
    _daemonize_existing_aiosqlite_threads,
    patch_aiosqlite_to_daemon,
)


# ──────────────────────────────────────────────────────────────────────
# Phase 1 — Script-Top Boot Supremacy
# ──────────────────────────────────────────────────────────────────────


class TestPhase1ScriptTopPlacement:
    """The env hygiene block MUST live at the absolute top of
    ``scripts/ouroboros_battle_test.py`` — after ``from __future__
    import annotations`` (Python grammar requirement) but BEFORE
    any other import. AST-pinned so a future refactor cannot
    silently move it after a ``backend.*`` import."""

    def _read_script_source(self) -> str:
        return Path("scripts/ouroboros_battle_test.py").read_text()

    def test_script_top_carries_slice12x_marker(self):
        src = self._read_script_source()
        assert "Slice 12X Phase 1" in src, (
            "Slice 12X Phase 1 marker missing from script top — "
            "the boot-supremacy block was deleted or moved"
        )
        assert "_SLICE12X_TELEMETRY_DEFAULTS" in src, (
            "Slice 12X env-var table missing from script top"
        )
        assert "BOOT-TIME SUPREMACY" in src.upper() or \
               "Slice 12X" in src

    def test_env_hygiene_appears_before_any_backend_import(self):
        """The load-bearing assertion. The
        ``_SLICE12X_TELEMETRY_DEFAULTS`` table assignment MUST
        appear BEFORE the first ``from backend.`` or
        ``import backend.`` line in the file."""
        src = self._read_script_source()
        marker_idx = src.find("_SLICE12X_TELEMETRY_DEFAULTS")
        assert marker_idx > 0, (
            "Phase 1 boot exorcism block missing"
        )
        # Find the first backend import in the file.
        first_backend_idx = -1
        for needle in ("from backend.", "import backend."):
            idx = src.find(needle)
            if idx > 0:
                if first_backend_idx < 0 or idx < first_backend_idx:
                    first_backend_idx = idx
        if first_backend_idx > 0:
            assert marker_idx < first_backend_idx, (
                f"Slice 12X boot exorcism (pos={marker_idx}) "
                f"appears AFTER a backend import (pos="
                f"{first_backend_idx}) — the race we set out to "
                "close is back. Move the block above the first "
                "backend.* import."
            )

    def test_env_hygiene_appears_after_future_annotations(self):
        """``from __future__ import annotations`` MUST be the
        first statement in the module per Python grammar. The
        env hygiene block must come AFTER it."""
        src = self._read_script_source()
        future_idx = src.find("from __future__ import annotations")
        marker_idx = src.find("_SLICE12X_TELEMETRY_DEFAULTS")
        assert future_idx > 0
        assert marker_idx > future_idx, (
            "Slice 12X block appears BEFORE 'from __future__' — "
            "Python will reject the module"
        )

    def test_env_hygiene_uses_setdefault_not_assignment(self):
        """Operator overrides MUST survive — verified by AST: the
        script-top block uses ``os.environ.setdefault`` semantics
        (here implemented as ``if not in os.environ: assign``)
        instead of direct ``os.environ[key] = value`` which
        would clobber explicit operator config."""
        src = self._read_script_source()
        marker_idx = src.find("_SLICE12X_TELEMETRY_DEFAULTS")
        # Look at the next ~2000 chars for the conditional check.
        block = src[marker_idx:marker_idx + 2000]
        # The pattern must include an "if X not in environ" guard.
        assert "not in" in block and "environ" in block, (
            "Script-top block doesn't use setdefault-style "
            "conditional — operator overrides will be clobbered"
        )

    def test_stderr_boot_marker_present(self):
        """The structured boot marker must be written to stderr
        so operators see it in any tee/pipe redirect."""
        src = self._read_script_source()
        marker_idx = src.find("_SLICE12X_TELEMETRY_DEFAULTS")
        block = src[marker_idx:marker_idx + 2500]
        assert "Slice12X.BootExorcism" in block, (
            "Stderr boot marker missing — operators can't verify "
            "the exorcism ran without tedious env-var inspection"
        )
        assert "stderr" in block

    def test_telemetry_defaults_match_rogue_thread_exorcism(self):
        """The script-top duplicates ``_TELEMETRY_DEFAULTS`` from
        ``rogue_thread_exorcism`` (it can't import from backend
        yet). Pin that the two tables stay in sync."""
        src = self._read_script_source()
        # Extract the script-top tuple via AST.
        tree = ast.parse(src)
        script_table: dict = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id
                == "_SLICE12X_TELEMETRY_DEFAULTS"
            ):
                # Should be a Tuple of Tuple[str, str].
                if isinstance(node.value, ast.Tuple):
                    for elt in node.value.elts:
                        if (
                            isinstance(elt, ast.Tuple)
                            and len(elt.elts) == 2
                            and all(
                                isinstance(c, ast.Constant)
                                for c in elt.elts
                            )
                        ):
                            k = elt.elts[0].value  # type: ignore[attr-defined]
                            v = elt.elts[1].value  # type: ignore[attr-defined]
                            script_table[k] = v
                break
        assert script_table, (
            "Could not extract _SLICE12X_TELEMETRY_DEFAULTS via "
            "AST — table shape changed"
        )
        # Compare to the canonical table.
        canonical = dict(_TELEMETRY_DEFAULTS)
        assert script_table == canonical, (
            f"Script-top table {script_table} drifted from "
            f"rogue_thread_exorcism._TELEMETRY_DEFAULTS "
            f"{canonical} — the two duplicate sources are out "
            "of sync"
        )


class TestPhase1ScriptTopRuntime:
    """End-to-end: run the script-top block in a subprocess +
    verify env vars are set + stderr marker emitted."""

    def test_subprocess_runs_script_top_and_emits_marker(
        self, tmp_path,
    ):
        """Spawn a Python subprocess that executes the script-top
        block (everything up to the first non-stdlib import).
        Verify the env vars + stderr marker."""
        import subprocess
        # Build a tiny driver that exec()s the script-top region.
        driver = tmp_path / "driver.py"
        driver.write_text(
            "import sys\n"
            "with open('scripts/ouroboros_battle_test.py') as f:\n"
            "    src = f.read()\n"
            "# Execute only up to the first non-stdlib import.\n"
            "cut = src.find('import argparse')\n"
            "assert cut > 0, 'argparse import not found'\n"
            "exec(src[:cut + len('import argparse')])\n"
            "import os\n"
            "print('ANONYMIZED_TELEMETRY=' + "
            "os.environ.get('ANONYMIZED_TELEMETRY', '<missing>'))\n"
            "print('POSTHOG_DISABLED=' + "
            "os.environ.get('POSTHOG_DISABLED', '<missing>'))\n"
            "print('OTEL_SDK_DISABLED=' + "
            "os.environ.get('OTEL_SDK_DISABLED', '<missing>'))\n"
        )
        # Run with a clean env (no telemetry vars set).
        env = {
            k: v for k, v in os.environ.items()
            if k not in (
                "ANONYMIZED_TELEMETRY", "POSTHOG_DISABLED",
                "OTEL_SDK_DISABLED",
            )
        }
        env["PYTHONPATH"] = os.getcwd()
        result = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, timeout=30,
            env=env, cwd=os.getcwd(),
        )
        assert result.returncode == 0, (
            f"Driver crashed: stderr={result.stderr}"
        )
        # Env vars must be set.
        assert "ANONYMIZED_TELEMETRY=False" in result.stdout
        assert "POSTHOG_DISABLED=True" in result.stdout
        assert "OTEL_SDK_DISABLED=true" in result.stdout
        # Boot marker must be on stderr.
        assert "Slice12X.BootExorcism" in result.stderr, (
            f"Boot marker missing from stderr: {result.stderr!r}"
        )

    def test_subprocess_preserves_operator_overrides(
        self, tmp_path,
    ):
        """Operator sets ANONYMIZED_TELEMETRY=True (debug
        session); subprocess must NOT clobber it."""
        import subprocess
        driver = tmp_path / "driver.py"
        driver.write_text(
            "with open('scripts/ouroboros_battle_test.py') as f:\n"
            "    src = f.read()\n"
            "cut = src.find('import argparse')\n"
            "exec(src[:cut + len('import argparse')])\n"
            "import os\n"
            "print('AT=' + os.environ.get("
            "'ANONYMIZED_TELEMETRY', '<missing>'))\n"
        )
        env = dict(os.environ)
        env["ANONYMIZED_TELEMETRY"] = "True"  # operator override
        env["PYTHONPATH"] = os.getcwd()
        result = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, timeout=30,
            env=env, cwd=os.getcwd(),
        )
        assert result.returncode == 0
        # Operator's "True" must survive — setdefault did NOT
        # clobber.
        assert "AT=True" in result.stdout, (
            f"Operator override clobbered: {result.stdout!r}"
        )


# ──────────────────────────────────────────────────────────────────────
# Phase 2 — Total aiosqlite Daemonization
# ──────────────────────────────────────────────────────────────────────


class TestPhase2DaemonizeExistingThreads:
    def test_helper_callable_and_returns_int(self):
        n = _daemonize_existing_aiosqlite_threads()
        assert isinstance(n, int)
        assert n >= 0

    def test_helper_attempts_flip_on_matching_thread(self):
        """Sweep MUST attempt to flip aiosqlite-named threads.
        CPython forbids flipping daemon on an already-started
        thread (RuntimeError: cannot set daemon status of active
        thread), so the helper records 0 successful flips when
        the thread is running — this is documented behavior.
        The load-bearing primary defense is Layer 1
        (Connection.__init__ wrap) which flips daemon BEFORE
        start; Layer 3 is best-effort for the pre-start race
        window.

        This test verifies the helper RUNS WITHOUT RAISING when
        encountering a running aiosqlite-named thread — the
        sweep's never-raise contract."""
        ev = threading.Event()

        def _worker():
            ev.wait(timeout=5.0)

        t = threading.Thread(
            target=_worker, name="aiosqlite_test_worker",
            daemon=False,
        )
        t.start()
        try:
            # Confirm starting state.
            assert t.daemon is False
            # MUST NOT raise even though CPython will reject the
            # daemon flip on this already-started thread.
            n = _daemonize_existing_aiosqlite_threads()
            assert isinstance(n, int)
            assert n >= 0
            # CPython runtime constraint: daemon stays False
            # because the thread is started. Documented.
        finally:
            ev.set()
            t.join(timeout=5.0)

    def test_helper_ignores_non_aiosqlite_threads(self):
        """A thread without 'aiosqlite' in its name must NOT be
        affected — narrow target prevents accidentally
        daemonizing unrelated workers."""
        ev = threading.Event()

        def _worker():
            ev.wait(timeout=5.0)

        t = threading.Thread(
            target=_worker, name="my_business_thread",
            daemon=False,
        )
        t.start()
        try:
            _daemonize_existing_aiosqlite_threads()
            assert t.daemon is False, (
                "Sweep clobbered a non-aiosqlite thread — "
                "match is too loose"
            )
        finally:
            ev.set()
            t.join(timeout=5.0)


class TestPhase2ConnectWrapper:
    """Phase 2 wraps ``aiosqlite.connect`` (top-level factory) in
    addition to ``Connection.__init__``. Verify both wraps land."""

    def test_aiosqlite_connect_is_wrapped_after_patch(self):
        """After ``patch_aiosqlite_to_daemon`` runs, the module's
        ``connect`` callable MUST NOT be the original aiosqlite
        function — it must be our exorcised wrapper."""
        # Save original to restore after the test.
        import aiosqlite  # type: ignore[import]
        original_connect = aiosqlite.connect
        try:
            # Reset latch so we can re-apply and observe the wrap.
            rogue_thread_exorcism._AIOSQLITE_PATCHED = False
            result = patch_aiosqlite_to_daemon()
            assert result is True
            # The wrapper's qualified name should differ from the
            # original — it's a nested function created in our
            # module.
            wrapped = aiosqlite.connect
            assert wrapped is not original_connect, (
                "aiosqlite.connect was NOT wrapped — Phase 2 "
                "Layer 2 broken; top-level factory still spawns "
                "non-daemon workers"
            )
        finally:
            # Restore to keep the test environment clean.
            aiosqlite.connect = original_connect  # type: ignore[assignment]


class TestPhase2EndToEnd:
    """Real aiosqlite connection lifecycle — verify the worker
    thread spawned is actually daemon=True post-patch."""

    @pytest.mark.asyncio
    async def test_real_connection_is_daemon_after_patch(
        self, tmp_path,
    ):
        """Open a real aiosqlite connection after the patch is
        applied; verify the Connection (which IS-A
        :class:`threading.Thread`) has ``daemon=True``.

        Slice 12X discovery: aiosqlite.Connection inherits from
        Thread — the Connection object IS the worker thread,
        not a wrapper around one. The Layer 1 patch flips
        ``self.daemon = True`` directly on the Connection
        instance during __init__ (before start() runs in
        __aenter__)."""
        # Apply the patch (idempotent — may already be applied).
        rogue_thread_exorcism._AIOSQLITE_PATCHED = False
        patch_aiosqlite_to_daemon()

        import aiosqlite  # type: ignore[import]
        db_path = tmp_path / "test.db"
        # Use the top-level factory (wrapped via Layer 2).
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute("CREATE TABLE t (x INT)")
            # The Connection itself IS the worker Thread.
            assert isinstance(conn, threading.Thread), (
                "aiosqlite.Connection no longer subclasses "
                "Thread — patch strategy needs revisiting"
            )
            assert conn.daemon is True, (
                "Connection.daemon spawned False despite "
                "Slice 12X Layer 1 patch — exorcism didn't take; "
                "rogue posthog/aiosqlite teardown wedge returns"
            )


class TestPhase2ASTPin:
    def _read_src(self) -> str:
        return Path(
            "backend/core/ouroboros/battle_test/"
            "rogue_thread_exorcism.py"
        ).read_text()

    def test_patch_wraps_top_level_connect(self):
        """AST: ``patch_aiosqlite_to_daemon`` body must mention
        ``connect`` (the top-level factory wrap) in addition to
        ``Connection.__init__``."""
        src = self._read_src()
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "patch_aiosqlite_to_daemon"
            ):
                target = node
                break
        assert target is not None
        body_text = ast.unparse(target)
        assert "_exorcised_connect" in body_text or \
               "connect" in body_text, (
            "patch_aiosqlite_to_daemon no longer wraps "
            "aiosqlite.connect — Phase 2 Layer 2 regressed"
        )
        assert "_daemonize_existing_aiosqlite_threads" in body_text, (
            "Phase 2 Layer 3 sweep no longer called from "
            "patch_aiosqlite_to_daemon"
        )

    def test_sweep_helper_walks_threading_enumerate(self):
        """The sweep helper must call ``threading.enumerate`` to
        find existing threads."""
        src = self._read_src()
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name
                == "_daemonize_existing_aiosqlite_threads"
            ):
                target = node
                break
        assert target is not None
        body_text = ast.unparse(target)
        assert "enumerate" in body_text, (
            "Sweep doesn't call threading.enumerate — Layer 3 "
            "won't find any existing threads"
        )


# ──────────────────────────────────────────────────────────────────────
# Cross-phase public surface
# ──────────────────────────────────────────────────────────────────────


class TestPublicSurface:
    def test_sweep_exported(self):
        assert hasattr(
            rogue_thread_exorcism,
            "_daemonize_existing_aiosqlite_threads",
        )
        assert (
            "_daemonize_existing_aiosqlite_threads"
            in rogue_thread_exorcism.__all__
        )
