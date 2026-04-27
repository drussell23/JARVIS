"""Phase 7.8 — cross-process AdaptationLedger advisory locking pins.

Pinned cage:
  * fcntl.flock advisory file locks wrap append paths
  * Best-effort fallback (no-op) when fcntl unavailable (Windows)
  * Per-feature kill switch JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED
    defaults TRUE (security hardening on by default)
  * Helpers NEVER raise — fail-open semantics
  * Wired into AdaptationLedger._append at the file-handle level
  * Multi-process write contention serialized correctly (subprocess
    smoke test)
  * Authority invariants
"""
from __future__ import annotations

import multiprocessing
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import List
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation import (
    _file_lock as fl,
)
from backend.core.ouroboros.governance.adaptation._file_lock import (
    flock_exclusive,
    flock_shared,
    is_flock_enabled,
)


# ---------------------------------------------------------------------------
# Section A — module constants + kill switch
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_truthy_constant_shape(self):
        assert fl._TRUTHY == ("1", "true", "yes", "on")

    def test_module_exposes_helpers(self):
        # Pin: only intentional public surface.
        assert "flock_exclusive" in fl.__all__
        assert "flock_shared" in fl.__all__
        assert "is_flock_enabled" in fl.__all__


class TestKillSwitch:
    def test_default_true(self, monkeypatch):
        # Defense-in-depth defaults TRUE — same convention as
        # P7.7 Rule 7.
        monkeypatch.delenv(
            "JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED", raising=False,
        )
        assert is_flock_enabled() is True

    def test_explicit_true_variants(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv(
                "JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED", v,
            )
            assert is_flock_enabled() is True, v

    def test_explicit_false_variants(self, monkeypatch):
        for v in ("0", "false", "FALSE", "No", "OFF", "", "  "):
            monkeypatch.setenv(
                "JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED", v,
            )
            assert is_flock_enabled() is False, v


# ---------------------------------------------------------------------------
# Section B — happy path (POSIX)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl is POSIX-only — happy-path tests skip on Windows",
)
class TestHappyPathPosix:
    def test_flock_exclusive_acquires_and_releases(self, tmp_path):
        # Verify the context manager yields True and the lock is
        # released after exit.
        target = tmp_path / "data.jsonl"
        target.write_text("", encoding="utf-8")
        with target.open("a") as f:
            with flock_exclusive(f.fileno()) as acquired:
                assert acquired is True
                f.write("line1\n")
        # After release, a second exclusive lock should succeed
        # immediately.
        with target.open("a") as f:
            with flock_exclusive(f.fileno()) as acquired:
                assert acquired is True

    def test_flock_shared_acquires_and_releases(self, tmp_path):
        target = tmp_path / "data.jsonl"
        target.write_text("hello\n", encoding="utf-8")
        with target.open("r") as f:
            with flock_shared(f.fileno()) as acquired:
                assert acquired is True
                assert f.read() == "hello\n"

    def test_kill_switch_off_yields_true_no_op(
        self, monkeypatch, tmp_path,
    ):
        # When the kill switch is off, the helper is a no-op AND
        # yields True (so callers don't think the lock failed).
        monkeypatch.setenv(
            "JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED", "false",
        )
        target = tmp_path / "data.jsonl"
        target.write_text("", encoding="utf-8")
        with target.open("a") as f:
            with flock_exclusive(f.fileno()) as acquired:
                assert acquired is True
                f.write("kill-switch-off\n")

    def test_concurrent_exclusive_locks_serialize(self, tmp_path):
        # Two threads racing to append — flock should serialize them
        # within the SAME process too (POSIX flock is per-fd).
        # Note: this is harder to prove deterministically; we just
        # assert no exception + both writes land.
        import threading
        target = tmp_path / "data.jsonl"
        target.write_text("", encoding="utf-8")

        def writer(token):
            with target.open("a") as f:
                with flock_exclusive(f.fileno()):
                    f.write(f"{token}\n")
                    f.flush()
                    time.sleep(0.01)

        threads = [
            threading.Thread(target=writer, args=(f"t{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = target.read_text().splitlines()
        assert sorted(lines) == sorted(f"t{i}" for i in range(5))


# ---------------------------------------------------------------------------
# Section C — fail-open: fcntl unavailable
# ---------------------------------------------------------------------------


class TestFailOpenNoFcntl:
    def test_no_fcntl_yields_true(self, tmp_path, monkeypatch):
        # Simulate fcntl unavailability by temporarily nulling the
        # cached module reference.
        target = tmp_path / "data.jsonl"
        target.write_text("", encoding="utf-8")
        with mock.patch.object(fl, "_FCNTL", None):
            fl._reset_log_emitted_for_test()
            with target.open("a") as f:
                with flock_exclusive(f.fileno()) as acquired:
                    assert acquired is True
                    f.write("no-fcntl\n")

    def test_no_fcntl_log_only_emitted_once(self, monkeypatch, tmp_path):
        # The "fcntl unavailable" log should fire ONCE, not on every call.
        target = tmp_path / "data.jsonl"
        target.write_text("", encoding="utf-8")
        with mock.patch.object(fl, "_FCNTL", None):
            fl._reset_log_emitted_for_test()
            with mock.patch.object(fl.logger, "info") as mock_info:
                with target.open("a") as f:
                    with flock_exclusive(f.fileno()):
                        pass
                with target.open("a") as f:
                    with flock_exclusive(f.fileno()):
                        pass
                with target.open("a") as f:
                    with flock_exclusive(f.fileno()):
                        pass
                # Should have logged exactly once.
                assert mock_info.call_count == 1


# ---------------------------------------------------------------------------
# Section D — fail-open: flock raises
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl is POSIX-only",
)
class TestFailOpenFlockRaises:
    def test_flock_raises_yields_false_no_exception(self, tmp_path):
        # Simulate a flock() OSError (e.g. NFS / unsupported FS).
        target = tmp_path / "data.jsonl"
        target.write_text("", encoding="utf-8")

        class _RaisingFcntl:
            LOCK_EX = 0
            LOCK_SH = 0
            LOCK_UN = 0

            @staticmethod
            def flock(fd, op):
                raise OSError("ENOTSUP: filesystem does not support locks")

        with mock.patch.object(fl, "_FCNTL", _RaisingFcntl()):
            with target.open("a") as f:
                with flock_exclusive(f.fileno()) as acquired:
                    assert acquired is False
                    # Caller should still be able to write — fail-open.
                    f.write("flock-raised\n")
        # File should still have the write.
        assert "flock-raised" in target.read_text()

    def test_release_failure_logged_not_raised(self, tmp_path):
        # If flock(LOCK_UN) raises on context exit, we should log but
        # NOT propagate.
        target = tmp_path / "data.jsonl"
        target.write_text("", encoding="utf-8")
        call_log: List[int] = []

        class _ReleaseFailFcntl:
            LOCK_EX = 0
            LOCK_SH = 0
            LOCK_UN = 99

            @staticmethod
            def flock(fd, op):
                if op == 99:  # LOCK_UN
                    raise OSError("release failed")
                call_log.append(op)

        with mock.patch.object(fl, "_FCNTL", _ReleaseFailFcntl()):
            with target.open("a") as f:
                # No exception should propagate from the context exit.
                with flock_exclusive(f.fileno()) as acquired:
                    assert acquired is True
                    f.write("acquired-but-release-fails\n")
        # Acquire was called.
        assert 0 in call_log


# ---------------------------------------------------------------------------
# Section E — AdaptationLedger integration
# ---------------------------------------------------------------------------


class TestLedgerIntegration:
    def test_ledger_append_imports_flock(self, monkeypatch, tmp_path):
        # Pin: ledger.py _append calls flock_exclusive. Verify by
        # patching flock_exclusive and confirming it's invoked.
        from backend.core.ouroboros.governance.adaptation import ledger
        from backend.core.ouroboros.governance.adaptation.ledger import (
            AdaptationEvidence,
            AdaptationLedger,
            AdaptationSurface,
        )

        monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
        monkeypatch.setenv(
            "JARVIS_ADAPTATION_LEDGER_PATH",
            str(tmp_path / "ledger.jsonl"),
        )

        invoked = {"count": 0}

        import contextlib

        @contextlib.contextmanager
        def fake_flock(fd):
            invoked["count"] += 1
            yield True

        with mock.patch(
            "backend.core.ouroboros.governance.adaptation._file_lock"
            ".flock_exclusive",
            side_effect=fake_flock,
        ):
            adapter = AdaptationLedger(path=tmp_path / "ledger.jsonl")
            evidence = AdaptationEvidence(
                window_days=7,
                observation_count=5,
                source_event_ids=("e1",),
                summary="test → test",
            )
            result = adapter.propose(
                proposal_id="adapt-test-7.8",
                surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
                proposal_kind="add_pattern",
                evidence=evidence,
                current_state_hash="sha256:abc",
                proposed_state_hash="sha256:def",
            )
            # Append must have happened (status OK or DUPLICATE),
            # and our fake flock must have been called.
            assert invoked["count"] >= 1, (
                f"flock_exclusive not invoked; result={result}"
            )

    def test_ledger_append_works_with_kill_switch_off(
        self, monkeypatch, tmp_path,
    ):
        # Kill switch off → flock no-op → ledger still functional.
        from backend.core.ouroboros.governance.adaptation.ledger import (
            AdaptationEvidence,
            AdaptationLedger,
            AdaptationSurface,
        )

        monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
        monkeypatch.setenv(
            "JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED", "false",
        )
        ledger_path = tmp_path / "ledger.jsonl"
        adapter = AdaptationLedger(path=ledger_path)
        evidence = AdaptationEvidence(
            window_days=7,
            observation_count=5,
            source_event_ids=("e1",),
            summary="kill switch off → test",
        )
        result = adapter.propose(
            proposal_id="adapt-test-7.8-killoff",
            surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
            proposal_kind="add_pattern",
            evidence=evidence,
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
        )
        # The proposal should land regardless.
        from backend.core.ouroboros.governance.adaptation.ledger import (
            ProposeStatus,
        )
        assert result.status in (
            ProposeStatus.OK, ProposeStatus.DUPLICATE_PROPOSAL_ID,
        ), f"unexpected status: {result.status} detail={result.detail}"
        assert ledger_path.exists()


# ---------------------------------------------------------------------------
# Section F — multiprocess contention (smoke test)
# ---------------------------------------------------------------------------


def _writer_child(args):
    """Child process: open the file, take exclusive lock, write a
    blob, sleep briefly, release."""
    path, token = args
    sys.path.insert(
        0,
        str(Path(__file__).resolve().parents[2]),
    )
    from backend.core.ouroboros.governance.adaptation._file_lock import (
        flock_exclusive,
    )
    with open(path, "a", encoding="utf-8") as f:
        with flock_exclusive(f.fileno()):
            for i in range(10):
                f.write(f"{token}-{i}\n")
                f.flush()
                # Small sleep inside the lock to give other
                # processes a chance to interleave (which they
                # MUST NOT — flock should serialize them).
                time.sleep(0.005)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="multiprocess flock test is POSIX-only",
)
class TestMultiprocessContention:
    def test_concurrent_writers_serialized(self, tmp_path):
        """Spawn N child processes all racing to write 10 lines.
        Without flock, lines from different processes would
        interleave; with flock, each process's 10-line block
        appears contiguously."""
        target = tmp_path / "concurrent.jsonl"
        target.write_text("", encoding="utf-8")

        with multiprocessing.Pool(processes=3) as pool:
            pool.map(
                _writer_child,
                [(str(target), f"P{i}") for i in range(3)],
            )

        lines = target.read_text().splitlines()
        # Each process wrote 10 lines → 30 total.
        assert len(lines) == 30, (
            f"expected 30 lines; got {len(lines)}: {lines!r}"
        )
        # Each process's lines must appear as a contiguous block
        # (flock-serialized). Check by walking the lines and
        # ensuring no process's run is interrupted.
        seen_processes = set()
        i = 0
        while i < len(lines):
            this_proc = lines[i].split("-")[0]
            seen_processes.add(this_proc)
            # Walk to end of this process's run.
            j = i
            while (
                j < len(lines)
                and lines[j].split("-")[0] == this_proc
            ):
                j += 1
            # The run length should be exactly 10.
            assert (j - i) == 10, (
                f"process {this_proc} run length = {j - i} (expected 10) "
                f"— flock did NOT serialize? lines={lines!r}"
            )
            i = j
        assert len(seen_processes) == 3


# ---------------------------------------------------------------------------
# Section G — authority invariants
# ---------------------------------------------------------------------------


_FL_PATH = Path(fl.__file__)


class TestAuthorityInvariants:
    def test_no_banned_governance_imports(self):
        import ast
        source = _FL_PATH.read_text()
        tree = ast.parse(source)
        banned_substrings = (
            "ledger",
            "scoped_tool_backend",
            "general_driver",
            "exploration_engine",
            "semantic_guardian",
            "orchestrator",
            "tool_executor",
            "phase_runners",
            "gate_runner",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for banned in banned_substrings:
                    assert banned not in node.module, (
                        f"banned import: {node.module}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in banned_substrings:
                        assert banned not in alias.name, (
                            f"banned import: {alias.name}"
                        )

    def test_only_stdlib(self):
        import ast
        source = _FL_PATH.read_text()
        tree = ast.parse(source)
        stdlib_prefixes = (
            "__future__", "contextlib", "logging", "os", "typing",
            "fcntl",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("backend."):
                    pytest.fail(
                        f"unexpected backend import: {node.module}"
                    )
                else:
                    assert any(
                        node.module.startswith(p) for p in stdlib_prefixes
                    ), f"unexpected import: {node.module}"

    def test_no_subprocess_or_network_tokens(self):
        source = _FL_PATH.read_text()
        for token in (
            "subprocess", "requests", "urllib", "socket",
            "http.client", "asyncio.create_subprocess",
        ):
            assert token not in source, f"banned token: {token}"
