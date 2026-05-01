"""Tier 1 #3 — Cross-process JSONL append helper regression spine.

Coverage tracks:

  * Env knobs — defaults, floors, garbage tolerance
  * fcntl detection — graceful degrade
  * flock_append_line / flock_append_lines — basic append + idempotent
    parent mkdir + lock-file creation + multiple processes
  * flock_critical_section — read-modify-write contract
  * Multi-process stress — N processes append M lines each; total
    count + line integrity preserved (no interleaving)
  * Wire-up pins — auto_action_router + invariant_drift_store source-
    token grep verifies the helper is actually called
  * Authority invariants — AST-pinned (stdlib only, no governance)
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path
from typing import List, Optional

import pytest

from backend.core.ouroboros.governance import (
    cross_process_jsonl as cpj,
)
from backend.core.ouroboros.governance.cross_process_jsonl import (
    CROSS_PROCESS_JSONL_SCHEMA_VERSION,
    fcntl_available,
    flock_append_line,
    flock_append_lines,
    flock_critical_section,
    lock_timeout_s,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_jsonl(tmp_path) -> Path:
    return tmp_path / "test.jsonl"


# ---------------------------------------------------------------------------
# 1. Env knobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_lock_timeout_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CROSS_PROCESS_LOCK_TIMEOUT_S", raising=False,
        )
        assert lock_timeout_s() == 5.0

    def test_lock_timeout_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CROSS_PROCESS_LOCK_TIMEOUT_S", "0.001",
        )
        assert lock_timeout_s() == 0.1  # floor

    def test_lock_timeout_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CROSS_PROCESS_LOCK_TIMEOUT_S", "garbage",
        )
        assert lock_timeout_s() == 5.0

    def test_lock_timeout_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CROSS_PROCESS_LOCK_TIMEOUT_S", "30.0",
        )
        assert lock_timeout_s() == 30.0

    def test_schema_version_pinned(self):
        assert CROSS_PROCESS_JSONL_SCHEMA_VERSION == \
            "cross_process_jsonl.1"


# ---------------------------------------------------------------------------
# 2. fcntl availability
# ---------------------------------------------------------------------------


class TestFcntlDetection:
    def test_fcntl_available_returns_bool(self):
        result = fcntl_available()
        assert isinstance(result, bool)

    def test_fcntl_available_true_on_posix(self):
        # On macOS/Linux this MUST be True (CI target)
        if sys.platform.startswith(("linux", "darwin")):
            assert fcntl_available() is True


# ---------------------------------------------------------------------------
# 3. flock_append_line — basic semantics
# ---------------------------------------------------------------------------


class TestFlockAppendLine:
    def test_append_creates_file(self, tmp_jsonl):
        result = flock_append_line(tmp_jsonl, '{"a": 1}')
        assert result is True
        assert tmp_jsonl.exists()
        assert tmp_jsonl.read_text() == '{"a": 1}\n'

    def test_append_extends_existing_file(self, tmp_jsonl):
        flock_append_line(tmp_jsonl, '{"a": 1}')
        flock_append_line(tmp_jsonl, '{"b": 2}')
        flock_append_line(tmp_jsonl, '{"c": 3}')
        content = tmp_jsonl.read_text()
        assert content == '{"a": 1}\n{"b": 2}\n{"c": 3}\n'

    def test_append_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "test.jsonl"
        result = flock_append_line(nested, '{"x": 1}')
        assert result is True
        assert nested.exists()

    def test_append_creates_lock_file(self, tmp_jsonl):
        flock_append_line(tmp_jsonl, '{"a": 1}')
        # Lock file is sibling with .lock appended to suffix
        lock_file = tmp_jsonl.with_suffix(
            tmp_jsonl.suffix + ".lock",
        )
        assert lock_file.exists()

    def test_append_returns_false_on_unwritable_parent(
        self, tmp_path,
    ):
        # Create a read-only parent
        ro = tmp_path / "readonly"
        ro.mkdir()
        target = ro / "subdir" / "test.jsonl"
        os.chmod(ro, 0o444)
        try:
            result = flock_append_line(target, '{"x": 1}')
            assert result is False
        finally:
            os.chmod(ro, 0o755)  # restore for tmp cleanup

    def test_append_never_raises_on_garbage_input(
        self, tmp_jsonl,
    ):
        # Coerces non-string defensively
        result = flock_append_line(tmp_jsonl, 12345)  # type: ignore[arg-type]
        assert result is True
        assert "12345" in tmp_jsonl.read_text()


# ---------------------------------------------------------------------------
# 4. flock_append_lines — batch atomic append
# ---------------------------------------------------------------------------


class TestFlockAppendLines:
    def test_batch_append(self, tmp_jsonl):
        lines = ['{"a": 1}', '{"b": 2}', '{"c": 3}']
        result = flock_append_lines(tmp_jsonl, lines)
        assert result is True
        content = tmp_jsonl.read_text().strip().split("\n")
        assert content == lines

    def test_empty_batch_succeeds_no_op(self, tmp_jsonl):
        # Edge case: no lines
        result = flock_append_lines(tmp_jsonl, [])
        assert result is True
        # File created (open in 'a' mode), but empty
        assert tmp_jsonl.exists()
        assert tmp_jsonl.read_text() == ""

    def test_batch_under_one_lock_acquire(
        self, tmp_jsonl, monkeypatch,
    ):
        # Track lock acquisitions — batch should only acquire once
        # for the whole batch, not once per line.
        from backend.core.ouroboros.governance import (
            cross_process_jsonl as cpj_mod,
        )
        original = cpj_mod._acquire_cross_process_lock
        acquire_count = [0]

        from contextlib import contextmanager

        @contextmanager
        def counting(*a, **kw):
            acquire_count[0] += 1
            with original(*a, **kw) as got:
                yield got

        monkeypatch.setattr(
            cpj_mod, "_acquire_cross_process_lock", counting,
        )
        flock_append_lines(
            tmp_jsonl, ['{"a": 1}', '{"b": 2}', '{"c": 3}'],
        )
        assert acquire_count[0] == 1


# ---------------------------------------------------------------------------
# 5. flock_critical_section — read-modify-write contract
# ---------------------------------------------------------------------------


class TestCriticalSection:
    def test_yields_true_on_success(self, tmp_jsonl):
        with flock_critical_section(tmp_jsonl) as acquired:
            assert acquired is True
            tmp_jsonl.write_text("inside\n")
        assert tmp_jsonl.read_text() == "inside\n"

    def test_release_on_exit(self, tmp_jsonl):
        # First acquire-release succeeds
        with flock_critical_section(tmp_jsonl) as a1:
            assert a1 is True
        # Second acquire after release succeeds
        with flock_critical_section(tmp_jsonl) as a2:
            assert a2 is True

    def test_release_on_exception(self, tmp_jsonl):
        # Even when caller raises inside the block, lock releases
        try:
            with flock_critical_section(tmp_jsonl) as acquired:
                assert acquired is True
                raise RuntimeError("simulated caller error")
        except RuntimeError:
            pass
        # Subsequent acquire should succeed (lock was released)
        with flock_critical_section(tmp_jsonl) as a2:
            assert a2 is True


# ---------------------------------------------------------------------------
# 6. In-process serialization — threading.Lock layer
# ---------------------------------------------------------------------------


class TestInProcessSerialization:
    def test_concurrent_threads_no_interleave(self, tmp_jsonl):
        """8 threads × 50 appends each — total 400 lines, no
        interleaving. The threading.Lock layer alone proves
        within-process correctness."""
        errors: List[Exception] = []

        def worker(idx: int):
            try:
                for i in range(50):
                    flock_append_line(
                        tmp_jsonl, f'{{"thread": {idx}, "i": {i}}}',
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        lines = tmp_jsonl.read_text().strip().split("\n")
        assert len(lines) == 400
        # Each line must be parseable JSON (no torn writes)
        import json
        for ln in lines:
            parsed = json.loads(ln)
            assert "thread" in parsed and "i" in parsed


# ---------------------------------------------------------------------------
# 7. Multi-process stress — proves cross-process atomicity
# ---------------------------------------------------------------------------


class TestMultiProcessStress:
    def test_concurrent_processes_no_interleave(self, tmp_path):
        """4 child processes × 25 appends each — 100 lines total.
        Without flock, append-mode has no cross-process atomicity
        guarantee on every system (some kernels do, some don't,
        especially on network filesystems). With flock, the helper
        guarantees serialization."""
        target = tmp_path / "stress.jsonl"

        worker_script = textwrap.dedent(f"""
            import sys, json
            sys.path.insert(0, {repr(str(Path.cwd()))})
            from pathlib import Path
            from backend.core.ouroboros.governance.cross_process_jsonl import (
                flock_append_line,
            )
            target = Path({repr(str(target))})
            pid = int(sys.argv[1])
            for i in range(25):
                flock_append_line(
                    target,
                    json.dumps({{"pid": pid, "i": i, "blob": "x" * 200}}),
                )
        """).strip()

        worker_path = tmp_path / "worker.py"
        worker_path.write_text(worker_script)

        procs = []
        for pid in range(4):
            p = subprocess.Popen(
                [sys.executable, str(worker_path), str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            procs.append(p)
        for p in procs:
            p.wait(timeout=30)
            assert p.returncode == 0, (
                f"worker failed: {p.stderr.read().decode()[:500]}"
            )

        lines = target.read_text().strip().split("\n")
        assert len(lines) == 100, (
            f"expected 100 lines, got {len(lines)}"
        )
        # Every line must parse (no interleaved partial writes)
        import json
        for ln in lines:
            parsed = json.loads(ln)
            assert "pid" in parsed
            assert "i" in parsed


# ---------------------------------------------------------------------------
# 8. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


_FORBIDDEN_GOVERNANCE_SUBSTRINGS = (
    # Helper is stdlib-only — must NOT import any governance module.
    "orchestrator",
    "phase_runners",
    "candidate_generator",
    "iron_gate",
    "change_engine",
    "policy",
    "semantic_guardian",
    "semantic_firewall",
    "providers",
    "doubleword_provider",
    "urgency_router",
    "auto_action_router",
    "subagent_scheduler",
    "invariant_drift",
    "posture_observer",
    "posture_health",
    "ide_observability",
    "confidence_",
)


def _module_path() -> Path:
    here = Path(__file__).resolve()
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            return (
                cur / "backend" / "core" / "ouroboros"
                / "governance" / "cross_process_jsonl.py"
            )
        cur = cur.parent
    raise RuntimeError("repo root not found")


class TestAuthorityInvariants:
    def test_no_governance_imports(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(
                        "backend.core.ouroboros.governance",
                    ):
                        offenders.append(alias.name)
                    for fb in _FORBIDDEN_GOVERNANCE_SUBSTRINGS:
                        if fb in alias.name:
                            offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.startswith(
                    "backend.core.ouroboros.governance",
                ):
                    offenders.append(mod)
        assert offenders == [], (
            f"cross_process_jsonl imports forbidden modules: "
            f"{offenders}"
        )

    def test_uses_fcntl_for_cross_process_lock(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        # Pin the structural shape: helper MUST use fcntl.flock for
        # cross-process safety. A refactor that drops fcntl would
        # silently revert to single-process-only correctness.
        assert "fcntl" in source
        assert "LOCK_EX" in source
        assert "LOCK_UN" in source

    def test_public_api_exported(self):
        expected = {
            "CROSS_PROCESS_JSONL_SCHEMA_VERSION",
            "fcntl_available",
            "flock_append_line",
            "flock_append_lines",
            "flock_critical_section",
            "lock_timeout_s",
        }
        assert set(cpj.__all__) == expected

    def test_auto_action_router_uses_helper(self):
        # Pin the wire-up site so a refactor doesn't silently drop
        # the cross-process flock from the proposal ledger.
        path = _module_path().parent / "auto_action_router.py"
        source = path.read_text(encoding="utf-8")
        assert "flock_append_line" in source, (
            "auto_action_router.AutoActionProposalLedger.append "
            "must use flock_append_line — wire-up dropped"
        )
        assert "Tier 1 #3" in source, (
            "auto_action_router must mark wiring with slice "
            "comment for traceability"
        )

    def test_invariant_drift_store_uses_helper(self):
        path = (
            _module_path().parent / "invariant_drift_store.py"
        )
        source = path.read_text(encoding="utf-8")
        assert "flock_critical_section" in source, (
            "invariant_drift_store.append_history must use "
            "flock_critical_section — wire-up dropped"
        )
        assert "flock_append_line" in source, (
            "invariant_drift_store.append_audit must use "
            "flock_append_line — wire-up dropped"
        )
        assert "Tier 1 #3" in source, (
            "invariant_drift_store must mark wiring with slice "
            "comment for traceability"
        )
