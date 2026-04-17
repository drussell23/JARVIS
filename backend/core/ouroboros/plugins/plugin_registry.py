"""Plugin registry — discovery, loading, lifecycle, subsystem wiring.

Flow at harness boot:

  1. Check ``JARVIS_PLUGINS_ENABLED`` master gate (default OFF).
  2. Resolve search paths: ``.ouroboros/plugins/`` + ``$JARVIS_PLUGINS_PATH``.
  3. Discover manifests via ``parse_manifest`` (one per plugin dir).
  4. For each manifest (sorted by name for deterministic order):
       a. Check per-type sub-gate (``JARVIS_PLUGINS_SENSORS_ENABLED`` etc.)
       b. ``importlib`` the entry_module (sys.path prepend scoped to the
          plugin dir so relative imports within the plugin work).
       c. Instantiate the entry_class with a :class:`PluginContext`.
       d. Wire the instance into the matching host subsystem:
            * sensor → start background task + on_tick scheduler
            * gate   → register with SemanticGuardian pattern extension
            * repl   → register with the harness command dispatch
       e. Record a :class:`PluginLoadOutcome` for observability.
  5. Emit one summary INFO line: ``[Plugins] loaded=N failed=M disabled=K``
  6. Every plugin error is **isolated** — one failure never prevents
     other plugins from loading.

Authority invariant: plugin code is untrusted. The registry:

  * never hands plugins write access to the risk engine, Iron Gate,
    tier floor, auto-committer, ledger, or any deterministic engine;
  * routes sensor-proposed intents through ``UnifiedIntakeRouter`` so
    every existing governance gate applies;
  * gate plugins return structured findings that feed
    SemanticGuardian, which already treats them as deterministic
    signals — they don't get to mutate the risk tier directly;
  * repl plugins can print to the operator console but cannot call
    into orchestrator methods.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from backend.core.ouroboros.plugins.plugin_base import (
    GatePlugin,
    Plugin,
    PluginContext,
    PluginType,
    ReplCommandPlugin,
    SensorPlugin,
)
from backend.core.ouroboros.plugins.plugin_manifest import (
    PluginManifest,
    PluginManifestError,
    discover_manifests,
)


logger = logging.getLogger("Ouroboros.Plugins")

_ENV_ENABLED = "JARVIS_PLUGINS_ENABLED"
_ENV_PATH = "JARVIS_PLUGINS_PATH"
_ENV_ALLOW_MUTATIONS = "JARVIS_PLUGINS_ALLOW_MUTATIONS"
_ENV_SENSORS = "JARVIS_PLUGINS_SENSORS_ENABLED"
_ENV_GATES = "JARVIS_PLUGINS_GATES_ENABLED"
_ENV_REPL = "JARVIS_PLUGINS_REPL_ENABLED"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def plugins_enabled() -> bool:
    """Master switch. Default OFF (fail-closed — plugins are
    operator-authored third-party code and must be explicitly
    opted in)."""
    return os.environ.get(_ENV_ENABLED, "0").strip().lower() in _TRUTHY


def _per_type_enabled(env_key: str) -> bool:
    """Per-type sub-gate — defaults ON iff the master is on."""
    val = os.environ.get(env_key, "").strip().lower()
    if val == "":
        return True
    return val in _TRUTHY


def mutations_allowed() -> bool:
    return os.environ.get(_ENV_ALLOW_MUTATIONS, "0").strip().lower() in _TRUTHY


def plugins_path(repo_root: Path) -> Tuple[Path, ...]:
    """Compose the search roots: repo-local ``.ouroboros/plugins/``
    first, then each directory listed in ``$JARVIS_PLUGINS_PATH``."""
    roots: List[Path] = [Path(repo_root) / ".ouroboros" / "plugins"]
    env_path = os.environ.get(_ENV_PATH, "").strip()
    if env_path:
        for part in env_path.split(os.pathsep):
            part = part.strip()
            if part:
                roots.append(Path(part).expanduser())
    return tuple(roots)


# ---------------------------------------------------------------------------
# Load outcome — one record per discovered plugin
# ---------------------------------------------------------------------------


@dataclass
class PluginLoadOutcome:
    """Observability record for a single plugin. Populated regardless
    of whether the plugin loaded successfully."""

    manifest: Optional[PluginManifest]
    plugin: Optional[Plugin] = None
    state: str = "pending"        # "loaded" | "failed" | "disabled_by_type" | "skipped_master_off"
    error: str = ""
    plugin_dir: Optional[Path] = None
    ticker_task: Optional[asyncio.Task] = None

    @property
    def ok(self) -> bool:
        return self.state == "loaded"

    @property
    def name(self) -> str:
        if self.manifest is not None:
            return self.manifest.name
        if self.plugin_dir is not None:
            return self.plugin_dir.name
        return "?"


# ---------------------------------------------------------------------------
# Registry — the lifecycle orchestrator
# ---------------------------------------------------------------------------


@dataclass
class PluginRegistry:
    """In-memory registry of loaded plugins + lifecycle tasks."""

    repo_root: Path
    outcomes: List[PluginLoadOutcome] = field(default_factory=list)
    _repl_commands: Dict[str, ReplCommandPlugin] = field(default_factory=dict)
    _gate_plugins: List[GatePlugin] = field(default_factory=list)
    _sensor_tasks: List[asyncio.Task] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Discovery + loading
    # ------------------------------------------------------------------

    async def discover_and_load(
        self, *, intake_router: Any = None,
    ) -> List[PluginLoadOutcome]:
        """Walk the search paths, parse manifests, import + instantiate
        + register each plugin. Returns the outcome list.

        Safe to call at any point after the intake router is available
        (sensor plugins need it for ``propose``). Missing router →
        sensor ``propose`` falls through to ``mutations_disabled``.
        """
        if not plugins_enabled():
            logger.info(
                "[Plugins] disabled by %s=0 — skipping discovery",
                _ENV_ENABLED,
            )
            return self.outcomes

        roots = plugins_path(self.repo_root)
        manifests = discover_manifests(roots)
        logger.info(
            "[Plugins] discovering: roots=%d manifests=%d",
            len(roots), len(manifests),
        )

        for manifest in manifests:
            await self._load_one(
                manifest=manifest, intake_router=intake_router,
            )

        loaded = sum(1 for o in self.outcomes if o.state == "loaded")
        failed = sum(1 for o in self.outcomes if o.state == "failed")
        disabled = sum(
            1 for o in self.outcomes
            if o.state in ("disabled_by_type", "skipped_master_off")
        )
        logger.info(
            "[Plugins] summary: loaded=%d failed=%d disabled=%d",
            loaded, failed, disabled,
        )
        return self.outcomes

    async def _load_one(
        self, *, manifest: PluginManifest, intake_router: Any,
    ) -> None:
        outcome = PluginLoadOutcome(
            manifest=manifest, plugin_dir=manifest.plugin_dir,
        )
        self.outcomes.append(outcome)

        # Per-type sub-gate.
        type_env = {
            "sensor": _ENV_SENSORS,
            "gate": _ENV_GATES,
            "repl": _ENV_REPL,
        }.get(manifest.type)
        if type_env and not _per_type_enabled(type_env):
            outcome.state = "disabled_by_type"
            logger.info(
                "[Plugins] %s.%s disabled by %s=0",
                manifest.type, manifest.name, type_env,
            )
            return

        # Build the context.
        can_submit = mutations_allowed() and intake_router is not None
        ctx = PluginContext(
            plugin_name=manifest.name,
            plugin_dir=manifest.plugin_dir,
            repo_root=self.repo_root,
            emit_info=(
                lambda msg, _qn=manifest.qualified_name:
                    logger.info("[%s] %s", _qn, msg)
            ),
            submit_intent=_make_submit_intent_fn(
                intake_router=intake_router, can_submit=can_submit,
            ),
            can_submit_intents=can_submit,
        )

        # Import the entry module + instantiate.
        try:
            plugin_instance = _import_and_instantiate(
                manifest=manifest, context=ctx,
            )
        except Exception as exc:  # noqa: BLE001
            outcome.state = "failed"
            outcome.error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "[Plugins] load failed: %s — %s",
                manifest.qualified_name, outcome.error,
            )
            return

        # Register with the appropriate subsystem.
        try:
            await self._register(manifest, plugin_instance)
        except Exception as exc:  # noqa: BLE001
            outcome.state = "failed"
            outcome.error = f"register: {type(exc).__name__}: {exc}"
            logger.warning(
                "[Plugins] register failed: %s — %s",
                manifest.qualified_name, outcome.error,
            )
            return

        outcome.plugin = plugin_instance
        outcome.state = "loaded"
        logger.info(
            "[Plugins] loaded: %s.%s v=%s (from %s)",
            manifest.type, manifest.name, manifest.version,
            manifest.plugin_dir,
        )

        # Start lifecycle hook. Errors isolated — start() failure is
        # logged but the plugin stays registered.
        try:
            await plugin_instance.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Plugins] start() failed for %s: %s",
                manifest.qualified_name, exc, exc_info=True,
            )

        # Spawn the tick loop for sensor plugins.
        if manifest.type == "sensor" and isinstance(plugin_instance, SensorPlugin):
            task = asyncio.create_task(
                _sensor_tick_loop(plugin_instance, manifest.tick_interval_s),
                name=f"plugin-sensor-{manifest.name}",
            )
            outcome.ticker_task = task
            self._sensor_tasks.append(task)

    async def _register(
        self, manifest: PluginManifest, plugin_instance: Plugin,
    ) -> None:
        t = manifest.type
        if t == "sensor":
            if not isinstance(plugin_instance, SensorPlugin):
                raise TypeError(
                    "sensor type expects SensorPlugin subclass, got "
                    f"{type(plugin_instance).__name__}"
                )
            # Sensor plugins don't need explicit registration beyond
            # the tick-loop task; they call propose() which goes
            # through the intake router directly.
        elif t == "gate":
            if not isinstance(plugin_instance, GatePlugin):
                raise TypeError(
                    "gate type expects GatePlugin subclass, got "
                    f"{type(plugin_instance).__name__}"
                )
            if not plugin_instance.pattern_name:
                raise ValueError(
                    "gate plugin missing required class attribute "
                    "pattern_name"
                )
            if plugin_instance.severity not in ("soft", "hard"):
                raise ValueError(
                    f"gate plugin severity must be 'soft'|'hard', "
                    f"got {plugin_instance.severity!r}"
                )
            self._gate_plugins.append(plugin_instance)
            # Register pattern with SemanticGuardian at module level
            # so every subsequent inspect() call picks it up.
            _register_guardian_plugin_pattern(plugin_instance)
        elif t == "repl":
            if not isinstance(plugin_instance, ReplCommandPlugin):
                raise TypeError(
                    "repl type expects ReplCommandPlugin subclass, got "
                    f"{type(plugin_instance).__name__}"
                )
            cmd = (plugin_instance.command_name or "").strip().lstrip("/")
            if not cmd:
                raise ValueError(
                    "repl plugin missing required class attribute "
                    "command_name"
                )
            if cmd in self._repl_commands:
                raise ValueError(
                    f"repl command /{cmd} already registered by another plugin"
                )
            self._repl_commands[cmd] = plugin_instance
        else:
            raise ValueError(f"unknown plugin type: {t}")

    # ------------------------------------------------------------------
    # Public lookup / teardown
    # ------------------------------------------------------------------

    def repl_command(self, name: str) -> Optional[ReplCommandPlugin]:
        return self._repl_commands.get(name.lstrip("/"))

    def repl_commands(self) -> Tuple[Tuple[str, ReplCommandPlugin], ...]:
        return tuple(sorted(self._repl_commands.items()))

    def gate_plugins(self) -> Tuple[GatePlugin, ...]:
        return tuple(self._gate_plugins)

    def loaded(self) -> Tuple[PluginLoadOutcome, ...]:
        return tuple(o for o in self.outcomes if o.state == "loaded")

    def failed(self) -> Tuple[PluginLoadOutcome, ...]:
        return tuple(o for o in self.outcomes if o.state == "failed")

    async def shutdown(self) -> None:
        """Cancel sensor ticker tasks + call stop() on every loaded
        plugin. Safe to call multiple times."""
        for task in self._sensor_tasks:
            if not task.done():
                task.cancel()
        for task in self._sensor_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._sensor_tasks.clear()
        for o in self.outcomes:
            if o.plugin is None:
                continue
            try:
                await o.plugin.stop()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[Plugins] stop() raised on %s — ignoring",
                    o.manifest.qualified_name if o.manifest else "?",
                    exc_info=True,
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_and_instantiate(
    *, manifest: PluginManifest, context: PluginContext,
) -> Plugin:
    """Import the plugin's entry module and instantiate its entry class.

    Scopes ``sys.path`` prepend to the plugin dir so sibling modules
    within the plugin resolve via plain relative imports. The prepend
    is unconditional but namespaced by the plugin's qualified name
    (not by adding the dir itself — we use importlib's spec-loader
    directly so no sys.modules pollution).
    """
    entry_path = manifest.plugin_dir / f"{manifest.entry_module}.py"
    if not entry_path.is_file():
        raise FileNotFoundError(
            f"entry_module file not found: {entry_path}"
        )
    module_name = f"ouroboros_plugin_{manifest.name}__{manifest.entry_module}"
    spec = importlib.util.spec_from_file_location(module_name, entry_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"spec_from_file_location returned None for {entry_path}")

    # Prepend plugin_dir so intra-plugin imports work (``from helpers
    # import foo`` within the same plugin dir).
    added_path = str(manifest.plugin_dir)
    path_was_added = added_path not in sys.path
    if path_was_added:
        sys.path.insert(0, added_path)
    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module   # importlib expects this
        spec.loader.exec_module(module)
    finally:
        if path_was_added and sys.path and sys.path[0] == added_path:
            sys.path.pop(0)

    if not hasattr(module, manifest.entry_class):
        raise AttributeError(
            f"entry_class {manifest.entry_class!r} not found in "
            f"{manifest.entry_module}"
        )
    cls = getattr(module, manifest.entry_class)
    if not isinstance(cls, type):
        raise TypeError(
            f"entry_class {manifest.entry_class!r} is not a class"
        )
    if not issubclass(cls, Plugin):
        raise TypeError(
            f"entry_class {manifest.entry_class!r} must subclass Plugin"
        )
    return cls(context=context)


def _make_submit_intent_fn(
    *, intake_router: Any, can_submit: bool,
) -> Callable[..., Awaitable[str]]:
    """Build the submit_intent callable for the plugin context.

    When mutations are disabled OR the router is unavailable, the
    callable returns ``"mutations_disabled"`` without side effects.
    """
    async def _submit(
        *,
        source: str = "voice_human",
        description: str = "",
        target_files: Tuple[str, ...] = (),
        urgency: str = "normal",
        evidence: Optional[Dict[str, Any]] = None,
    ) -> str:
        if not can_submit or intake_router is None:
            return "mutations_disabled"
        try:
            from backend.core.ouroboros.governance.intake.intent_envelope import (
                make_envelope,
            )
            envelope = make_envelope(
                source=source,
                description=description,
                target_files=tuple(target_files or ()),
                repo="jarvis",
                confidence=0.9,
                urgency=urgency,
                evidence=evidence or {},
                requires_human_ack=False,
            )
            return await intake_router.ingest(envelope)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[Plugins] submit_intent raised: %s", exc, exc_info=True,
            )
            return f"submit_error:{type(exc).__name__}"

    return _submit


async def _sensor_tick_loop(
    sensor: SensorPlugin, interval_s: float,
) -> None:
    """Periodically call ``sensor.on_tick()`` and submit any proposed
    signals. Cancel-aware + error-isolated per tick."""
    logger.debug(
        "[Plugins] sensor tick loop started: %s interval=%.0fs",
        sensor.name, interval_s,
    )
    try:
        while True:
            try:
                proposed = await sensor.on_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                sensor.context.emit_info(
                    f"on_tick raised — skipping: {exc}"
                )
                proposed = []
            for signal in (proposed or []):
                try:
                    verdict = await sensor.propose(signal)
                    sensor.context.emit_info(
                        f"proposed: {verdict} — {signal.description[:80]}"
                    )
                except Exception as exc:  # noqa: BLE001
                    sensor.context.emit_info(
                        f"propose raised: {exc}"
                    )
            await asyncio.sleep(max(1.0, interval_s))
    except asyncio.CancelledError:
        logger.debug(
            "[Plugins] sensor tick loop cancelled: %s", sensor.name,
        )
        raise


# ---------------------------------------------------------------------------
# SemanticGuardian pattern extension
# ---------------------------------------------------------------------------
#
# Gate plugins add new patterns to SemanticGuardian at runtime. The
# guardian was written before this module existed, so we extend it
# via a module-level hook rather than modifying its internals.


_PLUGIN_GATE_PATTERNS: Dict[str, GatePlugin] = {}


def _register_guardian_plugin_pattern(plugin: GatePlugin) -> None:
    """Add a gate plugin's pattern to the SemanticGuardian's registry.

    Imports the guardian lazily so a broken guardian import doesn't
    prevent other plugin types from loading.
    """
    from backend.core.ouroboros.governance import semantic_guardian as sg

    name = plugin.pattern_name.strip()
    if not name:
        raise ValueError("plugin gate has empty pattern_name")
    if name in _PLUGIN_GATE_PATTERNS:
        raise ValueError(
            f"plugin pattern {name!r} already registered by another plugin"
        )
    if name in sg._PATTERNS:
        raise ValueError(
            f"plugin pattern {name!r} collides with built-in guardian pattern"
        )

    def _wrapped(
        *, file_path: str, old_content: str, new_content: str,
    ) -> Optional[sg.Detection]:
        try:
            hit = plugin.inspect(
                file_path=file_path,
                old_content=old_content,
                new_content=new_content,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[Plugins] gate %s raised — returning no finding: %s",
                name, exc,
            )
            return None
        if hit is None:
            return None
        sev, msg = hit
        if sev not in ("soft", "hard"):
            return None
        return sg.Detection(
            pattern=f"plugin.{plugin.name}.{name}",
            severity=sev,
            message=msg,
            file_path=file_path,
        )

    # Register under the plugin-namespaced name so findings in the
    # [SemanticGuard] telemetry line are unambiguously plugin-origin.
    sg._PATTERNS[f"plugin.{plugin.name}.{name}"] = _wrapped
    _PLUGIN_GATE_PATTERNS[name] = plugin


def unregister_guardian_plugin_patterns() -> None:
    """Test helper — clears every plugin-contributed pattern from the
    guardian registry. Does NOT touch built-in patterns."""
    from backend.core.ouroboros.governance import semantic_guardian as sg
    keys_to_drop = [
        k for k in list(sg._PATTERNS.keys()) if k.startswith("plugin.")
    ]
    for k in keys_to_drop:
        sg._PATTERNS.pop(k, None)
    _PLUGIN_GATE_PATTERNS.clear()


# ---------------------------------------------------------------------------
# Module-level default registry — one per harness session
# ---------------------------------------------------------------------------


_DEFAULT_REGISTRY: Optional[PluginRegistry] = None


def get_default_registry() -> Optional[PluginRegistry]:
    return _DEFAULT_REGISTRY


def register_default_plugins(registry: Optional[PluginRegistry]) -> None:
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = registry


def reset_default_registry() -> None:
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None
    unregister_guardian_plugin_patterns()
