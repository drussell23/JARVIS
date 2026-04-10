"""Tests for harness REPL memory commands (``/memory``, ``/remember``, ``/forget``).

These exercise the unbound methods from ``BattleTestHarness`` against a
minimal fake wrapper that only provides the attributes the commands touch
(``_config.repo_path`` and ``_repl_print``). The full 6-layer harness is not
booted — we're testing the REPL dispatch logic and its interaction with the
persistent ``UserPreferenceStore`` singleton.

Each test uses ``tmp_path`` as the repo root so ``.jarvis/user_preferences``
lives in an isolated directory, and ``reset_default_store()`` clears the
module-level singleton before/after so state doesn't leak between cases.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from backend.core.ouroboros.battle_test.harness import BattleTestHarness
from backend.core.ouroboros.governance.user_preference_memory import (
    MemoryType,
    register_protected_path_provider,
    reset_default_store,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_harness(tmp_path: Path):
    """Minimal object with just the fields the REPL memory commands read.

    The four methods only access ``self._config.repo_path`` and
    ``self._repl_print(msg)``, so a ``SimpleNamespace`` is sufficient —
    no need to boot the real harness.
    """
    reset_default_store()
    register_protected_path_provider(None)
    printed: List[str] = []

    fake = SimpleNamespace(
        _config=SimpleNamespace(repo_path=tmp_path),
        _repl_print=printed.append,
    )
    # Attach the unbound methods so `fake._repl_cmd_memory(...)` works.
    # Duck-typed: these methods only read self._config.repo_path and call
    # self._repl_print — no need for a real BattleTestHarness instance.
    fake._repl_cmd_memory = lambda line: BattleTestHarness._repl_cmd_memory(fake, line)  # type: ignore[arg-type]
    fake._repl_cmd_remember = lambda line: BattleTestHarness._repl_cmd_remember(fake, line)  # type: ignore[arg-type]
    fake._repl_cmd_forget = lambda line: BattleTestHarness._repl_cmd_forget(fake, line)  # type: ignore[arg-type]
    fake._user_pref_store = lambda: BattleTestHarness._user_pref_store(fake)  # type: ignore[arg-type]
    fake.printed = printed

    yield fake

    reset_default_store()
    register_protected_path_provider(None)


def _find(printed: List[str], needle: str) -> bool:
    return any(needle in msg for msg in printed)


# ---------------------------------------------------------------------------
# _user_pref_store (singleton access)
# ---------------------------------------------------------------------------


class TestUserPrefStore:
    def test_returns_store_rooted_at_repo_path(self, fake_harness, tmp_path):
        store = fake_harness._user_pref_store()
        assert store is not None
        # store root should match our tmp_path — the store's memory directory
        # lives at <project_root>/.jarvis/user_preferences.
        assert store._dir == (tmp_path.resolve() / ".jarvis" / "user_preferences")

    def test_singleton_reuse(self, fake_harness):
        first = fake_harness._user_pref_store()
        second = fake_harness._user_pref_store()
        assert first is second


# ---------------------------------------------------------------------------
# /memory list
# ---------------------------------------------------------------------------


class TestMemoryList:
    def test_empty_list(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory")
        assert _find(fake_harness.printed, "No memories recorded")

    def test_list_after_add(self, fake_harness):
        store = fake_harness._user_pref_store()
        store.add(MemoryType.USER, "role", "Derek is an RSI architect")
        fake_harness.printed.clear()

        fake_harness._repl_cmd_memory("/memory list")
        assert _find(fake_harness.printed, "User Preference Memories")
        assert _find(fake_harness.printed, "role")
        assert _find(fake_harness.printed, "Derek is an RSI architect")

    def test_list_filtered_by_type(self, fake_harness):
        store = fake_harness._user_pref_store()
        store.add(MemoryType.USER, "user1", "user memory")
        store.add(MemoryType.FEEDBACK, "fb1", "feedback memory")
        fake_harness.printed.clear()

        fake_harness._repl_cmd_memory("/memory list feedback")
        assert _find(fake_harness.printed, "fb1")
        assert not _find(fake_harness.printed, "user1")

    def test_list_invalid_type_falls_back_to_all(self, fake_harness):
        store = fake_harness._user_pref_store()
        store.add(MemoryType.USER, "u1", "user one")
        fake_harness.printed.clear()

        fake_harness._repl_cmd_memory("/memory list gibberish")
        # Should not crash; shows all memories instead.
        assert _find(fake_harness.printed, "u1")


# ---------------------------------------------------------------------------
# /memory add
# ---------------------------------------------------------------------------


class TestMemoryAdd:
    def test_add_user_memory(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory add user my-role | I prefer terse answers")
        assert _find(fake_harness.printed, "Memory added")
        assert _find(fake_harness.printed, "user")

        store = fake_harness._user_pref_store()
        mems = store.find_by_type(MemoryType.USER)
        assert len(mems) == 1
        assert "terse" in mems[0].description

    def test_add_feedback_memory(self, fake_harness):
        fake_harness._repl_cmd_memory(
            "/memory add feedback no-mocks | Integration tests should hit the real DB"
        )
        store = fake_harness._user_pref_store()
        mems = store.find_by_type(MemoryType.FEEDBACK)
        assert len(mems) == 1

    def test_add_missing_pipe_shows_usage(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory add user bad-format no pipe")
        assert _find(fake_harness.printed, "Usage: /memory add")

    def test_add_missing_name(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory add user | just description")
        assert _find(fake_harness.printed, "Usage: /memory add")

    def test_add_empty_description(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory add user name-here |   ")
        assert _find(fake_harness.printed, "non-empty")

    def test_add_unknown_type_falls_back_to_user(self, fake_harness):
        """MemoryType.from_str is lenient — unknown values degrade to USER."""
        fake_harness._repl_cmd_memory("/memory add nonsense-type my-name | desc")
        assert _find(fake_harness.printed, "Memory added")
        store = fake_harness._user_pref_store()
        mems = store.find_by_type(MemoryType.USER)
        assert len(mems) == 1
        assert mems[0].name == "my-name"


# ---------------------------------------------------------------------------
# /memory rm
# ---------------------------------------------------------------------------


class TestMemoryRm:
    def test_remove_existing(self, fake_harness):
        store = fake_harness._user_pref_store()
        mem = store.add(MemoryType.USER, "victim", "to be removed")
        fake_harness.printed.clear()

        fake_harness._repl_cmd_memory(f"/memory rm {mem.id}")
        assert _find(fake_harness.printed, "Removed memory")
        assert store.get(mem.id) is None

    def test_remove_missing_id(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory rm nonexistent")
        assert _find(fake_harness.printed, "No memory matching")

    def test_remove_without_id(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory rm")
        assert _find(fake_harness.printed, "Usage: /memory rm")


# ---------------------------------------------------------------------------
# /memory forbid
# ---------------------------------------------------------------------------


class TestMemoryForbid:
    def test_forbid_creates_forbidden_path_memory(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory forbid secrets/")
        assert _find(fake_harness.printed, "Forbidden path added")

        store = fake_harness._user_pref_store()
        mems = store.find_by_type(MemoryType.FORBIDDEN_PATH)
        assert len(mems) == 1
        assert "secrets/" in mems[0].paths

    def test_forbid_registers_with_protected_path_provider(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory forbid legacy/auth/")
        # The store auto-registers itself as the protected-path provider,
        # so the tool_executor's hook should see our new pattern.
        from backend.core.ouroboros.governance.user_preference_memory import (
            get_protected_path_provider,
        )
        provider = get_protected_path_provider()
        assert provider is not None
        assert "legacy/auth/" in provider()

    def test_forbid_without_path(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory forbid")
        assert _find(fake_harness.printed, "Usage: /memory forbid")


# ---------------------------------------------------------------------------
# /memory show
# ---------------------------------------------------------------------------


class TestMemoryShow:
    def test_show_existing(self, fake_harness):
        store = fake_harness._user_pref_store()
        mem = store.add(
            MemoryType.PROJECT,
            "task195",
            "UserPreferenceMemory shipped",
        )
        fake_harness.printed.clear()

        fake_harness._repl_cmd_memory(f"/memory show {mem.id}")
        assert _find(fake_harness.printed, "project:task195")
        assert _find(fake_harness.printed, "UserPreferenceMemory shipped")

    def test_show_missing(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory show nonexistent")
        assert _find(fake_harness.printed, "No memory matching")

    def test_show_without_id(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory show")
        assert _find(fake_harness.printed, "Usage: /memory show")


# ---------------------------------------------------------------------------
# /memory <unknown subcommand>
# ---------------------------------------------------------------------------


class TestMemoryUnknownSubcommand:
    def test_unknown_subcommand_prints_usage(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory frobnicate")
        assert _find(fake_harness.printed, "Usage: /memory")


# ---------------------------------------------------------------------------
# /remember
# ---------------------------------------------------------------------------


class TestRemember:
    def test_remember_creates_user_memory(self, fake_harness):
        fake_harness._repl_cmd_remember("/remember I prefer terse code reviews")
        assert _find(fake_harness.printed, "Remembered")

        store = fake_harness._user_pref_store()
        mems = store.find_by_type(MemoryType.USER)
        assert len(mems) == 1
        assert "terse" in mems[0].description

    def test_remember_without_text(self, fake_harness):
        fake_harness._repl_cmd_remember("/remember")
        assert _find(fake_harness.printed, "Usage: /remember")

    def test_remember_with_only_whitespace(self, fake_harness):
        fake_harness._repl_cmd_remember("/remember    ")
        assert _find(fake_harness.printed, "Usage: /remember")

    def test_remember_upserts_on_repeat(self, fake_harness):
        """Repeat /remember with the same short prefix upserts, not duplicates."""
        fake_harness._repl_cmd_remember("/remember I prefer terse code reviews")
        fake_harness._repl_cmd_remember("/remember I prefer terse code reviews")
        store = fake_harness._user_pref_store()
        mems = store.find_by_type(MemoryType.USER)
        assert len(mems) == 1


# ---------------------------------------------------------------------------
# /forget
# ---------------------------------------------------------------------------


class TestForget:
    def test_forget_removes_memory(self, fake_harness):
        store = fake_harness._user_pref_store()
        mem = store.add(MemoryType.USER, "doomed", "to be forgotten")
        fake_harness.printed.clear()

        fake_harness._repl_cmd_forget(f"/forget {mem.id}")
        assert _find(fake_harness.printed, "Forgotten")
        assert store.get(mem.id) is None

    def test_forget_missing(self, fake_harness):
        fake_harness._repl_cmd_forget("/forget nonexistent")
        assert _find(fake_harness.printed, "No memory matching")

    def test_forget_without_id(self, fake_harness):
        fake_harness._repl_cmd_forget("/forget")
        assert _find(fake_harness.printed, "Usage: /forget")


# ---------------------------------------------------------------------------
# End-to-end sanity
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_add_list_show_remove_cycle(self, fake_harness):
        fake_harness._repl_cmd_memory("/memory add user my-preference | keep responses short")
        fake_harness._repl_cmd_memory("/memory list user")
        assert _find(fake_harness.printed, "my-preference")

        store = fake_harness._user_pref_store()
        mem = store.find_by_type(MemoryType.USER)[0]

        fake_harness.printed.clear()
        fake_harness._repl_cmd_memory(f"/memory show {mem.id}")
        assert _find(fake_harness.printed, "keep responses short")

        fake_harness.printed.clear()
        fake_harness._repl_cmd_forget(f"/forget {mem.id}")
        assert _find(fake_harness.printed, "Forgotten")
        assert store.get(mem.id) is None

    def test_remember_then_forbid_then_list(self, fake_harness):
        fake_harness._repl_cmd_remember("/remember always use asyncio not threads")
        fake_harness._repl_cmd_memory("/memory forbid tests/fixtures/")
        fake_harness._repl_cmd_memory("/memory")

        # List should include both — one USER and one FORBIDDEN_PATH
        combined = "\n".join(fake_harness.printed)
        assert "asyncio" in combined
        assert "tests/fixtures/" in combined
