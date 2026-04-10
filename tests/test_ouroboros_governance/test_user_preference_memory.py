"""Tests for UserPreferenceStore — persistent typed memory across O+V sessions.

Covers:
    - MemoryType enum tolerance
    - UserMemory round-trip through to_markdown() ↔ _parse_markdown()
    - CRUD (add, get, update, delete, list_all, find_by_type)
    - Persistence to disk and MEMORY.md index rebuild
    - Forbidden-path matching + _provide_protected_paths hook
    - Relevance scoring (path / tag / type bonus / FORBIDDEN_PATH boost / tiebreak)
    - format_for_prompt rendering
    - Postmortem hooks (record_approval_rejection, record_rollback) + dedupe
    - Protected-path provider auto-registration
    - Corrupt-file tolerance
    - Thread safety sanity check

These tests create a fresh ``tmp_path`` per case so ``.jarvis/user_preferences``
lives inside an isolated directory — no cross-test contamination, no
interference with the real repo.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.user_preference_memory import (
    MemoryType,
    UserMemory,
    UserPreferenceStore,
    _build_memory_id,
    _parse_list_value,
    _slug,
    _split_annotations,
    _yaml_escape,
    get_protected_path_provider,
    register_protected_path_provider,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_provider():
    """Ensure the global protected-path provider is cleared before and after."""
    register_protected_path_provider(None)
    yield
    register_protected_path_provider(None)


@pytest.fixture
def store(tmp_path: Path, clean_provider):
    """Fresh store rooted at tmp_path with the provider hook active."""
    return UserPreferenceStore(tmp_path, auto_register_protected_paths=True)


@pytest.fixture
def store_no_hook(tmp_path: Path, clean_provider):
    """Store that does NOT register itself as the global protected-path hook."""
    return UserPreferenceStore(tmp_path, auto_register_protected_paths=False)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_slug_basic(self):
        assert _slug("Hello World") == "hello_world"
        assert _slug("  Spaces  ") == "spaces"
        assert _slug("punct!@#$%^&*()") == "punct"

    def test_slug_preserves_alnum(self):
        assert _slug("abc123") == "abc123"

    def test_slug_empty(self):
        assert _slug("") == ""
        assert _slug("   ") == ""

    def test_slug_max_len(self):
        long = "a" * 100
        assert len(_slug(long, max_len=20)) == 20

    def test_slug_trims_trailing_underscore(self):
        # Long truncation should not leave dangling underscores.
        assert not _slug("foo_____", max_len=5).endswith("_")

    def test_build_memory_id(self):
        assert _build_memory_id(MemoryType.USER, "Derek Profile") == "user_derek_profile"
        assert _build_memory_id(MemoryType.FORBIDDEN_PATH, "auth middleware") == "forbidden_path_auth_middleware"

    def test_build_memory_id_fallback(self):
        # Pure-punct name slugs to "" → fallback to "unnamed".
        assert _build_memory_id(MemoryType.USER, "!!!") == "user_unnamed"

    def test_yaml_escape_passthrough(self):
        assert _yaml_escape("simple") == "simple"
        assert _yaml_escape("camelCase") == "camelCase"

    def test_yaml_escape_quotes_on_colon(self):
        assert _yaml_escape("key: value") == '"key: value"'

    def test_yaml_escape_quotes_on_leading_dash(self):
        assert _yaml_escape("-foo") == '"-foo"'

    def test_yaml_escape_quotes_on_hash(self):
        assert _yaml_escape("#comment") == '"#comment"'

    def test_yaml_escape_quotes_on_leading_space(self):
        assert _yaml_escape(" leading").startswith('"')

    def test_yaml_escape_escapes_inner_quotes(self):
        # Add a colon to force quoting, then verify inner quotes are escaped.
        out = _yaml_escape('has "quotes": yes')
        assert out.startswith('"')
        assert '\\"' in out

    def test_parse_list_value_inline(self):
        assert _parse_list_value("[a, b, c]") == ["a", "b", "c"]

    def test_parse_list_value_quoted(self):
        assert _parse_list_value('["a b", "c,d", e]') == ["a b", "c,d", "e"]

    def test_parse_list_value_bare_csv_fallback(self):
        # Tolerate hand-edited bare comma-separated values.
        assert _parse_list_value("a, b, c") == ["a", "b", "c"]

    def test_parse_list_value_empty(self):
        assert _parse_list_value("") == []
        assert _parse_list_value("[]") == []

    def test_split_annotations_all_present(self):
        body = "main content\n\n**Why:** the reason\n\n**How to apply:** the trigger"
        content, why, how = _split_annotations(body)
        assert content == "main content"
        assert why == "the reason"
        assert how == "the trigger"

    def test_split_annotations_only_content(self):
        content, why, how = _split_annotations("just content")
        assert content == "just content"
        assert why == ""
        assert how == ""

    def test_split_annotations_only_why(self):
        content, why, how = _split_annotations("main\n\n**Why:** because")
        assert content == "main"
        assert why == "because"
        assert how == ""


# ---------------------------------------------------------------------------
# MemoryType enum
# ---------------------------------------------------------------------------


class TestMemoryType:
    def test_values(self):
        assert MemoryType.USER.value == "user"
        assert MemoryType.FEEDBACK.value == "feedback"
        assert MemoryType.PROJECT.value == "project"
        assert MemoryType.REFERENCE.value == "reference"
        assert MemoryType.FORBIDDEN_PATH.value == "forbidden_path"
        assert MemoryType.STYLE.value == "style"

    def test_from_str_exact(self):
        assert MemoryType.from_str("feedback") is MemoryType.FEEDBACK
        assert MemoryType.from_str("forbidden_path") is MemoryType.FORBIDDEN_PATH

    def test_from_str_case_insensitive(self):
        assert MemoryType.from_str("USER") is MemoryType.USER
        assert MemoryType.from_str("  Style  ") is MemoryType.STYLE

    def test_from_str_unknown_falls_back_to_user(self):
        assert MemoryType.from_str("nonsense") is MemoryType.USER
        assert MemoryType.from_str("") is MemoryType.USER


# ---------------------------------------------------------------------------
# UserMemory dataclass behavior
# ---------------------------------------------------------------------------


class TestUserMemory:
    def _make(self, **overrides):
        base = dict(
            id="user_test",
            type=MemoryType.USER,
            name="test",
            description="desc",
            content="",
        )
        base.update(overrides)
        return UserMemory(**base)

    def test_frozen(self):
        mem = self._make()
        with pytest.raises(Exception):
            mem.name = "changed"  # type: ignore[misc]

    def test_matches_path_empty_paths(self):
        mem = self._make()
        assert mem.matches_path("src/foo.py") is False

    def test_matches_path_substring(self):
        mem = self._make(paths=("auth/",))
        assert mem.matches_path("src/auth/middleware.py") is True
        assert mem.matches_path("src/other/file.py") is False

    def test_matches_path_normalizes_backslashes(self):
        mem = self._make(paths=("auth/",))
        assert mem.matches_path("src\\auth\\middleware.py") is True

    def test_matches_path_empty_rel(self):
        mem = self._make(paths=("auth/",))
        assert mem.matches_path("") is False

    def test_to_markdown_minimal(self):
        mem = self._make(description="hello")
        md = mem.to_markdown()
        assert md.startswith("---\n")
        assert "id: user_test" in md
        assert "type: user" in md
        assert "name: test" in md
        assert "description: hello" in md

    def test_to_markdown_full_roundtrip(self):
        mem = self._make(
            description="a complex one: with colons",
            content="Main body text.",
            why="Because reasons.",
            how_to_apply="When condition X.",
            source="postmortem:op-1",
            tags=("alpha", "beta"),
            paths=("src/foo.py", "src/bar/"),
            created_at="2026-04-10T00:00:00Z",
            updated_at="2026-04-10T01:00:00Z",
        )
        md = mem.to_markdown()

        # Parse it back using the store's internal parser.
        parsed = UserPreferenceStore._parse_markdown(md, fallback_id="whatever")
        assert parsed is not None
        assert parsed.id == mem.id
        assert parsed.type is mem.type
        assert parsed.name == mem.name
        assert parsed.description == mem.description
        assert parsed.content == mem.content
        assert parsed.why == mem.why
        assert parsed.how_to_apply == mem.how_to_apply
        assert parsed.source == mem.source
        assert set(parsed.tags) == set(mem.tags)
        assert set(parsed.paths) == set(mem.paths)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestCRUD:
    def test_add_basic(self, store):
        mem = store.add(MemoryType.USER, "Derek", "The user's name")
        assert mem.id == "user_derek"
        assert mem.name == "Derek"
        assert mem.description == "The user's name"
        assert mem.created_at  # stamped
        assert mem.updated_at == mem.created_at

    def test_add_empty_name_raises(self, store):
        with pytest.raises(ValueError):
            store.add(MemoryType.USER, "", "desc")
        with pytest.raises(ValueError):
            store.add(MemoryType.USER, "   ", "desc")

    def test_add_empty_description_raises(self, store):
        with pytest.raises(ValueError):
            store.add(MemoryType.USER, "name", "")
        with pytest.raises(ValueError):
            store.add(MemoryType.USER, "name", "   ")

    def test_add_upsert_preserves_created_at(self, store):
        original = store.add(MemoryType.USER, "Derek", "first desc")
        # Sleep a tick so updated_at can differ.
        time.sleep(0.01)
        updated = store.add(MemoryType.USER, "Derek", "second desc")
        assert updated.id == original.id
        assert updated.description == "second desc"
        assert updated.created_at == original.created_at
        # updated_at may be the same ISO string depending on timing granularity.

    def test_add_strips_tags_and_paths(self, store):
        mem = store.add(
            MemoryType.FEEDBACK,
            "name",
            "desc",
            tags=("  alpha  ", "", "beta"),
            paths=(" src/foo ", "", "src/bar"),
        )
        assert mem.tags == ("alpha", "beta")
        assert mem.paths == ("src/foo", "src/bar")

    def test_get(self, store):
        mem = store.add(MemoryType.USER, "Derek", "desc")
        assert store.get(mem.id) == mem
        assert store.get("user_nonexistent") is None

    def test_update_whitelist_enforced(self, store):
        mem = store.add(MemoryType.USER, "Derek", "orig desc")
        # Try to mutate identity fields — should be silently ignored.
        updated = store.update(
            mem.id,
            id="hacked",
            type=MemoryType.FEEDBACK,
            name="hacked",
            created_at="2000-01-01T00:00:00Z",
            description="new desc",
        )
        assert updated is not None
        assert updated.id == mem.id  # unchanged
        assert updated.type is MemoryType.USER
        assert updated.name == "Derek"
        assert updated.created_at == mem.created_at
        assert updated.description == "new desc"

    def test_update_missing(self, store):
        assert store.update("user_missing", description="x") is None

    def test_update_no_mutable_fields_returns_existing(self, store):
        mem = store.add(MemoryType.USER, "Derek", "desc")
        updated = store.update(mem.id, id="ignored")
        assert updated == mem

    def test_update_tags_paths_normalized(self, store):
        mem = store.add(MemoryType.USER, "Derek", "desc")
        updated = store.update(mem.id, tags=["  a  ", "", "b"], paths=[" x ", "y"])
        assert updated is not None
        assert updated.tags == ("a", "b")
        assert updated.paths == ("x", "y")

    def test_delete(self, store, tmp_path):
        mem = store.add(MemoryType.USER, "Derek", "desc")
        on_disk = tmp_path / ".jarvis" / "user_preferences" / f"{mem.id}.md"
        assert on_disk.exists()
        assert store.delete(mem.id) is True
        assert not on_disk.exists()
        assert store.get(mem.id) is None

    def test_delete_missing(self, store):
        assert store.delete("user_never_existed") is False

    def test_list_all_sorted(self, store):
        store.add(MemoryType.USER, "b_user", "desc")
        store.add(MemoryType.USER, "a_user", "desc")
        store.add(MemoryType.FEEDBACK, "a_fb", "desc")
        all_mems = store.list_all()
        # sorted lexicographically by (type.value, id) — "feedback" < "user".
        assert all_mems[0].type is MemoryType.FEEDBACK
        assert all_mems[0].id == "feedback_a_fb"
        assert all_mems[1].type is MemoryType.USER
        assert all_mems[1].id == "user_a_user"
        assert all_mems[2].id == "user_b_user"

    def test_find_by_type(self, store):
        store.add(MemoryType.USER, "derek", "d")
        store.add(MemoryType.STYLE, "tabs", "d")
        store.add(MemoryType.STYLE, "braces", "d")
        styles = store.find_by_type(MemoryType.STYLE)
        assert len(styles) == 2
        assert all(m.type is MemoryType.STYLE for m in styles)


# ---------------------------------------------------------------------------
# Persistence + index rebuild
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_add_writes_file(self, store, tmp_path):
        mem = store.add(MemoryType.USER, "derek", "desc")
        memory_file = tmp_path / ".jarvis" / "user_preferences" / f"{mem.id}.md"
        assert memory_file.exists()
        raw = memory_file.read_text(encoding="utf-8")
        assert "id: user_derek" in raw
        assert "description: desc" in raw

    def test_index_rebuilt_on_add(self, store, tmp_path):
        store.add(MemoryType.USER, "derek", "Derek is the user")
        store.add(MemoryType.STYLE, "tabs", "use tabs not spaces")
        index = tmp_path / ".jarvis" / "user_preferences" / "MEMORY.md"
        assert index.exists()
        content = index.read_text(encoding="utf-8")
        assert "# User Preference Memory — Index" in content
        assert "## user" in content
        assert "## style" in content
        assert "[derek](user_derek.md)" in content
        assert "[tabs](style_tabs.md)" in content

    def test_index_rebuilt_on_delete(self, store, tmp_path):
        mem = store.add(MemoryType.USER, "derek", "desc")
        store.delete(mem.id)
        index = tmp_path / ".jarvis" / "user_preferences" / "MEMORY.md"
        assert index.exists()
        content = index.read_text(encoding="utf-8")
        assert "user_derek.md" not in content

    def test_load_picks_up_external_file(self, tmp_path, clean_provider):
        # Write a memory file by hand and verify load() picks it up.
        mem_dir = tmp_path / ".jarvis" / "user_preferences"
        mem_dir.mkdir(parents=True)
        (mem_dir / "user_handwritten.md").write_text(
            "---\n"
            "id: user_handwritten\n"
            "type: user\n"
            "name: handwritten\n"
            "description: written by a human\n"
            "---\n"
            "content body\n",
            encoding="utf-8",
        )
        store = UserPreferenceStore(tmp_path, auto_register_protected_paths=False)
        got = store.get("user_handwritten")
        assert got is not None
        assert got.description == "written by a human"
        assert "content body" in got.content

    def test_load_skips_non_md_files(self, tmp_path, clean_provider):
        mem_dir = tmp_path / ".jarvis" / "user_preferences"
        mem_dir.mkdir(parents=True)
        (mem_dir / "junk.txt").write_text("not a memory")
        (mem_dir / "subdir").mkdir()
        store = UserPreferenceStore(tmp_path, auto_register_protected_paths=False)
        assert store.list_all() == []

    def test_load_skips_corrupt_md(self, tmp_path, clean_provider):
        mem_dir = tmp_path / ".jarvis" / "user_preferences"
        mem_dir.mkdir(parents=True)
        # No frontmatter at all.
        (mem_dir / "user_corrupt.md").write_text("just some text, no frontmatter")
        # Incomplete frontmatter (missing required fields).
        (mem_dir / "user_incomplete.md").write_text(
            "---\n"
            "id: user_incomplete\n"
            "type: user\n"
            "---\n"
            "body\n"
        )
        # Valid one to prove the loader keeps going.
        (mem_dir / "user_good.md").write_text(
            "---\n"
            "id: user_good\n"
            "type: user\n"
            "name: good\n"
            "description: this one works\n"
            "---\n"
            "body\n"
        )
        store = UserPreferenceStore(tmp_path, auto_register_protected_paths=False)
        assert store.get("user_good") is not None
        assert store.get("user_corrupt") is None
        assert store.get("user_incomplete") is None

    def test_reload_discards_in_memory_state(self, tmp_path, clean_provider):
        store = UserPreferenceStore(tmp_path, auto_register_protected_paths=False)
        mem = store.add(MemoryType.USER, "derek", "desc")
        # Delete the file directly (simulating external edit).
        (tmp_path / ".jarvis" / "user_preferences" / f"{mem.id}.md").unlink()
        store.reload()
        assert store.get(mem.id) is None


# ---------------------------------------------------------------------------
# Forbidden-path matching + protected-path provider
# ---------------------------------------------------------------------------


class TestForbiddenPaths:
    def test_find_forbidden_for_path(self, store):
        store.add(
            MemoryType.FORBIDDEN_PATH,
            "auth rewrites",
            "Do not touch auth middleware",
            paths=("auth/middleware.py", "src/auth/"),
        )
        store.add(
            MemoryType.FORBIDDEN_PATH,
            "secrets",
            "Do not touch secrets dir",
            paths=("secrets/",),
        )
        # A USER memory with a paths field should NOT match here.
        store.add(MemoryType.USER, "derek", "desc", paths=("src/foo.py",))

        matches = store.find_forbidden_for_path("src/auth/login.py")
        assert len(matches) == 1
        assert matches[0].name == "auth rewrites"

        matches_secrets = store.find_forbidden_for_path("secrets/api_keys.env")
        assert len(matches_secrets) == 1
        assert matches_secrets[0].name == "secrets"

        assert store.find_forbidden_for_path("src/other/file.py") == []

    def test_provide_protected_paths_callback(self, store):
        store.add(
            MemoryType.FORBIDDEN_PATH,
            "auth",
            "no touch",
            paths=("auth/", "src/auth_service.py"),
        )
        store.add(
            MemoryType.FORBIDDEN_PATH,
            "secrets",
            "no touch",
            paths=("secrets/",),
        )
        store.add(MemoryType.USER, "derek", "desc", paths=("ignored/",))

        paths = store._provide_protected_paths()
        assert "auth/" in paths
        assert "src/auth_service.py" in paths
        assert "secrets/" in paths
        assert "ignored/" not in paths  # USER memory paths not included

    def test_auto_registers_hook(self, store):
        # The fixture creates a store with auto_register_protected_paths=True.
        provider = get_protected_path_provider()
        assert provider is not None

        store.add(
            MemoryType.FORBIDDEN_PATH,
            "auth",
            "no touch",
            paths=("auth/middleware.py",),
        )
        assert "auth/middleware.py" in list(provider())

    def test_no_hook_when_disabled(self, store_no_hook):
        # store_no_hook uses auto_register_protected_paths=False
        assert get_protected_path_provider() is None

    def test_register_and_clear_hook(self, clean_provider):
        def prov():
            return ["foo/"]

        register_protected_path_provider(prov)
        assert get_protected_path_provider() is prov
        register_protected_path_provider(None)
        assert get_protected_path_provider() is None


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------


class TestRelevanceScoring:
    def test_path_match_beats_type_bonus(self, store):
        # A STYLE memory with matching path should beat an untargeted USER memory.
        store.add(MemoryType.USER, "derek", "just a user memory")
        store.add(
            MemoryType.STYLE,
            "auth style",
            "use middleware pattern",
            paths=("auth/",),
        )
        ranked = store.find_relevant(target_files=["src/auth/login.py"])
        assert ranked[0].name == "auth style"

    def test_tag_match_beats_type_bonus(self, store):
        store.add(MemoryType.USER, "derek", "just a user memory")
        store.add(
            MemoryType.FEEDBACK,
            "testing rule",
            "description mentions testing",
            tags=("testing", "integration"),
        )
        ranked = store.find_relevant(description="add integration testing")
        assert ranked[0].name == "testing rule"

    def test_forbidden_path_surfaces_on_match(self, store):
        store.add(MemoryType.USER, "derek", "normal memory")
        store.add(
            MemoryType.FORBIDDEN_PATH,
            "auth",
            "do not touch auth",
            paths=("auth/",),
        )
        ranked = store.find_relevant(target_files=["src/auth/login.py"])
        assert ranked[0].name == "auth"
        assert ranked[0].type is MemoryType.FORBIDDEN_PATH

    def test_forbidden_path_not_surfaced_without_match(self, store):
        store.add(
            MemoryType.FORBIDDEN_PATH,
            "auth",
            "do not touch auth",
            paths=("auth/",),
        )
        ranked = store.find_relevant(target_files=["src/other.py"])
        # FORBIDDEN_PATH with no match → score 0 → not returned.
        assert all(m.type is not MemoryType.FORBIDDEN_PATH for m in ranked)

    def test_limit_honored(self, store):
        for i in range(10):
            store.add(MemoryType.USER, f"user{i}", f"desc {i}")
        ranked = store.find_relevant(limit=3)
        assert len(ranked) == 3

    def test_zero_score_excluded(self, store):
        # A REFERENCE memory with no tags/paths has score 0 with no context.
        store.add(MemoryType.REFERENCE, "linear", "pipeline bugs live in Linear")
        ranked = store.find_relevant()
        # No target_files and no description → no signal → score 0 → excluded.
        assert all(m.name != "linear" for m in ranked)

    def test_freshest_tiebreak(self, tmp_path, clean_provider, monkeypatch):
        store = UserPreferenceStore(tmp_path, auto_register_protected_paths=False)
        # Two USER memories; same score (just type bonus). Fresher one wins.
        # ISO format is second-precision, so drive timestamps via monkeypatch
        # to guarantee distinct values without sleeping a full second.
        from backend.core.ouroboros.governance import user_preference_memory as mod

        clock = ["2026-04-10T00:00:00Z"]
        monkeypatch.setattr(mod, "_utc_now_iso", lambda: clock[0])
        store.add(MemoryType.USER, "older", "older memory")
        clock[0] = "2026-04-10T00:00:05Z"
        store.add(MemoryType.USER, "newer", "newer memory")
        ranked = store.find_relevant()
        assert ranked[0].name == "newer"

    def test_path_match_normalizes_windows_separators(self, store):
        store.add(
            MemoryType.FORBIDDEN_PATH,
            "auth",
            "no touch",
            paths=("auth/middleware",),
        )
        ranked = store.find_relevant(target_files=["src\\auth\\middleware.py"])
        assert any(m.name == "auth" for m in ranked)


# ---------------------------------------------------------------------------
# format_for_prompt
# ---------------------------------------------------------------------------


class TestFormatForPrompt:
    def test_empty_when_no_memories(self, store):
        assert store.format_for_prompt(target_files=["src/foo.py"]) == ""

    def test_empty_when_no_matches(self, store):
        store.add(MemoryType.REFERENCE, "linear", "pipeline bugs")
        # No context — reference memory scores 0, prompt is empty.
        assert store.format_for_prompt() == ""

    def test_renders_section_header(self, store):
        store.add(MemoryType.USER, "derek", "the user")
        out = store.format_for_prompt()
        assert "## User Preferences (persistent memory)" in out
        assert "user:derek" in out
        assert "the user" in out

    def test_renders_why_and_how(self, store):
        store.add(
            MemoryType.FEEDBACK,
            "no mocks",
            "Do not mock the database in tests",
            why="Got burned by mock/prod divergence last quarter",
            how_to_apply="When writing integration tests",
            tags=("testing",),
        )
        out = store.format_for_prompt(description="add testing for X")
        assert "Why: Got burned by mock/prod divergence last quarter" in out
        assert "How to apply: When writing integration tests" in out

    def test_renders_forbidden_path_hard_block(self, store):
        store.add(
            MemoryType.FORBIDDEN_PATH,
            "auth",
            "do not touch auth",
            paths=("auth/middleware.py",),
        )
        out = store.format_for_prompt(target_files=["src/auth/middleware.py"])
        assert "HARD BLOCK on paths: auth/middleware.py" in out

    def test_includes_tags_in_bullet(self, store):
        store.add(
            MemoryType.USER,
            "derek",
            "Go expert",
            tags=("go", "backend"),
        )
        out = store.format_for_prompt()
        assert "[go, backend]" in out


# ---------------------------------------------------------------------------
# Postmortem hooks
# ---------------------------------------------------------------------------


class TestPostmortemHooks:
    def test_approval_rejection_creates_feedback(self, store):
        mem = store.record_approval_rejection(
            op_id="op-123",
            description="rewrite auth middleware",
            target_files=["src/auth/middleware.py"],
            reason="Legal hasn't signed off on storing session tokens this way",
        )
        assert mem is not None
        assert mem.type is MemoryType.FEEDBACK
        assert "Legal" in mem.why
        assert "rejection" in mem.tags
        assert "approval" in mem.tags
        assert mem.source.startswith("approval_reject:op-123")
        assert "src/auth/middleware.py" in mem.paths

    def test_approval_rejection_skipped_on_empty_reason(self, store):
        assert store.record_approval_rejection(
            op_id="op-empty",
            description="desc",
            target_files=["src/foo.py"],
            reason="",
        ) is None
        assert store.record_approval_rejection(
            op_id="op-empty",
            description="desc",
            target_files=[],
            reason="   ",
        ) is None

    def test_approval_rejection_dedupes_same_description(self, store):
        mem1 = store.record_approval_rejection(
            op_id="op-1",
            description="rewrite auth middleware",
            target_files=[],
            reason="first reason",
        )
        mem2 = store.record_approval_rejection(
            op_id="op-2",
            description="rewrite auth middleware",
            target_files=[],
            reason="second reason",
        )
        assert mem1 is not None
        assert mem2 is not None
        # Same description → same slug → same id → upsert.
        assert mem1.id == mem2.id
        assert mem2.why == "second reason"
        assert len(store.find_by_type(MemoryType.FEEDBACK)) == 1

    def test_approval_rejection_uses_approver(self, store):
        mem = store.record_approval_rejection(
            op_id="op-1",
            description="desc",
            target_files=[],
            reason="reason",
            approver="derek",
        )
        assert mem is not None
        assert "derek" in mem.source

    def test_approval_rejection_path_capped_at_four(self, store):
        mem = store.record_approval_rejection(
            op_id="op-1",
            description="desc",
            target_files=[f"src/f{i}.py" for i in range(10)],
            reason="reason",
        )
        assert mem is not None
        assert len(mem.paths) == 4

    def test_record_rollback_creates_feedback(self, store):
        mem = store.record_rollback(
            op_id="op-7",
            description="add caching to oracle",
            target_files=["backend/core/ouroboros/oracle.py"],
            failure_class="test",
            summary="test_oracle_query regressed with AttributeError",
        )
        assert mem is not None
        assert mem.type is MemoryType.FEEDBACK
        assert "test" in mem.tags
        assert "rollback" in mem.tags
        assert "AttributeError" in mem.why
        assert mem.source == "rollback:op-7"

    def test_record_rollback_skipped_on_empty_summary(self, store):
        assert store.record_rollback(
            op_id="op-1",
            description="desc",
            target_files=[],
            failure_class="build",
            summary="",
        ) is None


# ---------------------------------------------------------------------------
# Corrupt file tolerance
# ---------------------------------------------------------------------------


class TestCorruptFileHandling:
    def test_load_after_external_corruption(self, store, tmp_path):
        mem = store.add(MemoryType.USER, "derek", "desc")
        # Corrupt the file externally.
        path = tmp_path / ".jarvis" / "user_preferences" / f"{mem.id}.md"
        path.write_text("garbage without frontmatter", encoding="utf-8")
        store.reload()
        assert store.get(mem.id) is None
        # No crash — store is still functional.
        store.add(MemoryType.USER, "derek_again", "works")
        assert store.get("user_derek_again") is not None


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_adds(self, store):
        def worker(i: int):
            store.add(MemoryType.USER, f"user{i}", f"desc {i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        all_mems = store.list_all()
        assert len(all_mems) == 20
        names = {m.name for m in all_mems}
        assert names == {f"user{i}" for i in range(20)}

    def test_concurrent_reads_and_writes(self, store):
        errors: list = []

        def writer():
            try:
                for i in range(20):
                    store.add(MemoryType.USER, f"w{i}", f"desc {i}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def reader():
            try:
                for _ in range(30):
                    store.list_all()
                    store.find_relevant(target_files=["src/foo.py"])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent access raised: {errors}"
