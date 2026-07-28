"""Operator-remappable keybindings for the O+V cockpit — one engine, every surface.

The cockpit grew its keys the way most TUIs do: each surface hardcoded its
own (`c-c`/`c-d` in the bipartite app, `c-o`/arrows/`escape` in the fallback
PromptSession, `escape enter` in input_continuation). Three surfaces, three
private key tables, no way for an operator to see them in one place — let
alone change one.

This module is the single seam all of that routes through:

* **Actions, not keys.** A surface declares ``namespace:action`` entries with
  DEFAULT keys as data (``("ctrl+c",)``); the effective keys are resolved at
  bind time against the operator's config. No call-site carries a literal
  prompt_toolkit key again.
* **Operator config** at ``.jarvis/keybindings.json`` (repo) or
  ``~/.jarvis/keybindings.json``, same shape as Claude Code's::

      {
        "$docs": "https://code.claude.com/docs/en/keybindings",
        "bindings": [
          {"context": "Chat", "bindings": {
              "ctrl+j": "chat:newline",     // rebind
              "ctrl+p": null                 // unbind a default
          }}
        ]
      }

* **Hot reload.** Mount-based consumers get a ``DynamicKeyBindings`` whose
  bindings rebuild when the file's mtime changes — edits apply without
  restarting the cockpit. Call-site consumers (``bind_action``) pick changes
  up on the next attach.
* **Validation, not silence.** Parse errors, unknown keys, reserved keys and
  terminal-multiplexer conflicts become WARNINGS surfaced via ``/keys`` —
  a broken config degrades to defaults, never to a dead cockpit.

Keystroke syntax mirrors Claude Code's docs: modifiers ``ctrl+``/``shift+``/
``alt+`` (aliases ``control``, ``opt``, ``option``, ``meta``), chords as
space-separated sequences (``ctrl+x ctrl+e``), a standalone uppercase letter
implies Shift, and special keys ``escape esc enter return tab space up down
left right backspace delete home end pageup pagedown insert f1..f24``.
``cmd``/``super``/``win`` are rejected with a warning — most terminals never
deliver the Super modifier, and a binding that works only sometimes is worse
than none.

Env:
  * ``JARVIS_KEYMAP_ENABLED``            master (default true; off = defaults only)
  * ``JARVIS_KEYBINDINGS_FILE``          explicit config path
  * ``JARVIS_KEYMAP_RELOAD_THROTTLE_S``  min seconds between mtime probes (default 1.0)

NEVER raises into an input path: every public function degrades to the
defaults it was given.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

KEYMAP_SCHEMA_VERSION: str = "keymap.1"

MASTER_FLAG_ENV_VAR: str = "JARVIS_KEYMAP_ENABLED"
CONFIG_PATH_ENV_VAR: str = "JARVIS_KEYBINDINGS_FILE"
RELOAD_THROTTLE_ENV_VAR: str = "JARVIS_KEYMAP_RELOAD_THROTTLE_S"

#: Contexts seeded up front so config validation has a vocabulary before any
#: surface mounts. Surfaces may add their own via :func:`register_context`.
_KNOWN_CONTEXTS: Dict[str, str] = {
    "Global": "applies on every cockpit surface",
    "Chat": "the operator prompt",
    "Autocomplete": "the / palette is open",
    "Deck": "deck selection / focus",
    "Confirmation": "an Iron Gate or approval prompt is on screen",
    "HistorySearch": "history search",
}

#: Keys that cannot be rebound, with the reason shown in the warning.
RESERVED_KEYSTROKES: Dict[str, str] = {
    "ctrl+m": "identical to Enter in terminals (both send CR)",
}

#: Rebindable, but the operator deserves a heads-up.
TERMINAL_CONFLICTS: Dict[str, str] = {
    "ctrl+b": "tmux prefix (tmux swallows it unless pressed twice)",
    "ctrl+a": "GNU screen prefix",
    "ctrl+z": "unix process suspend (SIGTSTP)",
}


def is_keymap_enabled() -> bool:
    """Master flag — default TRUE. Off means defaults only (the config file
    is neither read nor watched); the action seam itself stays live so no
    call-site has to fork."""
    return os.environ.get(MASTER_FLAG_ENV_VAR, "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


# ===========================================================================
# Keystroke parser — Claude Code syntax → prompt_toolkit key sequences
# ===========================================================================

_MOD_ALIASES: Dict[str, str] = {
    "ctrl": "ctrl", "control": "ctrl",
    "shift": "shift",
    "alt": "alt", "opt": "alt", "option": "alt", "meta": "alt",
    "cmd": "cmd", "command": "cmd", "super": "cmd", "win": "cmd",
}

#: name → prompt_toolkit key. ``space`` maps to the literal character —
#: prompt_toolkit treats any single printable char as a key of its own.
_SPECIAL_KEYS: Dict[str, str] = {
    "escape": "escape", "esc": "escape",
    "enter": "enter", "return": "enter",
    "tab": "tab",
    "space": " ",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "backspace": "backspace", "delete": "delete",
    "home": "home", "end": "end",
    "pageup": "pageup", "pagedown": "pagedown",
    "insert": "insert",
}
_SPECIAL_KEYS.update({f"f{i}": f"f{i}" for i in range(1, 25)})

#: Special keys that accept ``c-``/``s-`` prefixes in prompt_toolkit.
_PREFIXABLE = frozenset({
    "tab", "up", "down", "left", "right", "delete",
    "home", "end", "pageup", "pagedown", "insert",
})


class KeystrokeError(ValueError):
    """One keystroke string could not be translated. Collected as a
    warning by config parsing; only raised out of :func:`parse_keystroke`
    itself."""


def _pt_keys_valid(keys: Tuple[str, ...]) -> bool:
    """True when every step is a key prompt_toolkit will accept."""
    try:
        from prompt_toolkit.keys import ALL_KEYS, KEY_ALIASES
    except ImportError:
        return True  # headless — accept; binding never happens anyway
    for k in keys:
        if k in ALL_KEYS or k in KEY_ALIASES or len(k) == 1:
            continue
        return False
    return True


def _parse_single(token: str) -> Tuple[str, ...]:
    """One non-chord token (``ctrl+shift+left``) → pt key step(s).

    Alt is delivered by terminals as an escape prefix, so ``alt+x`` becomes
    the two-step sequence ``("escape", "x")`` — prompt_toolkit's own model.
    """
    parts = [p.strip() for p in token.split("+") if p.strip()]
    if not parts:
        raise KeystrokeError(f"empty keystroke in {token!r}")
    mods = set()
    for p in parts[:-1]:
        mod = _MOD_ALIASES.get(p.lower())
        if mod is None:
            raise KeystrokeError(f"unknown modifier {p!r} in {token!r}")
        mods.add(mod)
    base_raw = parts[-1]
    if "cmd" in mods:
        raise KeystrokeError(
            f"{token!r}: cmd/super is not delivered by most terminals; "
            "use ctrl or alt instead"
        )

    base = _SPECIAL_KEYS.get(base_raw.lower())
    prefix: Tuple[str, ...] = ("escape",) if "alt" in mods else ()

    if base is not None:
        key = base
        if base in _PREFIXABLE:
            if "ctrl" in mods and "shift" in mods:
                key = f"c-s-{base}"
            elif "ctrl" in mods:
                key = f"c-{base}"
            elif "shift" in mods:
                key = f"s-{base}"
        elif mods - {"alt"}:
            raise KeystrokeError(
                f"{token!r}: {base_raw} does not combine with "
                f"{'+'.join(sorted(mods - {'alt'}))}"
            )
        steps = prefix + (key,)
    elif len(base_raw) == 1:
        ch = base_raw
        if "ctrl" in mods:
            # ctrl+K == ctrl+k per Claude Code semantics (stylistic caps).
            ch = ch.lower()
            key = f"c-{ch}"
        elif "shift" in mods:
            key = ch.upper()
        else:
            # A standalone uppercase letter implies Shift — pass through.
            key = ch
        steps = prefix + (key,)
    else:
        raise KeystrokeError(f"unknown key {base_raw!r} in {token!r}")

    if not _pt_keys_valid(steps):
        raise KeystrokeError(
            f"{token!r} maps to {steps!r}, which this terminal stack "
            "cannot deliver"
        )
    return steps


def parse_keystroke(spec: object) -> Tuple[str, ...]:
    """``"ctrl+x ctrl+e"`` → ``("c-x", "c-e")``; ``"alt+enter"`` →
    ``("escape", "enter")``. Chords are space-separated. Raises
    :class:`KeystrokeError` on anything unparseable."""
    text = str(spec or "").strip()
    if not text:
        raise KeystrokeError("empty keystroke")
    steps: List[str] = []
    for token in text.split():
        steps.extend(_parse_single(token))
    return tuple(steps)


def _join_steps(steps: Tuple[str, ...]) -> str:
    """Canonical string for a parsed key sequence. The literal space key
    is spelled ``space`` so the join/split round-trip stays lossless."""
    return " ".join("space" if s == " " else s for s in steps)


def _split_canon(canon: str) -> Tuple[str, ...]:
    """Inverse of :func:`_join_steps`."""
    return tuple(" " if s == "space" else s for s in canon.split(" "))


def canonical_keystroke(spec: object) -> str:
    """Canonical form used for override matching — two spellings of the
    same combo (``control+E`` / ``ctrl+e``) compare equal. Raises
    :class:`KeystrokeError` on unparseable input."""
    return _join_steps(parse_keystroke(spec))


# ===========================================================================
# Action catalog — what exists, discoverable via /keys
# ===========================================================================


@dataclass(frozen=True)
class ActionSpec:
    """One remappable action, registered by the surface that owns it."""

    action: str
    context: str
    default_keys: Tuple[str, ...]
    description: str = ""
    schema_version: str = KEYMAP_SCHEMA_VERSION


_CATALOG: Dict[str, ActionSpec] = {}
_CATALOG_LOCK = threading.Lock()


def register_context(name: str, description: str = "") -> None:
    """Add a context to the known vocabulary. NEVER raises."""
    try:
        if isinstance(name, str) and name.strip():
            _KNOWN_CONTEXTS.setdefault(name.strip(), str(description or ""))
    except Exception:  # noqa: BLE001
        pass


def register_action_spec(
    action: str,
    context: str,
    default_keys: Tuple[str, ...],
    description: str = "",
) -> None:
    """Record an action in the catalog (idempotent — first registration
    wins so a remount doesn't churn descriptions). NEVER raises."""
    try:
        if not isinstance(action, str) or ":" not in action:
            return
        with _CATALOG_LOCK:
            _CATALOG.setdefault(action, ActionSpec(
                action=action,
                context=str(context or "Chat"),
                default_keys=tuple(str(k) for k in (default_keys or ())),
                description=str(description or ""),
            ))
        register_context(context)
    except Exception:  # noqa: BLE001
        pass


def action_catalog() -> Tuple[ActionSpec, ...]:
    """Snapshot of every registered action, sorted for stable display."""
    try:
        with _CATALOG_LOCK:
            specs = tuple(_CATALOG.values())
        return tuple(sorted(specs, key=lambda s: (s.context, s.action)))
    except Exception:  # noqa: BLE001
        return ()


# ===========================================================================
# Config — load, validate, hot-reload
# ===========================================================================


@dataclass(frozen=True)
class KeymapConfig:
    """Parsed operator config. ``blocks`` maps context → {canonical key →
    action-or-None}; ``raw_spelling`` remembers what the operator typed so
    warnings and ``/keys`` show their words, not ours."""

    path: Optional[str] = None
    blocks: Dict[str, Dict[str, Optional[str]]] = field(default_factory=dict)
    raw_spelling: Dict[str, str] = field(default_factory=dict)
    warnings: Tuple[str, ...] = ()
    schema_version: str = KEYMAP_SCHEMA_VERSION


def resolve_config_path() -> Optional[Path]:
    """First existing of: env override → ``<repo>/.jarvis/keybindings.json``
    → ``~/.jarvis/keybindings.json``. The ENV path is returned even when the
    file does not exist yet, so ``/keys init`` knows where to write."""
    try:
        env = os.environ.get(CONFIG_PATH_ENV_VAR, "").strip()
        if env:
            return Path(env).expanduser()
        repo = Path(os.environ.get("JARVIS_REPO_PATH", ".")).resolve()
        candidates = (
            repo / ".jarvis" / "keybindings.json",
            Path.home() / ".jarvis" / "keybindings.json",
        )
        for cand in candidates:
            if cand.is_file():
                return cand
        return candidates[0]  # default write target
    except Exception:  # noqa: BLE001
        return None


def parse_config(text: object, *, path: Optional[str] = None) -> KeymapConfig:
    """Parse + validate one config document. NEVER raises — structural
    problems become warnings and the offending entry is skipped, so one bad
    line costs one binding, not the file."""
    warnings: List[str] = []
    blocks: Dict[str, Dict[str, Optional[str]]] = {}
    raw_spelling: Dict[str, str] = {}
    try:
        doc = json.loads(str(text or ""))
    except Exception as exc:  # noqa: BLE001
        return KeymapConfig(
            path=path, warnings=(f"config is not valid JSON: {exc}",),
        )
    if not isinstance(doc, dict):
        return KeymapConfig(
            path=path, warnings=("config root must be an object",),
        )
    entries = doc.get("bindings")
    if entries is None:
        return KeymapConfig(path=path)
    if not isinstance(entries, list):
        return KeymapConfig(
            path=path, warnings=('"bindings" must be an array of blocks',),
        )
    for i, block in enumerate(entries):
        if not isinstance(block, dict):
            warnings.append(f"bindings[{i}] is not an object — skipped")
            continue
        context = block.get("context")
        if not isinstance(context, str) or not context.strip():
            warnings.append(f'bindings[{i}] has no "context" — skipped')
            continue
        context = context.strip()
        if context not in _KNOWN_CONTEXTS:
            warnings.append(
                f"bindings[{i}]: unknown context {context!r} "
                f"(known: {', '.join(sorted(_KNOWN_CONTEXTS))})"
            )
        mapping = block.get("bindings")
        if not isinstance(mapping, dict):
            warnings.append(
                f'bindings[{i}] ({context}) has no "bindings" map — skipped'
            )
            continue
        dest = blocks.setdefault(context, {})
        for raw_key, action in mapping.items():
            low = str(raw_key).strip().lower()
            if low in RESERVED_KEYSTROKES:
                warnings.append(
                    f"{context}: {raw_key!r} is reserved — "
                    f"{RESERVED_KEYSTROKES[low]}"
                )
                continue
            if low in TERMINAL_CONFLICTS:
                warnings.append(
                    f"{context}: {raw_key!r} conflicts with "
                    f"{TERMINAL_CONFLICTS[low]}"
                )
            try:
                canon = canonical_keystroke(raw_key)
            except KeystrokeError as exc:
                warnings.append(f"{context}: {exc}")
                continue
            if action is not None and (
                not isinstance(action, str) or ":" not in action
            ):
                warnings.append(
                    f"{context}: {raw_key!r} → {action!r} is not a "
                    '"namespace:action" id or null'
                )
                continue
            if canon in dest and dest[canon] != action:
                warnings.append(
                    f"{context}: duplicate binding for {raw_key!r} — "
                    "last one wins"
                )
            dest[canon] = action
            raw_spelling.setdefault(canon, str(raw_key))
    return KeymapConfig(
        path=path, blocks=blocks, raw_spelling=raw_spelling,
        warnings=tuple(warnings),
    )


class KeymapStore:
    """Config cache with throttled mtime-based hot reload.

    ``generation`` bumps on every observed change; mounts compare it to
    decide whether their compiled KeyBindings are stale. Thread-safe —
    prompt_toolkit consults bindings from the UI thread while surfaces
    mount from wherever they boot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._config = KeymapConfig()
        self._generation = 0
        self._mtime: Optional[float] = None
        self._path_seen: Optional[str] = None
        self._last_probe = 0.0
        self._loaded_once = False

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def config(self) -> KeymapConfig:
        return self._config

    def _throttle_s(self) -> float:
        try:
            return max(0.1, float(
                os.environ.get(RELOAD_THROTTLE_ENV_VAR, "1.0")
            ))
        except (TypeError, ValueError):
            return 1.0

    def maybe_reload(self, *, force: bool = False) -> KeymapConfig:
        """Re-read the config iff the file changed (path, mtime, or
        deletion). Throttled so per-keystroke consumers cost a monotonic
        read, not a stat. NEVER raises."""
        try:
            if not is_keymap_enabled():
                # Master off: behave as "no config", but keep the seam live.
                with self._lock:
                    if self._config.blocks or self._config.warnings:
                        self._config = KeymapConfig()
                        self._generation += 1
                    return self._config
            now = time.monotonic()
            with self._lock:
                if (
                    not force and self._loaded_once
                    and (now - self._last_probe) < self._throttle_s()
                ):
                    return self._config
                self._last_probe = now
                path = resolve_config_path()
                path_str = str(path) if path is not None else None
                mtime: Optional[float] = None
                if path is not None:
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        mtime = None
                if (
                    self._loaded_once
                    and path_str == self._path_seen
                    and mtime == self._mtime
                ):
                    return self._config
                self._path_seen, self._mtime = path_str, mtime
                self._loaded_once = True
                if path is None or mtime is None:
                    fresh = KeymapConfig(path=path_str)
                else:
                    try:
                        text = path.read_text(encoding="utf-8")
                    except OSError as exc:
                        fresh = KeymapConfig(
                            path=path_str,
                            warnings=(f"config unreadable: {exc}",),
                        )
                    else:
                        fresh = parse_config(text, path=path_str)
                self._config = fresh
                self._generation += 1
                if fresh.warnings:
                    logger.debug(
                        "[Keymap] %d warning(s) loading %s: %s",
                        len(fresh.warnings), path_str,
                        "; ".join(fresh.warnings),
                    )
                return self._config
        except Exception:  # noqa: BLE001
            logger.debug("[Keymap] reload degraded", exc_info=True)
            return self._config


_STORE = KeymapStore()


def get_store() -> KeymapStore:
    return _STORE


# ===========================================================================
# Resolution — defaults × config → effective key sequences
# ===========================================================================


def _default_sequences(
    default_keys: Tuple[str, ...],
) -> List[Tuple[str, str]]:
    """Parse declared defaults → [(canonical, spelled)] pairs, dropping any
    the parser rejects (a surface typo'ing its OWN default should lose that
    key, not the cockpit)."""
    out: List[Tuple[str, str]] = []
    for spec in default_keys:
        try:
            out.append((canonical_keystroke(spec), str(spec)))
        except KeystrokeError:
            logger.debug("[Keymap] bad default keystroke %r", spec)
    return out


def effective_key_sequences(
    action: str,
    default_keys: Tuple[str, ...],
    *,
    context: str = "Chat",
) -> Tuple[Tuple[str, ...], ...]:
    """The key sequences *action* should bind, after the operator's say.

    Resolution:
      1. start from the declared defaults;
      2. drop any default the config re-assigns or nulls in this context
         or in ``Global``;
      3. add any config key mapped TO this action in this context or
         ``Global``.

    NEVER raises; on any internal failure returns the parsed defaults."""
    defaults = _default_sequences(tuple(default_keys or ()))
    try:
        cfg = get_store().maybe_reload()
        overrides: Dict[str, Optional[str]] = {}
        for ctx in ("Global", context):
            overrides.update(cfg.blocks.get(ctx, {}))
        kept = [
            canon for canon, _ in defaults
            if canon not in overrides or overrides[canon] == action
        ]
        added = [
            canon for canon, mapped in overrides.items()
            if mapped == action and canon not in kept
        ]
        seen: set = set()
        out: List[Tuple[str, ...]] = []
        for canon in kept + added:
            if canon in seen:
                continue
            seen.add(canon)
            out.append(_split_canon(canon))
        return tuple(out)
    except Exception:  # noqa: BLE001
        logger.debug("[Keymap] resolution degraded for %s", action,
                     exc_info=True)
        return tuple(_split_canon(c) for c, _ in defaults)


# ===========================================================================
# Consumption — mounts (hot-reloadable) + call-site binding (static)
# ===========================================================================


@dataclass
class _MountEntry:
    action: str
    default_keys: Tuple[str, ...]
    context: str
    filter: Any
    eager: Any
    handler: Callable[..., Any]


class KeymapMount:
    """A surface's set of remappable actions, compiled lazily into
    prompt_toolkit KeyBindings and recompiled when the config changes.

    Usage::

        mount = KeymapMount("cockpit")

        @mount.action("app:detach", ("ctrl+c",), context="Global",
                      description="leave the cockpit; daemon keeps running")
        def _detach(event):
            event.app.exit()

        kb = merge_key_bindings([base_kb, mount.key_bindings()])

    ``key_bindings()`` returns a ``DynamicKeyBindings`` — prompt_toolkit
    consults it per key-processing pass, which is what makes a config edit
    land WITHOUT remounting the Application."""

    def __init__(self, surface: str) -> None:
        self.surface = str(surface or "surface")
        self._entries: List[_MountEntry] = []
        self._lock = threading.Lock()
        self._cache_gen = -1
        self._cache_kb: Any = None

    def action(
        self,
        action_id: str,
        default_keys: Tuple[str, ...],
        *,
        context: str = "Chat",
        description: str = "",
        filter: Any = None,  # noqa: A002 — prompt_toolkit's own name
        eager: Any = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator registering one remappable action on this mount."""

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            try:
                register_action_spec(
                    action_id, context, tuple(default_keys), description,
                )
                with self._lock:
                    self._entries.append(_MountEntry(
                        action=action_id,
                        default_keys=tuple(default_keys),
                        context=context,
                        filter=filter,
                        eager=eager,
                        handler=fn,
                    ))
                    self._cache_gen = -1  # new entry → recompile
            except Exception:  # noqa: BLE001
                logger.debug("[Keymap] mount.action degraded", exc_info=True)
            return fn

        return deco

    def _compile(self) -> Any:
        from prompt_toolkit.key_binding import KeyBindings
        kb = KeyBindings()
        with self._lock:
            entries = list(self._entries)
        for entry in entries:
            try:
                sequences = effective_key_sequences(
                    entry.action, entry.default_keys, context=entry.context,
                )
                kwargs: Dict[str, Any] = {}
                if entry.filter is not None:
                    kwargs["filter"] = entry.filter
                if entry.eager is not None:
                    kwargs["eager"] = entry.eager
                for seq in sequences:
                    kb.add(*seq, **kwargs)(entry.handler)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[Keymap] binding %s degraded", entry.action,
                    exc_info=True,
                )
        return kb

    def _current(self) -> Any:
        try:
            store = get_store()
            store.maybe_reload()
            with self._lock:
                stale = (
                    self._cache_kb is None
                    or self._cache_gen != store.generation
                )
            if stale:
                compiled = self._compile()
                with self._lock:
                    self._cache_kb = compiled
                    self._cache_gen = store.generation
            return self._cache_kb
        except Exception:  # noqa: BLE001
            logger.debug("[Keymap] mount refresh degraded", exc_info=True)
            return self._cache_kb

    def key_bindings(self) -> Any:
        """The hot-reloadable KeyBindings for this mount. Returns a plain
        empty KeyBindings when prompt_toolkit is unavailable."""
        try:
            from prompt_toolkit.key_binding.key_bindings import (
                DynamicKeyBindings,
            )
            return DynamicKeyBindings(self._current)
        except Exception:  # noqa: BLE001
            try:
                from prompt_toolkit.key_binding import KeyBindings
                return KeyBindings()
            except Exception:  # noqa: BLE001
                return None


def bind_action(
    kb: Any,
    action_id: str,
    default_keys: Tuple[str, ...],
    handler: Callable[..., Any],
    *,
    context: str = "Chat",
    description: str = "",
    filter: Any = None,  # noqa: A002
    eager: Any = None,
) -> bool:
    """Call-site variant for modules that own their KeyBindings object:
    resolves effective keys ONCE and binds them into *kb*. Registers the
    action in the catalog either way. Config edits apply on the next
    mount/attach rather than live — the trade for zero structural change
    at the call site. NEVER raises; returns True when ≥1 key bound."""
    try:
        register_action_spec(action_id, context, tuple(default_keys),
                             description)
        if kb is None:
            return False
        sequences = effective_key_sequences(
            action_id, tuple(default_keys), context=context,
        )
        kwargs: Dict[str, Any] = {}
        if filter is not None:
            kwargs["filter"] = filter
        if eager is not None:
            kwargs["eager"] = eager
        bound = 0
        for seq in sequences:
            try:
                kb.add(*seq, **kwargs)(handler)
                bound += 1
            except Exception:  # noqa: BLE001
                logger.debug("[Keymap] bind %s %r degraded", action_id, seq,
                             exc_info=True)
        return bound > 0
    except Exception:  # noqa: BLE001
        logger.debug("[Keymap] bind_action degraded", exc_info=True)
        return False


# ===========================================================================
# Introspection — the /keys verb reads these
# ===========================================================================


def describe_keymap() -> Dict[str, Any]:
    """Everything an operator needs to see in one place: the effective
    binding per action (with its source), the config path, and every
    warning. Semantic validation (config actions nobody registered) happens
    HERE rather than at load — surfaces register their actions as they
    mount, so load-time checks would false-positive on boot order."""
    cfg = get_store().maybe_reload(force=True)
    rows: List[Dict[str, Any]] = []
    known_actions = {s.action for s in action_catalog()}
    for spec in action_catalog():
        effective = effective_key_sequences(
            spec.action, spec.default_keys, context=spec.context,
        )
        canon_defaults = tuple(c for c, _ in _default_sequences(
            spec.default_keys,
        ))
        effective_canon = tuple(" ".join(seq) for seq in effective)
        rows.append({
            "context": spec.context,
            "action": spec.action,
            "keys": effective_canon,
            "default_keys": spec.default_keys,
            "customized": tuple(effective_canon) != canon_defaults,
            "description": spec.description,
        })
    semantic: List[str] = []
    for ctx, mapping in cfg.blocks.items():
        for canon, action in mapping.items():
            if action is not None and action not in known_actions:
                spelled = cfg.raw_spelling.get(canon, canon)
                semantic.append(
                    f"{ctx}: {spelled!r} → unknown action {action!r}"
                )
    return {
        "schema_version": KEYMAP_SCHEMA_VERSION,
        "enabled": is_keymap_enabled(),
        "config_path": cfg.path,
        "config_present": bool(cfg.blocks) or bool(cfg.warnings),
        "warnings": list(cfg.warnings) + semantic,
        "contexts": dict(_KNOWN_CONTEXTS),
        "actions": rows,
    }


CONFIG_TEMPLATE: str = """{
  "$docs": "https://code.claude.com/docs/en/keybindings",
  "bindings": [
    {
      "context": "Chat",
      "bindings": {
      }
    }
  ]
}
"""


def write_config_template() -> Optional[str]:
    """Create the config file (with parents) if it doesn't exist. Returns
    the path written / already present, or None on failure. NEVER raises."""
    try:
        path = resolve_config_path()
        if path is None:
            return None
        if path.is_file():
            return str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        get_store().maybe_reload(force=True)
        return str(path)
    except Exception:  # noqa: BLE001
        logger.debug("[Keymap] template write failed", exc_info=True)
        return None


__all__ = [
    "ActionSpec",
    "CONFIG_PATH_ENV_VAR",
    "CONFIG_TEMPLATE",
    "KEYMAP_SCHEMA_VERSION",
    "KeymapConfig",
    "KeymapMount",
    "KeymapStore",
    "KeystrokeError",
    "MASTER_FLAG_ENV_VAR",
    "RESERVED_KEYSTROKES",
    "TERMINAL_CONFLICTS",
    "action_catalog",
    "bind_action",
    "canonical_keystroke",
    "describe_keymap",
    "effective_key_sequences",
    "get_store",
    "is_keymap_enabled",
    "parse_config",
    "parse_keystroke",
    "register_action_spec",
    "register_context",
    "resolve_config_path",
    "write_config_template",
]
