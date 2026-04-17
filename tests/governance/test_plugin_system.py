"""Tests for the plugin system — manifest, registry, subsystem wiring.

Three scope axes:

  1. Manifest schema: required fields, type enum, name normalization,
    YAML/JSON fallback, malformed input tolerance.
  2. Registry lifecycle: discovery against a tmp plugin dir, env gates
    (master + per-type + mutations), import + instantiate, subsystem
    wiring (sensor / gate / repl), error isolation, teardown.
  3. End-to-end: an example-shaped sensor, gate, repl plugin each
    loaded from a tmp dir and exercised through the registered API.
"""
from __future__ import annotations

import asyncio
import os
import textwrap
from pathlib import Path
from typing import Any, Iterator, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.plugins import (
    PluginLoadOutcome,
    PluginManifest,
    PluginManifestError,
    PluginRegistry,
    parse_manifest,
    plugins_enabled,
    plugins_path,
)
from backend.core.ouroboros.plugins.plugin_base import (
    GatePlugin,
    PluginContext,
    ReplCommandPlugin,
    SensorPlugin,
    SensorPluginSignal,
)
from backend.core.ouroboros.plugins.plugin_manifest import (
    discover_manifests,
)
from backend.core.ouroboros.plugins.plugin_registry import (
    reset_default_registry,
    unregister_guardian_plugin_patterns,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_PLUGINS_"):
            monkeypatch.delenv(key, raising=False)
    reset_default_registry()
    yield
    reset_default_registry()


# ---------------------------------------------------------------------------
# Fixtures — tmp plugin dir builders
# ---------------------------------------------------------------------------


def _write_plugin(
    dir_: Path,
    name: str,
    manifest_text: str,
    module_filename: str,
    module_src: str,
) -> Path:
    """Write a complete plugin dir under ``dir_/<name>/``.

    ``manifest_text`` goes to ``manifest.yaml`` (or ``.json`` if the
    text starts with ``{``). ``module_src`` is written to
    ``<module_filename>.py``."""
    plugin_dir = dir_ / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    is_json = manifest_text.lstrip().startswith("{")
    manifest_fn = "manifest.json" if is_json else "manifest.yaml"
    (plugin_dir / manifest_fn).write_text(manifest_text, encoding="utf-8")
    (plugin_dir / f"{module_filename}.py").write_text(
        module_src, encoding="utf-8",
    )
    return plugin_dir


# ---------------------------------------------------------------------------
# Manifest schema — validation + YAML/JSON fallback
# ---------------------------------------------------------------------------


def test_manifest_required_fields_json(tmp_path):
    """JSON manifest minimal — only required fields."""
    plugin_dir = tmp_path / "ok_json"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(
        '{"name": "ok_json", "type": "sensor", '
        '"entry_module": "mod", "entry_class": "P"}',
        encoding="utf-8",
    )
    (plugin_dir / "mod.py").write_text("class P: pass\n")
    manifest = parse_manifest(plugin_dir)
    assert manifest.name == "ok_json"
    assert manifest.type == "sensor"
    assert manifest.entry_module == "mod"
    assert manifest.entry_class == "P"
    assert manifest.version == "0.0.0"


def test_manifest_yaml_preferred_when_present(tmp_path):
    """Both yaml + json exist → yaml wins."""
    plugin_dir = tmp_path / "both"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.yaml").write_text(
        "name: both_yaml\ntype: gate\nentry_module: g\nentry_class: G\n",
    )
    (plugin_dir / "manifest.json").write_text(
        '{"name": "both_json", "type": "sensor", '
        '"entry_module": "j", "entry_class": "J"}',
    )
    (plugin_dir / "g.py").write_text("x = 1\n")
    try:
        manifest = parse_manifest(plugin_dir)
    except PluginManifestError:
        # PyYAML not installed locally → json fallback fires.
        pytest.skip("PyYAML not installed; YAML-preferred path untestable")
    assert manifest.name == "both_yaml"


def test_manifest_missing_required_field_raises(tmp_path):
    plugin_dir = tmp_path / "bad"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(
        '{"name": "bad"}',  # missing type / entry_module / entry_class
    )
    with pytest.raises(PluginManifestError) as excinfo:
        parse_manifest(plugin_dir)
    assert "type" in str(excinfo.value)


def test_manifest_invalid_type_rejected(tmp_path):
    plugin_dir = tmp_path / "badtype"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(
        '{"name": "badtype", "type": "supervisor", '
        '"entry_module": "m", "entry_class": "C"}',
    )
    with pytest.raises(PluginManifestError) as excinfo:
        parse_manifest(plugin_dir)
    assert "supervisor" in str(excinfo.value)


def test_manifest_invalid_name_rejected(tmp_path):
    plugin_dir = tmp_path / "badname"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(
        '{"name": "Invalid Name With Spaces!", "type": "sensor", '
        '"entry_module": "m", "entry_class": "C"}',
    )
    with pytest.raises(PluginManifestError) as excinfo:
        parse_manifest(plugin_dir)
    assert "not valid" in str(excinfo.value)


def test_manifest_no_file_rejected(tmp_path):
    plugin_dir = tmp_path / "empty"
    plugin_dir.mkdir()
    with pytest.raises(PluginManifestError):
        parse_manifest(plugin_dir)


def test_manifest_malformed_json_rejected(tmp_path):
    plugin_dir = tmp_path / "malformed"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text("{this is not json")
    with pytest.raises(PluginManifestError):
        parse_manifest(plugin_dir)


def test_manifest_extra_fields_preserved(tmp_path):
    """Unknown fields land in ``extra`` for plugin code to read."""
    plugin_dir = tmp_path / "x"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(
        '{"name": "x", "type": "repl", "entry_module": "m", '
        '"entry_class": "C", "custom_field": "value"}',
    )
    manifest = parse_manifest(plugin_dir)
    assert manifest.extra.get("custom_field") == "value"


def test_discover_manifests_skips_broken(tmp_path):
    """A broken manifest must not prevent discovery of others."""
    _write_plugin(
        tmp_path, "good",
        '{"name": "good", "type": "sensor", '
        '"entry_module": "m", "entry_class": "C"}',
        "m", "class C: pass\n",
    )
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text("{broken")
    manifests = discover_manifests((tmp_path,))
    names = [m.name for m in manifests]
    assert "good" in names
    assert "bad" not in names


def test_discover_manifests_deterministic_order(tmp_path):
    for n in ("zulu", "alpha", "mike"):
        _write_plugin(
            tmp_path, n,
            f'{{"name": "{n}", "type": "sensor", '
            f'"entry_module": "m", "entry_class": "C"}}',
            "m", "class C: pass\n",
        )
    names = [m.name for m in discover_manifests((tmp_path,))]
    assert names == ["alpha", "mike", "zulu"]


def test_discover_manifests_dedupes_by_name(tmp_path, tmp_path_factory):
    """Same plugin name appearing in two roots — first wins."""
    second_root = tmp_path_factory.mktemp("root2")
    _write_plugin(
        tmp_path, "dup",
        '{"name": "dup", "type": "sensor", '
        '"entry_module": "m", "entry_class": "C"}',
        "m", "class C: pass\n",
    )
    _write_plugin(
        second_root, "dup",
        '{"name": "dup", "type": "sensor", '
        '"entry_module": "m", "entry_class": "C"}',
        "m", "class C: pass\n",
    )
    manifests = discover_manifests((tmp_path, second_root))
    names = [m.name for m in manifests]
    assert names.count("dup") == 1


# ---------------------------------------------------------------------------
# Env gates
# ---------------------------------------------------------------------------


def test_plugins_disabled_by_default():
    assert plugins_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_plugins_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", val)
    assert plugins_enabled() is True


def test_plugins_path_default_contains_repo_local(tmp_path):
    paths = plugins_path(tmp_path)
    assert tmp_path / ".ouroboros" / "plugins" in paths


def test_plugins_path_extended_by_env(monkeypatch, tmp_path):
    extra = str(tmp_path / "operator-wide")
    monkeypatch.setenv("JARVIS_PLUGINS_PATH", extra + os.pathsep + "/not/real")
    paths = plugins_path(tmp_path)
    assert Path(extra) in paths


# ---------------------------------------------------------------------------
# Registry — discover + load + register (full lifecycle)
# ---------------------------------------------------------------------------


def _seed_sensor_plugin(tmp_path: Path) -> Path:
    return _write_plugin(
        tmp_path, "t_sensor",
        textwrap.dedent("""\
            {"name": "t_sensor", "type": "sensor",
             "entry_module": "mod", "entry_class": "TS",
             "tick_interval_s": 60}
        """),
        "mod",
        textwrap.dedent("""
            from typing import List
            from backend.core.ouroboros.plugins.plugin_base import (
                SensorPlugin, SensorPluginSignal,
            )

            class TS(SensorPlugin):
                async def on_tick(self):
                    return [SensorPluginSignal(
                        description="t_sensor proposed this",
                        target_files=("foo.py",),
                        urgency="low",
                    )]
        """),
    )


def _seed_gate_plugin(tmp_path: Path) -> Path:
    return _write_plugin(
        tmp_path, "t_gate",
        textwrap.dedent("""\
            {"name": "t_gate", "type": "gate",
             "entry_module": "g", "entry_class": "TG"}
        """),
        "g",
        textwrap.dedent("""
            from typing import Optional, Tuple
            from backend.core.ouroboros.plugins.plugin_base import GatePlugin

            class TG(GatePlugin):
                pattern_name = "forbidden_banana"
                severity = "hard"
                def inspect(self, *, file_path, old_content, new_content):
                    if "banana" in (new_content or "") and "banana" not in (old_content or ""):
                        return ("hard", "banana detected in candidate")
                    return None
        """),
    )


def _seed_repl_plugin(tmp_path: Path) -> Path:
    return _write_plugin(
        tmp_path, "t_repl",
        textwrap.dedent("""\
            {"name": "t_repl", "type": "repl",
             "entry_module": "c", "entry_class": "TR"}
        """),
        "c",
        textwrap.dedent("""
            from backend.core.ouroboros.plugins.plugin_base import ReplCommandPlugin

            class TR(ReplCommandPlugin):
                command_name = "ping"
                async def run(self, args):
                    return f"pong:{args}"
        """),
    )


def test_registry_skips_everything_when_master_disabled(tmp_path, monkeypatch):
    """Fail-closed: no plugins load when JARVIS_PLUGINS_ENABLED is off."""
    _seed_sensor_plugin(tmp_path / ".ouroboros" / "plugins")
    reg = PluginRegistry(repo_root=tmp_path)

    async def _run():
        return await reg.discover_and_load()

    asyncio.run(_run())
    assert reg.outcomes == []


def test_registry_loads_all_three_types(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", "1")
    plugin_root = tmp_path / ".ouroboros" / "plugins"
    _seed_sensor_plugin(plugin_root)
    _seed_gate_plugin(plugin_root)
    _seed_repl_plugin(plugin_root)

    reg = PluginRegistry(repo_root=tmp_path)

    async def _run():
        await reg.discover_and_load()
        return reg

    asyncio.run(_run())
    try:
        names = {o.manifest.name for o in reg.outcomes if o.manifest}
        assert names == {"t_sensor", "t_gate", "t_repl"}
        loaded = [o for o in reg.outcomes if o.state == "loaded"]
        assert len(loaded) == 3
    finally:
        asyncio.run(reg.shutdown())


def test_registry_per_type_gate_disables_specific_type(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_PLUGINS_SENSORS_ENABLED", "0")
    plugin_root = tmp_path / ".ouroboros" / "plugins"
    _seed_sensor_plugin(plugin_root)
    _seed_repl_plugin(plugin_root)
    reg = PluginRegistry(repo_root=tmp_path)

    async def _run():
        await reg.discover_and_load()
    asyncio.run(_run())
    try:
        by_name = {o.manifest.name: o for o in reg.outcomes if o.manifest}
        assert by_name["t_sensor"].state == "disabled_by_type"
        assert by_name["t_repl"].state == "loaded"
    finally:
        asyncio.run(reg.shutdown())


def test_registry_isolates_one_broken_plugin(tmp_path, monkeypatch):
    """A plugin whose entry_module raises on import must NOT prevent
    sibling plugins from loading."""
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", "1")
    plugin_root = tmp_path / ".ouroboros" / "plugins"
    _seed_repl_plugin(plugin_root)
    # Plant a broken plugin.
    _write_plugin(
        plugin_root, "broken",
        textwrap.dedent("""\
            {"name": "broken", "type": "repl",
             "entry_module": "c", "entry_class": "X"}
        """),
        "c",
        "raise RuntimeError('exploded on import')\n",
    )
    reg = PluginRegistry(repo_root=tmp_path)

    async def _run():
        await reg.discover_and_load()
    asyncio.run(_run())
    try:
        by_name = {o.manifest.name: o for o in reg.outcomes if o.manifest}
        assert by_name["t_repl"].state == "loaded"
        assert by_name["broken"].state == "failed"
        assert "exploded on import" in by_name["broken"].error
    finally:
        asyncio.run(reg.shutdown())


def test_registry_rejects_wrong_subclass(tmp_path, monkeypatch):
    """A manifest declaring type=gate but entry_class not subclassing
    GatePlugin must land as 'failed' with a clear error."""
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", "1")
    plugin_root = tmp_path / ".ouroboros" / "plugins"
    _write_plugin(
        plugin_root, "wrong_subclass",
        textwrap.dedent("""\
            {"name": "wrong_subclass", "type": "gate",
             "entry_module": "m", "entry_class": "NotAPlugin"}
        """),
        "m", "class NotAPlugin:\n    pass\n",
    )
    reg = PluginRegistry(repo_root=tmp_path)

    async def _run():
        await reg.discover_and_load()
    asyncio.run(_run())
    try:
        o = reg.outcomes[0]
        assert o.state == "failed"
        assert "subclass Plugin" in o.error
    finally:
        asyncio.run(reg.shutdown())


def test_registry_repl_duplicate_name_collision(tmp_path, monkeypatch):
    """Two repl plugins claiming the same command_name — second fails,
    first stays loaded."""
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", "1")
    plugin_root = tmp_path / ".ouroboros" / "plugins"
    _write_plugin(
        plugin_root, "a_first",
        textwrap.dedent("""\
            {"name": "a_first", "type": "repl",
             "entry_module": "c", "entry_class": "CA"}
        """),
        "c",
        textwrap.dedent("""
            from backend.core.ouroboros.plugins.plugin_base import ReplCommandPlugin
            class CA(ReplCommandPlugin):
                command_name = "samecmd"
                async def run(self, args):
                    return "from a"
        """),
    )
    _write_plugin(
        plugin_root, "b_second",
        textwrap.dedent("""\
            {"name": "b_second", "type": "repl",
             "entry_module": "c", "entry_class": "CB"}
        """),
        "c",
        textwrap.dedent("""
            from backend.core.ouroboros.plugins.plugin_base import ReplCommandPlugin
            class CB(ReplCommandPlugin):
                command_name = "samecmd"
                async def run(self, args):
                    return "from b"
        """),
    )
    reg = PluginRegistry(repo_root=tmp_path)

    async def _run():
        await reg.discover_and_load()
    asyncio.run(_run())
    try:
        by_name = {o.manifest.name: o for o in reg.outcomes if o.manifest}
        assert by_name["a_first"].state == "loaded"
        assert by_name["b_second"].state == "failed"
        assert "already registered" in by_name["b_second"].error
    finally:
        asyncio.run(reg.shutdown())


def test_sensor_plugin_propose_gated_by_mutations_env(
    tmp_path, monkeypatch,
):
    """With JARVIS_PLUGINS_ALLOW_MUTATIONS unset, sensor.propose()
    returns 'mutations_disabled' without calling the intake router.
    This is the fail-closed discipline — plugins can't submit intents
    by default."""
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", "1")
    plugin_root = tmp_path / ".ouroboros" / "plugins"
    _seed_sensor_plugin(plugin_root)

    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")

    reg = PluginRegistry(repo_root=tmp_path)

    async def _run():
        await reg.discover_and_load(intake_router=router)
        # Pull the sensor out of the registry + call propose directly.
        sensor = reg.outcomes[0].plugin
        result = await sensor.propose(SensorPluginSignal(
            description="test", urgency="low",
        ))
        return result

    result = asyncio.run(_run())
    try:
        assert result == "mutations_disabled"
        # Router was never called.
        router.ingest.assert_not_called()
    finally:
        asyncio.run(reg.shutdown())


def test_sensor_plugin_propose_submits_when_allowed(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_PLUGINS_ALLOW_MUTATIONS", "1")
    plugin_root = tmp_path / ".ouroboros" / "plugins"
    _seed_sensor_plugin(plugin_root)

    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")

    reg = PluginRegistry(repo_root=tmp_path)

    async def _run():
        await reg.discover_and_load(intake_router=router)
        sensor = reg.outcomes[0].plugin
        return await sensor.propose(SensorPluginSignal(
            description="test intent", target_files=("x.py",),
        ))

    result = asyncio.run(_run())
    try:
        assert result == "enqueued"
        # The registry's sensor tick loop may also have called propose
        # by the time we assert (race with the explicit test call),
        # so we check >= 1 ingest call + verify at least one carries
        # the plugin_source tag.
        assert router.ingest.call_count >= 1
        assert any(
            call.args[0].evidence.get("plugin_source") == "t_sensor"
            for call in router.ingest.call_args_list
        )
    finally:
        asyncio.run(reg.shutdown())


# ---------------------------------------------------------------------------
# Gate plugin integration with SemanticGuardian
# ---------------------------------------------------------------------------


def test_gate_plugin_registered_with_guardian(tmp_path, monkeypatch):
    """A loaded gate plugin's pattern must fire when SemanticGuardian
    inspects a candidate that matches it."""
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", "1")
    plugin_root = tmp_path / ".ouroboros" / "plugins"
    _seed_gate_plugin(plugin_root)

    reg = PluginRegistry(repo_root=tmp_path)

    async def _run():
        await reg.discover_and_load()
    asyncio.run(_run())
    try:
        # Now drive the guardian through a candidate that should trip
        # the plugin-registered pattern.
        from backend.core.ouroboros.governance.semantic_guardian import (
            SemanticGuardian,
        )
        # Pass all pattern names — the plugin-registered name has the
        # 'plugin.t_gate.forbidden_banana' prefix.
        g = SemanticGuardian()
        # Override patterns list to include our plugin-registered name.
        from backend.core.ouroboros.governance import semantic_guardian as sg
        all_names = tuple(sg._PATTERNS.keys())
        g.patterns = all_names
        findings = g.inspect(
            file_path="x.py",
            old_content="x = 1\n",
            new_content="x = 'banana'\n",
        )
        patterns = [f.pattern for f in findings]
        assert any("plugin.t_gate.forbidden_banana" in p for p in patterns)
        banana_finding = next(
            f for f in findings if "banana" in f.pattern
        )
        assert banana_finding.severity == "hard"
    finally:
        asyncio.run(reg.shutdown())
        unregister_guardian_plugin_patterns()


def test_gate_plugin_exception_isolated_from_guardian(
    tmp_path, monkeypatch,
):
    """When a gate plugin's inspect() raises, the guardian continues
    without crashing — the plugin is quietly returning None for that
    candidate."""
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", "1")
    plugin_root = tmp_path / ".ouroboros" / "plugins"
    _write_plugin(
        plugin_root, "raising_gate",
        textwrap.dedent("""\
            {"name": "raising_gate", "type": "gate",
             "entry_module": "g", "entry_class": "RG"}
        """),
        "g",
        textwrap.dedent("""
            from backend.core.ouroboros.plugins.plugin_base import GatePlugin

            class RG(GatePlugin):
                pattern_name = "always_boom"
                severity = "hard"
                def inspect(self, *, file_path, old_content, new_content):
                    raise RuntimeError("plugin exploded")
        """),
    )

    reg = PluginRegistry(repo_root=tmp_path)

    async def _run():
        await reg.discover_and_load()
    asyncio.run(_run())
    try:
        from backend.core.ouroboros.governance.semantic_guardian import (
            SemanticGuardian,
        )
        from backend.core.ouroboros.governance import semantic_guardian as sg
        g = SemanticGuardian()
        g.patterns = tuple(sg._PATTERNS.keys())
        findings = g.inspect(
            file_path="x.py", old_content="x = 1\n", new_content="x = 2\n",
        )
        # Plugin raised but guardian didn't — other patterns still ran.
        assert isinstance(findings, list)
    finally:
        asyncio.run(reg.shutdown())
        unregister_guardian_plugin_patterns()


# ---------------------------------------------------------------------------
# REPL plugin lookup
# ---------------------------------------------------------------------------


def test_repl_plugin_lookup_by_command_name(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", "1")
    plugin_root = tmp_path / ".ouroboros" / "plugins"
    _seed_repl_plugin(plugin_root)

    reg = PluginRegistry(repo_root=tmp_path)

    async def _run():
        await reg.discover_and_load()
        plugin = reg.repl_command("ping")
        assert plugin is not None
        return await plugin.run("world")

    result = asyncio.run(_run())
    try:
        assert result == "pong:world"
    finally:
        asyncio.run(reg.shutdown())


def test_repl_plugin_lookup_tolerates_slash_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", "1")
    plugin_root = tmp_path / ".ouroboros" / "plugins"
    _seed_repl_plugin(plugin_root)

    reg = PluginRegistry(repo_root=tmp_path)

    async def _run():
        await reg.discover_and_load()
        # Both forms must resolve to the same plugin.
        assert reg.repl_command("ping") is reg.repl_command("/ping")

    asyncio.run(_run())
    try:
        pass
    finally:
        asyncio.run(reg.shutdown())


# ---------------------------------------------------------------------------
# Example plugins in-tree — catches a broken example before it ships
# ---------------------------------------------------------------------------


def test_bundled_examples_parse_without_error():
    """Every example plugin under .ouroboros/plugins/examples/ must
    have a valid manifest + importable module so operators can copy
    them as templates."""
    repo = Path(__file__).resolve().parent.parent.parent
    examples = repo / ".ouroboros" / "plugins" / "examples"
    if not examples.is_dir():
        pytest.skip("no bundled examples dir")
    manifests = discover_manifests((examples,))
    names = {m.name for m in manifests}
    # The three example plugins must all discover.
    assert "heartbeat_sensor" in names
    assert "todo_deadline_gate" in names
    assert "greet_cmd" in names


def test_bundled_examples_load_successfully(monkeypatch):
    """Full end-to-end: enable plugins + point at the examples dir +
    verify each loads without error."""
    monkeypatch.setenv("JARVIS_PLUGINS_ENABLED", "1")
    repo = Path(__file__).resolve().parent.parent.parent
    examples_parent = repo / ".ouroboros" / "plugins"
    if not (examples_parent / "examples").is_dir():
        pytest.skip("no bundled examples dir")

    # Point search at the "examples" dir as a root.
    monkeypatch.setenv(
        "JARVIS_PLUGINS_PATH",
        str(examples_parent / "examples"),
    )

    # Point the repo_root away from the real repo so we don't also
    # pick up any operator-authored plugins under .ouroboros/plugins/
    # that might happen to exist.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        reg = PluginRegistry(repo_root=Path(td))

        async def _run():
            await reg.discover_and_load()
        asyncio.run(_run())
        try:
            by_state = {}
            for o in reg.outcomes:
                by_state.setdefault(o.state, []).append(o)
            # Every example is either loaded or (for the sensor) possibly
            # disabled if the operator has set the sub-gate — here we
            # didn't, so all three should load.
            loaded_names = {
                o.manifest.name for o in by_state.get("loaded", [])
                if o.manifest
            }
            assert "heartbeat_sensor" in loaded_names
            assert "todo_deadline_gate" in loaded_names
            assert "greet_cmd" in loaded_names
        finally:
            asyncio.run(reg.shutdown())
            unregister_guardian_plugin_patterns()


# ---------------------------------------------------------------------------
# Harness wiring — AST canaries
# ---------------------------------------------------------------------------


def test_harness_dispatches_plugins_command():
    from pathlib import Path as _Path
    path = _Path(__file__).resolve().parent.parent.parent / (
        "backend/core/ouroboros/battle_test/harness.py"
    )
    src = path.read_text(encoding="utf-8")
    assert "_repl_cmd_plugins" in src
    assert "/plugins" in src


def test_harness_delegates_plugin_slash_commands():
    from pathlib import Path as _Path
    path = _Path(__file__).resolve().parent.parent.parent / (
        "backend/core/ouroboros/battle_test/harness.py"
    )
    src = path.read_text(encoding="utf-8")
    assert "_try_dispatch_plugin_command" in src


def test_harness_wires_plugin_registry_at_boot():
    from pathlib import Path as _Path
    path = _Path(__file__).resolve().parent.parent.parent / (
        "backend/core/ouroboros/battle_test/harness.py"
    )
    src = path.read_text(encoding="utf-8")
    # Must use the public PluginRegistry + register_default_plugins
    # surface, not touch internals.
    assert "PluginRegistry" in src
    assert "register_default_plugins" in src
    assert "plugins_enabled" in src
