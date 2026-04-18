"""Regression spine for the FORBIDDEN_APP memory type.

Task 4 of the VisionSensor + Visual VERIFY implementation plan. Pins:

* ``MemoryType.FORBIDDEN_APP`` enum variant and lenient string parser.
* ``UserMemory.apps`` storage field and ``matches_app`` exact-match
  (case-insensitive) semantics — substring matching is deliberately
  rejected because denylists must not over-trigger (``com.apple.mail``
  must not block ``com.apple.mailapp``).
* ``UserPreferenceStore`` CRUD on the ``apps`` field, plus round-trip
  through :meth:`to_markdown` / :meth:`_parse_markdown`.
* ``find_forbidden_for_app`` / ``is_forbidden_app`` lookup methods.
* ``_provide_protected_apps`` provider hook + module-level register /
  get wiring (parallel to ``_provide_protected_paths``).
* Relevance scoring: FORBIDDEN_APP matches dominate their bucket via
  the same ``2x`` multiplier as FORBIDDEN_PATH.

The legacy path-based test file remains untouched. This file is
scoped purely to the new app-based denylist surface so a future
regression in FORBIDDEN_APP cannot be accidentally masked by an
unrelated path-hook change.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.user_preference_memory import (
    MemoryType,
    UserMemory,
    UserPreferenceStore,
    get_protected_app_provider,
    get_protected_path_provider,
    register_protected_app_provider,
    register_protected_path_provider,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_providers():
    """Clear both global providers before and after each test."""
    register_protected_path_provider(None)
    register_protected_app_provider(None)
    yield
    register_protected_path_provider(None)
    register_protected_app_provider(None)


@pytest.fixture
def store(tmp_path: Path, clean_providers):
    """Fresh store rooted at tmp_path with both providers auto-registered."""
    return UserPreferenceStore(
        tmp_path,
        auto_register_protected_paths=True,
        auto_register_protected_apps=True,
    )


@pytest.fixture
def isolated_store(tmp_path: Path, clean_providers):
    """Store with BOTH auto-registration flags off — used for provider-hook tests."""
    return UserPreferenceStore(
        tmp_path,
        auto_register_protected_paths=False,
        auto_register_protected_apps=False,
    )


# ---------------------------------------------------------------------------
# Enum + lenient parse
# ---------------------------------------------------------------------------


def test_memory_type_forbidden_app_present():
    assert MemoryType.FORBIDDEN_APP.value == "forbidden_app"


def test_memory_type_from_str_parses_forbidden_app():
    assert MemoryType.from_str("forbidden_app") is MemoryType.FORBIDDEN_APP
    assert MemoryType.from_str("FORBIDDEN_APP") is MemoryType.FORBIDDEN_APP
    assert MemoryType.from_str(" forbidden_app ") is MemoryType.FORBIDDEN_APP


def test_memory_type_from_str_unknown_still_falls_back_to_user():
    # Existing invariant preserved after adding a new variant
    assert MemoryType.from_str("forbidden_apps") is MemoryType.USER
    assert MemoryType.from_str("") is MemoryType.USER


# ---------------------------------------------------------------------------
# UserMemory.apps + matches_app
# ---------------------------------------------------------------------------


def test_user_memory_apps_defaults_empty():
    m = UserMemory(
        id="t1",
        type=MemoryType.FORBIDDEN_APP,
        name="x",
        description="y",
        content="",
    )
    assert m.apps == ()


def test_user_memory_matches_app_exact_bundle_id():
    m = UserMemory(
        id="t1",
        type=MemoryType.FORBIDDEN_APP,
        name="no_1password",
        description="no 1password",
        content="",
        apps=("com.1password.mac",),
    )
    assert m.matches_app("com.1password.mac") is True
    assert m.matches_app("COM.1PASSWORD.MAC") is True     # case-insensitive
    assert m.matches_app(" com.1password.mac ") is True   # strips whitespace


def test_user_memory_matches_app_rejects_substring_overlap():
    """Denylist must not over-trigger. ``com.apple.mail`` must NOT match
    ``com.apple.mailapp`` even though the former is a prefix.
    """
    m = UserMemory(
        id="t1",
        type=MemoryType.FORBIDDEN_APP,
        name="no_mail",
        description="no mail",
        content="",
        apps=("com.apple.mail",),
    )
    assert m.matches_app("com.apple.mailapp") is False
    assert m.matches_app("com.apple.mail.daemon") is False
    assert m.matches_app("") is False


def test_user_memory_matches_app_returns_false_when_apps_empty():
    m = UserMemory(
        id="t1",
        type=MemoryType.USER,
        name="x",
        description="y",
        content="",
    )
    assert m.matches_app("com.anything") is False


# ---------------------------------------------------------------------------
# UserPreferenceStore.add — apps accepted + lowercased
# ---------------------------------------------------------------------------


def test_add_accepts_apps_argument(store):
    mem = store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="no_1password",
        description="never analyze 1Password frames",
        apps=("com.1password.mac", "com.1password7.mac"),
    )
    assert set(mem.apps) == {"com.1password.mac", "com.1password7.mac"}


def test_add_lowercases_and_strips_apps(store):
    mem = store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="no_mix",
        description="case mix",
        apps=("  COM.Apple.Mail  ", "\torg.Mozilla.Firefox"),
    )
    assert mem.apps == ("com.apple.mail", "org.mozilla.firefox")


def test_add_drops_empty_app_entries(store):
    mem = store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="no_blanks",
        description="trim blanks",
        apps=("", "   ", "com.real.app"),
    )
    assert mem.apps == ("com.real.app",)


# ---------------------------------------------------------------------------
# UserPreferenceStore.update — apps mutable, lowercased
# ---------------------------------------------------------------------------


def test_update_allows_apps_mutation(store):
    mem = store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="rolling",
        description="rolling denylist",
        apps=("com.first.app",),
    )
    updated = store.update(mem.id, apps=("com.second.app", "COM.THIRD.APP"))
    assert updated is not None
    assert updated.apps == ("com.second.app", "com.third.app")


def test_update_ignores_type_mutation(store):
    """Same guardrail as paths/tags — type is identity, not mutable."""
    mem = store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="identity_lock",
        description="type is identity",
        apps=("com.x.y",),
    )
    updated = store.update(mem.id, type=MemoryType.USER)
    assert updated is not None
    assert updated.type is MemoryType.FORBIDDEN_APP  # unchanged


# ---------------------------------------------------------------------------
# Disk round-trip
# ---------------------------------------------------------------------------


def test_apps_roundtrip_through_disk(tmp_path: Path, clean_providers):
    s1 = UserPreferenceStore(
        tmp_path, auto_register_protected_apps=False
    )
    s1.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="no_kc",
        description="never analyze Keychain Access",
        apps=("com.apple.keychainaccess",),
    )
    # Fresh store reads from disk — apps must survive verbatim.
    s2 = UserPreferenceStore(
        tmp_path, auto_register_protected_apps=False
    )
    reloaded = s2.find_by_type(MemoryType.FORBIDDEN_APP)
    assert len(reloaded) == 1
    assert reloaded[0].apps == ("com.apple.keychainaccess",)


def test_legacy_file_without_apps_field_still_parses(tmp_path: Path, clean_providers):
    """V1 memory files predating FORBIDDEN_APP must load with empty ``apps``."""
    memdir = tmp_path / ".jarvis" / "user_preferences"
    memdir.mkdir(parents=True)
    (memdir / "user_example.md").write_text(
        "---\n"
        "id: user_example\n"
        "type: user\n"
        "name: example\n"
        "description: legacy user memory from before FORBIDDEN_APP shipped\n"
        "source: user\n"
        "---\n"
        "body content\n",
        encoding="utf-8",
    )
    s = UserPreferenceStore(tmp_path, auto_register_protected_apps=False)
    loaded = s.get("user_example")
    assert loaded is not None
    assert loaded.apps == ()


# ---------------------------------------------------------------------------
# find_forbidden_for_app / is_forbidden_app
# ---------------------------------------------------------------------------


def test_is_forbidden_app_positive(store):
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="no_signal",
        description="never Signal",
        apps=("org.whispersystems.signal-desktop",),
    )
    assert store.is_forbidden_app("org.whispersystems.signal-desktop") is True
    # Case-insensitive
    assert store.is_forbidden_app("Org.WhisperSystems.Signal-Desktop") is True


def test_is_forbidden_app_negative(store):
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="no_signal",
        description="never Signal",
        apps=("org.whispersystems.signal-desktop",),
    )
    assert store.is_forbidden_app("com.unrelated.app") is False
    assert store.is_forbidden_app("") is False


def test_is_forbidden_app_ignores_non_forbidden_types(store):
    """A USER memory whose ``apps`` happens to contain a bundle id is NOT
    a denylist. Only FORBIDDEN_APP entries count."""
    # UserPreferenceStore.add accepts apps on any type; the filter fires
    # at lookup time.
    store.add(
        memory_type=MemoryType.USER,
        name="uses_terminal",
        description="user uses Terminal heavily",
        apps=("com.apple.terminal",),
    )
    assert store.is_forbidden_app("com.apple.terminal") is False


def test_find_forbidden_for_app_returns_all_matches(store):
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="mail_block_a",
        description="first mail rule",
        apps=("com.apple.mail",),
    )
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="mail_block_b",
        description="second mail rule",
        apps=("com.apple.mail", "com.microsoft.outlook"),
    )
    hits = store.find_forbidden_for_app("com.apple.mail")
    assert {m.name for m in hits} == {"mail_block_a", "mail_block_b"}


def test_find_forbidden_for_app_empty_when_no_match(store):
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="x",
        description="x",
        apps=("com.foo.bar",),
    )
    assert store.find_forbidden_for_app("com.other.app") == []


# ---------------------------------------------------------------------------
# Provider hook
# ---------------------------------------------------------------------------


def test_provide_protected_apps_returns_flat_union(store):
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="rule_a",
        description="a",
        apps=("com.a.one", "com.a.two"),
    )
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="rule_b",
        description="b",
        apps=("com.b.one",),
    )
    provided = list(store._provide_protected_apps())
    assert set(provided) == {"com.a.one", "com.a.two", "com.b.one"}


def test_provide_protected_apps_excludes_non_forbidden(store):
    store.add(
        memory_type=MemoryType.USER,
        name="user_pref",
        description="not a denylist",
        apps=("com.notblocked.app",),
    )
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="real_block",
        description="yes",
        apps=("com.actuallyblocked.app",),
    )
    provided = list(store._provide_protected_apps())
    assert "com.notblocked.app" not in provided
    assert "com.actuallyblocked.app" in provided


def test_auto_register_app_provider_wires_global_hook(store):
    # store fixture passes auto_register_protected_apps=True
    hook = get_protected_app_provider()
    assert hook is not None
    # Bound-method equality (not identity — each attribute access produces
    # a fresh bound-method object, so ``is`` fails even when the underlying
    # function is the same).
    assert hook == store._provide_protected_apps
    # Invoking the hook returns an iterable of strings, same as the
    # protected-path provider contract.
    assert isinstance(list(hook()), list)


def test_isolated_store_does_not_register_global_hook(isolated_store):
    assert get_protected_app_provider() is None


def test_path_and_app_hooks_are_independent(store):
    # Both providers should be set by the default fixture — they do not
    # clobber each other.
    assert get_protected_path_provider() is not None
    assert get_protected_app_provider() is not None


# ---------------------------------------------------------------------------
# Relevance scoring — FORBIDDEN_APP match dominates bucket
# ---------------------------------------------------------------------------


def test_forbidden_app_match_surfaces_in_find_relevant(store):
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="no_1password",
        description="never 1Password",
        apps=("com.1password.mac",),
    )
    # Add a USER memory that would otherwise score higher (base bonus + tag)
    store.add(
        memory_type=MemoryType.USER,
        name="identity",
        description="user identity pref",
        tags=("identity",),
    )
    hits = store.find_relevant(
        target_app_id="com.1password.mac",
        description="identity",
        limit=5,
    )
    # FORBIDDEN_APP must be first (double-score bucket dominance)
    assert hits[0].type is MemoryType.FORBIDDEN_APP
    assert hits[0].name == "no_1password"


def test_forbidden_app_does_not_surface_without_matching_bundle(store):
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="no_1p",
        description="never 1Password",
        apps=("com.1password.mac",),
    )
    hits = store.find_relevant(target_app_id="com.other.app", limit=5)
    # Without a matching bundle id, the forbidden memory does not score
    # and should not appear.
    assert all(m.type is not MemoryType.FORBIDDEN_APP for m in hits)


def test_non_forbidden_apps_match_gives_single_bucket_not_double(store):
    """A USER memory with ``apps=(...)`` that matches the bundle should
    score path-match once (vision-scope signal) but NOT get the
    FORBIDDEN_APP double-bucket — that multiplier is the denial
    dominance, not a generic bundle-match reward.
    """
    # Two USER memories; one has a matching app tag, one doesn't.
    store.add(
        memory_type=MemoryType.USER,
        name="terminal_user",
        description="user uses Terminal",
        apps=("com.apple.terminal",),
    )
    store.add(
        memory_type=MemoryType.USER,
        name="plain_user",
        description="plain note",
    )
    hits = store.find_relevant(target_app_id="com.apple.terminal", limit=5)
    # Both surface (both get USER baseline); the terminal_user one scores
    # higher due to the single bundle-match increment, not the double.
    names = [m.name for m in hits]
    assert "terminal_user" in names
    # Order: terminal_user before plain_user
    assert names.index("terminal_user") < names.index("plain_user")


def test_find_relevant_app_match_case_insensitive(store):
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="no_kc",
        description="never Keychain",
        apps=("com.apple.keychainaccess",),
    )
    hits_lower = store.find_relevant(target_app_id="com.apple.keychainaccess")
    hits_mixed = store.find_relevant(target_app_id="COM.APPLE.KEYCHAINACCESS")
    assert hits_lower and hits_mixed
    assert hits_lower[0].id == hits_mixed[0].id


def test_find_relevant_without_target_app_id_behaves_like_pre_task4(store):
    """Backward-compat: existing callers that don't pass ``target_app_id``
    get identical behaviour to the pre-Task-4 code path — FORBIDDEN_APP
    memories with no matching bundle simply don't score, same as before."""
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="no_app",
        description="some denylist",
        apps=("com.some.app",),
    )
    store.add(
        memory_type=MemoryType.USER,
        name="plain",
        description="plain user memory",
    )
    hits = store.find_relevant(description="plain", limit=5)
    # The FORBIDDEN_APP entry has no target_app_id to match against and
    # no other score source, so it should not appear.
    assert all(m.type is not MemoryType.FORBIDDEN_APP for m in hits)


# ---------------------------------------------------------------------------
# format_for_prompt plumbs target_app_id
# ---------------------------------------------------------------------------


def test_format_for_prompt_threads_target_app_id(store):
    store.add(
        memory_type=MemoryType.FORBIDDEN_APP,
        name="no_1password",
        description="never 1Password",
        apps=("com.1password.mac",),
    )
    rendered = store.format_for_prompt(target_app_id="com.1password.mac")
    assert "no_1password" in rendered
    assert "forbidden_app" in rendered
