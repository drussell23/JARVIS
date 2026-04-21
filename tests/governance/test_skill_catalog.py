"""Slice 2+3+4 tests — SkillCatalog + Invoker + Marketplace + REPL."""
from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest

from backend.core.ouroboros.governance.skill_catalog import (
    SKILL_CATALOG_SCHEMA_VERSION,
    SkillAuthorityError,
    SkillCatalog,
    SkillCatalogError,
    SkillInvocationError,
    SkillInvocationOutcome,
    SkillInvoker,
    SkillMarketplace,
    SkillSource,
    dispatch_skill_command,
    get_default_catalog,
    get_default_invoker,
    reset_default_catalog,
    reset_default_invoker,
)
from backend.core.ouroboros.governance.skill_manifest import (
    SkillManifest,
    SkillManifestError,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_default_catalog()
    reset_default_invoker()
    yield
    reset_default_catalog()
    reset_default_invoker()


def _mk(
    name: str = "greet",
    *,
    entrypoint: str = "tests.governance._skill_fixture_greet:run",
    plugin_namespace: str = "",
    permissions=(),
    args_schema=None,
) -> SkillManifest:
    return SkillManifest.from_mapping({
        "name": name,
        "description": f"test skill {name}",
        "trigger": "when testing",
        "entrypoint": entrypoint,
        "plugin_namespace": plugin_namespace,
        "permissions": list(permissions),
        "args_schema": args_schema or {},
    })


# ===========================================================================
# Schema version
# ===========================================================================


def test_catalog_schema_version_stable():
    assert SKILL_CATALOG_SCHEMA_VERSION == "skill_catalog.v1"


# ===========================================================================
# SkillCatalog — authority + lifecycle
# ===========================================================================


def test_register_operator_skill():
    cat = SkillCatalog()
    m = cat.register(_mk(), source=SkillSource.OPERATOR)
    assert m.qualified_name == "greet"
    assert cat.has("greet")


def test_register_orchestrator_skill():
    cat = SkillCatalog()
    cat.register(_mk(), source=SkillSource.ORCHESTRATOR)
    assert cat.has("greet")


def test_register_model_source_rejected():
    cat = SkillCatalog()

    class FakeSource(str):
        pass

    with pytest.raises(SkillAuthorityError):
        cat.register(_mk(), source=FakeSource("model"))  # type: ignore[arg-type]


def test_register_duplicate_rejected():
    cat = SkillCatalog()
    cat.register(_mk(), source=SkillSource.OPERATOR)
    with pytest.raises(SkillCatalogError):
        cat.register(_mk(), source=SkillSource.OPERATOR)


def test_register_cap_enforced():
    cat = SkillCatalog(max_skills=2)
    cat.register(_mk("a"), source=SkillSource.OPERATOR)
    cat.register(_mk("b"), source=SkillSource.OPERATOR)
    with pytest.raises(SkillCatalogError):
        cat.register(_mk("c"), source=SkillSource.OPERATOR)


def test_unregister_round_trip():
    cat = SkillCatalog()
    cat.register(_mk(), source=SkillSource.OPERATOR)
    assert cat.unregister("greet") is True
    assert cat.unregister("greet") is False
    assert not cat.has("greet")


def test_qualified_name_lookup_with_namespace():
    cat = SkillCatalog()
    cat.register(
        _mk(plugin_namespace="rp"), source=SkillSource.OPERATOR,
    )
    assert cat.has("rp:greet")
    assert not cat.has("greet")
    assert cat.get("rp:greet").plugin_namespace == "rp"


def test_list_all_sorted_by_qname():
    cat = SkillCatalog()
    cat.register(_mk("b"), source=SkillSource.OPERATOR)
    cat.register(_mk("a"), source=SkillSource.OPERATOR)
    names = [m.qualified_name for m in cat.list_all()]
    assert names == ["a", "b"]


def test_list_by_namespace():
    cat = SkillCatalog()
    cat.register(_mk("x"), source=SkillSource.OPERATOR)
    cat.register(
        _mk("y", plugin_namespace="rp"), source=SkillSource.OPERATOR,
    )
    cat.register(
        _mk("z", plugin_namespace="rp"), source=SkillSource.OPERATOR,
    )
    rp_skills = cat.list_by_namespace("rp")
    assert [m.name for m in rp_skills] == ["y", "z"]
    bare = cat.list_by_namespace(None)
    assert [m.name for m in bare] == ["x"]


def test_listener_fires_on_register_unregister():
    cat = SkillCatalog()
    events: List[str] = []
    cat.on_change(lambda p: events.append(p["event_type"]))
    cat.register(_mk(), source=SkillSource.OPERATOR)
    cat.unregister("greet")
    assert events == ["skill_registered", "skill_unregistered"]


def test_listener_exception_does_not_break_catalog():
    cat = SkillCatalog()

    def _bad(_p: Dict[str, Any]) -> None:
        raise RuntimeError("boom")

    cat.on_change(_bad)
    cat.register(_mk(), source=SkillSource.OPERATOR)
    assert cat.has("greet")


# ===========================================================================
# Singleton
# ===========================================================================


def test_default_catalog_singleton():
    a = get_default_catalog()
    b = get_default_catalog()
    assert a is b


# ===========================================================================
# SkillInvoker — resolution + validation + invocation
# ===========================================================================


# Dynamic fixture module — inject into sys.modules so the invoker's
# importlib path resolves to our test handler.
class _FixtureModule:
    @staticmethod
    async def run(manifest, args):  # type: ignore[no-untyped-def]
        return {"received": dict(args), "qname": manifest.qualified_name}

    @staticmethod
    def sync_run(manifest, args):  # type: ignore[no-untyped-def]
        return "sync-ok"

    @staticmethod
    def boom(manifest, args):  # type: ignore[no-untyped-def]
        raise ValueError("intentional")

    @staticmethod
    def not_callable():  # not exported as callable under this name
        return 42


sys.modules["tests.governance._skill_fixture_greet"] = _FixtureModule  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_invoke_async_handler():
    cat = SkillCatalog()
    cat.register(
        _mk(
            args_schema={"who": {"type": "string", "required": True}},
        ),
        source=SkillSource.OPERATOR,
    )
    inv = SkillInvoker(catalog=cat)
    outcome = await inv.invoke("greet", args={"who": "world"})
    assert outcome.ok is True
    assert "world" in outcome.result_preview


@pytest.mark.asyncio
async def test_invoke_sync_handler():
    cat = SkillCatalog()
    cat.register(
        _mk(
            name="sync-skill",
            entrypoint="tests.governance._skill_fixture_greet:sync_run",
        ),
        source=SkillSource.OPERATOR,
    )
    inv = SkillInvoker(catalog=cat)
    outcome = await inv.invoke("sync-skill")
    assert outcome.ok is True
    assert outcome.result_preview == "sync-ok"


@pytest.mark.asyncio
async def test_invoke_unknown_skill():
    inv = SkillInvoker()
    outcome = await inv.invoke("does-not-exist")
    assert outcome.ok is False
    assert "unknown skill" in outcome.error


@pytest.mark.asyncio
async def test_invoke_handler_raise_captured():
    cat = SkillCatalog()
    cat.register(
        _mk(
            name="boom",
            entrypoint="tests.governance._skill_fixture_greet:boom",
        ),
        source=SkillSource.OPERATOR,
    )
    inv = SkillInvoker(catalog=cat)
    outcome = await inv.invoke("boom")
    assert outcome.ok is False
    assert "handler_raise:ValueError" in outcome.error


@pytest.mark.asyncio
async def test_invoke_bad_entrypoint_captured():
    cat = SkillCatalog()
    cat.register(
        _mk(
            name="missing",
            entrypoint="nonexistent.module.path:fn",
        ),
        source=SkillSource.OPERATOR,
    )
    inv = SkillInvoker(catalog=cat)
    outcome = await inv.invoke("missing")
    assert outcome.ok is False
    assert "entrypoint_error" in outcome.error


@pytest.mark.asyncio
async def test_invoke_args_validation_error():
    cat = SkillCatalog()
    cat.register(
        _mk(
            args_schema={"who": {"type": "string", "required": True}},
        ),
        source=SkillSource.OPERATOR,
    )
    inv = SkillInvoker(catalog=cat)
    # missing required arg
    outcome = await inv.invoke("greet", args={})
    assert outcome.ok is False
    assert "args_validation_error" in outcome.error


@pytest.mark.asyncio
async def test_invoke_records_duration():
    cat = SkillCatalog()
    cat.register(_mk(), source=SkillSource.OPERATOR)
    inv = SkillInvoker(catalog=cat)
    outcome = await inv.invoke("greet", args={})
    assert outcome.duration_ms >= 0.0


@pytest.mark.asyncio
async def test_invoke_result_preview_bounded():
    cat = SkillCatalog()

    class _BigModule:
        @staticmethod
        def huge(manifest, args):
            return "x" * 5000

    sys.modules["tests.governance._skill_fixture_big"] = _BigModule  # type: ignore[assignment]
    cat.register(
        SkillManifest.from_mapping({
            "name": "big",
            "description": "test",
            "trigger": "when testing",
            "entrypoint": "tests.governance._skill_fixture_big:huge",
        }),
        source=SkillSource.OPERATOR,
    )
    inv = SkillInvoker(catalog=cat)
    outcome = await inv.invoke("big", output_preview_chars=100)
    assert outcome.ok is True
    assert len(outcome.result_preview) <= 100


def test_resolve_entrypoint_caches():
    """Second resolve returns the same callable without re-importing."""
    cat = SkillCatalog()
    cat.register(_mk(), source=SkillSource.OPERATOR)
    inv = SkillInvoker(catalog=cat)
    m = cat.get("greet")
    fn1 = inv.resolve_entrypoint(m)
    fn2 = inv.resolve_entrypoint(m)
    assert fn1 is fn2


def test_resolve_entrypoint_attr_missing():
    cat = SkillCatalog()
    cat.register(
        _mk(
            name="bad",
            entrypoint="tests.governance._skill_fixture_greet:missing_attr",
        ),
        source=SkillSource.OPERATOR,
    )
    inv = SkillInvoker(catalog=cat)
    m = cat.get("bad")
    with pytest.raises(SkillInvocationError):
        inv.resolve_entrypoint(m)


# ===========================================================================
# SkillMarketplace — install / remove / discover
# ===========================================================================


def _write_manifest(dir_: Path, **overrides: Any) -> Path:
    data = {
        "name": "demo",
        "description": "demo skill",
        "trigger": "when demoing",
        "entrypoint": "tests.governance._skill_fixture_greet:run",
        "version": "1.0.0",
        **overrides,
    }
    import yaml  # type: ignore[import-untyped]
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "manifest.yaml").write_text(yaml.safe_dump(data))
    return dir_


def test_marketplace_install_from_directory(tmp_path: Path):
    src = tmp_path / "source" / "demo"
    _write_manifest(src, name="demo")
    root = tmp_path / "marketplace"
    cat = SkillCatalog()
    market = SkillMarketplace(root, catalog=cat)
    m = market.install_from_directory(src)
    assert m.qualified_name == "demo"
    # On-disk copy exists
    assert (root / "demo" / "manifest.yaml").exists()
    assert cat.has("demo")


def test_marketplace_install_namespaced(tmp_path: Path):
    src = tmp_path / "source" / "demo"
    _write_manifest(src, name="demo", plugin_namespace="rp")
    root = tmp_path / "marketplace"
    cat = SkillCatalog()
    market = SkillMarketplace(root, catalog=cat)
    m = market.install_from_directory(src)
    assert m.qualified_name == "rp:demo"
    assert (root / "rp" / "demo" / "manifest.yaml").exists()


def test_marketplace_install_refuses_duplicate(tmp_path: Path):
    src = tmp_path / "source" / "demo"
    _write_manifest(src, name="demo")
    root = tmp_path / "marketplace"
    market = SkillMarketplace(root)
    market.install_from_directory(src)
    with pytest.raises(SkillCatalogError):
        market.install_from_directory(src)


def test_marketplace_install_replace_existing(tmp_path: Path):
    src = tmp_path / "source" / "demo"
    _write_manifest(src, name="demo", version="1.0.0")
    root = tmp_path / "marketplace"
    market = SkillMarketplace(root)
    market.install_from_directory(src)
    # Bump version + replace
    _write_manifest(src, name="demo", version="2.0.0")
    m2 = market.install_from_directory(src, replace_existing=True)
    assert m2.version == "2.0.0"


def test_marketplace_install_rejects_missing_manifest(tmp_path: Path):
    src = tmp_path / "empty_dir"
    src.mkdir()
    market = SkillMarketplace(tmp_path / "root")
    with pytest.raises(SkillCatalogError):
        market.install_from_directory(src)


def test_marketplace_remove(tmp_path: Path):
    src = tmp_path / "source" / "demo"
    _write_manifest(src, name="demo")
    root = tmp_path / "marketplace"
    cat = SkillCatalog()
    market = SkillMarketplace(root, catalog=cat)
    market.install_from_directory(src)
    assert market.remove("demo") is True
    assert cat.has("demo") is False
    assert not (root / "demo").exists()


def test_marketplace_remove_unknown_returns_false(tmp_path: Path):
    market = SkillMarketplace(tmp_path / "root")
    assert market.remove("does-not-exist") is False


def test_marketplace_discover(tmp_path: Path):
    root = tmp_path / "market"
    _write_manifest(root / "a", name="a")
    _write_manifest(root / "b", name="b")
    _write_manifest(root / "rp" / "x", name="x", plugin_namespace="rp")
    cat = SkillCatalog()
    market = SkillMarketplace(root, catalog=cat)
    loaded = market.discover()
    qnames = {m.qualified_name for m in loaded}
    assert qnames == {"a", "b", "rp:x"}


def test_marketplace_discover_skips_broken_manifest(tmp_path: Path):
    root = tmp_path / "market"
    _write_manifest(root / "good", name="good")
    # Write a deliberately malformed manifest
    (root / "bad").mkdir(parents=True)
    (root / "bad" / "manifest.yaml").write_text("name: BAD uppercase: broken")
    cat = SkillCatalog()
    market = SkillMarketplace(root, catalog=cat)
    loaded = market.discover()
    qnames = {m.qualified_name for m in loaded}
    assert "good" in qnames
    # Bad one skipped — no crash
    assert not cat.has("BAD")


def test_marketplace_discover_idempotent(tmp_path: Path):
    root = tmp_path / "market"
    _write_manifest(root / "a", name="a")
    cat = SkillCatalog()
    market = SkillMarketplace(root, catalog=cat)
    first = market.discover()
    second = market.discover()
    assert len(first) == 1
    assert len(second) == 0  # already registered → skipped


def test_marketplace_discover_empty_root(tmp_path: Path):
    market = SkillMarketplace(tmp_path / "does-not-exist")
    assert market.discover() == []


# ===========================================================================
# REPL dispatcher
# ===========================================================================


def test_repl_unmatched_falls_through():
    r = dispatch_skill_command("/plan mode on")
    assert r.matched is False


def test_repl_skills_list_empty():
    cat = SkillCatalog()
    r = dispatch_skill_command("/skills", catalog=cat)
    assert r.ok is True
    assert "no skills registered" in r.text.lower()


def test_repl_skills_list_populated():
    cat = SkillCatalog()
    cat.register(_mk("alpha"), source=SkillSource.OPERATOR)
    cat.register(
        _mk("beta", plugin_namespace="rp"), source=SkillSource.OPERATOR,
    )
    r = dispatch_skill_command("/skills", catalog=cat)
    assert "alpha" in r.text
    assert "rp:beta" in r.text


def test_repl_skills_show():
    cat = SkillCatalog()
    cat.register(_mk(), source=SkillSource.OPERATOR)
    r = dispatch_skill_command("/skills show greet", catalog=cat)
    assert r.ok is True
    assert "description" in r.text
    assert "entrypoint" in r.text


def test_repl_skills_show_short_form():
    cat = SkillCatalog()
    cat.register(_mk(), source=SkillSource.OPERATOR)
    r = dispatch_skill_command("/skills greet", catalog=cat)
    assert r.ok is True
    assert "greet" in r.text


def test_repl_skills_show_unknown():
    r = dispatch_skill_command(
        "/skills show missing", catalog=SkillCatalog(),
    )
    assert r.ok is False


def test_repl_skills_help():
    r = dispatch_skill_command("/skills help")
    assert r.ok is True
    assert "install" in r.text
    assert "run" in r.text


def test_repl_skills_run_requires_coroutine_runner():
    cat = SkillCatalog()
    cat.register(_mk(), source=SkillSource.OPERATOR)
    r = dispatch_skill_command("/skills run greet", catalog=cat)
    # No runner supplied
    assert r.ok is False
    assert "coroutine runner" in r.text.lower()


def test_repl_skills_run_ok():
    cat = SkillCatalog()
    cat.register(
        _mk(
            args_schema={"who": {"type": "string", "default": "you"}},
        ),
        source=SkillSource.OPERATOR,
    )
    r = dispatch_skill_command(
        '/skills run greet who=world',
        catalog=cat,
        run_coroutine=lambda coro: asyncio.run(coro),
    )
    assert r.ok is True
    assert "ran greet" in r.text


def test_repl_skills_run_bad_arg_form():
    cat = SkillCatalog()
    cat.register(_mk(), source=SkillSource.OPERATOR)
    r = dispatch_skill_command(
        "/skills run greet has-no-equal",
        catalog=cat,
        run_coroutine=lambda coro: asyncio.run(coro),
    )
    assert r.ok is False
    assert "k=v" in r.text


def test_repl_skills_install_no_marketplace():
    r = dispatch_skill_command("/skills install /tmp/x")
    assert r.ok is False
    assert "no marketplace" in r.text.lower()


def test_repl_skills_install_round_trip(tmp_path: Path):
    src = tmp_path / "source" / "demo"
    _write_manifest(src, name="demo")
    root = tmp_path / "marketplace"
    cat = SkillCatalog()
    market = SkillMarketplace(root, catalog=cat)
    r = dispatch_skill_command(
        f"/skills install {src}",
        catalog=cat, marketplace=market,
    )
    assert r.ok is True
    assert cat.has("demo")


def test_repl_skills_remove_round_trip(tmp_path: Path):
    src = tmp_path / "source" / "demo"
    _write_manifest(src, name="demo")
    root = tmp_path / "marketplace"
    cat = SkillCatalog()
    market = SkillMarketplace(root, catalog=cat)
    market.install_from_directory(src)
    r = dispatch_skill_command(
        "/skills remove demo",
        catalog=cat, marketplace=market,
    )
    assert r.ok is True
    assert not cat.has("demo")


def test_repl_skills_discover_round_trip(tmp_path: Path):
    root = tmp_path / "market"
    _write_manifest(root / "a", name="a")
    cat = SkillCatalog()
    market = SkillMarketplace(root, catalog=cat)
    r = dispatch_skill_command(
        "/skills discover",
        catalog=cat, marketplace=market,
    )
    assert r.ok is True
    assert cat.has("a")


# ===========================================================================
# Value coercion in REPL
# ===========================================================================


def test_repl_coerces_arg_types():
    from backend.core.ouroboros.governance.skill_catalog import (
        _coerce_scalar,
    )
    assert _coerce_scalar("true") is True
    assert _coerce_scalar("false") is False
    assert _coerce_scalar("42") == 42
    assert _coerce_scalar("3.14") == 3.14
    assert _coerce_scalar("hello") == "hello"
