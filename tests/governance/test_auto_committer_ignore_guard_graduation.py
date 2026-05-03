"""AutoCommitterIgnoreGuard Slice 3 -- graduation regression spine.

Pins:
  * Master flag default flipped false -> true
  * Module owns register_flags + register_shipped_invariants
  * FlagRegistry seeds the 2 arc flags
  * shipped_code_invariants pin discoverable + passes against
    live source (incl. the load-bearing ``--no-index`` flag check)
  * SSE event constant + publish helper exist
  * AutoCommitter fires SSE on Layer 1 skip + Layer 2 abort
  * Operator escape hatch preserved
  * E2E at graduated defaults: tracked-but-ignored target
    refused without any env-var overrides
"""
from __future__ import annotations

import importlib
import pathlib
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

from backend.core.ouroboros.governance.auto_committer import (
    AutoCommitter,
)
from backend.core.ouroboros.governance.gitignore_guard import (
    GitignoreGuardOutcome,
    gitignore_guard_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
        "JARVIS_GITIGNORE_CHECK_TIMEOUT_S",
        "JARVIS_AUTO_COMMIT_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def git_repo(tmp_path):
    """Real git repo with .gitignore + tracked legacy + clean."""
    if not shutil.which("git"):
        pytest.skip("git binary not available")

    def _run(*args):
        return subprocess.run(
            ["git", *args],
            cwd=str(tmp_path), capture_output=True, text=True,
            check=True,
        )

    _run("init", "-q", "-b", "main")
    _run("config", "user.email", "test@example.com")
    _run("config", "user.name", "test")
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    (tmp_path / "src.py").write_text("x = 1\n")
    (tmp_path / "tracked_legacy.pyc").write_text("legacy")
    _run("add", ".gitignore", "src.py")
    _run("add", "-f", "tracked_legacy.pyc")
    _run("commit", "-q", "-m", "initial")
    return tmp_path


# ---------------------------------------------------------------------------
# Graduated default
# ---------------------------------------------------------------------------


class TestGraduatedDefault:
    def test_master_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
            raising=False,
        )
        assert gitignore_guard_enabled() is True

    @pytest.mark.parametrize(
        "raw", ["0", "false", "FALSE", "no", "off"],
    )
    def test_operator_escape_hatch(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", raw,
        )
        assert gitignore_guard_enabled() is False


# ---------------------------------------------------------------------------
# Module-owned registration callables
# ---------------------------------------------------------------------------


_MODULE = "backend.core.ouroboros.governance.gitignore_guard"


class TestModuleOwnedRegistration:
    def test_register_flags_callable(self):
        mod = importlib.import_module(_MODULE)
        fn = getattr(mod, "register_flags", None)
        assert callable(fn)

    def test_register_shipped_invariants_callable(self):
        mod = importlib.import_module(_MODULE)
        fn = getattr(mod, "register_shipped_invariants", None)
        assert callable(fn)


# ---------------------------------------------------------------------------
# FlagRegistry seeding
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeding:
    def test_both_flags_seeded(self):
        from backend.core.ouroboros.governance.flag_registry import (
            FlagRegistry,
        )
        registry = FlagRegistry()
        mod = importlib.import_module(_MODULE)
        mod.register_flags(registry)
        names = {spec.name for spec in registry.list_all()}
        assert "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED" in names
        assert "JARVIS_GITIGNORE_CHECK_TIMEOUT_S" in names

    def test_master_flag_default_true(self):
        from backend.core.ouroboros.governance.flag_registry import (
            FlagRegistry,
        )
        registry = FlagRegistry()
        mod = importlib.import_module(_MODULE)
        mod.register_flags(registry)
        spec = next(
            (s for s in registry.list_all() if s.name ==
             "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED"),
            None,
        )
        assert spec is not None
        assert spec.default is True


# ---------------------------------------------------------------------------
# Shipped invariants
# ---------------------------------------------------------------------------


class TestShippedInvariants:
    def test_invariants_returned(self):
        mod = importlib.import_module(_MODULE)
        invariants = mod.register_shipped_invariants()
        assert isinstance(invariants, list)
        assert len(invariants) >= 1

    def test_pin_passes_against_live_source(self):
        """Load-bearing: validates that the actual shipped code
        respects the contract -- pure-stdlib + closed-5 enum +
        the load-bearing ``--no-index`` flag presence."""
        import ast as _ast
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        mod = importlib.import_module(_MODULE)
        for inv in mod.register_shipped_invariants():
            target_path = repo_root / inv.target_file
            source = target_path.read_text()
            tree = _ast.parse(source)
            violations = inv.validate(tree, source)
            assert violations == (), (
                f"{inv.invariant_name!r} flagged: {violations}"
            )


# ---------------------------------------------------------------------------
# SSE event surface
# ---------------------------------------------------------------------------


class TestSSEEvent:
    def test_event_constant_defined(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_AUTO_COMMITTER_IGNORED_BLOCKED,
        )
        assert EVENT_TYPE_AUTO_COMMITTER_IGNORED_BLOCKED == (
            "auto_committer_ignored_blocked"
        )

    def test_publish_helper_exists(self):
        from backend.core.ouroboros.governance import (
            ide_observability_stream as mod,
        )
        assert hasattr(mod, "publish_auto_committer_ignored_blocked")
        assert callable(mod.publish_auto_committer_ignored_blocked)

    def test_publish_helper_returns_none_when_stream_disabled(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_auto_committer_ignored_blocked,
        )
        out = publish_auto_committer_ignored_blocked(
            op_id="op-1", layer="layer1_prestage",
            blocked_paths=("x.pyc",),
            skipped_count=1, aborted=False,
        )
        assert out is None

    def test_publish_helper_never_raises(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_auto_committer_ignored_blocked,
        )
        publish_auto_committer_ignored_blocked(
            op_id="", layer="", blocked_paths=(),
        )


# ---------------------------------------------------------------------------
# AutoCommitter fires SSE on Layer 1 + Layer 2
# ---------------------------------------------------------------------------


class TestAutoCommitterSSEFire:
    @pytest.mark.asyncio
    async def test_layer1_skip_fires_sse(self, git_repo, monkeypatch):
        import backend.core.ouroboros.governance.auto_committer as ac
        monkeypatch.setattr(ac, "_ENABLED", True)
        calls: List[Dict[str, Any]] = []

        def _spy(**kwargs):
            calls.append(kwargs)
            return "evt-1"

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream."
            "publish_auto_committer_ignored_blocked",
            _spy,
        )
        (git_repo / "tracked_legacy.pyc").write_text("modified")
        (git_repo / "src.py").write_text("y = 2")
        committer = AutoCommitter(repo_root=git_repo)
        await committer.commit(
            op_id="op-l1-test",
            description="layer 1 fire test",
            target_files=("src.py", "tracked_legacy.pyc"),
        )
        # Layer 1 fired
        layer1_calls = [
            c for c in calls if c["layer"] == "layer1_prestage"
        ]
        assert len(layer1_calls) >= 1
        assert "tracked_legacy.pyc" in layer1_calls[0]["blocked_paths"]
        assert layer1_calls[0]["aborted"] is False

    @pytest.mark.asyncio
    async def test_layer2_abort_fires_sse(self, git_repo, monkeypatch):
        import backend.core.ouroboros.governance.auto_committer as ac
        monkeypatch.setattr(ac, "_ENABLED", True)
        calls: List[Dict[str, Any]] = []

        def _spy(**kwargs):
            calls.append(kwargs)
            return "evt-2"

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream."
            "publish_auto_committer_ignored_blocked",
            _spy,
        )
        # Force Layer 1 to fail-open via patched find_ignored_targets
        # so Layer 2 catches the breach.
        original_find = (
            __import__(
                "backend.core.ouroboros.governance.gitignore_guard",
                fromlist=["find_ignored_targets"],
            ).find_ignored_targets
        )
        call_count = {"n": 0}

        def _selectively_fail(repo_root, target_files, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated Layer 1 failure")
            return original_find(repo_root, target_files)

        with mock.patch(
            "backend.core.ouroboros.governance."
            "gitignore_guard.find_ignored_targets",
            side_effect=_selectively_fail,
        ):
            (git_repo / "tracked_legacy.pyc").write_text("modified")
            committer = AutoCommitter(repo_root=git_repo)
            result = await committer.commit(
                op_id="op-l2-test",
                description="layer 2 fire test",
                target_files=("tracked_legacy.pyc",),
            )

        assert result.committed is False
        layer2_calls = [
            c for c in calls if c["layer"] == "layer2_validator"
        ]
        assert len(layer2_calls) == 1
        assert "tracked_legacy.pyc" in (
            layer2_calls[0]["blocked_paths"]
        )
        assert layer2_calls[0]["aborted"] is True
        assert layer2_calls[0]["op_id"] == "op-l2-test"

    @pytest.mark.asyncio
    async def test_sse_publish_failure_does_not_break_committer(
        self, git_repo, monkeypatch,
    ):
        """SSE publish blowing up must NOT prevent the
        AutoCommitter from doing its job (best-effort
        observability)."""
        import backend.core.ouroboros.governance.auto_committer as ac
        monkeypatch.setattr(ac, "_ENABLED", True)

        def _explode(**_):
            raise RuntimeError("publish boom")

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream."
            "publish_auto_committer_ignored_blocked",
            _explode,
        )
        (git_repo / "tracked_legacy.pyc").write_text("modified")
        (git_repo / "src.py").write_text("y = 2")
        committer = AutoCommitter(repo_root=git_repo)
        # Should still partition correctly: src.py committed,
        # ignored skipped + recorded.
        result = await committer.commit(
            op_id="op-pub-fail",
            description="publish failure test",
            target_files=("src.py", "tracked_legacy.pyc"),
        )
        assert result.committed is True
        assert "tracked_legacy.pyc" in result.skipped_ignored


# ---------------------------------------------------------------------------
# E2E at graduated defaults
# ---------------------------------------------------------------------------


class TestGraduatedEndToEnd:
    @pytest.mark.asyncio
    async def test_no_env_overrides_layer1_active(
        self, git_repo, monkeypatch,
    ):
        """At graduated defaults (no GUARD_ENABLED env var),
        Layer 1 must refuse a tracked-but-ignored path."""
        import backend.core.ouroboros.governance.auto_committer as ac
        monkeypatch.setattr(ac, "_ENABLED", True)
        # NOTE: GUARD_ENABLED env explicitly UNSET -> exercises
        # the graduated default
        monkeypatch.delenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
            raising=False,
        )
        # Stub SSE to avoid side-effects
        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream."
            "publish_auto_committer_ignored_blocked",
            lambda **kw: None,
        )
        (git_repo / "tracked_legacy.pyc").write_text("modified")
        committer = AutoCommitter(repo_root=git_repo)
        result = await committer.commit(
            op_id="op-grad-e2e",
            description="graduated defaults",
            target_files=("tracked_legacy.pyc",),
        )
        # Layer 1 refused -> nothing_to_stage -> committed=False
        assert result.committed is False
        assert result.skipped_reason == "nothing_to_stage"
        assert "tracked_legacy.pyc" in result.skipped_ignored

    @pytest.mark.asyncio
    async def test_explicit_master_off_disables_guard(
        self, git_repo, monkeypatch,
    ):
        """Operator escape hatch: explicit "false" disables the
        guard so AutoCommitter behaves as pre-Slice-2 (force-
        commits ignored paths). This proves the escape hatch
        works for legacy operators / migration scenarios."""
        import backend.core.ouroboros.governance.auto_committer as ac
        monkeypatch.setattr(ac, "_ENABLED", True)
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
            "false",
        )
        (git_repo / "tracked_legacy.pyc").write_text("modified")
        committer = AutoCommitter(repo_root=git_repo)
        result = await committer.commit(
            op_id="op-grad-escape",
            description="escape hatch active",
            target_files=("tracked_legacy.pyc",),
        )
        # Guard off -> AutoCommitter happily commits the modified
        # tracked-but-ignored file (the pre-Slice-2 breach mode).
        assert result.committed is True
        assert result.skipped_ignored == ()
        assert result.aborted_validator_breach == ()
