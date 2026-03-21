"""Tests for EpisodicFailureMemory."""
import pytest

from backend.core.ouroboros.governance.episodic_memory import (
    EpisodicFailureMemory,
    FailureEpisode,
)


class TestFailureEpisode:
    def test_frozen(self):
        ep = FailureEpisode(
            file_path="parser.py", attempt=1, failure_class="test",
            error_summary="assertion failed", specific_errors=("assert x == 1",),
            line_numbers=(47,),
        )
        with pytest.raises(AttributeError):
            ep.attempt = 2


class TestEpisodicFailureMemory:
    def test_record_and_retrieve(self):
        mem = EpisodicFailureMemory("op-123")
        mem.record("parser.py", attempt=1, failure_class="test",
                   error_summary="assertion failed")
        assert mem.total_episodes == 1
        assert mem.has_failures("parser.py")
        episodes = mem.get_episodes("parser.py")
        assert len(episodes) == 1
        assert episodes[0].file_path == "parser.py"

    def test_multiple_files(self):
        mem = EpisodicFailureMemory("op-123")
        mem.record("a.py", 1, "test", "failed A")
        mem.record("b.py", 1, "build", "failed B")
        assert mem.total_episodes == 2
        assert mem.has_failures("a.py")
        assert mem.has_failures("b.py")
        assert not mem.has_failures("c.py")

    def test_multiple_attempts_same_file(self):
        mem = EpisodicFailureMemory("op-123")
        mem.record("parser.py", 1, "test", "first failure")
        mem.record("parser.py", 2, "test", "second failure")
        episodes = mem.get_episodes("parser.py")
        assert len(episodes) == 2
        assert episodes[0].attempt == 1
        assert episodes[1].attempt == 2

    def test_format_for_prompt_single_file(self):
        mem = EpisodicFailureMemory("op-123")
        mem.record("parser.py", 1, "test", "assertion failed",
                   specific_errors=["assert x == 1"], line_numbers=[47])
        text = mem.format_for_prompt("parser.py")
        assert "parser.py" in text
        assert "Attempt 1" in text
        assert "assert x == 1" in text
        assert "47" in text
        assert "Do not repeat" in text

    def test_format_for_prompt_all_files(self):
        mem = EpisodicFailureMemory("op-123")
        mem.record("a.py", 1, "test", "failed A")
        mem.record("b.py", 1, "build", "failed B")
        text = mem.format_for_prompt()
        assert "a.py" in text
        assert "b.py" in text

    def test_format_empty_returns_empty_string(self):
        mem = EpisodicFailureMemory("op-123")
        assert mem.format_for_prompt() == ""
        assert mem.format_for_prompt("nonexistent.py") == ""

    def test_clear(self):
        mem = EpisodicFailureMemory("op-123")
        mem.record("a.py", 1, "test", "failed")
        mem.clear()
        assert mem.total_episodes == 0
        assert not mem.has_failures()

    def test_get_all_episodes(self):
        mem = EpisodicFailureMemory("op-123")
        mem.record("a.py", 1, "test", "failed A")
        mem.record("b.py", 1, "build", "failed B")
        all_eps = mem.get_all_episodes()
        assert "a.py" in all_eps
        assert "b.py" in all_eps

    def test_specific_errors_and_line_numbers(self):
        mem = EpisodicFailureMemory("op-123")
        mem.record("parser.py", 1, "test", "multiple failures",
                   specific_errors=["error 1", "error 2"],
                   line_numbers=[10, 20, 30])
        ep = mem.get_episodes("parser.py")[0]
        assert ep.specific_errors == ("error 1", "error 2")
        assert ep.line_numbers == (10, 20, 30)
