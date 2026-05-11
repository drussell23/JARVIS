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

import enum
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
INLINE_HELP_ENABLED_ENV_VAR: str = "JARVIS_REPL_INLINE_HELP_ENABLED"


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
            # ``text`` includes the leading slash. Route through
            # fuzzy_match — it returns prefix hits first, falls back
            # to substring + edit-distance only when prefix yields
            # nothing AND the operator has typed enough to make
            # fuzzy meaningful. Byte-identical to legacy prefix-match
            # for the common case (operator typing a known verb).
            try:
                matches = fuzzy_match(text, registry)
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
    try:
        from backend.core.ouroboros.battle_test.repl_input_polish import (
            build_mention_completer,
        )
        mention_completer = build_mention_completer()
    except Exception:  # noqa: BLE001 — defensive
        mention_completer = None
    if mention_completer is None:
        return slash_completer
    try:
        from prompt_toolkit.completion import merge_completers
    except ImportError:
        return slash_completer
    return merge_completers([slash_completer, mention_completer])


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
    "INLINE_HELP_ENABLED_ENV_VAR",
    "MASTER_FLAG_ENV_VAR",
    "REPL_COMPLETION_SCHEMA_VERSION",
    "VerbCategory",
    "VerbDescriptor",
    "VerbRegistry",
    "build_completer",
    "build_completion_wiring",
    "build_history",
    "discover_verbs",
    "format_verb_help",
    "fuzzy_match",
    "is_completion_enabled",
    "is_history_enabled",
    "is_inline_help_enabled",
    "resolve_help_for_buffer",
    "resolve_history_path",
    "suggest_for_typo",
]
