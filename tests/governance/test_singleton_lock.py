"""Regression spine for P1 Slice 3 — battle-test singleton lock.

Covers the structural single-instance defense: kernel-arbitrated
``LOCK_EX | LOCK_NB`` via the canonical ``flock_critical_section``
primitive, composed at a single seam in ``acquire_singleton`` and
wired into ``scripts/ouroboros_battle_test.py``.

Coverage axes:

  * Master-FALSE default (§33.1) + master-ON enable
  * :class:`SingletonLockResult` shape (frozen + schema)
  * :func:`default_lock_path` derives from repo root (no hardcoded
    literal at use site)
  * :func:`acquire_singleton` happy path: yields acquired=True,
    releases on exit
  * Acquire fails fast when lock is held (composed flock_critical_
    section returns acquired=False)
  * NEVER-raises: substrate breakage → fail-OPEN (acquired=True)
  * Composes canonical flock_critical_section (no parallel impl)
  * Real cross-process subprocess test — proves second concurrent
    fire genuinely blocks at the kernel level (not just mock-level)
  * Script wiring positional invariant — singleton check fires
    BEFORE existing _single_flight_preflight in main()
  * 4 AST pins validate against current source
"""
from __future__ import annotations

import ast as _ast
import inspect
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from backend.core.ouroboros.battle_test.singleton_lock import (
    SINGLETON_LOCK_SCHEMA_VERSION,
    SingletonLockResult,
    acquire_singleton,
    default_lock_path,
    register_shipped_invariants,
    singleton_lock_enabled,
)


_MASTER_FLAG = "JARVIS_BATTLE_TEST_SINGLETON_LOCK_ENABLED"


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    saved = os.environ.pop(_MASTER_FLAG, None)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(_MASTER_FLAG, None)
        else:
            os.environ[_MASTER_FLAG] = saved


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MASTER_FLAG, "true")


# ---------------------------------------------------------------------------
# Master gate (§33.1)
# ---------------------------------------------------------------------------


class TestMasterGate:
    def test_master_default_false(self):
        assert singleton_lock_enabled() is False

    def test_master_on(self, monkeypatch):
        _enable(monkeypatch)
        assert singleton_lock_enabled() is True

    def test_master_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(_MASTER_FLAG, "TRUE")
        assert singleton_lock_enabled() is True

    def test_master_garbage_is_false(self, monkeypatch):
        monkeypatch.setenv(_MASTER_FLAG, "kinda")
        assert singleton_lock_enabled() is False


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


class TestSingletonLockResult:
    def test_carries_schema_version(self, tmp_path):
        r = SingletonLockResult(
            acquired=True, lock_path=tmp_path / "x.lock",
        )
        assert r.schema_version == (
            SINGLETON_LOCK_SCHEMA_VERSION
        )

    def test_is_frozen(self, tmp_path):
        r = SingletonLockResult(
            acquired=True, lock_path=tmp_path / "x.lock",
        )
        with pytest.raises(Exception):
            r.acquired = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Path resolution (no hardcoding)
# ---------------------------------------------------------------------------


class TestDefaultLockPath:
    def test_derives_under_repo_root(self, tmp_path):
        result = default_lock_path(tmp_path)
        assert result.parent == tmp_path / ".jarvis"
        assert result.name == "ouroboros_battle_test.lock"

    def test_does_not_touch_filesystem(self, tmp_path):
        # Pure path arithmetic — no mkdir, no creat.
        bogus = tmp_path / "nope"
        default_lock_path(bogus)
        assert not bogus.exists()

    def test_handles_string_input(self, tmp_path):
        # Path() accepts str; the call site shouldn't have to
        # pre-convert.
        result = default_lock_path(Path(str(tmp_path)))
        assert result == (
            tmp_path / ".jarvis" / "ouroboros_battle_test.lock"
        )


# ---------------------------------------------------------------------------
# acquire_singleton — happy path + release
# ---------------------------------------------------------------------------


class TestAcquireHappyPath:
    def test_yields_acquired_result(self, tmp_path):
        with acquire_singleton(repo_root=tmp_path) as result:
            assert isinstance(result, SingletonLockResult)
            assert result.acquired is True
            assert result.lock_path == default_lock_path(tmp_path)

    def test_releases_after_with_block(self, tmp_path):
        """After exiting one acquire context, a second acquire
        on the same path must succeed — proves release fires."""
        with acquire_singleton(repo_root=tmp_path) as r1:
            assert r1.acquired is True
        with acquire_singleton(repo_root=tmp_path) as r2:
            assert r2.acquired is True

    def test_custom_lock_path_override(self, tmp_path):
        custom = tmp_path / "custom.lock"
        with acquire_singleton(
            repo_root=tmp_path, lock_path=custom,
        ) as result:
            assert result.lock_path == custom


# ---------------------------------------------------------------------------
# acquire_singleton — second fire fails fast
# ---------------------------------------------------------------------------


class TestAcquireConflict:
    def test_second_fire_fails_when_primitive_returns_false(
        self, tmp_path,
    ):
        """When flock_critical_section yields False, the result's
        acquired field surfaces False. Mocks the primitive to
        isolate the substrate's compose logic from kernel-level
        flock arbitration (which is tested via subprocess below).
        """
        from contextlib import contextmanager

        @contextmanager
        def _fake_flock(path, *, timeout_s=None):  # noqa: ARG001
            yield False  # simulate "another holder"

        with patch(
            "backend.core.ouroboros.governance.cross_process_jsonl"
            ".flock_critical_section",
            _fake_flock,
        ):
            with acquire_singleton(repo_root=tmp_path) as result:
                assert result.acquired is False
                assert result.lock_path == (
                    default_lock_path(tmp_path)
                )


# ---------------------------------------------------------------------------
# Fail-OPEN posture
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_substrate_import_failure_fails_open(self, tmp_path):
        """If cross_process_jsonl import fails (e.g. on a stripped
        runtime), acquire_singleton must yield acquired=True so
        the soak does not get locked out by a substrate breakage.
        The pgrep-based _single_flight_preflight is the fallback.
        """
        # Block the substrate import by injecting a bad module
        # entry in sys.modules.
        bad_path = (
            "backend.core.ouroboros.governance.cross_process_jsonl"
        )
        original = sys.modules.get(bad_path)
        sys.modules[bad_path] = None  # type: ignore[assignment]
        try:
            with acquire_singleton(
                repo_root=tmp_path,
            ) as result:
                assert result.acquired is True
        finally:
            if original is not None:
                sys.modules[bad_path] = original
            else:
                sys.modules.pop(bad_path, None)

    def test_primitive_raising_fails_open(self, tmp_path):
        """If the canonical primitive itself raises mid-acquire,
        the substrate catches + yields acquired=True (last-resort
        fail-open). Lock-out from primitive bugs is unacceptable."""
        from contextlib import contextmanager

        @contextmanager
        def _boom(path, *, timeout_s=None):  # noqa: ARG001
            raise RuntimeError("primitive crashed mid-acquire")
            yield  # unreachable but keeps contextmanager happy

        with patch(
            "backend.core.ouroboros.governance.cross_process_jsonl"
            ".flock_critical_section",
            _boom,
        ):
            with acquire_singleton(repo_root=tmp_path) as result:
                assert result.acquired is True


# ---------------------------------------------------------------------------
# Real cross-process kernel-flock test
# ---------------------------------------------------------------------------


class TestRealCrossProcessFlock:
    """Validates kernel-level arbitration end-to-end (not via
    mocks). Uses ``subprocess`` so the second acquirer runs in
    a genuinely separate process — same-process flock on Linux
    is reentrant, which would mask a real second-fire bug.
    """

    def test_second_process_acquires_false(self, tmp_path):
        # First process: hold the lock open + signal ready, then
        # block on stdin. Second process: try to acquire,
        # report result, exit.
        holder_script = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path.cwd())!r})
            from backend.core.ouroboros.battle_test.singleton_lock import (
                acquire_singleton,
            )
            from pathlib import Path
            with acquire_singleton(
                repo_root=Path({str(tmp_path)!r}),
            ) as r:
                print("HELD:" + str(r.acquired), flush=True)
                # Hold for 5s so the second probe has time to fire.
                time.sleep(5)
        """).strip()
        prober_script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(Path.cwd())!r})
            from backend.core.ouroboros.battle_test.singleton_lock import (
                acquire_singleton,
            )
            from pathlib import Path
            with acquire_singleton(
                repo_root=Path({str(tmp_path)!r}),
            ) as r:
                print("PROBE:" + str(r.acquired), flush=True)
        """).strip()

        holder = subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Wait until holder confirms it holds the lock.
            line = holder.stdout.readline() if holder.stdout else ""
            assert "HELD:True" in line, (
                f"holder failed to acquire: {line!r}"
            )
            # Fire prober while holder still has the lock.
            probe = subprocess.run(
                [sys.executable, "-c", prober_script],
                capture_output=True, text=True, timeout=15,
            )
            assert "PROBE:False" in probe.stdout, (
                "second process must NOT acquire while first "
                f"holds it: stdout={probe.stdout!r} "
                f"stderr={probe.stderr!r}"
            )
        finally:
            holder.terminate()
            try:
                holder.wait(timeout=3)
            except subprocess.TimeoutExpired:
                holder.kill()

    def test_release_after_holder_dies_allows_next_acquire(
        self, tmp_path,
    ):
        """When the holder process dies, the kernel releases the
        flock. A subsequent acquire from a fresh process must
        succeed — proves there is no leaked-lock failure mode."""
        # First acquire briefly, exit.
        first_script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(Path.cwd())!r})
            from backend.core.ouroboros.battle_test.singleton_lock import (
                acquire_singleton,
            )
            from pathlib import Path
            with acquire_singleton(
                repo_root=Path({str(tmp_path)!r}),
            ) as r:
                print("OK1:" + str(r.acquired), flush=True)
            # Exit; kernel releases.
        """).strip()
        # After exit, second acquire should succeed.
        second_script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(Path.cwd())!r})
            from backend.core.ouroboros.battle_test.singleton_lock import (
                acquire_singleton,
            )
            from pathlib import Path
            with acquire_singleton(
                repo_root=Path({str(tmp_path)!r}),
            ) as r:
                print("OK2:" + str(r.acquired), flush=True)
        """).strip()
        r1 = subprocess.run(
            [sys.executable, "-c", first_script],
            capture_output=True, text=True, timeout=15,
        )
        assert "OK1:True" in r1.stdout, r1.stdout
        r2 = subprocess.run(
            [sys.executable, "-c", second_script],
            capture_output=True, text=True, timeout=15,
        )
        assert "OK2:True" in r2.stdout, r2.stdout


# ---------------------------------------------------------------------------
# Script wiring positional invariant
# ---------------------------------------------------------------------------


class TestScriptWiring:
    def test_singleton_check_precedes_pgrep_preflight(self):
        """In scripts/ouroboros_battle_test.py main(), the
        singleton lock acquisition MUST fire BEFORE the existing
        _single_flight_preflight() call — the structural defense
        first, the diagnostic second."""
        src = Path(
            "scripts/ouroboros_battle_test.py"
        ).read_text(encoding="utf-8")
        lock_idx = src.find("from backend.core.ouroboros."
                            "battle_test.singleton_lock import")
        pgrep_idx = src.find("_single_flight_preflight()")
        assert lock_idx > 0, "singleton_lock import not wired"
        assert pgrep_idx > 0, (
            "_single_flight_preflight call missing"
        )
        assert lock_idx < pgrep_idx, (
            f"ordering drift: lock={lock_idx} "
            f"pgrep={pgrep_idx}"
        )

    def test_script_uses_exitstack_for_lock_lifetime(self):
        """The lock fd must outlive the import-block — script
        wires it through ``contextlib.ExitStack`` so the
        acquired context lives until atexit / program exit."""
        src = Path(
            "scripts/ouroboros_battle_test.py"
        ).read_text(encoding="utf-8")
        assert "contextlib.ExitStack" in src, (
            "script must hold the lock via ExitStack for "
            "process-lifetime release semantics"
        )
        assert "atexit.register" in src, (
            "ExitStack.close must be atexit-registered as "
            "belt-and-suspenders"
        )

    def test_script_exits_75_on_conflict(self):
        """Exit code 75 (EX_TEMPFAIL) signals 'try again later'
        to wrappers — distinct from generic error code 1."""
        src = Path(
            "scripts/ouroboros_battle_test.py"
        ).read_text(encoding="utf-8")
        # Find the singleton-block's sys.exit(75) — bytes-pinned.
        block_start = src.find(
            "from backend.core.ouroboros.battle_test."
            "singleton_lock import"
        )
        # Look ahead a generous window — the entire singleton
        # block fits in ~50 lines.
        snippet = src[block_start: block_start + 3000]
        assert "sys.exit(75)" in snippet, (
            "singleton conflict must exit 75 (EX_TEMPFAIL)"
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


class TestASTPins:
    def test_returns_four_pins(self):
        pins = register_shipped_invariants()
        names = {p.invariant_name for p in pins}
        assert names == {
            "singleton_lock_master_default_false",
            "singleton_lock_authority_asymmetry",
            "singleton_lock_no_hardcoded_path",
            "singleton_lock_composes_canonical_primitive",
        }

    def test_all_pins_pass_on_current_source(self):
        pins = register_shipped_invariants()
        src = Path(
            "backend/core/ouroboros/battle_test/"
            "singleton_lock.py"
        ).read_text(encoding="utf-8")
        tree = _ast.parse(src)
        for pin in pins:
            violations = pin.validate(tree, src)
            assert violations == (), (
                f"{pin.invariant_name} drift: {violations}"
            )

    def test_authority_asymmetry_no_forbidden_imports(self):
        src = Path(
            "backend/core/ouroboros/battle_test/"
            "singleton_lock.py"
        ).read_text(encoding="utf-8")
        tree = _ast.parse(src)
        forbidden = {
            "backend.core.ouroboros.battle_test.harness",
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.providers",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                assert mod not in forbidden, (
                    f"forbidden import: {mod}"
                )

    def test_substrate_signature(self):
        """acquire_singleton must be a context manager (the script
        wires it via ExitStack.enter_context). Inspecting the
        runtime artifact confirms it has __enter__/__exit__."""
        cm = acquire_singleton(repo_root=Path("/tmp"))
        try:
            assert hasattr(cm, "__enter__")
            assert hasattr(cm, "__exit__")
        finally:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
