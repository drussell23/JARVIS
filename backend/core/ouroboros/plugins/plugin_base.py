"""Abstract base classes + shared types for O+V plugins.

Plugins subclass one of:

  * :class:`SensorPlugin`       — new intake sources
  * :class:`GatePlugin`         — new SemanticGuardian pattern detectors
  * :class:`ReplCommandPlugin`  — new REPL slash commands

Each subclass is paired with a ``manifest.yaml`` (or ``.json``) in its
plugin directory. The registry discovers the manifest, validates it,
then imports the declared entry-point module + instantiates the
declared class.

Plugin lifecycle is explicit — subclasses override the stage methods
that matter for them. Bases provide no-op defaults so plugins that
don't need e.g. ``async start()`` don't pay for the override.

All plugins receive a :class:`PluginContext` at construction time.
The context is the narrow, explicit interface into the host harness:
plugins do not reach into module globals or import harness internals
directly. Anything a plugin needs must flow through this object.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


logger = logging.getLogger("Ouroboros.Plugins.Base")


class PluginType(str, Enum):
    """V1 plugin type enum. Additions here must be paired with a new
    abstract base class below, a registry branch, and a subsystem
    integration point. Keep narrow on purpose."""

    SENSOR = "sensor"
    GATE = "gate"
    REPL = "repl"


@dataclass(frozen=True)
class PluginContext:
    """Narrow host-harness interface exposed to plugins.

    Plugins are third-party code; they should not import
    ``backend.core.ouroboros.battle_test.harness`` directly. Anything
    they need must flow through the context.

    Attributes
    ----------
    plugin_name:
        Declared in the manifest. Used as a namespace prefix on
        plugin-authored sensors / commands so internals can identify
        the origin (``plugin.<name>.*``).
    plugin_dir:
        Absolute path to the plugin's directory. Plugins may read
        files from here (config, templates, fixtures) but MUST NOT
        treat any other path as plugin-writable unless explicitly
        handed a writable location.
    repo_root:
        Repo root the harness booted against.
    emit_info:
        Log an INFO-level line prefixed with the plugin's namespace.
        Delegated so plugin authors don't need to set up their own
        logger with the right prefix.
    submit_intent:
        Callable that wraps ``UnifiedIntakeRouter.ingest``, available
        only when ``JARVIS_PLUGINS_ALLOW_MUTATIONS=1``. When disabled
        (default), the callable returns ``"mutations_disabled"``
        without submitting — plugins that rely on it should check
        ``self.context.can_submit_intents`` first.
    """

    plugin_name: str
    plugin_dir: Path
    repo_root: Path
    emit_info: Callable[[str], None]
    submit_intent: Callable[..., Any]
    can_submit_intents: bool = False


# ---------------------------------------------------------------------------
# Abstract base: Plugin (root)
# ---------------------------------------------------------------------------


class Plugin(abc.ABC):
    """Root plugin base. Every plugin type extends this."""

    plugin_type: PluginType = PluginType.SENSOR  # subclasses override

    def __init__(self, *, context: PluginContext) -> None:
        self._context = context

    @property
    def context(self) -> PluginContext:
        return self._context

    @property
    def name(self) -> str:
        return self._context.plugin_name

    async def start(self) -> None:
        """Called once after registration, on the running event loop.
        Plugins that don't need async startup leave this as a no-op."""
        return None

    async def stop(self) -> None:
        """Called once at harness shutdown. Plugins with background
        tasks should cancel them here."""
        return None


# ---------------------------------------------------------------------------
# SensorPlugin — intake source
# ---------------------------------------------------------------------------


@dataclass
class SensorPluginSignal:
    """One signal a SensorPlugin can propose.

    Equivalent to the shape consumed by ``make_envelope`` but plugin-
    facing: plugins don't need to know about IntentEnvelope or the
    intake router's enum. The registry translates this into a real
    envelope + submits via the context.
    """

    description: str
    target_files: Tuple[str, ...] = ()
    urgency: str = "normal"         # "low" | "normal" | "high" | "critical"
    evidence: Dict[str, Any] = field(default_factory=dict)


class SensorPlugin(Plugin):
    """Base for sensor plugins. Plugins either:

      (a) run a long-lived async task that proposes signals by calling
          ``self.propose(signal)`` — e.g. polling an external system;
          OR
      (b) respond to host events by overriding ``on_tick`` (called
          every N seconds by the registry), which can return zero or
          more signals.

    Signals flow through the normal IntakeRouter → every governance
    gate applies.
    """

    plugin_type = PluginType.SENSOR

    async def on_tick(self) -> List[SensorPluginSignal]:
        """Called every tick. Return zero or more signals to submit.
        Default: no-op. Plugins that use the push model (``propose``)
        leave this as is."""
        return []

    async def propose(self, signal: SensorPluginSignal) -> str:
        """Submit a signal through the host's intake router.

        Returns the ingest verdict: ``"enqueued"``, ``"queued_behind"``,
        ``"deduplicated"``, ``"backpressure"``, ``"pending_ack"``, or
        ``"mutations_disabled"`` (when the mutation gate is off).
        """
        if not self._context.can_submit_intents:
            self._context.emit_info(
                "propose skipped — mutations disabled "
                "(set JARVIS_PLUGINS_ALLOW_MUTATIONS=1 to enable)"
            )
            return "mutations_disabled"
        return await self._context.submit_intent(
            source="voice_human",  # operator-triggered via plugin
            description=signal.description,
            target_files=signal.target_files,
            urgency=signal.urgency,
            evidence={**signal.evidence, "plugin_source": self.name},
        )


# ---------------------------------------------------------------------------
# GatePlugin — new SemanticGuardian pattern
# ---------------------------------------------------------------------------


class GatePlugin(Plugin):
    """Base for guardian-pattern plugins.

    A gate plugin contributes one named pattern to
    :class:`SemanticGuardian`. Its ``inspect`` method runs against a
    (file_path, old_content, new_content) triple and returns either:

      * ``None`` — no finding
      * ``(severity, message)`` — a detection (``"soft"`` / ``"hard"``)

    The registry wraps these into :class:`Detection` objects and
    merges them into the guardian's default pattern list.
    """

    plugin_type = PluginType.GATE

    # Subclasses MUST set these.
    pattern_name: str = ""     # canonical snake_case — unique across plugins
    severity: str = "soft"     # "soft" | "hard"

    def inspect(
        self,
        *,
        file_path: str,
        old_content: str,
        new_content: str,
    ) -> Optional[Tuple[str, str]]:
        """Return ``None`` when the pattern doesn't match, else
        ``(severity, message)`` where severity ∈ {"soft", "hard"} and
        message is a concise one-liner for operator logs + diff preview.

        Must not raise — exceptions propagate into the registry and
        the gate is quietly disabled for the rest of the session.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# ReplCommandPlugin — new slash command
# ---------------------------------------------------------------------------


class ReplCommandPlugin(Plugin):
    """Base for REPL slash-command plugins.

    Subclasses declare ``command_name`` (no slash prefix, e.g. ``"greet"``
    → REPL ``/greet`` works) and implement ``run(args)``. The harness
    dispatches when the operator types a matching command.
    """

    plugin_type = PluginType.REPL

    command_name: str = ""           # e.g. "greet" (no slash)
    summary: str = ""                # one-line help shown in /help

    async def run(self, args: str) -> str:
        """Execute the command. Return a string to echo to the operator
        console. Empty string = no output.

        Must not raise — the registry catches and prints a safe error
        so a broken plugin doesn't crash the REPL.
        """
        raise NotImplementedError
