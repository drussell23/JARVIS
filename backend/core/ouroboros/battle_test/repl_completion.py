"""ReplCompletion — auto-discovered slash-command palette + persistent history.
==============================================================================

Slice 3 of the **Gap #7 closure arc** (presentation restraint).

Root problem
------------

O+V's REPL surfaces ~20 verbs (``/risk``, ``/budget``, ``/cancel``,
``/attach``, ``/expand``, ``/review``, ``/accept``, ``/reject``,
``/narrate``, ``/preflight``, ``/organism``, …) but provides **no
discovery mechanism** — the operator must remember each verb. Tab
completion is disabled (``complete_while_typing=False``,
``enable_history_search=False``). History does not persist across
sessions. CC's ``/`` palette + ``↑/↓/Ctrl+R`` are absent.

The fix is **auto-discovery**: walk the ``SerpentREPL`` class for
``_handle_<verb>`` methods at boot time, build a completer from the
result, and wire ``prompt_toolkit``'s ``FileHistory`` for persistence.
Adding a new ``_handle_<verb>`` method automatically registers the
verb in the palette — single source of truth, no hardcoded
parallel list.

Architectural reuse — zero duplication
---------------------------------------

* :class:`SerpentREPL.``_handle_*`` methods` — the *existing* dispatch
  pattern is the source of truth. ``inspect.getmembers`` walks them.
* ``prompt_toolkit.completion.Completer`` — stdlib-style abstract
  class; we implement one subclass with the slash-prefix gate.
* ``prompt_toolkit.history.FileHistory`` — handles atomic writes,
  reads, and rotation natively. No custom file format.
* House style: frozen dataclass, ``schema_version``, master flag,
  ``register_flags`` / ``register_shipped_invariants`` (Slice 5).

Authority boundary
------------------

* §1 deterministic — pure introspection + UI plumbing; no LLM
* §7 fail-closed — every helper degrades silently on missing
  prompt_toolkit, missing FS access, missing methods. Completion
  off → input still works (just without dropdown).
* §8 observable — :class:`VerbRegistry` projection ready for the
  future ``GET /observability/repl-verbs`` route.

What this module does NOT do
----------------------------

* Define verbs — Slice 3 is consumer-only over the existing
  ``_handle_*`` convention.
* Modify the dispatch loop — completion is presentation, not
  routing. Existing dispatch in ``serpent_flow.py`` is unchanged.
"""
from __future__ import annotations

import inspect
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ReplCompletion")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


REPL_COMPLETION_SCHEMA_VERSION: str = "repl_completion.v1"


MASTER_FLAG_ENV_VAR: str = "JARVIS_REPL_COMPLETION_ENABLED"
HISTORY_PATH_ENV_VAR: str = "JARVIS_REPL_HISTORY_FILE"
HISTORY_ENABLED_ENV_VAR: str = "JARVIS_REPL_HISTORY_ENABLED"


# Default: project-local. Operators with multiple O+V projects get
# distinct histories per repo. Override via ``JARVIS_REPL_HISTORY_FILE``.
_DEFAULT_HISTORY_PATH: str = ".jarvis/repl_history"


# Methods on SerpentREPL with this prefix become slash-verbs.
_HANDLER_PREFIX: str = "_handle_"


# Built-in verbs that don't have ``_handle_*`` methods (handled
# directly in the REPL dispatch loop). They're registered here so
# the palette is complete.
_BUILTIN_VERBS: Tuple[Tuple[str, str], ...] = (
    ("/help", "show available commands"),
    ("/status", "current op + cost + posture snapshot"),
    ("/cost", "session cost breakdown"),
    ("/posture", "current strategic posture (EXPLORE / HARDEN / ...)"),
    ("/lessons", "session lessons (infra/code tagged learnings)"),
    ("/quit", "shut down the organism"),
    ("/exit", "shut down the organism (alias for /quit)"),
)


# ===========================================================================
# Master flag + history flag
# ===========================================================================


def is_completion_enabled() -> bool:
    """``JARVIS_REPL_COMPLETION_ENABLED``. **Default true** post Slice 5
    graduation (2026-05-04). Operators flip ``=false`` to disable the
    slash-command palette + tab completion + history search. NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def is_history_enabled() -> bool:
    """``JARVIS_REPL_HISTORY_ENABLED``. Default true — persistent
    history is conventional and minimally invasive. Operators can
    opt out for confidentiality (set to ``false``)."""
    raw = os.environ.get(HISTORY_ENABLED_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def resolve_history_path() -> Optional[Path]:
    """Resolve the per-session history file path.

    Precedence:

      1. ``JARVIS_REPL_HISTORY_FILE`` env (operator override)
      2. ``.jarvis/repl_history`` (project-local default)

    Returns ``None`` when history is disabled OR the parent directory
    can't be created (read-only fs / sandbox). NEVER raises.
    """
    if not is_history_enabled():
        return None
    explicit = os.environ.get(HISTORY_PATH_ENV_VAR, "").strip()
    raw_path = explicit if explicit else _DEFAULT_HISTORY_PATH
    try:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ReplCompletion] history path resolution failed (%r)",
            raw_path, exc_info=True,
        )
        return None


# ===========================================================================
# Frozen records
# ===========================================================================


@dataclass(frozen=True)
class VerbDescriptor:
    """One discovered REPL verb.

    Fields
    ------
    * ``slash_form`` — operator-typed verb (e.g. ``"/expand"``).
    * ``handler_method`` — the ``_handle_*`` method name on
      :class:`SerpentREPL` (empty string for built-ins).
    * ``description`` — first line of the handler's docstring (or a
      curated string for built-ins). Shown in the completion dropdown.
    """

    slash_form: str
    handler_method: str
    description: str
    schema_version: str = REPL_COMPLETION_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "slash_form": self.slash_form,
            "handler_method": self.handler_method,
            "description": self.description,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class VerbRegistry:
    """Frozen snapshot of the discovered + built-in verbs."""

    verbs: Tuple[VerbDescriptor, ...]
    schema_version: str = REPL_COMPLETION_SCHEMA_VERSION

    def __len__(self) -> int:
        return len(self.verbs)

    def slash_forms(self) -> Tuple[str, ...]:
        return tuple(v.slash_form for v in self.verbs)

    def find(self, slash_form: object) -> Optional[VerbDescriptor]:
        if not isinstance(slash_form, str):
            return None
        for v in self.verbs:
            if v.slash_form == slash_form:
                return v
        return None


# ===========================================================================
# Helpers
# ===========================================================================


def _method_name_to_slash(method_name: str) -> str:
    """Convert a method name to its slash-form.

    Examples:
      * ``_handle_expand`` → ``/expand``
      * ``_handle_mutation_gate`` → ``/mutation-gate``
      * ``_handle_verify_confirm`` → ``/verify-confirm``

    Mirrors the existing dispatch convention in ``serpent_flow.py``
    where multi-word verbs use hyphens (``/mutation-gate``).
    """
    if not method_name.startswith(_HANDLER_PREFIX):
        return ""
    suffix = method_name[len(_HANDLER_PREFIX):]
    if not suffix:
        return ""
    return "/" + suffix.replace("_", "-")


def _first_doc_line(method: object) -> str:
    """Extract the first non-empty line of the method's docstring.

    Returns ``""`` when no docstring is available. NEVER raises.
    Strips Rich/Sphinx markup so the dropdown stays clean.
    """
    try:
        doc = inspect.getdoc(method) or ""
    except Exception:  # noqa: BLE001
        return ""
    for line in doc.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Strip ``literal`` backticks and Sphinx role prefixes for
        # cleaner dropdown rendering.
        cleaned = re.sub(r"``([^`]+)``", r"\1", stripped)
        cleaned = re.sub(r"^:[a-zA-Z]+:", "", cleaned).strip()
        # Drop the Sphinx "directive: rest" prefix — keep readable text only
        if cleaned:
            # Truncate to ~80 chars for tidy dropdown rendering
            return cleaned[:80]
    return ""


# ===========================================================================
# Auto-discovery — walk SerpentREPL._handle_* methods
# ===========================================================================


_HANDLER_BLOCKLIST: frozenset = frozenset({
    # Methods that match _handle_* but aren't operator-typed verbs.
    # Add here if a future refactor introduces a private helper that
    # accidentally matches the prefix.
})


def discover_verbs(repl_instance: object) -> VerbRegistry:
    """Walk ``repl_instance`` for ``_handle_<verb>`` methods and
    return a :class:`VerbRegistry` covering them PLUS the built-in
    verbs (``/help``, ``/status``, etc.) that don't have
    ``_handle_*`` handlers.

    Auto-discovery is the structural source of truth — adding a new
    ``_handle_*`` method registers the verb without any other edit.
    Removing one removes the verb from the palette automatically.

    NEVER raises. Returns a registry with at least the built-ins
    when ``repl_instance`` is invalid / has no ``_handle_*``
    methods.
    """
    discovered: List[VerbDescriptor] = []
    seen: set = set()

    if repl_instance is not None:
        try:
            members = inspect.getmembers(repl_instance, predicate=callable)
        except Exception:  # noqa: BLE001
            members = []
        for name, method in members:
            if not name.startswith(_HANDLER_PREFIX):
                continue
            if name in _HANDLER_BLOCKLIST:
                continue
            slash = _method_name_to_slash(name)
            if not slash or slash in seen:
                continue
            seen.add(slash)
            discovered.append(
                VerbDescriptor(
                    slash_form=slash,
                    handler_method=name,
                    description=_first_doc_line(method),
                )
            )

    # Layer in built-ins (don't override discovered descriptions —
    # but `/help`, `/status`, etc. don't have `_handle_*` so this is
    # additive in practice).
    for slash, desc in _BUILTIN_VERBS:
        if slash in seen:
            continue
        seen.add(slash)
        discovered.append(
            VerbDescriptor(
                slash_form=slash,
                handler_method="",
                description=desc,
            )
        )

    # Stable alphabetical order — the completion dropdown is more
    # navigable when verbs sort consistently across runs.
    discovered.sort(key=lambda v: v.slash_form)
    return VerbRegistry(verbs=tuple(discovered))


# ===========================================================================
# prompt_toolkit Completer — slash-prefix gate
# ===========================================================================


def build_completer(registry: VerbRegistry) -> Optional[object]:
    """Build a ``prompt_toolkit.completion.Completer`` that fires only
    when the input starts with ``/``. Returns ``None`` when
    prompt_toolkit isn't available (headless / sandbox).

    The completer matches by *prefix* against each verb's slash form
    and yields ``Completion`` objects with a ``display_meta`` set to
    the verb's description. The dropdown looks like:

      ::

        /accept        accept a pending Gap #4 review
        /attach        attach a file to the active op
        ...

    NEVER raises into the prompt_toolkit dispatch path.
    """
    try:
        from prompt_toolkit.completion import Completer, Completion
    except ImportError:
        return None

    verbs = list(registry.verbs)

    class _SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            # Only trigger when input starts with a slash. Operators
            # typing prose / goals / content shouldn't see verb
            # suggestions interleaved with their natural input.
            if not text.startswith("/"):
                return
            # ``text`` includes the leading slash; match by prefix.
            for verb in verbs:
                if verb.slash_form.startswith(text):
                    yield Completion(
                        text=verb.slash_form,
                        start_position=-len(text),
                        display=verb.slash_form,
                        display_meta=verb.description,
                    )

    return _SlashCompleter()


# ===========================================================================
# prompt_toolkit History wrapper — FileHistory + graceful fallback
# ===========================================================================


def build_history(
    path: Optional[Path] = None,
) -> Optional[object]:
    """Construct a ``prompt_toolkit.history.History`` instance.

    Defaults to a ``FileHistory`` at ``.jarvis/repl_history``. Falls
    back to ``InMemoryHistory`` when the file isn't writable. Returns
    ``None`` when prompt_toolkit isn't available OR history is
    disabled via :func:`is_history_enabled`.
    """
    if not is_history_enabled():
        return None
    try:
        from prompt_toolkit.history import FileHistory, InMemoryHistory
    except ImportError:
        return None

    eff_path = path if path is not None else resolve_history_path()
    if eff_path is None:
        try:
            return InMemoryHistory()
        except Exception:  # noqa: BLE001
            return None

    try:
        # FileHistory writes atomically per command — no buffering
        # contention with concurrent SIGINT.
        return FileHistory(str(eff_path))
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ReplCompletion] FileHistory(%r) failed; using in-memory",
            str(eff_path), exc_info=True,
        )
        try:
            return InMemoryHistory()
        except Exception:  # noqa: BLE001
            return None


# ===========================================================================
# Convenience: one-shot wire-up for SerpentREPL._loop
# ===========================================================================


@dataclass(frozen=True)
class CompletionWiring:
    """Result of :func:`build_completion_wiring` — bundles everything
    a caller needs to thread into ``PromptSession(...)``.

    Fields
    ------
    * ``completer`` — ``prompt_toolkit.completion.Completer`` or ``None``
    * ``history`` — ``prompt_toolkit.history.History`` or ``None``
    * ``enable_history_search`` — ``True`` when history is wired
      (Ctrl+R works automatically when prompt_toolkit has a history
      to search)
    * ``registry`` — the discovered :class:`VerbRegistry` (also
      surfaces via ``/help`` rendering in Slice 5)
    """

    completer: Optional[object]
    history: Optional[object]
    enable_history_search: bool
    registry: VerbRegistry
    schema_version: str = REPL_COMPLETION_SCHEMA_VERSION


def build_completion_wiring(
    repl_instance: object,
    *,
    history_path: Optional[Path] = None,
) -> CompletionWiring:
    """One-shot wire-up. Caller in ``serpent_flow.py``::

        wiring = build_completion_wiring(self)
        session_kwargs = {}
        if wiring.completer is not None:
            session_kwargs["completer"] = wiring.completer
        if wiring.history is not None:
            session_kwargs["history"] = wiring.history
            session_kwargs["enable_history_search"] = wiring.enable_history_search
        session = PromptSession(**session_kwargs, ...)

    NEVER raises. Always returns a wiring (even with ``None`` slots
    when prompt_toolkit / FS access is unavailable).
    """
    registry = discover_verbs(repl_instance)
    completer: Optional[object] = None
    history: Optional[object] = None
    enable_search = False

    if is_completion_enabled():
        try:
            completer = build_completer(registry)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ReplCompletion] build_completer failed", exc_info=True,
            )

        try:
            history = build_history(history_path)
            if history is not None:
                enable_search = True
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ReplCompletion] build_history failed", exc_info=True,
            )

    return CompletionWiring(
        completer=completer,
        history=history,
        enable_history_search=enable_search,
        registry=registry,
    )


__all__ = [
    "CompletionWiring",
    "HISTORY_ENABLED_ENV_VAR",
    "HISTORY_PATH_ENV_VAR",
    "MASTER_FLAG_ENV_VAR",
    "REPL_COMPLETION_SCHEMA_VERSION",
    "VerbDescriptor",
    "VerbRegistry",
    "build_completer",
    "build_completion_wiring",
    "build_history",
    "discover_verbs",
    "is_completion_enabled",
    "is_history_enabled",
    "resolve_history_path",
]
