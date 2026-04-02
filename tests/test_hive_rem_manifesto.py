"""
Tests for backend.hive.rem_manifesto_reviewer

Covers:
- _parse_changed_files correctly parses git output
- _filter_secret_paths removes .env, credentials.json, key.pem but keeps normal files
- _cap_files limits to max
- No changes -> no threads, 0 calls
- Changed files -> threads created with calls >= 1
- Respects budget (budget=3, 5 files -> only 3 processed)
- Skips binary and secret files
- Saves last_rem_at timestamp after run
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.hive.rem_manifesto_reviewer import ManifestoReviewer
from backend.hive.thread_manager import ThreadManager
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    ThreadState,
)


# ============================================================================
# Fixtures
# ============================================================================


def _make_reasoning_msg(thread_id: str) -> MagicMock:
    """Create a mock PersonaReasoningMessage with required fields."""
    msg = MagicMock()
    msg.thread_id = thread_id
    msg.persona = "jarvis"
    msg.role = "body"
    msg.intent = PersonaIntent.OBSERVE
    msg.references = []
    msg.reasoning = "Manifesto alignment analysed."
    msg.confidence = 0.80
    msg.model_used = "mock-model"
    msg.token_cost = 100
    msg.type = "persona_reasoning"
    msg.manifesto_principle = None
    msg.validate_verdict = None
    return msg


@pytest.fixture()
def thread_manager() -> ThreadManager:
    """In-memory ThreadManager (no disk persistence)."""
    return ThreadManager(storage_dir=None)


@pytest.fixture()
def persona_engine() -> MagicMock:
    """Mock PersonaEngine whose generate_reasoning returns a MagicMock message."""
    engine = MagicMock()
    engine.generate_reasoning = AsyncMock(
        side_effect=lambda persona, intent, thread: _make_reasoning_msg(
            thread.thread_id
        )
    )
    return engine


@pytest.fixture()
def relay() -> MagicMock:
    """Mock HudRelayAgent."""
    return MagicMock()


@pytest.fixture()
def reviewer(
    persona_engine: MagicMock,
    thread_manager: ThreadManager,
    relay: MagicMock,
    tmp_path: Path,
) -> ManifestoReviewer:
    """ManifestoReviewer with tmp_path for repo_root and state_dir."""
    return ManifestoReviewer(
        persona_engine,
        thread_manager,
        relay,
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
    )


# ============================================================================
# _parse_changed_files
# ============================================================================


class TestParseChangedFiles:
    """Verify git output parsing into a unique sorted file list."""

    def test_basic_parse(self, reviewer: ManifestoReviewer) -> None:
        git_output = (
            "\n"
            "backend/hive/rem_council.py\n"
            "backend/hive/thread_models.py\n"
            "\n"
            "backend/hive/rem_council.py\n"
            "tests/test_something.py\n"
        )
        result = reviewer._parse_changed_files(git_output)
        assert result == [
            "backend/hive/rem_council.py",
            "backend/hive/thread_models.py",
            "tests/test_something.py",
        ]

    def test_empty_output(self, reviewer: ManifestoReviewer) -> None:
        assert reviewer._parse_changed_files("") == []

    def test_only_blank_lines(self, reviewer: ManifestoReviewer) -> None:
        assert reviewer._parse_changed_files("\n\n\n") == []

    def test_deduplication(self, reviewer: ManifestoReviewer) -> None:
        git_output = "a.py\nb.py\na.py\nb.py\nc.py\n"
        result = reviewer._parse_changed_files(git_output)
        assert result == ["a.py", "b.py", "c.py"]

    def test_sorted_output(self, reviewer: ManifestoReviewer) -> None:
        git_output = "z.py\na.py\nm.py\n"
        result = reviewer._parse_changed_files(git_output)
        assert result == ["a.py", "m.py", "z.py"]


# ============================================================================
# _filter_secret_paths
# ============================================================================


class TestFilterSecretPaths:
    """Verify secret/credential file filtering."""

    def test_removes_env_file(self, reviewer: ManifestoReviewer) -> None:
        files = ["app.py", ".env", "config/.env"]
        result = reviewer._filter_secret_paths(files)
        assert ".env" not in result
        assert "config/.env" not in result
        assert "app.py" in result

    def test_removes_credentials(self, reviewer: ManifestoReviewer) -> None:
        files = ["main.py", "credentials.json", "path/to/credentials.yaml"]
        result = reviewer._filter_secret_paths(files)
        assert all("credentials" not in f for f in result)
        assert "main.py" in result

    def test_removes_key_pem(self, reviewer: ManifestoReviewer) -> None:
        files = ["server.py", "private.key", "cert.pem", "store.p12", "bundle.pfx"]
        result = reviewer._filter_secret_paths(files)
        assert result == ["server.py"]

    def test_removes_ssh(self, reviewer: ManifestoReviewer) -> None:
        files = ["readme.md", ".ssh/id_rsa", "path/.ssh/config"]
        result = reviewer._filter_secret_paths(files)
        assert result == ["readme.md"]

    def test_removes_secret_in_path(self, reviewer: ManifestoReviewer) -> None:
        files = ["utils.py", "config/secret_keys.py"]
        result = reviewer._filter_secret_paths(files)
        assert result == ["utils.py"]

    def test_case_insensitive(self, reviewer: ManifestoReviewer) -> None:
        files = ["app.py", "CREDENTIALS.json", "Secret_config.yaml"]
        result = reviewer._filter_secret_paths(files)
        assert result == ["app.py"]

    def test_keeps_normal_files(self, reviewer: ManifestoReviewer) -> None:
        files = ["backend/core/router.py", "tests/test_auth.py", "README.md"]
        result = reviewer._filter_secret_paths(files)
        assert result == files


# ============================================================================
# _cap_files
# ============================================================================


class TestCapFiles:
    """Verify file list capping."""

    def test_caps_to_max(self, reviewer: ManifestoReviewer) -> None:
        files = [f"file_{i}.py" for i in range(20)]
        result = reviewer._cap_files(files, 5)
        assert len(result) == 5
        assert result == files[:5]

    def test_under_cap_unchanged(self, reviewer: ManifestoReviewer) -> None:
        files = ["a.py", "b.py"]
        result = reviewer._cap_files(files, 10)
        assert result == files

    def test_empty_list(self, reviewer: ManifestoReviewer) -> None:
        assert reviewer._cap_files([], 10) == []

    def test_cap_zero(self, reviewer: ManifestoReviewer) -> None:
        assert reviewer._cap_files(["a.py", "b.py"], 0) == []


# ============================================================================
# _is_binary
# ============================================================================


class TestIsBinary:
    """Verify binary extension detection."""

    def test_binary_extensions(self, reviewer: ManifestoReviewer) -> None:
        for ext in [".png", ".jpg", ".gif", ".zip", ".exe", ".so", ".pyc", ".dylib"]:
            assert reviewer._is_binary(f"file{ext}") is True

    def test_non_binary(self, reviewer: ManifestoReviewer) -> None:
        for ext in [".py", ".js", ".ts", ".md", ".txt", ".yaml", ".json"]:
            assert reviewer._is_binary(f"file{ext}") is False

    def test_case_insensitive(self, reviewer: ManifestoReviewer) -> None:
        assert reviewer._is_binary("image.PNG") is True
        assert reviewer._is_binary("image.Jpg") is True


# ============================================================================
# run() -- no changes
# ============================================================================


class TestRunNoChanges:
    """No changed files -> no threads, 0 calls."""

    @pytest.mark.asyncio
    async def test_no_changes_returns_empty(
        self,
        reviewer: ManifestoReviewer,
        persona_engine: MagicMock,
        thread_manager: ThreadManager,
    ) -> None:
        with patch.object(reviewer, "_get_changed_files", return_value=[]):
            thread_ids, calls_used, should_escalate, escalation_id = (
                await reviewer.run(budget=5)
            )

        assert thread_ids == []
        assert calls_used == 0
        assert should_escalate is False
        assert escalation_id is None
        persona_engine.generate_reasoning.assert_not_called()


# ============================================================================
# run() -- changed files
# ============================================================================


class TestRunWithChanges:
    """Changed files -> threads created with persona reasoning."""

    @pytest.mark.asyncio
    async def test_creates_threads_for_changed_files(
        self,
        reviewer: ManifestoReviewer,
        persona_engine: MagicMock,
        thread_manager: ThreadManager,
        tmp_path: Path,
    ) -> None:
        # Create actual files in the tmp repo root.
        (tmp_path / "foo.py").write_text("print('hello')\n", encoding="utf-8")
        (tmp_path / "bar.py").write_text("x = 1\n", encoding="utf-8")

        with patch.object(
            reviewer, "_get_changed_files", return_value=["bar.py", "foo.py"]
        ):
            thread_ids, calls_used, should_escalate, escalation_id = (
                await reviewer.run(budget=5)
            )

        assert len(thread_ids) == 2
        assert calls_used == 2
        assert should_escalate is False
        assert escalation_id is None

        # Verify threads are in DEBATING state with messages.
        for tid in thread_ids:
            thread = thread_manager.get_thread(tid)
            assert thread is not None
            assert thread.state == ThreadState.DEBATING
            assert thread.trigger_event == "rem_manifesto_review"
            assert thread.cognitive_state == CognitiveState.REM
            # First message: AgentLogMessage, second: reasoning.
            assert len(thread.messages) == 2
            assert isinstance(thread.messages[0], AgentLogMessage)
            assert thread.messages[0].category == "manifesto"

        assert persona_engine.generate_reasoning.call_count == 2


# ============================================================================
# run() -- budget enforcement
# ============================================================================


class TestBudgetEnforcement:
    """Budget=3 with 5 files -> only 3 processed."""

    @pytest.mark.asyncio
    async def test_respects_budget(
        self,
        reviewer: ManifestoReviewer,
        persona_engine: MagicMock,
        thread_manager: ThreadManager,
        tmp_path: Path,
    ) -> None:
        # Create 5 files.
        for i in range(5):
            (tmp_path / f"file_{i}.py").write_text(f"# file {i}\n", encoding="utf-8")

        changed = [f"file_{i}.py" for i in range(5)]
        with patch.object(reviewer, "_get_changed_files", return_value=changed):
            thread_ids, calls_used, should_escalate, escalation_id = (
                await reviewer.run(budget=3)
            )

        assert len(thread_ids) == 3
        assert calls_used == 3
        assert persona_engine.generate_reasoning.call_count == 3

    @pytest.mark.asyncio
    async def test_zero_budget_no_calls(
        self,
        reviewer: ManifestoReviewer,
        persona_engine: MagicMock,
        thread_manager: ThreadManager,
    ) -> None:
        with patch.object(
            reviewer, "_get_changed_files", return_value=["a.py", "b.py"]
        ):
            thread_ids, calls_used, should_escalate, escalation_id = (
                await reviewer.run(budget=0)
            )

        assert calls_used == 0
        assert len(thread_ids) == 0
        persona_engine.generate_reasoning.assert_not_called()


# ============================================================================
# run() -- skips binary and secret files
# ============================================================================


class TestSkipsBinaryAndSecrets:
    """Binary and secret files are filtered before processing."""

    @pytest.mark.asyncio
    async def test_skips_binary_files(
        self,
        reviewer: ManifestoReviewer,
        persona_engine: MagicMock,
        thread_manager: ThreadManager,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "app.py").write_text("pass\n", encoding="utf-8")

        with patch.object(
            reviewer,
            "_get_changed_files",
            return_value=["app.py", "logo.png", "font.woff2", "archive.zip"],
        ):
            thread_ids, calls_used, _, _ = await reviewer.run(budget=10)

        # Only app.py should be processed.
        assert len(thread_ids) == 1
        assert calls_used == 1

    @pytest.mark.asyncio
    async def test_skips_secret_files(
        self,
        reviewer: ManifestoReviewer,
        persona_engine: MagicMock,
        thread_manager: ThreadManager,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "app.py").write_text("pass\n", encoding="utf-8")

        with patch.object(
            reviewer,
            "_get_changed_files",
            return_value=[
                "app.py",
                ".env",
                "credentials.json",
                "server.key",
                "cert.pem",
            ],
        ):
            thread_ids, calls_used, _, _ = await reviewer.run(budget=10)

        # Only app.py should be processed.
        assert len(thread_ids) == 1
        assert calls_used == 1

    @pytest.mark.asyncio
    async def test_all_filtered_returns_empty(
        self,
        reviewer: ManifestoReviewer,
        persona_engine: MagicMock,
        thread_manager: ThreadManager,
    ) -> None:
        with patch.object(
            reviewer,
            "_get_changed_files",
            return_value=[".env", "logo.png", "credentials.json"],
        ):
            thread_ids, calls_used, _, _ = await reviewer.run(budget=10)

        assert thread_ids == []
        assert calls_used == 0


# ============================================================================
# Timestamp persistence
# ============================================================================


class TestTimestampPersistence:
    """Verify last_rem_at is saved after each run."""

    @pytest.mark.asyncio
    async def test_saves_timestamp_after_run(
        self,
        reviewer: ManifestoReviewer,
        tmp_path: Path,
    ) -> None:
        with patch.object(reviewer, "_get_changed_files", return_value=[]):
            await reviewer.run(budget=5)

        ts_path = tmp_path / "state" / "last_rem_at"
        assert ts_path.exists()
        content = ts_path.read_text(encoding="utf-8").strip()
        # Should be a valid ISO timestamp.
        assert "T" in content
        # Should be parseable.
        from datetime import datetime

        datetime.fromisoformat(content)

    @pytest.mark.asyncio
    async def test_saves_timestamp_even_with_changes(
        self,
        reviewer: ManifestoReviewer,
        persona_engine: MagicMock,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "x.py").write_text("pass\n", encoding="utf-8")
        with patch.object(reviewer, "_get_changed_files", return_value=["x.py"]):
            await reviewer.run(budget=5)

        ts_path = tmp_path / "state" / "last_rem_at"
        assert ts_path.exists()

    def test_load_timestamp_returns_none_when_missing(
        self, reviewer: ManifestoReviewer
    ) -> None:
        assert reviewer._load_last_rem_timestamp() is None

    def test_save_then_load_roundtrip(
        self, reviewer: ManifestoReviewer
    ) -> None:
        reviewer._save_last_rem_timestamp()
        loaded = reviewer._load_last_rem_timestamp()
        assert loaded is not None
        assert "T" in loaded


# ============================================================================
# _read_file
# ============================================================================


class TestReadFile:
    """Verify file reading with line cap."""

    def test_reads_file_content(
        self, reviewer: ManifestoReviewer, tmp_path: Path
    ) -> None:
        (tmp_path / "sample.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
        content = reviewer._read_file("sample.py")
        assert "line1" in content
        assert "line3" in content

    def test_missing_file_returns_empty(
        self, reviewer: ManifestoReviewer
    ) -> None:
        assert reviewer._read_file("nonexistent.py") == ""

    def test_respects_line_cap(
        self, reviewer: ManifestoReviewer, tmp_path: Path
    ) -> None:
        # Write 300 lines, cap is 200.
        lines = [f"line_{i}" for i in range(300)]
        (tmp_path / "big.py").write_text("\n".join(lines), encoding="utf-8")
        content = reviewer._read_file("big.py")
        # Should only have first 200 lines.
        assert "line_199" in content
        assert "line_200" not in content
