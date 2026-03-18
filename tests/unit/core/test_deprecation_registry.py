"""tests/unit/core/test_deprecation_registry.py — P3-5 deprecation policy tests."""
from __future__ import annotations

import pytest

from backend.core.deprecation_registry import (
    GraceWindowStatus,
    DeprecationEntry,
    DeprecationExpiredError,
    DeprecationRegistry,
    deprecated,
    get_deprecation_registry,
)


class TestDeprecationEntry:
    def test_current_when_version_before_deprecated_since(self):
        e = DeprecationEntry(
            item_id="foo",
            deprecated_since="2.3.0",
            removal_version="3.0.0",
            migration_path="use bar",
        )
        assert e.status("2.2.9") == GraceWindowStatus.CURRENT

    def test_deprecated_warn_within_grace_window(self):
        e = DeprecationEntry(
            item_id="foo",
            deprecated_since="2.3.0",
            removal_version="3.0.0",
            migration_path="use bar",
        )
        assert e.status("2.5.0") == GraceWindowStatus.DEPRECATED_WARN

    def test_deprecated_fail_at_removal_version(self):
        e = DeprecationEntry(
            item_id="foo",
            deprecated_since="2.3.0",
            removal_version="3.0.0",
            migration_path="use bar",
        )
        assert e.status("3.0.0") == GraceWindowStatus.DEPRECATED_FAIL

    def test_deprecated_fail_after_removal_version(self):
        e = DeprecationEntry(
            item_id="foo",
            deprecated_since="2.3.0",
            removal_version="3.0.0",
            migration_path="use bar",
        )
        assert e.status("3.1.0") == GraceWindowStatus.DEPRECATED_FAIL

    def test_entry_is_frozen(self):
        import dataclasses
        e = DeprecationEntry(
            item_id="foo",
            deprecated_since="2.0.0",
            removal_version="3.0.0",
            migration_path="use bar",
        )
        assert dataclasses.is_dataclass(e)
        with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            e.item_id = "tampered"  # type: ignore[misc]


class TestDeprecationRegistry:
    def _registry(self) -> DeprecationRegistry:
        return DeprecationRegistry()

    def test_register_and_check_current(self):
        r = self._registry()
        r.register("myapi", deprecated_since="2.3.0", removal_version="3.0.0",
                   migration_path="use newapi")
        status = r.check("myapi", "2.2.0")
        assert status == GraceWindowStatus.CURRENT

    def test_check_unknown_item_returns_current(self):
        r = self._registry()
        assert r.check("nonexistent", "2.0.0") == GraceWindowStatus.CURRENT

    def test_check_deprecated_warn_emits_warning(self, caplog):
        import logging
        r = self._registry()
        r.register("myapi", deprecated_since="2.3.0", removal_version="3.0.0",
                   migration_path="use newapi")
        with caplog.at_level(logging.WARNING):
            status = r.check("myapi", "2.5.0")
        assert status == GraceWindowStatus.DEPRECATED_WARN
        assert any("DEPRECATED" in rec.message for rec in caplog.records)

    def test_check_deprecated_fail_raises(self):
        r = self._registry()
        r.register("myapi", deprecated_since="2.3.0", removal_version="3.0.0",
                   migration_path="use newapi")
        with pytest.raises(DeprecationExpiredError) as exc_info:
            r.check("myapi", "3.0.0")
        err = exc_info.value
        assert err.item_id == "myapi"
        assert err.removal_version == "3.0.0"
        assert "newapi" in err.migration_path

    def test_register_same_deprecated_since_as_removal_raises(self):
        r = self._registry()
        with pytest.raises(ValueError):
            r.register("bad", deprecated_since="3.0.0", removal_version="3.0.0",
                       migration_path="")

    def test_register_deprecated_since_after_removal_raises(self):
        r = self._registry()
        with pytest.raises(ValueError):
            r.register("bad", deprecated_since="4.0.0", removal_version="3.0.0",
                       migration_path="")

    def test_get_all_expired(self):
        r = self._registry()
        r.register("old", deprecated_since="1.0.0", removal_version="2.0.0",
                   migration_path="x")
        r.register("current", deprecated_since="3.0.0", removal_version="4.0.0",
                   migration_path="y")
        expired = r.get_all_expired("3.0.0")
        assert len(expired) == 1
        assert expired[0].item_id == "old"

    def test_get_all_warned(self):
        r = self._registry()
        r.register("warn1", deprecated_since="2.0.0", removal_version="3.0.0",
                   migration_path="a")
        r.register("warn2", deprecated_since="2.0.0", removal_version="3.0.0",
                   migration_path="b")
        r.register("future", deprecated_since="5.0.0", removal_version="6.0.0",
                   migration_path="c")
        warned = r.get_all_warned("2.5.0")
        assert {e.item_id for e in warned} == {"warn1", "warn2"}

    def test_all_warned_items_have_migration_paths(self):
        r = self._registry()
        r.register("item", deprecated_since="2.0.0", removal_version="3.0.0",
                   migration_path="use bar()")
        warned = r.get_all_warned("2.5.0")
        for entry in warned:
            assert r.has_migration_path(entry.item_id), (
                f"'{entry.item_id}' deprecated but has no migration path"
            )

    def test_all_entries_snapshot(self):
        r = self._registry()
        r.register("a", deprecated_since="1.0.0", removal_version="2.0.0", migration_path="x")
        r.register("b", deprecated_since="2.0.0", removal_version="3.0.0", migration_path="y")
        assert set(r.all_entries().keys()) == {"a", "b"}

    def test_replace_registration_updates_entry(self):
        r = self._registry()
        r.register("item", deprecated_since="2.0.0", removal_version="3.0.0",
                   migration_path="old")
        r.register("item", deprecated_since="2.0.0", removal_version="4.0.0",
                   migration_path="new")
        entry = r.all_entries()["item"]
        assert entry.removal_version == "4.0.0"
        assert entry.migration_path == "new"


class TestDeprecatedDecorator:
    def test_decorated_function_callable_when_current(self):
        r = DeprecationRegistry()

        @deprecated(since="99.0.0", remove_by="100.0.0", migrate_to="use new()", registry=r)
        def my_fn():
            return "ok"

        assert my_fn() == "ok"

    def test_decorated_function_warns_when_deprecated(self, caplog):
        import logging
        import sys
        r = DeprecationRegistry()

        # Inject __version__ into a fake module so the check resolves to a known version
        module_name = "fake_module_for_test"
        if module_name not in sys.modules:
            import types
            fake = types.ModuleType(module_name)
            fake.__version__ = "2.5.0"  # type: ignore[attr-defined]
            sys.modules[module_name] = fake

        # Manually create entry to target our known version
        item_id = f"{module_name}.my_fn"
        r.register(item_id, deprecated_since="2.3.0", removal_version="3.0.0",
                   migration_path="use new()")

        with caplog.at_level(logging.WARNING):
            status = r.check(item_id, "2.5.0")
        assert status == GraceWindowStatus.DEPRECATED_WARN

    def test_decorator_registers_with_module_qualname(self):
        r = DeprecationRegistry()

        @deprecated(since="2.0.0", remove_by="3.0.0", migrate_to="use y()", registry=r)
        def legacy_x():
            return 42

        # item_id should be "<module>.legacy_x"
        entries = r.all_entries()
        assert any("legacy_x" in k for k in entries)


class TestDeprecationExpiredError:
    def test_error_message_contains_migration(self):
        entry = DeprecationEntry(
            item_id="comm.v1",
            deprecated_since="2.0.0",
            removal_version="3.0.0",
            migration_path="use comm.v2",
        )
        err = DeprecationExpiredError(entry, "3.0.0")
        assert "comm.v1" in str(err)
        assert "3.0.0" in str(err)
        assert "comm.v2" in str(err)


class TestModuleSingleton:
    def test_get_deprecation_registry_is_reused(self):
        r1 = get_deprecation_registry()
        r2 = get_deprecation_registry()
        assert r1 is r2
