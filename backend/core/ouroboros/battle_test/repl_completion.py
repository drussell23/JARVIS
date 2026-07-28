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

import contextlib
import enum
import inspect
import re
import logging
import os
import re
from backend.core.ouroboros.battle_test.verb_usage import (
    UNDOCUMENTED,
    derive_usage,
    extract_operator_section,
    mine_subcommands,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, FrozenSet, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ReplCompletion")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


REPL_COMPLETION_SCHEMA_VERSION: str = "repl_completion.v1"


MASTER_FLAG_ENV_VAR: str = "JARVIS_REPL_COMPLETION_ENABLED"
HISTORY_PATH_ENV_VAR: str = "JARVIS_REPL_HISTORY_FILE"
HISTORY_ENABLED_ENV_VAR: str = "JARVIS_REPL_HISTORY_ENABLED"
INLINE_HELP_ENABLED_ENV_VAR: str = "JARVIS_REPL_INLINE_HELP_ENABLED"
AUTOSUGGEST_ENABLED_ENV_VAR: str = "JARVIS_REPL_AUTOSUGGEST_ENABLED"
COMPLETE_WHILE_TYPING_ENV_VAR: str = "JARVIS_REPL_COMPLETE_WHILE_TYPING"
UNIFIED_REGISTRY_ENV_VAR: str = "JARVIS_UNIFIED_VERB_REGISTRY_ENABLED"


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


def is_inline_help_enabled() -> bool:
    """``JARVIS_REPL_INLINE_HELP_ENABLED``. §41.3 Slice 3 #12 —
    default true. Gates the inline ``?`` tooltip keybinding that
    surfaces verb help mid-line without disrupting the input
    buffer. Implicitly off when
    ``JARVIS_REPL_COMPLETION_ENABLED=false`` (no verb registry
    available). NEVER raises."""
    if not is_completion_enabled():
        return False
    raw = os.environ.get(INLINE_HELP_ENABLED_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def is_autosuggest_enabled() -> bool:
    """``JARVIS_REPL_AUTOSUGGEST_ENABLED``. Default true. Gates the
    history ghost-text (fish/CC-style grey suggestion completed with →).
    Implicitly off when history is off — there is nothing to suggest
    from. NEVER raises."""
    if not is_history_enabled():
        return False
    raw = os.environ.get(AUTOSUGGEST_ENABLED_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def is_complete_while_typing_enabled() -> bool:
    """``JARVIS_REPL_COMPLETE_WHILE_TYPING``. Default true — the palette
    opens as the operator types ``/`` instead of demanding an explicit
    Tab, matching the attach surfaces (both already pass
    ``complete_while_typing=True``). Flip ``=false`` to restore
    Tab-only completion on the daemon REPL. NEVER raises."""
    raw = os.environ.get(COMPLETE_WHILE_TYPING_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def is_unified_registry_enabled() -> bool:
    """``JARVIS_UNIFIED_VERB_REGISTRY_ENABLED``. Default true. Gates the
    reconciliation of the two historic verb sources — ``discover_verbs``
    (the daemon's ``_handle_*`` walk) and ``registry_from_dispatch``
    (the 60-verb dispatch table + client verbs) — into ONE registry, so
    every surface completes the same vocabulary. NEVER raises."""
    raw = os.environ.get(UNIFIED_REGISTRY_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def build_auto_suggest() -> Optional[object]:
    """History ghost-text, threaded so a large history file never lands
    on a keystroke. Returns ``None`` when disabled or prompt_toolkit is
    unavailable. NEVER raises."""
    if not is_autosuggest_enabled():
        return None
    try:
        from prompt_toolkit.auto_suggest import (
            AutoSuggestFromHistory,
            ThreadedAutoSuggest,
        )
        return ThreadedAutoSuggest(AutoSuggestFromHistory())
    except Exception:  # noqa: BLE001
        return None


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


class VerbCategory(str, enum.Enum):
    """Closed 4-value taxonomy — bytes-pinned via AST.

    Group verbs for tutorial mode + ``/help`` rendering. Categories
    derive from the handler's docstring tag (``@category: lifecycle``)
    or default to OPERATIONAL when no tag is present.
    """

    LIFECYCLE = "lifecycle"       # /accept /reject /cancel /quit /goal
    INTROSPECTION = "introspection"   # /status /posture /budget /memory
    NAVIGATION = "navigation"     # /expand /attach /review
    OPERATIONAL = "operational"   # everything else (default)


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
    * ``aliases`` — additional slash-forms that route to the same
      handler (e.g. ``/exit`` aliases ``/quit``). Empty tuple by default.
    * ``examples`` — operator-facing usage examples surfaced in
      ``--help`` output AND injected into error messages. Each entry
      is one example line (e.g. ``"/cancel op-abc123 --immediate"``).
    * ``arg_spec`` — short usage line derived from the handler's
      signature (e.g. ``"<op_id> [--immediate]"``). Empty for verbs
      taking no arguments.
    * ``category`` — :class:`VerbCategory` for tutorial grouping.

    All extension fields default to empty/OPERATIONAL so existing
    callers constructing :class:`VerbDescriptor` keyword-only with the
    original three fields remain byte-identical.
    """

    slash_form: str
    handler_method: str
    description: str
    aliases: Tuple[str, ...] = ()
    examples: Tuple[str, ...] = ()
    arg_spec: str = ""
    category: VerbCategory = VerbCategory.OPERATIONAL
    schema_version: str = REPL_COMPLETION_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "slash_form": self.slash_form,
            "handler_method": self.handler_method,
            "description": self.description,
            "aliases": list(self.aliases),
            "examples": list(self.examples),
            "arg_spec": self.arg_spec,
            "category": self.category.value,
            "schema_version": self.schema_version,
        }

    def matches(self, slash_form: object) -> bool:
        """True iff ``slash_form`` matches the primary form OR an alias."""
        if not isinstance(slash_form, str):
            return False
        if slash_form == self.slash_form:
            return True
        return slash_form in self.aliases


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
        """Find by primary slash form OR alias. NEVER raises."""
        if not isinstance(slash_form, str):
            return None
        for v in self.verbs:
            if v.matches(slash_form):
                return v
        return None

    def by_category(
        self, category: object,
    ) -> Tuple[VerbDescriptor, ...]:
        """Filter verbs by category. NEVER raises."""
        try:
            value = (
                category.value if hasattr(category, "value")
                else str(category)
            )
        except Exception:  # noqa: BLE001
            return ()
        return tuple(
            v for v in self.verbs if v.category.value == value
        )

    def categories(self) -> Tuple[str, ...]:
        """Unique categories present, sorted. NEVER raises."""
        return tuple(sorted(
            {v.category.value for v in self.verbs}
        ))


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
        # @-tag lines aren't the description; skip past them.
        if stripped.startswith("@") and ":" in stripped:
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


# Lightweight docstring @-tag parser. Convention (additive — no
# handler is REQUIRED to use these; missing tags default to
# empty/OPERATIONAL):
#
#   @arg_spec: <op_id> [--immediate]
#   @example: /cancel op-abc123
#   @example: /cancel op-abc123 --immediate
#   @category: lifecycle
#   @alias: /stop
#
# Multiple ``@example`` and ``@alias`` lines collect into tuples.
_TAG_LINE_RE = re.compile(r"^\s*@(\w+)\s*:\s*(.+?)\s*$")


def _parse_doc_tags(method: object) -> dict:
    """Parse @-tag metadata out of a handler's docstring. NEVER raises."""
    out: dict = {
        "aliases": [],
        "examples": [],
        "arg_spec": "",
        "category": VerbCategory.OPERATIONAL,
    }
    try:
        doc = inspect.getdoc(method) or ""
    except Exception:  # noqa: BLE001
        return out
    for line in doc.splitlines():
        m = _TAG_LINE_RE.match(line)
        if not m:
            continue
        tag, value = m.group(1).lower(), m.group(2).strip()
        if not value:
            continue
        if tag == "alias":
            slash = value if value.startswith("/") else f"/{value}"
            out["aliases"].append(slash[:64])
        elif tag == "example":
            out["examples"].append(value[:200])
        elif tag == "arg_spec":
            out["arg_spec"] = value[:160]
        elif tag == "category":
            try:
                out["category"] = VerbCategory(value.lower())
            except (TypeError, ValueError):
                pass  # unknown category → stays OPERATIONAL
    return out


def _infer_arg_spec_from_signature(method: object) -> str:
    """Build a usage-line fallback from the method's signature.

    Used when no ``@arg_spec:`` tag is present. NEVER raises —
    returns empty on failure. Skips ``self`` and ignores *args/
    **kwargs so the output stays operator-friendly.
    """
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return ""
    parts: List[str] = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        kind = param.kind
        if kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if param.default is inspect.Parameter.empty:
            parts.append(f"<{name}>")
        else:
            parts.append(f"[{name}]")
    return " ".join(parts)[:160]


# ===========================================================================
# §41.3 Slice 3 #14 — arg completion substrate
# ===========================================================================


class ArgKind(str, enum.Enum):
    """Closed 4-value taxonomy — bytes-pinned via AST.

    Determines how :func:`get_arg_candidates` resolves completions
    for an argument position:

    * STATIC   — enumeration of fixed values (e.g., posture names,
                 risk tiers). Values come from ``static_values``.
    * DYNAMIC  — runtime state (e.g., active op IDs). Resolved by
                 a callable registered via
                 :func:`register_arg_provider` keyed by
                 ``provider_key``.
    * PATH     — filesystem path; delegated to the existing
                 :mod:`repl_input_polish` PathCompleter when
                 available.
    * FREE     — no completion; operator types whatever they want.
    """

    STATIC = "static"
    DYNAMIC = "dynamic"
    PATH = "path"
    FREE = "free"


@dataclass(frozen=True)
class ArgPositionSpec:
    """One argument position within a verb's signature.

    Fields
    ------
    * ``name`` — operator-facing argument name from
      :attr:`VerbDescriptor.arg_spec` (e.g., ``"op_id"``,
      ``"category"``).
    * ``required`` — True for ``<x>``; False for ``[x]`` or
      ``[--flag]``.
    * ``kind`` — :class:`ArgKind` controlling how candidates are
      resolved.
    * ``static_values`` — populated when ``kind == STATIC``.
    * ``provider_key`` — populated when ``kind == DYNAMIC``;
      looks up a callable in the module-level provider registry.
    * ``flag_form`` — non-empty when this position is a flag
      (e.g., ``"--immediate"``); empty for positional args.
    """

    name: str
    required: bool
    kind: ArgKind = ArgKind.FREE
    static_values: Tuple[str, ...] = ()
    provider_key: str = ""
    flag_form: str = ""
    schema_version: str = REPL_COMPLETION_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "required": self.required,
            "kind": self.kind.value,
            "static_values": list(self.static_values),
            "provider_key": self.provider_key,
            "flag_form": self.flag_form,
            "schema_version": self.schema_version,
        }


# Module-level provider registry — convention-based dispatch.
# Keyed by argument NAME (e.g., "op_id", "category"); operator-side
# wiring registers callables at boot via
# :func:`register_arg_provider`. The substrate's own seeds (e.g.,
# ``category`` → :class:`VerbCategory` values) live in
# ``_STATIC_DEFAULTS`` so they're discoverable without any runtime
# registration.

_ARG_PROVIDERS: "dict[str, Callable[[str], Tuple[str, ...]]]" = {}


# Default static value tables. Convention-based dispatch reads
# from this table when an argument's NAME matches; the parser
# automatically tags such positions as :attr:`ArgKind.STATIC`.
# Operators / wiring sites can override per-position by calling
# :func:`register_arg_provider` (DYNAMIC kind takes precedence).
def _seed_static_defaults() -> "dict[str, Tuple[str, ...]]":
    # NEVER raises — VerbCategory is defined above; this is a
    # closed-taxonomy enumeration we know exists.
    return {
        "category": tuple(c.value for c in VerbCategory),
    }


_STATIC_DEFAULTS: "dict[str, Tuple[str, ...]]" = (
    _seed_static_defaults()
)


# Argument names whose semantics are filesystem paths. Convention-
# based — wiring sites can extend by appending.
_PATH_NAMES: FrozenSet[str] = frozenset({
    "path", "file", "file_path", "filepath",
})


def register_arg_provider(
    key: object,
    provider: object,
) -> bool:
    """Register a runtime provider for DYNAMIC arg completion.

    The provider must be a callable ``(prefix: str) ->
    Tuple[str, ...]``. NEVER raises. Returns True on success,
    False when the input is invalid.

    Operator-side example::

        register_arg_provider(
            "op_id",
            lambda prefix: tuple(
                op for op in gls.active_op_ids()
                if op.startswith(prefix)
            ),
        )
    """
    try:
        if not isinstance(key, str) or not key.strip():
            return False
        if not callable(provider):
            return False
        _ARG_PROVIDERS[key.strip()] = provider  # type: ignore[assignment]
        return True
    except Exception:  # noqa: BLE001
        return False


def unregister_arg_provider(key: object) -> bool:
    """Remove a previously-registered provider. NEVER raises."""
    try:
        if not isinstance(key, str):
            return False
        return _ARG_PROVIDERS.pop(key, None) is not None
    except Exception:  # noqa: BLE001
        return False


def list_arg_providers() -> Tuple[str, ...]:
    """Snapshot of registered provider keys for introspection."""
    try:
        return tuple(sorted(_ARG_PROVIDERS.keys()))
    except Exception:  # noqa: BLE001
        return ()


# Regex covering the six arg_spec atom forms:
#   <name>             — required positional
#   [name]             — optional positional
#   [--flag]           — optional flag, no value
#   [--flag <value>]   — optional flag with value
#   <a|b|c>            — required choice (STATIC values inline)
#   [a|b|c]            — optional choice (STATIC values inline)
# Choice atoms exist so a verb's MINED subcommand vocabulary
# (verb_usage.mine_subcommands) can ride the same spec grammar the
# doc-tag convention uses — per-verb static values with no parallel
# table and no per-verb provider registration. Choice alternatives
# are listed first so the atom regex prefers them over bare names.
# Compiled once; pure-function consumers are thread-safe.
_ARG_SPEC_ATOM_RE = re.compile(
    r"<(?P<req_choice>[a-zA-Z_][\w-]*(?:\|[a-zA-Z_][\w-]*)+)>"
    r"|\[(?P<opt_choice>[a-zA-Z_][\w-]*(?:\|[a-zA-Z_][\w-]*)+)\]"
    r"|<(?P<req>[a-zA-Z_][\w-]*)>"
    r"|\[--(?P<flag>[a-zA-Z][\w-]*)(?:\s+<(?P<flag_val>[a-zA-Z_][\w-]*)>)?\]"
    r"|\[(?P<opt>[a-zA-Z_][\w-]*)\]"
)


def _classify_arg_name(name: str) -> Tuple[ArgKind, Tuple[str, ...], str]:
    """Classify a position by convention. NEVER raises.

    Returns ``(kind, static_values, provider_key)`` triple. Rules
    (first match wins):

    1. ``name`` in :data:`_PATH_NAMES` → PATH
    2. ``name`` in :data:`_STATIC_DEFAULTS` → STATIC with the
       seeded values
    3. ``name`` is a registered provider key → DYNAMIC with that
       key
    4. otherwise → FREE
    """
    try:
        clean = name.strip().lower()
    except Exception:  # noqa: BLE001
        return ArgKind.FREE, (), ""
    if not clean:
        return ArgKind.FREE, (), ""
    if clean in _PATH_NAMES:
        return ArgKind.PATH, (), ""
    if clean in _STATIC_DEFAULTS:
        return ArgKind.STATIC, _STATIC_DEFAULTS[clean], ""
    if clean in _ARG_PROVIDERS:
        return ArgKind.DYNAMIC, (), clean
    return ArgKind.FREE, (), ""


def parse_arg_spec(raw: object) -> Tuple[ArgPositionSpec, ...]:
    """Parse an arg_spec string into structured position specs.

    Pure function; NEVER raises. Returns empty tuple on bad input.

    Convention examples::

        ""                                       → ()
        "<op_id>"                                → (REQ op_id,)
        "<op_id> [--immediate]"                  → (REQ op_id, FLAG --immediate)
        "[category]"                             → (OPT category,)
        "<set <amount>>"  (not supported)        → graceful fallback

    Recognition order (per atom):
      1. Flag form ``[--name]`` or ``[--name <value>]``
      2. Required positional ``<name>``
      3. Optional positional ``[name]``
    """
    try:
        text = str(raw or "")
    except Exception:  # noqa: BLE001
        return ()
    if not text.strip():
        return ()
    out: List[ArgPositionSpec] = []
    for match in _ARG_SPEC_ATOM_RE.finditer(text):
        gd = match.groupdict()
        if gd.get("req_choice") or gd.get("opt_choice"):
            raw_choice = gd.get("req_choice") or gd.get("opt_choice") or ""
            values = tuple(
                v for v in (p.strip() for p in raw_choice.split("|")) if v
            )
            out.append(ArgPositionSpec(
                name="subcommand",
                required=bool(gd.get("req_choice")),
                kind=ArgKind.STATIC,
                static_values=values,
            ))
        elif gd.get("flag"):
            flag_name = gd["flag"]
            flag_val = gd.get("flag_val") or ""
            if flag_val:
                # Flag with value — completion targets the value
                kind, statics, key = _classify_arg_name(flag_val)
                out.append(ArgPositionSpec(
                    name=flag_val,
                    required=False,
                    kind=kind,
                    static_values=statics,
                    provider_key=key,
                    flag_form=f"--{flag_name}",
                ))
            else:
                # Bare flag — completes to its own literal
                out.append(ArgPositionSpec(
                    name=flag_name,
                    required=False,
                    kind=ArgKind.STATIC,
                    static_values=(f"--{flag_name}",),
                    provider_key="",
                    flag_form=f"--{flag_name}",
                ))
        elif gd.get("req"):
            name = gd["req"]
            kind, statics, key = _classify_arg_name(name)
            out.append(ArgPositionSpec(
                name=name,
                required=True,
                kind=kind,
                static_values=statics,
                provider_key=key,
            ))
        elif gd.get("opt"):
            name = gd["opt"]
            kind, statics, key = _classify_arg_name(name)
            out.append(ArgPositionSpec(
                name=name,
                required=False,
                kind=kind,
                static_values=statics,
                provider_key=key,
            ))
    return tuple(out)


def get_arg_candidates(
    position: object,
    prefix: object = "",
    *,
    max_results: int = 32,
) -> Tuple[str, ...]:
    """Resolve completion candidates for one argument position.

    Pure dispatcher composing the static_values table OR the
    registered DYNAMIC provider; PATH is intentionally left to the
    completer (which already integrates the existing
    ``repl_input_polish`` PathCompleter); FREE returns ``()``.

    NEVER raises. Prefix-filters and de-dupes case-insensitively.
    """
    try:
        if not isinstance(position, ArgPositionSpec):
            return ()
        pref = str(prefix or "")
    except Exception:  # noqa: BLE001
        return ()
    candidates: Tuple[str, ...] = ()
    if position.kind is ArgKind.STATIC:
        candidates = position.static_values
    elif position.kind is ArgKind.DYNAMIC:
        provider = _ARG_PROVIDERS.get(position.provider_key)
        if provider is None:
            return ()
        try:
            raw = provider(pref)
        except Exception:  # noqa: BLE001
            return ()
        if not isinstance(raw, tuple):
            try:
                raw = tuple(raw)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                return ()
        candidates = tuple(
            str(c) for c in raw if c is not None
        )
    else:
        # PATH + FREE — completer handles separately or yields none
        return ()
    if not candidates:
        return ()
    pref_lower = pref.lower()
    seen: set = set()
    out: List[str] = []
    for c in candidates:
        if not isinstance(c, str):
            continue
        if pref_lower and not c.lower().startswith(pref_lower):
            continue
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
        if len(out) >= max(1, int(max_results)):
            break
    return tuple(out)


def cursor_arg_position(buffer_text: object) -> Tuple[int, str]:
    """Determine the operator's current argument position.

    Returns ``(position_index, prefix)`` where ``position_index``
    is 0 for the first arg slot (just after the verb), 1 for the
    second, etc., and ``prefix`` is the partial text typed at the
    current position.

    Returns ``(-1, "")`` when the buffer doesn't represent an
    arg-completable state (no leading slash, no verb token yet,
    or the parser can't decide).

    NEVER raises.
    """
    try:
        text = str(buffer_text or "")
    except Exception:  # noqa: BLE001
        return -1, ""
    stripped_left = text.lstrip()
    if not stripped_left.startswith("/"):
        return -1, ""
    # Quote-aware split: '/attach "my file<cursor>' is ONE half-typed
    # argument, not two. A naive split(" ") put the cursor in a
    # phantom second position the moment a path contained a space.
    tokens = _split_arg_tokens(stripped_left)
    # tokens[0] is the verb (e.g., "/cancel"); subsequent tokens
    # are arg slots. Trailing empty string means "started a new
    # token" (operator typed a space).
    if len(tokens) <= 1:
        return -1, ""
    position_index = len(tokens) - 2  # tokens[1] is position 0
    prefix = tokens[-1]
    return position_index, prefix


def _split_arg_tokens(text: str) -> List[str]:
    """Split on spaces OUTSIDE quotes, preserving the naive-split
    contract the completer depends on: a trailing unquoted space yields
    a trailing ``""`` token ("operator started a new slot"), and quote
    characters stay in the token (the prefix the operator actually
    typed, byte-for-byte, so ``start_position`` math stays honest).
    Unbalanced quotes — the mid-typing state — simply extend the final
    token. NEVER raises."""
    tokens: List[str] = [""]
    quote = ""
    for ch in text:
        if quote:
            tokens[-1] += ch
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
            tokens[-1] += ch
        elif ch == " ":
            tokens.append("")
        else:
            tokens[-1] += ch
    return tokens


# ===========================================================================
# §41.3 Slice 2/3 substrate helpers — typo suggestion + fuzzy match + help
# ===========================================================================


def _levenshtein(a: str, b: str, *, cap: int = 4) -> int:
    """Bounded Levenshtein distance. NEVER raises.

    Early-exits when the running minimum exceeds ``cap`` — keeps the
    cost O(min(len, cap)) per call so this is safe to run against
    every verb on every unknown-verb error.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    # Two-row dynamic programming
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        row_min = curr[0]
        for j, cb in enumerate(b, start=1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            curr[j] = min(ins, dele, sub)
            if curr[j] < row_min:
                row_min = curr[j]
        if row_min > cap:
            return cap + 1
        prev = curr
    return prev[-1]


def suggest_for_typo(
    typed: object,
    registry: VerbRegistry,
    *,
    max_distance: int = 2,
    max_results: int = 3,
) -> Tuple[str, ...]:
    """Suggest verb candidates for a probable typo. NEVER raises.

    Returns a tuple of slash forms ordered by edit distance. Used by
    the unknown-verb error path so operators see ``did you mean
    /cancel?`` when they type ``/cancl``. The distance cap is
    operator-tunable via :data:`max_distance` (default 2 — catches
    one transposition or one missing char).
    """
    try:
        text = str(typed or "").strip()
    except Exception:  # noqa: BLE001
        return ()
    if not text or not text.startswith("/"):
        return ()
    scored: List[Tuple[int, str]] = []
    for v in registry.verbs:
        for candidate in (v.slash_form,) + tuple(v.aliases):
            dist = _levenshtein(text, candidate, cap=max_distance + 1)
            if dist <= max_distance:
                scored.append((dist, candidate))
    scored.sort(key=lambda x: (x[0], x[1]))
    # De-dup while preserving order (a verb's alias may tie with its
    # primary form — keep the primary first).
    seen: set = set()
    out: List[str] = []
    for _, name in scored:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= max(1, int(max_results)):
            break
    return tuple(out)


def fuzzy_match(
    prefix: object,
    registry: VerbRegistry,
    *,
    max_results: int = 8,
) -> Tuple[VerbDescriptor, ...]:
    """Fuzzy-match verbs for the completion palette. NEVER raises.

    Routing:

    1. Prefix-match always wins — the existing palette behavior
       (line-by-line ``startswith``) is preserved when the operator
       is typing a known prefix.
    2. When prefix-match yields 0 results AND ``prefix`` is at least
       2 chars past the leading slash, fall back to a substring +
       edit-distance composite score so ``/risc`` still surfaces
       ``/risk`` and ``/budgt`` surfaces ``/budget``.

    The completer composes this helper to extend behavior without
    changing the existing prefix-match path's byte-identical output.
    """
    try:
        text = str(prefix or "").strip()
    except Exception:  # noqa: BLE001
        return ()
    if not text or not text.startswith("/"):
        return ()
    # Phase 1 — prefix-match
    prefix_hits = tuple(
        v for v in registry.verbs
        if v.slash_form.startswith(text)
        or any(a.startswith(text) for a in v.aliases)
    )
    if prefix_hits:
        return prefix_hits[: max(1, int(max_results))]
    # Phase 2 — only invoke fuzzy when prefix-match has nothing AND
    # operator has typed enough that it isn't a "just started a /"
    # case (avoid surfacing nonsense when the user has only typed
    # ``/`` or ``/x``).
    if len(text) < 3:
        return ()
    scored: List[Tuple[int, VerbDescriptor]] = []
    body = text[1:]
    for v in registry.verbs:
        candidate_body = v.slash_form[1:]
        score = _levenshtein(body, candidate_body, cap=4)
        if body in candidate_body:
            # Substring hit beats pure edit-distance — bias toward 0.
            score = min(score, max(0, len(candidate_body) - len(body)))
        if score <= 4:
            scored.append((score, v))
    scored.sort(key=lambda x: (x[0], x[1].slash_form))
    return tuple(v for _, v in scored[: max(1, int(max_results))])


def resolve_help_for_buffer(
    buffer_text: object,
    registry: VerbRegistry,
    *,
    fuzzy_max_distance: int = 2,
) -> Optional[str]:
    """Resolve the help block to surface for an inline ``?``
    press at the current buffer state. NEVER raises.

    Returns the formatted help text or ``None`` when no help is
    appropriate (in which case the caller should insert a
    literal ``?``).

    Decision sequence — every input maps deterministically to
    exactly one outcome:

    1. Master gate via :func:`is_inline_help_enabled` — when off,
       returns ``None`` so the keybinding inserts a literal ``?``.
    2. Non-string / empty / non-slash buffer → ``None`` (operator
       isn't typing a verb).
    3. Buffer is exactly ``"/"`` or starts with ``"/ "`` → ``None``
       (no verb word yet).
    4. Extract first whitespace-delimited token. Exact registry
       match (primary OR alias via :meth:`VerbRegistry.find`) →
       render via :func:`format_verb_help`.
    5. Fuzzy fallback — only when the operator has typed enough
       characters to be unambiguous (verb word length ≥ 3) AND
       :func:`fuzzy_match` returns exactly one confident result
       within ``fuzzy_max_distance``. Two or more matches → return
       ``None`` so the operator isn't biased toward an arbitrary
       choice."""
    if not is_inline_help_enabled():
        return None
    try:
        text = str(buffer_text or "")
    except Exception:  # noqa: BLE001
        return None
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None
    # Extract the first whitespace-delimited token.
    parts = stripped.split(None, 1)
    if not parts:
        return None
    verb_word = parts[0]
    if verb_word == "/" or len(verb_word) < 2:
        return None
    # Exact primary/alias match wins — no ambiguity. Buggy
    # registries that raise from find() degrade to None per the
    # NEVER-raises contract.
    try:
        exact = registry.find(verb_word)
    except Exception:  # noqa: BLE001
        return None
    if exact is not None:
        try:
            return format_verb_help(exact)
        except Exception:  # noqa: BLE001
            return None
    # Fuzzy fallback — operator may still be typing the verb.
    # Require enough characters to be confident before surfacing
    # an unrequested verb's help.
    if len(verb_word) < 3:
        return None
    try:
        candidates = fuzzy_match(
            verb_word, registry, max_results=2,
        )
    except Exception:  # noqa: BLE001
        return None
    if len(candidates) != 1:
        # Zero matches → no help. Two+ → ambiguous; surfacing the
        # top one biases the operator. Either way, decline.
        return None
    # Confidence check: top match must be within edit distance.
    top = candidates[0]
    try:
        dist = _levenshtein(
            verb_word[1:], top.slash_form[1:],
            cap=max(1, int(fuzzy_max_distance)) + 1,
        )
    except Exception:  # noqa: BLE001
        return None
    if dist > fuzzy_max_distance:
        return None
    try:
        return format_verb_help(top)
    except Exception:  # noqa: BLE001
        return None


def format_verb_hint(
    verb: VerbDescriptor,
    *,
    max_examples: int = 1,
    indent: str = "  ",
) -> str:
    """Render a **compact** one-block hint suitable for inline
    injection into error / suggestion messages.

    Closes §41.3 Slice 2 #19 by composing the existing
    :class:`VerbDescriptor` fields (no new registry, no
    hardcoded verb→hint map). Distinct from :func:`format_verb_help`
    which renders the full help block (usage + description +
    aliases + all examples) for ``/verb --help`` and the
    inline ``?`` tooltip.

    Output shape::

        usage: /cancel <op_id> [--immediate]
          example: /cancel op-abc123

    When the verb has no ``arg_spec`` it still surfaces the
    primary slash form so the operator at least sees what to
    type. NEVER raises — returns the verb's slash_form as a
    last-resort fallback when anything goes wrong.
    """
    try:
        if not isinstance(verb, VerbDescriptor):
            return ""
        slash = verb.slash_form
        usage = slash if not verb.arg_spec else f"{slash} {verb.arg_spec}"
        lines: List[str] = [f"{indent}usage: {usage}"]
        cap = max(0, int(max_examples))
        if cap > 0 and verb.examples:
            for ex in verb.examples[:cap]:
                lines.append(f"{indent}  example: {ex}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        try:
            return f"{indent}usage: {verb.slash_form}"  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return ""


def format_verb_help(verb: VerbDescriptor) -> str:
    """Render a ``/verb --help`` block. NEVER raises.

    Output shape::

        /cancel <op_id> [--immediate]
          Cancel a pending op via cooperative cancellation.

          aliases: /stop
          examples:
            /cancel op-abc123
            /cancel op-abc123 --immediate
    """
    try:
        usage = verb.slash_form
        if verb.arg_spec:
            usage = f"{usage} {verb.arg_spec}"
        lines = [usage]
        if verb.description:
            lines.append(f"  {verb.description}")
        if verb.aliases:
            lines.append("")
            lines.append(f"  aliases: {', '.join(verb.aliases)}")
        if verb.examples:
            lines.append("")
            lines.append("  examples:")
            for ex in verb.examples:
                lines.append(f"    {ex}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return verb.slash_form if verb else ""


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
            tags = _parse_doc_tags(method)
            arg_spec = tags["arg_spec"] or _infer_arg_spec_from_signature(method)
            discovered.append(
                VerbDescriptor(
                    slash_form=slash,
                    handler_method=name,
                    description=_first_doc_line(method),
                    aliases=tuple(tags["aliases"]),
                    examples=tuple(tags["examples"]),
                    arg_spec=arg_spec,
                    category=tags["category"],
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


def build_completer(
    registry: VerbRegistry, *, max_results: int = 8,
    include_mentions: bool = True,
) -> Optional[object]:
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
            # §41.3 Slice 3 #14 — when the operator has typed past
            # the verb name (text contains a space), dispatch to
            # per-position arg completion instead of verb-name
            # matching. The verb itself is parsed deterministically
            # from token 0 via ``registry.find``; the position
            # index + prefix come from ``cursor_arg_position``.
            try:
                pos_index, pos_prefix = cursor_arg_position(text)
            except Exception:  # noqa: BLE001
                pos_index, pos_prefix = -1, ""
            if pos_index >= 0:
                try:
                    verb_token = text.lstrip().split(" ", 1)[0]
                    verb = registry.find(verb_token)
                except Exception:  # noqa: BLE001
                    verb = None
                if verb is None or not verb.arg_spec:
                    return
                try:
                    positions = parse_arg_spec(verb.arg_spec)
                except Exception:  # noqa: BLE001
                    positions = ()
                if pos_index >= len(positions):
                    return
                position = positions[pos_index]
                # PATH is delegated to the existing @-mention path
                # completer (already merged in below). FREE +
                # over-position positions yield nothing — operator
                # is past the spec.
                if position.kind in (ArgKind.PATH, ArgKind.FREE):
                    return
                try:
                    candidates = get_arg_candidates(
                        position, pos_prefix,
                    )
                except Exception:  # noqa: BLE001
                    candidates = ()
                for cand in candidates:
                    yield Completion(
                        text=cand,
                        start_position=-len(pos_prefix),
                        display=cand,
                        display_meta=(
                            f"{position.name}"
                            f"{' (required)' if position.required else ''}"
                        ),
                    )
                return
            # ``text`` includes the leading slash. Route through
            # fuzzy_match — it returns prefix hits first, falls back
            # to substring + edit-distance only when prefix yields
            # nothing AND the operator has typed enough to make
            # fuzzy meaningful. Byte-identical to legacy prefix-match
            # for the common case (operator typing a known verb).
            try:
                matches = fuzzy_match(
                    text, registry, max_results=max_results,
                )
            except Exception:  # noqa: BLE001 — defensive
                matches = ()
            for verb in matches:
                yield Completion(
                    text=verb.slash_form,
                    start_position=-len(text),
                    display=verb.slash_form,
                    display_meta=verb.description,
                )

    slash_completer = _SlashCompleter()

    # §37 Slice 7 (2026-05-05) — merge in the @-mention path
    # completer. Each completer self-gates (slash on `/` prefix,
    # mention on `@` word-boundary), so they never collide. When
    # mention completer is unavailable (polish off / prompt_toolkit
    # missing), fall through to slash-only.
    #
    # ``include_mentions=False`` exists for callers that bring their
    # OWN mention completer (build_attach_completer merges the
    # repo-rooted MentionPathCompleter) — without it the attach
    # surface ran BOTH mention completers and every `@` prefix
    # yielded duplicate candidates.
    # `:emoji:` shortcodes ride every surface's chain — self-gating on
    # the `:name` fragment, so verbs, mentions and emoji never collide.
    extra_completers = []
    try:
        from backend.core.ouroboros.battle_test.emoji_shortcodes import (
            EmojiShortcodeCompleter,
            is_emoji_shortcodes_enabled,
        )
        if is_emoji_shortcodes_enabled():
            extra_completers.append(EmojiShortcodeCompleter())
    except Exception:  # noqa: BLE001 — emoji are decoration, typing is not
        pass

    if include_mentions:
        try:
            from backend.core.ouroboros.battle_test.repl_input_polish import (
                build_mention_completer,
            )
            mention_completer = build_mention_completer()
            if mention_completer is not None:
                extra_completers.append(mention_completer)
        except Exception:  # noqa: BLE001 — defensive
            pass
    if not extra_completers:
        return slash_completer
    try:
        from prompt_toolkit.completion import merge_completers
    except ImportError:
        return slash_completer
    return merge_completers([slash_completer, *extra_completers])


# ===========================================================================
# prompt_toolkit History wrapper — FileHistory + graceful fallback
# ===========================================================================


HISTORY_MAX_ENTRIES_ENV_VAR: str = "JARVIS_REPL_HISTORY_MAX_ENTRIES"

#: One history object per resolved file path. Several surfaces live in ONE
#: process (the client's cockpit + fallback + ghost-text + Ctrl+R search all
#: build history); handing each its own ``FileHistory`` on the same path
#: means multiple write-behind caches racing on one file and Up-arrow state
#: that disagrees between widgets. Keyed by path so tests with tmp files
#: stay isolated.
_HISTORY_SINGLETONS: "dict[str, object]" = {}


def _history_max_entries() -> int:
    """Bound on stored entries — deep enough that yesterday's goal is
    reachable, bounded so the file cannot grow without limit across a
    long-lived install. Env: ``JARVIS_REPL_HISTORY_MAX_ENTRIES``."""
    try:
        return max(50, int(
            os.environ.get(HISTORY_MAX_ENTRIES_ENV_VAR, "2000")
        ))
    except (TypeError, ValueError):
        return 2000


def _trim_history_file(path: Path, max_entries: int) -> None:
    """Keep the newest *max_entries*. Rewritten atomically (tmp +
    ``os.replace``) so a crash mid-trim cannot leave a truncated or empty
    history. Entry-aware: ``FileHistory`` writes a ``# timestamp`` comment
    line per entry followed by ``+``-prefixed content, so ENTRIES are
    counted by their comment lines, never by raw lines. NEVER raises —
    a failed trim must not cost the operator their history."""
    try:
        if not path.exists():
            return
        lines = path.read_text(errors="replace").splitlines(keepends=True)
        starts = [i for i, ln in enumerate(lines) if ln.startswith("#")]
        if len(starts) <= max_entries:
            return
        cut = starts[len(starts) - max_entries]
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(lines[cut:]))
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        logger.debug("[ReplCompletion] history trim degraded", exc_info=True)


def _make_file_history_class():
    """The deduplicating ``FileHistory`` subclass, created lazily so this
    module stays importable without prompt_toolkit."""
    from prompt_toolkit.history import FileHistory

    class _DedupedFileHistory(FileHistory):
        """Refuses blanks and immediate repeats.

        A history full of one repeated command is one the operator stops
        pressing Up on. Nothing else is filtered: a heuristic that
        silently dropped some commands would leave the operator unable
        to trust that Up shows what they actually did."""

        def append_string(self, string: str) -> None:
            try:
                candidate = str(string or "")
                if not candidate.strip():
                    return
                try:
                    strings = self.get_strings()
                    last = strings[-1] if strings else None
                except Exception:  # noqa: BLE001
                    last = None
                if candidate == last:
                    return
                super().append_string(candidate)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[ReplCompletion] history append degraded",
                    exc_info=True,
                )

    return _DedupedFileHistory


def reset_history_cache_for_tests() -> None:
    """Drop the history singletons. Test hook only."""
    _HISTORY_SINGLETONS.clear()


def build_history(
    path: Optional[Path] = None,
) -> Optional[object]:
    """THE ``prompt_toolkit.history.History`` for a surface.

    Defaults to a deduplicating ``FileHistory`` at
    ``.jarvis/repl_history`` — ONE instance per path per process (see
    ``_HISTORY_SINGLETONS``), created with owner-only permissions (the
    file is the operator's typing) and trimmed atomically to
    ``JARVIS_REPL_HISTORY_MAX_ENTRIES`` on first open. Falls back to
    ``InMemoryHistory`` when the file isn't writable. Returns ``None``
    when prompt_toolkit isn't available OR history is disabled via
    :func:`is_history_enabled`.
    """
    if not is_history_enabled():
        return None
    try:
        from prompt_toolkit.history import InMemoryHistory
    except ImportError:
        return None

    eff_path = path if path is not None else resolve_history_path()
    if eff_path is None:
        try:
            return InMemoryHistory()
        except Exception:  # noqa: BLE001
            return None

    key = str(eff_path)
    cached = _HISTORY_SINGLETONS.get(key)
    if cached is not None:
        return cached

    try:
        _trim_history_file(eff_path, _history_max_entries())
        # Owner-only, whether the file is new or inherited from an
        # earlier install that created it with the default umask.
        try:
            if not eff_path.exists():
                eff_path.touch(mode=0o600)
            else:
                os.chmod(eff_path, 0o600)
        except OSError:
            pass
        # FileHistory writes atomically per command — no buffering
        # contention with concurrent SIGINT.
        history = _make_file_history_class()(key)
        _HISTORY_SINGLETONS[key] = history
        return history
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ReplCompletion] FileHistory(%r) failed; using in-memory",
            key, exc_info=True,
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
    auto_suggest: Optional[object] = None
    complete_while_typing: bool = False
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
    # ONE registry for every surface (gap #3 reconciliation): the daemon's
    # discovered ``_handle_*`` verbs merged with the dispatch table it also
    # routes to. Kill-switch inside unified_registry restores the split.
    registry = unified_registry(repl_instance)
    completer: Optional[object] = None
    history: Optional[object] = None
    auto_suggest: Optional[object] = None
    enable_search = False

    if is_completion_enabled():
        try:
            # A browsable palette over the unified table — the historic
            # default of 8 was a typo-suggestion cap, not a menu size.
            completer = build_completer(registry, max_results=512)
            if completer is not None:
                # Off-thread for the same reason as the attach surface:
                # first-completion work (registry walks, fs walks) must
                # land between keystrokes, not on one.
                try:
                    from prompt_toolkit.completion import ThreadedCompleter
                    completer = ThreadedCompleter(completer)
                except ImportError:
                    pass
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

        try:
            auto_suggest = build_auto_suggest()
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ReplCompletion] build_auto_suggest failed", exc_info=True,
            )

    return CompletionWiring(
        completer=completer,
        history=history,
        enable_history_search=enable_search,
        registry=registry,
        auto_suggest=auto_suggest,
        complete_while_typing=(
            completer is not None and is_complete_while_typing_enabled()
        ),
    )


__all__ = [
    "ArgKind",
    "ArgPositionSpec",
    "AUTOSUGGEST_ENABLED_ENV_VAR",
    "COMPLETE_WHILE_TYPING_ENV_VAR",
    "CompletionWiring",
    "HISTORY_ENABLED_ENV_VAR",
    "HISTORY_MAX_ENTRIES_ENV_VAR",
    "HISTORY_PATH_ENV_VAR",
    "INLINE_HELP_ENABLED_ENV_VAR",
    "MASTER_FLAG_ENV_VAR",
    "REPL_COMPLETION_SCHEMA_VERSION",
    "UNIFIED_REGISTRY_ENV_VAR",
    "VerbCategory",
    "VerbDescriptor",
    "VerbRegistry",
    "build_auto_suggest",
    "build_completer",
    "build_completion_wiring",
    "build_history",
    "cursor_arg_position",
    "discover_verbs",
    "format_verb_help",
    "format_verb_hint",
    "fuzzy_match",
    "get_arg_candidates",
    "is_autosuggest_enabled",
    "is_complete_while_typing_enabled",
    "is_completion_enabled",
    "is_history_enabled",
    "is_inline_help_enabled",
    "is_unified_registry_enabled",
    "list_arg_providers",
    "parse_arg_spec",
    "unified_registry",
    "register_arg_provider",
    "reset_history_cache_for_tests",
    "resolve_help_for_buffer",
    "resolve_history_path",
    "suggest_for_typo",
    "unregister_arg_provider",
]


#: Sentences a docstring's first line uses to address MAINTAINERS, not
#: operators. Dropped whole — "NEVER raises" is a contract with the caller and
#: means nothing in a menu; "Parse ``/x`` line and dispatch" describes the
#: plumbing rather than the effect. Patterns, not a per-verb list, so a verb
#: added tomorrow is cleaned by the same rules.
_MAINTAINER_NOISE = (
    r"NEVER\s+raises?\.?",
    r"Never\s+raises?\.?",
    r"Parse\s+(a\s+)?``?/?[\w.\- ]+``?\s+line(\s*\+?\s*and\s+dispatch)?\.?",
    r"Parse\s+(a\s+)?``?/?[\w.\- ]+``?\s+line\s*\+\s*dispatch\.?",
    r"``?matched=(True|False)``?\.?",
    r"auto-discovered\.?",
    r"canonical\s+entry\s+point\.?",
    r"operator\s+surface\.?",
)

#: Internal provenance markers — section numbers, slice ids, path codes. They
#: are load-bearing in the source and pure noise in a palette: an operator
#: cannot act on "§38.11-F".
_PROVENANCE = (
    r"§\s*[\d.]+[A-Za-z\-]*",
    r"\bSlice\s+\d+\w*\b",
    r"\bPath\s+[A-Z]\.\d+\b",
    r"\bPhase\s+\d+\w*\b",
    r"\bU\d+\s+empirical\s+wiring\b",
    r"\bGap\s+#\d+\b",
    r"\(\s*\d{4}-\d{2}-\d{2}\s*\)",
)


def _humanise(line: str) -> str:
    """One docstring line -> operator prose, or "" if nothing survives.

    Docstrings are written for whoever maintains the function. Their first
    line is usually a contract ("NEVER raises") or a restatement of the
    plumbing ("Parse ``/canvas`` line and dispatch") — both true, both useless
    in a menu that is supposed to answer "what does this do for me?".

    Rules, not exceptions: strip RST markup, drop maintainer boilerplate and
    provenance markers, then judge what remains. Returning "" is a real answer
    and lets the caller fall through to the module docstring, which for these
    single-purpose ``*_repl.py`` files is usually the sentence we wanted."""
    text = str(line or "").strip()
    if not text:
        return ""
    for pattern in _MAINTAINER_NOISE + _PROVENANCE:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = text.replace("``", "").replace("`", "")
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" .,;:—-\u2014")
    # A fragment that is only punctuation, a bare verb echo, or two words of
    # residue is worse than falling through — it looks like a description and
    # carries none.
    if len(text) < 12:
        return ""
    low = text.lower().lstrip("/")
    # Reject a line that only ECHOES the verb. "/anticipate REPL" and
    # "/causal REPL dispatcher" are what survives stripping a docstring that
    # never described anything, and a menu entry reading "<verb> REPL" beside
    # the verb is worse than a blank: it looks like help and answers nothing.
    if re.fullmatch(r"[\w\-]+(\s+(repl|verb|dispatcher|surface|command))+", low):
        return ""
    # Reject sentence fragments — residue from a stripped clause, not prose.
    if low.split()[0] in ("and", "or", "but", "with", "then", "returns",
                          "short-circuit", "lets"):
        return ""
    if not re.search(r"[a-z]{3,}\s+[a-z]{3,}", low):
        return ""
    words = low.split()
    # A clause that was cut mid-thought. "Canonical entry point — by" is what a
    # stripped provenance marker leaves behind, and it reads as a description
    # right up until you try to use it.
    if len(words) < 4 or words[-1] in (
        "by", "for", "with", "and", "or", "the", "a", "an", "to", "of", "in",
        "on", "from", "when", "that", "is", "are",
    ):
        return ""
    return text[0].upper() + text[1:]


def _help_bound(text: str) -> str:
    """A SANITY bound on resolved help — not a layout decision.

    `[:88]` used to sit at every return of `_describe`, which put line-breaking
    in the resolver: descriptions arrived pre-cut, so the palette's wrapping
    could never fire and every entry was one truncated line regardless of how
    much room the terminal had.

    Where a description breaks depends on the terminal width and the name
    column, neither of which this layer knows. So it stops deciding, and
    `layout_palette` — which does know — wraps with a hanging indent.

    A bound is still needed: a docstring line can be kilobytes, and the
    palette would render it as a wall. This one is generous enough that real
    prose survives to be wrapped, and tight enough that pathological input
    cannot take the screen. Env: ``JARVIS_VERB_HELP_MAX_CHARS``.
    """
    try:
        limit = max(40, int(os.environ.get("JARVIS_VERB_HELP_MAX_CHARS", "220")))
    except (TypeError, ValueError):
        limit = 220
    text = " ".join(str(text).split())      # collapse newlines: the layout owns them
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _describe(fn: object) -> str:
    """One line of operator-facing help for a dispatcher. NEVER raises.

    Cascade, because no single source is reliably operator-facing:

      1. the function's docstring, HUMANISED — nearest the behaviour, so it is
         the line most likely to be corrected when behaviour changes;
      2. its MODULE's docstring, humanised — these verbs live in
         single-purpose ``*_repl.py`` files whose header describes the feature
         rather than the function;
      3. an honest placeholder. Never a blank, and never maintainer jargon
         dressed up as help."""
    def _first_line(doc: object) -> str:
        text = doc if isinstance(doc, str) else ""
        text = text.strip()
        if not text:
            return ""
        # Prefer the first line, but a docstring that opens with a bare
        # ``/verb`` heading keeps its meaning on the NEXT line.
        for raw in text.splitlines()[:3]:
            human = _humanise(raw)
            if human:
                return human
        return ""

    try:
        import sys as _sys
        mod = _sys.modules.get(getattr(fn, "__module__", "") or "")

        # 1. AUTHORED help. The only source written FOR an operator.
        #
        # A module opts in with a dict beside its ``__aliases__``:
        #
        #     __verb_help__ = {"moltbook": "read the agora feed"}
        #
        # Same convention as __aliases__, so there is one place a module
        # declares palette metadata. This exists because the humanisation
        # below cannot invent what nobody wrote: these docstrings are
        # contracts for maintainers, and stripping them yields "<verb> REPL".
        name = str(getattr(fn, "__name__", ""))
        verb = name[len("dispatch_"):-len("_command")] if (
            name.startswith("dispatch_") and name.endswith("_command")
        ) else name

        authored = getattr(mod, "__verb_help__", None)
        if isinstance(authored, dict):
            text = authored.get(verb) or authored.get(f"/{verb}")
            if isinstance(text, str) and text.strip():
                return _help_bound(text)

        # 1b. An ``Operator:`` section in the function's OWN docstring.
        #
        #     Ranked with authored help rather than with the humanised
        #     docstring below, because it is the same KIND of thing: a
        #     sentence someone wrote for the person typing the verb. It sits
        #     beside the implementation instead of in a dict at the top of
        #     the module, which is the only reason to prefer one over the
        #     other, and 45 verbs were never going to get dict entries.
        operator_line = extract_operator_section(getattr(fn, "__doc__", None))
        if operator_line:
            return _help_bound(operator_line)

        # 2. The FUNCTION docstring, humanised — accepted only if it survives
        #    as prose.
        #
        #    The module docstring is deliberately NOT a fallback. It is a file
        #    header: it narrates the feature's history and provenance ("Wave 1
        #    #8", "§38.11-F operator surface"), which is the wrong GENRE, not
        #    merely the wrong wording. Mining it produced entries like
        #    "/autobiography REPL — Wave 1 #8" — confident, well-formed, and
        #    useless. A blank is the honest state for "nobody has written this
        #    yet"; plausible noise is not.
        own = _first_line(getattr(fn, "__doc__", ""))
        if own:
            return _help_bound(own)

        # 3. No prose anywhere — mine the verb's own SUBCOMMAND VOCABULARY.
        #
        #    Ranked above signature derivation because it is strictly more
        #    informative here: every dispatcher takes the whole line as one
        #    string, so the signature says "(line)" while the body knows the
        #    verb accepts status | history | explain. That is the single most
        #    useful thing a palette can tell someone about a verb they have
        #    not used, and it was sitting unread in the source.
        mined = mine_subcommands(fn)
        if mined:
            return _help_bound(" | ".join(mined))

        # 4. No prose anywhere. The SIGNATURE still knows what the verb
        #    takes, and "what arguments does this want" is most of what an
        #    operator wants from a palette entry anyway. Translated to POSIX
        #    brackets and stripped of injected plumbing — a raw Python
        #    signature would be worse than the blank it replaces.
        derived = derive_usage(fn, verb)
        if derived:
            return _help_bound(derived)
    except Exception:  # noqa: BLE001
        pass
    # 5. Nothing authored, nothing extractable, nothing to derive. Say so
    #    plainly rather than printing residue: fabricated prose reads as a
    #    description that happens to be wrong, and the operator acts on it.
    #
    #    UNDOCUMENTED rather than "" so the gap is VISIBLE and countable —
    #    ``/help --undocumented`` can list exactly what still needs writing,
    #    which a blank column cannot.
    return UNDOCUMENTED


@contextlib.contextmanager
def _root_logging_preserved():
    """Hold the root logger harmless across an arbitrary-module import walk.

    Priming the verb registry imports every module in the dispatch packages
    purely to read function names off them. Import is not inert: a module that
    calls ``logging.basicConfig`` at import time installs a handler on the ROOT
    logger, and from then on every unrelated log record in the cockpit carries
    that module's format. ``isolated_agent_worker`` did exactly this, and the
    operator's terminal filled with ``[worker]``-stamped lines emitted by
    subsystems containing no worker.

    That module is fixed at the source, but the exposure is structural: any
    future module acquires the same power simply by being importable. Since
    introspection has no business mutating the host's logging, the walk is
    made non-destructive by construction — handlers and level are snapshotted
    and restored, so a stray ``basicConfig`` affects nothing.

    Deliberately narrow: only root handlers and level are restored. Named
    loggers a module configures for ITSELF are its own business."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        try:
            if root.handlers != saved_handlers:
                root.handlers[:] = saved_handlers
            root.setLevel(saved_level)
        except Exception:  # noqa: BLE001 — never break completion over logging
            pass


def _pt_completer_base() -> Any:
    """prompt_toolkit's `Completer`, or `object` when it is unavailable.

    Inheriting is load-bearing, not tidy: prompt_toolkit consumes a completer
    through `get_completions_async`, which the base class supplies by wrapping
    the sync method. A duck-typed completer satisfies every static reading of
    the protocol and raises `AttributeError` on the first keystroke.
    """
    try:
        from prompt_toolkit.completion import Completer
        return Completer
    except Exception:  # noqa: BLE001
        return object


class MentionPathCompleter(_pt_completer_base()):  # type: ignore[misc]
    """Completes `@path` mentions against the repo.

    A mention the operator cannot TYPE is a feature only someone who already
    knows the tree can use. This makes `@bac<tab>` reach
    `@backend/core/...`, which is the difference between a parser and a
    surface.

    Deliberately NOT prompt_toolkit's `PathCompleter`: that completes from
    the process CWD and would happily offer `/etc/passwd`. Mentions name
    files in the REPO the organism works on, so the walk is rooted there and
    cannot climb out — a completer that offers what the daemon will then
    refuse to read teaches the operator nothing.
    """

    def __init__(self, root: Optional[str] = None, max_results: int = 12) -> None:
        self._root = root
        self._max = max(1, int(max_results))

    def _repo_root(self):
        import pathlib as _p
        if self._root:
            return _p.Path(self._root)
        return _p.Path(os.environ.get("JARVIS_REPO_PATH", ".")).resolve()

    def get_completions(self, document: Any, _event: Any = None):
        from prompt_toolkit.completion import Completion
        try:
            text = document.text_before_cursor or ""
            at = text.rfind("@")
            if at < 0:
                return
            # Only the token being typed — an `@` earlier in a finished
            # sentence must not re-open the menu on every later keystroke.
            fragment = text[at + 1:]
            if " " in fragment or "\n" in fragment:
                return
            root = self._repo_root()
            if not root.is_dir():
                return
            for path in self._walk(root, fragment):
                yield Completion(path, start_position=-len(fragment))
        except Exception:  # noqa: BLE001 — completion must never break typing
            return

    def _walk(self, root: Any, fragment: str) -> List[str]:
        """Repo-relative paths matching *fragment*. Bounded and prefix-first.

        Bounded because a mention completer that walks a large repo on every
        keystroke stalls the very keystroke that opened it — the failure the
        threaded completer exists to avoid, reintroduced one layer down.
        """
        import itertools
        frag = fragment.lower()
        hits: List[str] = []
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv",
                ".mypy_cache", ".pytest_cache", "dist", "build"}
        try:
            walker = os.walk(str(root))
            for dirpath, dirnames, filenames in itertools.islice(walker, 4000):
                dirnames[:] = [d for d in dirnames
                               if d not in skip and not d.startswith(".")]
                rel_dir = os.path.relpath(dirpath, str(root))
                for name in filenames:
                    if name.startswith("."):
                        continue
                    rel = name if rel_dir == "." else os.path.join(rel_dir, name)
                    low = rel.lower()
                    if not frag or low.startswith(frag) or frag in low:
                        hits.append(rel)
                        if len(hits) >= self._max * 4:
                            break
                if len(hits) >= self._max * 4:
                    break
        except Exception:  # noqa: BLE001
            return hits[: self._max]
        # Prefix matches first — what the operator is typing beats what merely
        # contains it — then shortest, since a shallower path is likelier the
        # one meant.
        hits.sort(key=lambda r: (0 if r.lower().startswith(frag) else 1, len(r)))
        return hits[: self._max]


def _dispatch_arg_spec(verb: str, fn: object) -> str:
    """Arg spec for a dispatch verb — the piece that makes the ~330-line
    arg-completion machinery LIVE on the attach surface, where every
    dispatcher's Python signature is the uninformative ``(line)``.

    Sources, most-authored first (same ranking philosophy as _describe):

      1. a module-level ``__verb_args__ = {"cancel": "<op_id>"}`` dict —
         the same one-place-per-module convention as ``__verb_help__`` /
         ``__aliases__``, for verbs whose arguments aren't literals in
         the function body;
      2. the verb's MINED subcommand vocabulary rendered as a choice
         atom (``[status|history|explain]``) — read from the code, so a
         dispatcher that grows a subcommand completes it with no other
         edit.

    NEVER raises; "" means "no arg completion", which is where every
    dispatch verb was before this seam."""
    try:
        import sys as _sys
        mod = _sys.modules.get(getattr(fn, "__module__", "") or "")
        authored = getattr(mod, "__verb_args__", None)
        if isinstance(authored, dict):
            spec = authored.get(verb) or authored.get(f"/{verb}")
            if isinstance(spec, str) and spec.strip():
                return spec.strip()
        subs = mine_subcommands(fn)
        if subs:
            return "[" + "|".join(subs) + "]"
    except Exception:  # noqa: BLE001
        pass
    return ""


def unified_registry(repl_instance: object = None) -> VerbRegistry:
    """THE verb registry — the reconciliation of the two historic sources
    that drifted for four palette PRs:

      * ``registry_from_dispatch()`` — the 60-verb dispatch table the
        daemon actually routes to, plus the client-handled verbs;
      * ``discover_verbs(repl_instance)`` — the daemon's ``_handle_*``
        walk, which carries the metadata the dispatch table lacks
        (doc-tag arg specs, aliases, examples, categories).

    Merge policy per colliding slash form: the dispatch descriptor's
    HUMANISED description wins unless it is UNDOCUMENTED and the
    discovered one has prose; every other field prefers whichever side
    actually has data. Kill-switch ``JARVIS_UNIFIED_VERB_REGISTRY_ENABLED=
    false`` restores the legacy per-surface split. NEVER raises."""
    if not is_unified_registry_enabled():
        return discover_verbs(repl_instance)
    try:
        merged: dict = {
            d.slash_form: d for d in registry_from_dispatch().verbs
        }
        for d in discover_verbs(repl_instance).verbs:
            base = merged.get(d.slash_form)
            if base is None:
                merged[d.slash_form] = d
                continue
            description = base.description
            if (not description or description == UNDOCUMENTED) and (
                d.description
            ):
                description = d.description
            merged[d.slash_form] = VerbDescriptor(
                slash_form=base.slash_form,
                handler_method=d.handler_method or base.handler_method,
                description=description,
                aliases=tuple(dict.fromkeys(base.aliases + d.aliases)),
                examples=tuple(dict.fromkeys(base.examples + d.examples)),
                arg_spec=base.arg_spec or d.arg_spec,
                category=d.category if d.handler_method else base.category,
            )
        return VerbRegistry(
            verbs=tuple(sorted(merged.values(), key=lambda v: v.slash_form))
        )
    except Exception:  # noqa: BLE001
        logger.debug("[Completion] unified registry degraded", exc_info=True)
        return discover_verbs(repl_instance)


def registry_from_dispatch() -> VerbRegistry:
    """Build a :class:`VerbRegistry` from the AUTO-DISCOVERED dispatch table.

    ``discover_verbs`` walks a live ``SerpentREPL`` for ``_handle_*`` methods,
    which the attach cockpit does not have — it is a thin client with no REPL
    instance. Its verbs come from ``repl_dispatch_registry``, the same 60
    ``dispatch_<verb>_command`` callables the daemon actually routes to.

    The DOCSTRING is the single source of truth for the dropdown text: it sits
    beside the implementation, so a verb whose behaviour changes and whose
    docstring does not is a review problem rather than a silently stale menu
    entry in some parallel table.

    NEVER raises — an empty registry yields a completer that simply offers
    nothing, which is what a cockpit attached to an unreachable daemon should
    do anyway."""
    descriptors: list = []
    try:
        from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
            _VERB_TO_DISPATCHER,
            prime_registry,
        )
        try:
            with _root_logging_preserved():
                prime_registry()
        except Exception:  # noqa: BLE001 — a partial table still completes
            pass
        for verb, fn in sorted(_VERB_TO_DISPATCHER.items()):
            descriptors.append(
                VerbDescriptor(
                    slash_form=f"/{verb}",
                    handler_method="",
                    description=_describe(fn),
                    arg_spec=_dispatch_arg_spec(verb, fn),
                )
            )
    except Exception:  # noqa: BLE001
        logger.debug("[Completion] dispatch registry unavailable", exc_info=True)

    # Client-handled verbs belong in the SAME palette. `/deck`, `wake` and
    # `detach` never reach the daemon, so a dispatch-only registry both omits
    # them AND — because no verb starts with "/deck" — sends the operator into
    # the fuzzy fallback, which answered "/deck" with "/bus, /cost". A palette
    # that invents plausible wrong answers is worse than one that says nothing.
    try:
        from backend.core.ouroboros.cli.ov import client_verbs
        known = {d.slash_form for d in descriptors}
        for verb, help_text in sorted(client_verbs().items()):
            slash = f"/{verb}"
            if slash in known:
                continue
            descriptors.append(
                VerbDescriptor(
                    slash_form=slash, handler_method="",
                    description=help_text or "handled by this cockpit",
                )
            )
    except Exception:  # noqa: BLE001 — daemon verbs alone still complete
        logger.debug("[Completion] client verbs unavailable", exc_info=True)

    return VerbRegistry(verbs=tuple(descriptors))


def build_attach_completer() -> Optional[object]:
    """The cockpit's ``/`` palette, threaded so typing never blocks.

    ``ThreadedCompleter`` matters here specifically: priming the dispatch
    registry walks packages and the first call is not instant. On the event
    loop that would stall the keystroke that triggered it — the operator types
    ``/`` and the terminal freezes. Off-thread, prompt_toolkit renders the
    menu when results arrive and the buffer stays live throughout.

    NEVER raises."""
    try:
        # A browsable palette, not a "did you mean" hint. The default cap of
        # 8 exists for typo suggestion, and it silently truncated a 60-verb
        # menu to its first eight alphabetically — the operator saw
        # /anticipate..../bus and reasonably concluded the palette was broken.
        # prompt_toolkit's CompletionsMenu scrolls, so the ceiling only needs
        # to exceed the table.
        completer = build_completer(
            registry_from_dispatch(), max_results=512,
            # The repo-rooted MentionPathCompleter below is this
            # surface's `@` vocabulary — without this flag the polish
            # module's cwd-rooted PathCompleter ALSO merged in and every
            # mention completed twice.
            include_mentions=False,
        )
        if completer is None:
            return None
        # `/` opens verbs, `@` opens files. One surface, two vocabularies —
        # dispatched on the sigil the operator typed rather than on a mode
        # they have to enter.
        try:
            from prompt_toolkit.completion import merge_completers
            completer = merge_completers(
                [completer, MentionPathCompleter()],
            )
        except Exception:  # noqa: BLE001 — verbs alone still complete
            pass
        try:
            from prompt_toolkit.completion import ThreadedCompleter
            return ThreadedCompleter(completer)
        except ImportError:
            return completer
    except Exception:  # noqa: BLE001
        return None
