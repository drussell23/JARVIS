"""Tests for multi-file coordinated generation in the orchestrator.

Covers:
  • ``_iter_candidate_files`` across single-file, multi-file, disabled-flag, and
    malformed inputs.
  • ``_apply_multi_file_candidate`` happy-path (every file applies).
  • ``_apply_multi_file_candidate`` rollback path (second file fails → first
    file restored from snapshot).
  • ``_apply_multi_file_candidate`` new-file rollback (file that did not exist
    pre-apply is unlinked on failure).

These tests do NOT boot the full GovernanceStack — the multi-file helper is
deliberately composable on top of a mocked ``change_engine.execute``.
"""
from __future__ import annotations

import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.change_engine import ChangePhase, ChangeResult
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator


# ── _iter_candidate_files static helper ──────────────────────────────────────


class TestIterCandidateFiles:
    """Static helper that returns every (file_path, full_content) pair."""

    def test_single_file_legacy(self):
        cand = {"file_path": "src/a.py", "full_content": "x=1\n"}
        assert GovernedOrchestrator._iter_candidate_files(cand) == [
            ("src/a.py", "x=1\n")
        ]

    def test_multi_file(self):
        cand = {
            "file_path": "src/a.py",
            "full_content": "x=1\n",
            "files": [
                {"file_path": "src/a.py", "full_content": "x=1\n"},
                {"file_path": "src/b.py", "full_content": "y=2\n"},
                {"file_path": "src/c.py", "full_content": "z=3\n"},
            ],
        }
        pairs = GovernedOrchestrator._iter_candidate_files(cand)
        assert len(pairs) == 3
        assert pairs[0] == ("src/a.py", "x=1\n")
        assert pairs[1] == ("src/b.py", "y=2\n")
        assert pairs[2] == ("src/c.py", "z=3\n")

    def test_feature_flag_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MULTI_FILE_GEN_ENABLED", "false")
        cand = {
            "file_path": "src/a.py",
            "full_content": "x=1\n",
            "files": [
                {"file_path": "src/a.py", "full_content": "x=1\n"},
                {"file_path": "src/b.py", "full_content": "y=2\n"},
            ],
        }
        # Disabled flag collapses multi-file back to the primary only.
        pairs = GovernedOrchestrator._iter_candidate_files(cand)
        assert pairs == [("src/a.py", "x=1\n")]

    def test_dedupes_repeated_paths(self):
        cand = {
            "file_path": "src/a.py",
            "full_content": "x=1\n",
            "files": [
                {"file_path": "src/a.py", "full_content": "x=1\n"},
                {"file_path": "src/a.py", "full_content": "x=1\n"},  # duplicate
                {"file_path": "src/b.py", "full_content": "y=2\n"},
            ],
        }
        pairs = GovernedOrchestrator._iter_candidate_files(cand)
        assert len(pairs) == 2

    def test_skips_malformed_entries(self):
        cand = {
            "file_path": "src/a.py",
            "full_content": "x=1\n",
            "files": [
                {"file_path": "src/a.py", "full_content": "x=1\n"},
                "not a dict",
                {"file_path": "", "full_content": "empty path skipped"},
                {"file_path": "src/c.py", "full_content": "z=3\n"},
            ],
        }
        pairs = GovernedOrchestrator._iter_candidate_files(cand)
        paths = [p for p, _ in pairs]
        assert "src/a.py" in paths
        assert "src/c.py" in paths
        assert "" not in paths

    def test_empty_files_list_falls_back_to_primary(self):
        cand = {"file_path": "src/a.py", "full_content": "x=1\n", "files": []}
        pairs = GovernedOrchestrator._iter_candidate_files(cand)
        assert pairs == [("src/a.py", "x=1\n")]


# ── _apply_multi_file_candidate rollback semantics ───────────────────────────


def _orchestrator_for_apply(project_root: Path):
    """Build a partial orchestrator with just enough for _apply_multi_file_candidate.

    We avoid the full constructor (which wires the entire stack) and poke the
    private fields the helper reads directly. The helper only needs:
      • ``_config.project_root``
      • ``_stack.change_engine.execute``
      • ``_build_profile``
      • ``_record_ledger`` (async)
    """
    orch = GovernedOrchestrator.__new__(GovernedOrchestrator)
    orch._config = MagicMock()
    orch._config.project_root = project_root
    orch._stack = MagicMock()
    orch._build_profile = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
    orch._record_ledger = AsyncMock()  # type: ignore[method-assign]
    return orch


def _ctx(description: str = "multi-file test op") -> MagicMock:
    ctx = MagicMock()
    ctx.description = description
    ctx.op_id = "op-multi-1"
    ctx.target_files = ("src/a.py",)
    return ctx


class TestApplyMultiFileCandidate:
    @pytest.mark.asyncio
    async def test_all_files_apply_cleanly(self, tmp_path):
        """Happy path: every file apply returns success."""
        orch = _orchestrator_for_apply(tmp_path)

        # Every ChangeEngine.execute returns success.
        orch._stack.change_engine.execute = AsyncMock(
            return_value=ChangeResult(
                op_id="per-file",
                success=True,
                phase_reached=ChangePhase.VERIFIED,
            )
        )

        candidate = {
            "file_path": "src/a.py",
            "full_content": "a=1\n",
            "files": [
                {"file_path": "src/a.py", "full_content": "a=1\n"},
                {"file_path": "src/b.py", "full_content": "b=2\n"},
                {"file_path": "src/c.py", "full_content": "c=3\n"},
            ],
        }
        files = GovernedOrchestrator._iter_candidate_files(candidate)
        assert len(files) == 3

        result = await orch._apply_multi_file_candidate(
            _ctx(), candidate, files, snapshots={},
        )

        assert result.success is True
        assert result.rolled_back is False
        assert orch._stack.change_engine.execute.await_count == 3
        # Final ledger entry marks multi-file completion.
        final_ledger_call = orch._record_ledger.await_args_list[-1]
        assert final_ledger_call.args[2]["event"] == "multi_file_apply_complete"
        assert final_ledger_call.args[2]["file_count"] == 3

    @pytest.mark.asyncio
    async def test_rollback_restores_previously_applied_files(self, tmp_path):
        """When the second file fails, the first file is restored from snapshot."""
        orch = _orchestrator_for_apply(tmp_path)

        # Pre-create the first file with its original content.
        (tmp_path / "src").mkdir()
        original_a = "a_original\n"
        (tmp_path / "src" / "a.py").write_text(original_a)

        # The first file "applies" successfully: we simulate the success path
        # by manually writing the new content to disk (the real ChangeEngine
        # would do this). The second file fails immediately before writing.
        execute_call_count = {"n": 0}

        async def _fake_execute(req):
            execute_call_count["n"] += 1
            if execute_call_count["n"] == 1:
                # Simulate the first file being written to disk successfully.
                req.target_file.parent.mkdir(parents=True, exist_ok=True)
                req.target_file.write_text(req.proposed_content)
                return ChangeResult(
                    op_id=req.op_id or "per-file",
                    success=True,
                    phase_reached=ChangePhase.VERIFIED,
                )
            # Second file fails before any write.
            return ChangeResult(
                op_id=req.op_id or "per-file",
                success=False,
                phase_reached=ChangePhase.VERIFY,
                rolled_back=True,
                error="simulated verify failure",
            )

        orch._stack.change_engine.execute = AsyncMock(side_effect=_fake_execute)

        candidate = {
            "file_path": "src/a.py",
            "full_content": "a_new_content\n",
            "files": [
                {"file_path": "src/a.py", "full_content": "a_new_content\n"},
                {"file_path": "src/b.py", "full_content": "b_new_content\n"},
            ],
        }
        files = GovernedOrchestrator._iter_candidate_files(candidate)
        snapshots = {"src/a.py": original_a}

        result = await orch._apply_multi_file_candidate(
            _ctx(), candidate, files, snapshots,
        )

        assert result.success is False
        assert result.rolled_back is True
        assert "src/b.py" in (result.error or "")

        # Verify that src/a.py was restored from the snapshot.
        restored = (tmp_path / "src" / "a.py").read_text()
        assert restored == original_a

        # Ledger recorded the rollback event.
        rollback_entries = [
            c for c in orch._record_ledger.await_args_list
            if len(c.args) >= 3 and isinstance(c.args[2], dict)
            and c.args[2].get("event") == "multi_file_rollback"
        ]
        assert len(rollback_entries) == 1
        payload = rollback_entries[0].args[2]
        assert payload["failed_file"] == "src/b.py"
        assert payload["rolled_back_count"] == 1

    @pytest.mark.asyncio
    async def test_rollback_unlinks_newly_created_files(self, tmp_path):
        """Files with no snapshot (new files) get unlinked on batch failure."""
        orch = _orchestrator_for_apply(tmp_path)

        # First file is NEW (no pre-existing copy, no snapshot).
        # Second file fails → first file should be removed from disk.
        async def _fake_execute(req):
            if "new_file" in str(req.target_file):
                req.target_file.parent.mkdir(parents=True, exist_ok=True)
                req.target_file.write_text(req.proposed_content)
                return ChangeResult(
                    op_id=req.op_id or "per-file",
                    success=True,
                    phase_reached=ChangePhase.VERIFIED,
                )
            return ChangeResult(
                op_id=req.op_id or "per-file",
                success=False,
                phase_reached=ChangePhase.VERIFY,
                error="simulated",
            )

        orch._stack.change_engine.execute = AsyncMock(side_effect=_fake_execute)

        candidate = {
            "file_path": "src/new_file.py",
            "full_content": "new content\n",
            "files": [
                {"file_path": "src/new_file.py", "full_content": "new content\n"},
                {"file_path": "src/failing.py", "full_content": "doomed\n"},
            ],
        }
        files = GovernedOrchestrator._iter_candidate_files(candidate)
        # No snapshots for either file (both are "new" from the batch's POV).
        result = await orch._apply_multi_file_candidate(
            _ctx(), candidate, files, snapshots={},
        )

        assert result.success is False
        assert result.rolled_back is True
        # The newly-created file should have been removed.
        assert not (tmp_path / "src" / "new_file.py").exists()

    @pytest.mark.asyncio
    async def test_change_engine_exception_recorded_as_failure(self, tmp_path):
        """A raised exception inside change_engine.execute becomes a batch failure."""
        orch = _orchestrator_for_apply(tmp_path)

        orch._stack.change_engine.execute = AsyncMock(
            side_effect=RuntimeError("boom")
        )

        candidate = {
            "file_path": "src/a.py",
            "full_content": "a=1\n",
            "files": [
                {"file_path": "src/a.py", "full_content": "a=1\n"},
                {"file_path": "src/b.py", "full_content": "b=2\n"},
            ],
        }
        files = GovernedOrchestrator._iter_candidate_files(candidate)
        result = await orch._apply_multi_file_candidate(
            _ctx(), candidate, files, snapshots={},
        )

        assert result.success is False
        assert "change_engine_raise" in (result.error or "") or "boom" in (result.error or "")
