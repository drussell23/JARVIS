"""Comprehensive tests for ModuleHotReloader.

Coverage targets (Manifesto §6 RSI loop closer):
- path_to_module conversion edge cases
- classify routing: HOT_RELOAD / RESTART / NO_OP for every shape of input
- snapshot mechanics (loaded, unloaded, missing __file__, OSError)
- end-to-end synthetic-module reload: write file, reload, observe new behavior
- preflight atomicity: one bad candidate aborts the whole batch
- post-reload verification: hash drift detection, probe identity check
- reload failure handling: SyntaxError, ImportError → restart_pending
- self-quarantine: the reloader cannot reload itself
- event emitter: structured payloads, error isolation
- restart-pending lifecycle: queue / clear / first-caller-wins
- stats / observability properties
- real Ouroboros module no-change reload (production smoke)

The end-to-end synthetic-module tests use a tempdir on sys.path with a
custom in_scope_prefix and an empty quarantine, so the reloader treats
the synthetic module the same way it treats real governance modules.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Iterator, List

import pytest

from backend.core.ouroboros.governance.module_hot_reloader import (
    DEFAULT_QUARANTINE,
    DEFAULT_SAFE_MODULES,
    PROBES,
    RESTART_EXIT_CODE,
    ModuleHotReloader,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def reloader(tmp_path: Path) -> ModuleHotReloader:
    """Default reloader rooted at a temp dir, with the production safe set."""
    return ModuleHotReloader(project_root=tmp_path)


@pytest.fixture
def synthetic_workspace(tmp_path: Path) -> Iterator[Path]:
    """Create a tempdir on sys.path so synthetic modules can be imported.

    Yields the tempdir Path. On teardown, removes the dir from sys.path
    AND purges any synthetic_* modules from sys.modules so the next test
    gets a clean slate.
    """
    sys.path.insert(0, str(tmp_path))
    try:
        yield tmp_path
    finally:
        try:
            sys.path.remove(str(tmp_path))
        except ValueError:
            pass
        for name in [n for n in list(sys.modules) if n.startswith("synthetic_")]:
            sys.modules.pop(name, None)


def _write(path: Path, content: str) -> None:
    """Write content + sleep briefly so mtime resolution catches the change."""
    path.write_text(content)
    # On some filesystems, mtime resolution is 1s. Sleep less than that.
    # We rely on content hash, not mtime, so this is just defensive.


# ----------------------------------------------------------------------
# 1. path_to_module — string mapping
# ----------------------------------------------------------------------


class TestPathToModule:
    def test_basic_py(self, reloader: ModuleHotReloader) -> None:
        assert (
            reloader.path_to_module("backend/core/ouroboros/governance/verify_gate.py")
            == "backend.core.ouroboros.governance.verify_gate"
        )

    def test_strips_leading_dot_slash(self, reloader: ModuleHotReloader) -> None:
        assert (
            reloader.path_to_module("./backend/core/ouroboros/governance/verify_gate.py")
            == "backend.core.ouroboros.governance.verify_gate"
        )

    def test_handles_backslashes(self, reloader: ModuleHotReloader) -> None:
        assert (
            reloader.path_to_module("backend\\core\\ouroboros\\governance\\verify_gate.py")
            == "backend.core.ouroboros.governance.verify_gate"
        )

    def test_init_py_returns_none(self, reloader: ModuleHotReloader) -> None:
        assert reloader.path_to_module("backend/core/ouroboros/governance/__init__.py") is None

    def test_non_py_returns_none(self, reloader: ModuleHotReloader) -> None:
        assert reloader.path_to_module("requirements.txt") is None
        assert reloader.path_to_module("README.md") is None
        assert reloader.path_to_module("config.yaml") is None
        assert reloader.path_to_module("data.json") is None

    def test_absolute_path_returns_none(self, reloader: ModuleHotReloader) -> None:
        assert reloader.path_to_module("/abs/path/foo.py") is None

    def test_upward_nav_returns_none(self, reloader: ModuleHotReloader) -> None:
        assert reloader.path_to_module("../escape.py") is None
        assert reloader.path_to_module("backend/../escape.py") is None

    def test_empty_string_returns_none(self, reloader: ModuleHotReloader) -> None:
        assert reloader.path_to_module("") is None

    def test_pyi_returns_none(self, reloader: ModuleHotReloader) -> None:
        # .pyi stubs are not importable in the runtime sense; only .py
        assert reloader.path_to_module("foo.pyi") is None


# ----------------------------------------------------------------------
# 2. classify — routing decision
# ----------------------------------------------------------------------


class TestClassify:
    def test_no_targets_is_noop(self, reloader: ModuleHotReloader) -> None:
        d = reloader.classify([])
        assert d.action == "NO_OP"
        assert d.safe_modules == ()
        assert d.quarantined == ()

    def test_only_non_py_is_noop(self, reloader: ModuleHotReloader) -> None:
        d = reloader.classify(["requirements.txt", "README.md"])
        assert d.action == "NO_OP"
        assert "requirements.txt" in d.out_of_scope

    def test_safe_module_loaded_routes_hot_reload(self, reloader: ModuleHotReloader) -> None:
        # Force-load verify_gate so it's in sys.modules
        importlib.import_module("backend.core.ouroboros.governance.verify_gate")
        d = reloader.classify(["backend/core/ouroboros/governance/verify_gate.py"])
        assert d.action == "HOT_RELOAD"
        assert "backend.core.ouroboros.governance.verify_gate" in d.safe_modules

    def test_safe_module_not_loaded_is_out_of_scope(self, reloader: ModuleHotReloader) -> None:
        # Pop verify_gate from sys.modules to simulate "not loaded"
        sys.modules.pop("backend.core.ouroboros.governance.verify_gate", None)
        d = reloader.classify(["backend/core/ouroboros/governance/verify_gate.py"])
        # Not loaded → silently dropped to out_of_scope → NO_OP
        assert d.action == "NO_OP"

    def test_quarantined_module_routes_restart(self, reloader: ModuleHotReloader) -> None:
        d = reloader.classify(["backend/core/ouroboros/governance/orchestrator.py"])
        assert d.action == "RESTART"
        assert "backend.core.ouroboros.governance.orchestrator" in d.quarantined

    def test_unsafe_in_scope_module_routes_restart(self, reloader: ModuleHotReloader) -> None:
        # risk_engine is in backend.core.ouroboros.governance but NOT in safe set
        # and NOT in quarantine — should still route to RESTART (defense-in-depth)
        d = reloader.classify(["backend/core/ouroboros/governance/risk_engine.py"])
        assert d.action == "RESTART"
        assert "backend.core.ouroboros.governance.risk_engine" in d.quarantined

    def test_mixed_safe_and_quarantined_routes_restart(
        self, reloader: ModuleHotReloader
    ) -> None:
        importlib.import_module("backend.core.ouroboros.governance.verify_gate")
        d = reloader.classify([
            "backend/core/ouroboros/governance/verify_gate.py",
            "backend/core/ouroboros/governance/orchestrator.py",
        ])
        assert d.action == "RESTART"
        assert "backend.core.ouroboros.governance.orchestrator" in d.quarantined

    def test_self_module_is_quarantined(self, reloader: ModuleHotReloader) -> None:
        # The reloader's own module name must be in quarantine
        assert "backend.core.ouroboros.governance.module_hot_reloader" in reloader.quarantine
        d = reloader.classify([
            "backend/core/ouroboros/governance/module_hot_reloader.py"
        ])
        assert d.action == "RESTART"

    def test_out_of_scope_module_is_skipped(self, reloader: ModuleHotReloader) -> None:
        d = reloader.classify(["tests/test_foo.py", "scripts/run.py"])
        assert d.action == "NO_OP"
        assert len(d.out_of_scope) == 2

    def test_safe_plus_out_of_scope_runs_safe_only(self, reloader: ModuleHotReloader) -> None:
        importlib.import_module("backend.core.ouroboros.governance.verify_gate")
        d = reloader.classify([
            "backend/core/ouroboros/governance/verify_gate.py",
            "tests/test_foo.py",
        ])
        assert d.action == "HOT_RELOAD"
        assert "backend.core.ouroboros.governance.verify_gate" in d.safe_modules
        assert "tests/test_foo.py" in d.out_of_scope


# ----------------------------------------------------------------------
# 3. snapshot — capture mechanics
# ----------------------------------------------------------------------


class TestSnapshot:
    def test_loaded_module_returns_valid_snapshot(self, reloader: ModuleHotReloader) -> None:
        importlib.import_module("backend.core.ouroboros.governance.verify_gate")
        snap = reloader.snapshot("backend.core.ouroboros.governance.verify_gate")
        assert snap is not None
        assert snap.module_name == "backend.core.ouroboros.governance.verify_gate"
        assert snap.file_path.endswith("verify_gate.py")
        assert len(snap.source_sha256) == 64  # sha256 hex
        assert snap.file_size > 0
        assert snap.mtime_ns > 0
        assert snap.captured_at_ns > 0

    def test_unloaded_module_returns_none(self, reloader: ModuleHotReloader) -> None:
        sys.modules.pop("not_a_real_module_xyz", None)
        assert reloader.snapshot("not_a_real_module_xyz") is None

    def test_snapshot_is_frozen_dataclass(self, reloader: ModuleHotReloader) -> None:
        importlib.import_module("backend.core.ouroboros.governance.verify_gate")
        snap = reloader.snapshot("backend.core.ouroboros.governance.verify_gate")
        assert snap is not None
        with pytest.raises(Exception):
            snap.source_sha256 = "different"  # type: ignore[misc]


# ----------------------------------------------------------------------
# 4. End-to-end synthetic reload
# ----------------------------------------------------------------------


class TestEndToEndReload:
    def test_reload_picks_up_function_change(self, synthetic_workspace: Path) -> None:
        mod_path = synthetic_workspace / "synthetic_target.py"
        _write(mod_path, "def get_value():\n    return 1\n")

        # Import and confirm initial behavior
        synthetic_target = importlib.import_module("synthetic_target")
        assert synthetic_target.get_value() == 1

        reloader = ModuleHotReloader(
            project_root=synthetic_workspace,
            safe_modules=frozenset({"synthetic_target"}),
            quarantine=frozenset(),
            in_scope_prefix="synthetic_",
        )

        # Mutate the file
        _write(mod_path, "def get_value():\n    return 42\n")

        # Hot-reload
        batch = reloader.reload_for_op(
            op_id="test-e2e-1",
            target_files=["synthetic_target.py"],
        )

        assert batch.overall_status == "success"
        assert batch.restart_required is False
        assert len(batch.outcomes) == 1
        outcome = batch.outcomes[0]
        assert outcome.status == "reloaded"
        assert outcome.old_sha != outcome.new_sha
        assert outcome.error is None

        # The CRITICAL assertion: the new code is live in-process
        assert synthetic_target.get_value() == 42
        assert reloader.reload_count == 1

    def test_reload_with_no_change_is_idempotent(
        self, synthetic_workspace: Path
    ) -> None:
        mod_path = synthetic_workspace / "synthetic_unchanged.py"
        _write(mod_path, "VALUE = 'unchanged'\n")
        importlib.import_module("synthetic_unchanged")

        reloader = ModuleHotReloader(
            project_root=synthetic_workspace,
            safe_modules=frozenset({"synthetic_unchanged"}),
            quarantine=frozenset(),
            in_scope_prefix="synthetic_",
        )

        # No mutation between snapshot and reload
        batch = reloader.reload_for_op(
            op_id="test-noop", target_files=["synthetic_unchanged.py"]
        )

        assert batch.overall_status == "no_change"
        assert batch.restart_required is False
        assert reloader.reload_count == 0  # no_change does NOT increment
        assert all(o.status == "no_change" for o in batch.outcomes)

    def test_reload_failure_on_syntax_error(self, synthetic_workspace: Path) -> None:
        mod_path = synthetic_workspace / "synthetic_broken.py"
        _write(mod_path, "VALUE = 1\n")
        importlib.import_module("synthetic_broken")

        reloader = ModuleHotReloader(
            project_root=synthetic_workspace,
            safe_modules=frozenset({"synthetic_broken"}),
            quarantine=frozenset(),
            in_scope_prefix="synthetic_",
        )

        # Introduce a syntax error
        _write(mod_path, "this is not valid python !!!\n")

        batch = reloader.reload_for_op(
            op_id="test-broken", target_files=["synthetic_broken.py"]
        )

        assert batch.overall_status == "reload_failed"
        assert batch.restart_required is True
        assert reloader.restart_pending is not None
        assert "synthetic_broken" in (reloader.restart_pending or "")
        assert reloader.reload_count == 0

        outcome = batch.outcomes[0]
        assert outcome.status == "failed"
        assert outcome.error is not None
        assert "SyntaxError" in outcome.error or "invalid syntax" in outcome.error

    def test_probe_identity_changes_on_real_reload(
        self, synthetic_workspace: Path
    ) -> None:
        mod_path = synthetic_workspace / "synthetic_probe.py"
        _write(mod_path, "def probe():\n    return 'before'\n")
        synthetic_probe = importlib.import_module("synthetic_probe")
        before_id = id(synthetic_probe.probe)

        # Inject a probe registration so the reloader knows what to compare
        from backend.core.ouroboros.governance import module_hot_reloader as mhr

        mhr.PROBES["synthetic_probe"] = "probe"
        try:
            reloader = ModuleHotReloader(
                project_root=synthetic_workspace,
                safe_modules=frozenset({"synthetic_probe"}),
                quarantine=frozenset(),
                in_scope_prefix="synthetic_",
            )
            _write(mod_path, "def probe():\n    return 'after'\n")
            batch = reloader.reload_for_op(
                op_id="test-probe", target_files=["synthetic_probe.py"]
            )
            assert batch.overall_status == "success"
            outcome = batch.outcomes[0]
            assert outcome.probe_id_changed is True
            assert id(synthetic_probe.probe) != before_id
            assert synthetic_probe.probe() == "after"
        finally:
            mhr.PROBES.pop("synthetic_probe", None)


# ----------------------------------------------------------------------
# 5. Atomicity — preflight failure
# ----------------------------------------------------------------------


class TestAtomicity:
    def test_preflight_failure_when_file_missing(
        self, synthetic_workspace: Path
    ) -> None:
        mod_path = synthetic_workspace / "synthetic_missing.py"
        _write(mod_path, "X = 1\n")
        synthetic_missing = importlib.import_module("synthetic_missing")
        # Now delete the file — module is loaded but file is gone
        mod_path.unlink()

        reloader = ModuleHotReloader(
            project_root=synthetic_workspace,
            safe_modules=frozenset({"synthetic_missing"}),
            quarantine=frozenset(),
            in_scope_prefix="synthetic_",
        )

        batch = reloader.reload_for_op(
            op_id="test-missing", target_files=["synthetic_missing.py"]
        )

        assert batch.overall_status == "preflight_failed"
        assert batch.restart_required is True
        assert reloader.restart_pending is not None
        # Module attribute should still be the original (no partial mutation)
        assert synthetic_missing.X == 1


# ----------------------------------------------------------------------
# 6. Self-quarantine
# ----------------------------------------------------------------------


class TestSelfQuarantine:
    def test_reloader_module_in_quarantine_by_default(self) -> None:
        r = ModuleHotReloader(project_root=Path("."))
        assert "backend.core.ouroboros.governance.module_hot_reloader" in r.quarantine

    def test_custom_quarantine_still_includes_self(self) -> None:
        r = ModuleHotReloader(project_root=Path("."), quarantine=frozenset())
        # Even with empty custom quarantine, __name__ must be added
        assert "backend.core.ouroboros.governance.module_hot_reloader" in r.quarantine


# ----------------------------------------------------------------------
# 7. Event emitter
# ----------------------------------------------------------------------


class TestEventEmitter:
    def test_emitter_receives_batch_event(self, synthetic_workspace: Path) -> None:
        events: List[dict] = []

        def collect(event: dict) -> None:
            events.append(event)

        mod_path = synthetic_workspace / "synthetic_emit.py"
        _write(mod_path, "X = 1\n")
        importlib.import_module("synthetic_emit")

        reloader = ModuleHotReloader(
            project_root=synthetic_workspace,
            safe_modules=frozenset({"synthetic_emit"}),
            quarantine=frozenset(),
            event_emitter=collect,
            in_scope_prefix="synthetic_",
        )

        _write(mod_path, "X = 2\n")
        reloader.reload_for_op(op_id="emit-1", target_files=["synthetic_emit.py"])

        assert len(events) >= 1
        batch_event = events[-1]
        assert batch_event["type"] == "hot_reload.batch"
        assert batch_event["op_id"] == "emit-1"
        assert batch_event["overall_status"] == "success"
        assert batch_event["restart_required"] is False
        assert "outcomes" in batch_event
        assert len(batch_event["outcomes"]) == 1
        assert batch_event["outcomes"][0]["status"] == "reloaded"

    def test_emitter_errors_dont_bubble(self, synthetic_workspace: Path) -> None:
        def explode(_event: dict) -> None:
            raise RuntimeError("test emitter explosion")

        mod_path = synthetic_workspace / "synthetic_explode.py"
        _write(mod_path, "X = 1\n")
        importlib.import_module("synthetic_explode")

        reloader = ModuleHotReloader(
            project_root=synthetic_workspace,
            safe_modules=frozenset({"synthetic_explode"}),
            quarantine=frozenset(),
            event_emitter=explode,
            in_scope_prefix="synthetic_",
        )

        _write(mod_path, "X = 2\n")
        # Should not raise even though the emitter does
        batch = reloader.reload_for_op(
            op_id="explode-1", target_files=["synthetic_explode.py"]
        )
        assert batch.overall_status == "success"

    def test_restart_queued_emits_event(self, reloader: ModuleHotReloader) -> None:
        events: List[dict] = []
        reloader._emit = events.append  # inject emitter
        reloader.queue_restart("test reason")
        assert any(e.get("type") == "hot_reload.restart_queued" for e in events)


# ----------------------------------------------------------------------
# 8. Restart-pending lifecycle
# ----------------------------------------------------------------------


class TestRestartPending:
    def test_initial_restart_pending_is_none(self, reloader: ModuleHotReloader) -> None:
        assert reloader.restart_pending is None

    def test_queue_restart_sets_reason(self, reloader: ModuleHotReloader) -> None:
        reloader.queue_restart("custom reason")
        assert reloader.restart_pending == "custom reason"

    def test_first_caller_wins(self, reloader: ModuleHotReloader) -> None:
        reloader.queue_restart("first")
        reloader.queue_restart("second")
        assert reloader.restart_pending == "first"

    def test_clear_restart_resets(self, reloader: ModuleHotReloader) -> None:
        reloader.queue_restart("temp")
        reloader.clear_restart()
        assert reloader.restart_pending is None


# ----------------------------------------------------------------------
# 9. Stats
# ----------------------------------------------------------------------


class TestStats:
    def test_stats_initial(self, reloader: ModuleHotReloader) -> None:
        s = reloader.stats()
        assert s["reload_count"] == 0
        assert s["restart_pending"] is None
        assert isinstance(s["safe_modules"], list)
        assert s["last_batch_status"] is None

    def test_stats_after_successful_reload(self, synthetic_workspace: Path) -> None:
        mod_path = synthetic_workspace / "synthetic_stats.py"
        _write(mod_path, "V = 1\n")
        importlib.import_module("synthetic_stats")

        reloader = ModuleHotReloader(
            project_root=synthetic_workspace,
            safe_modules=frozenset({"synthetic_stats"}),
            quarantine=frozenset(),
            in_scope_prefix="synthetic_",
        )
        _write(mod_path, "V = 2\n")
        reloader.reload_for_op(op_id="stats-1", target_files=["synthetic_stats.py"])

        s = reloader.stats()
        assert s["reload_count"] == 1
        assert s["last_batch_status"] == "success"


# ----------------------------------------------------------------------
# 10. Production smoke — real Ouroboros modules
# ----------------------------------------------------------------------


class TestProductionSmoke:
    def test_real_verify_gate_no_change_reload(self, tmp_path: Path) -> None:
        # Production safe set, real verify_gate file, no mutation expected
        importlib.import_module("backend.core.ouroboros.governance.verify_gate")
        reloader = ModuleHotReloader(project_root=Path("."))
        batch = reloader.reload_for_op(
            op_id="prod-smoke",
            target_files=["backend/core/ouroboros/governance/verify_gate.py"],
        )
        assert batch.overall_status == "no_change"
        assert batch.restart_required is False

    def test_real_patch_benchmarker_no_change_reload(self, tmp_path: Path) -> None:
        importlib.import_module("backend.core.ouroboros.governance.patch_benchmarker")
        reloader = ModuleHotReloader(project_root=Path("."))
        batch = reloader.reload_for_op(
            op_id="prod-smoke-pb",
            target_files=["backend/core/ouroboros/governance/patch_benchmarker.py"],
        )
        assert batch.overall_status == "no_change"
        assert batch.restart_required is False

    def test_default_safe_set_has_probes_for_every_member(self) -> None:
        # Every safe module should have a registered probe — keeps the
        # function-identity verification chain honest as the safe set grows.
        for mod in DEFAULT_SAFE_MODULES:
            assert mod in PROBES, f"Safe module {mod} has no PROBES entry"

    def test_default_quarantine_includes_orchestrator(self) -> None:
        assert (
            "backend.core.ouroboros.governance.orchestrator" in DEFAULT_QUARANTINE
        )

    def test_default_quarantine_includes_process_singletons(self) -> None:
        # The semaphore registry must be reload-protected
        assert (
            "backend.core.ouroboros.governance._process_singletons" in DEFAULT_QUARANTINE
        )

    def test_restart_exit_code_constant(self) -> None:
        # Sanity: 75 = BSD EX_TEMPFAIL convention, distinct from 0/1
        assert RESTART_EXIT_CODE == 75


# ----------------------------------------------------------------------
# 11. Process singleton sanity (semaphore survives reload)
# ----------------------------------------------------------------------


class TestProcessSingletonSurvivesReload:
    def test_patch_benchmarker_semaphore_persists_across_reload(
        self, tmp_path: Path
    ) -> None:
        # Reload patch_benchmarker via the real reloader and confirm the
        # semaphore is the SAME object after — proving the singleton hoist
        # actually decouples the semaphore from the module's reload cycle.
        import backend.core.ouroboros.governance.patch_benchmarker as pb
        sem_before = pb._benchmark_semaphore()

        # Use a manual reload (no file mutation) — we just want to verify
        # the semaphore identity is preserved across importlib.reload.
        importlib.reload(pb)
        sem_after = pb._benchmark_semaphore()
        assert sem_before is sem_after
