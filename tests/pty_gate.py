"""The single gate for tests that need a real pseudo-terminal.

WHY THIS EXISTS
---------------
Thirty-one tests across three files — the entire proof that the cockpit
responds to a keystroke — sat behind two independently written
``_require_pty`` helpers that both called ``pytest.skip("out of pty devices")``.

In every sandboxed or containerised runner ``pty.openpty()`` raises, so all
thirty-one vanished and the run reported **green**. That is how the cockpit's
interactive behaviour came to be described as "never run under a real TTY"
while a 650-line suite proving it sat in the repository, passing in 80 seconds
the moment anyone gave it a terminal.

A skip is a legitimate answer to "this environment has no pty". Reporting it as
success is not. This module makes the distinction impossible to lose:

  * ONE gate, so the two copies cannot drift;
  * every skip is RECORDED, and the terminal summary says loudly how much of
    the suite did not run and what that means;
  * ``JARVIS_PTY_TESTS_REQUIRED`` turns the skip into a FAILURE, so a runner
    that is supposed to have a terminal cannot quietly stop having one.

The env var is the adaptive part. A developer laptop legitimately varies; a CI
job that claims to exercise the cockpit does not, and it should fail loudly the
day its base image drops ``/dev/ptmx``.
"""

from __future__ import annotations

import os
from typing import List, Tuple

import pytest

#: Set to a truthy value where a pty is a REQUIREMENT rather than a nicety.
#: Unset (the default) preserves the skip, so a laptop without one still runs
#: the rest of the suite.
REQUIRED_ENV_VAR = "JARVIS_PTY_TESTS_REQUIRED"

#: Every skip taken this session, for the terminal summary. Module scope
#: because the summary hook runs in a different fixture context entirely and a
#: pytest stash would tie this to one plugin's lifetime.
SKIPPED: List[Tuple[str, str]] = []


def pty_required() -> bool:
    """Is a pty mandatory in this environment?"""
    raw = str(os.environ.get(REQUIRED_ENV_VAR, "")).strip().lower()
    return raw not in ("", "0", "false", "no", "off")


def open_pty(nodeid: str = "") -> Tuple[int, int]:
    """Allocate a master/slave pair, or skip — recording that we did.

    Returns the pair so callers that need the fds get them from the same call
    that decides whether they may proceed. A caller that only needs the
    decision closes them immediately; that is cheaper than a second syscall
    path with its own error handling, and it keeps ONE definition of "can this
    machine give us a terminal".
    """
    import pty

    try:
        return pty.openpty()
    except OSError as exc:
        reason = f"pty allocation unavailable in this environment: {exc}"
        SKIPPED.append((nodeid or "<unknown>", str(exc)))
        if pty_required():
            pytest.fail(
                f"{reason}\n"
                f"{REQUIRED_ENV_VAR} is set, so this runner is expected to "
                f"provide a pseudo-terminal. The cockpit's interactive "
                f"behaviour is UNPROVEN without one."
            )
        pytest.skip(reason)
        raise AssertionError("unreachable")  # pragma: no cover


def require_pty(nodeid: str = "") -> None:
    """Skip unless this machine can give us a terminal. Allocates nothing."""
    master, slave = open_pty(nodeid)
    os.close(master)
    os.close(slave)


# ---------------------------------------------------------------------------
# PTY AFFINITY — deciding WHICH tests need a terminal, without a decorator
# ---------------------------------------------------------------------------
#
# WHY THIS IS AST-BASED AND NOT A MARKER
# --------------------------------------
# `require_pty()` above answers "can this machine give us a terminal". It has
# to be CALLED to matter, and seven of the eight pty-driving files in this
# repository never call it -- not from malice but because a decorator is a
# thing a human must remember. One of them (`test_region_layout_mount.py`)
# allocates directly in the test body, so on a machine without `/dev/ptmx` it
# does not skip, it ERRORS with a raw `OSError: out of pty devices` while its
# gated neighbours skip cleanly. Same environment, same cause, two verdicts.
#
# So the gate is applied by COLLECTION, from evidence in the source, and the
# question becomes "what evidence proves a test drives a terminal".
#
# TWO SIGNALS THAT LOOK RIGHT AND ARE NOT
# ---------------------------------------
# 1. "The module imports prompt_toolkit or rich."  MEASURED: 90 test files do,
#    and 8 use a pty. Marking on that signal would convert ~82 files of
#    currently-passing tests into skips on every runner without a terminal --
#    the exact "reported green while nothing ran" failure this module exists to
#    prevent, running in the opposite direction. Rendering into a
#    `Console(record=True)` needs no kernel anything.
#
# 2. "The module imports `pty`."  MEASURED: it catches ONE of the eight.
#    The other seven build a child-process script as a STRING and drive it
#    through `subprocess`; `pid, fd = pty.fork()` lives inside a triple-quoted
#    literal. `test_alt_screen_boot.py` imports neither `pty` nor
#    `prompt_toolkit` nor `rich` at module level. An import graph cannot see
#    it, and neither can any tool that only walks imports.
#
# So the detector reads BOTH the import graph AND the string literals, because
# in this repository the terminal is driven from inside a string as often as
# it is driven from an import.
#
# CONFIGURATION IS EXTEND-ONLY, DELIBERATELY
# ------------------------------------------
# Every signal set below is overridable through the environment, and the
# override EXTENDS rather than replaces. A detector that an env var could
# narrow to nothing is a detector that a typo silently disarms, which is how
# this class of bug returns wearing a configuration flag. The one honest way
# to turn it off is `JARVIS_PTY_AUTOMARK_DISABLED`, which says so out loud and
# is reported in the terminal summary.

#: Modules whose import means the test itself allocates a terminal.
DEFAULT_PTY_MODULES = ("pty",)

#: Attribute names on those modules that allocate. `pty.openpty`/`pty.fork`.
DEFAULT_PTY_CALLS = ("openpty", "fork")

#: In-tree helpers that own a pty on the caller's behalf. Matched on the FULL
#: dotted path and on the final segment, so `from .pty_console import ...`
#: (a relative import, whose `module` is just `pty_console`) is caught too.
DEFAULT_PTY_HELPERS = ("tests.ui.pty_console", "tests.pty_gate")

#: Substrings that betray a pty driven from inside an embedded child script.
#: These are matched against STRING LITERALS, which is where seven of the
#: eight live.
DEFAULT_PTY_SOURCE_SIGNALS = ("pty.fork(", "pty.openpty(", "TIOCSWINSZ")

MODULES_ENV_VAR = "JARVIS_PTY_SIGNAL_MODULES"
HELPERS_ENV_VAR = "JARVIS_PTY_HELPER_MODULES"
SIGNALS_ENV_VAR = "JARVIS_PTY_SOURCE_SIGNALS"
DISABLE_ENV_VAR = "JARVIS_PTY_AUTOMARK_DISABLED"

#: Marker applied to anything the detector recognises. Registered in
#: `pytest_configure` so `-m pty` and `-m "not pty"` both work and no
#: PytestUnknownMarkWarning is emitted.
PTY_MARKER = "pty"

#: path -> (fingerprint, verdict). Collection asks once per ITEM and a file
#: holds many; parsing 3,326 files per run without this would cost more than
#: the suite it gates.
_AFFINITY_CACHE: "dict" = {}

#: Files the detector could not parse AND could not read. Surfaced in the
#: summary rather than swallowed: "we could not tell" is not "no".
UNDECIDABLE: List[Tuple[str, str]] = []


def _env_extend(var: str, defaults: Tuple[str, ...]) -> Tuple[str, ...]:
    """Defaults PLUS anything the environment adds. Never fewer. NEVER raises."""
    out = list(defaults)
    try:
        raw = str(os.environ.get(var, "") or "")
        for piece in raw.replace(",", " ").split():
            piece = piece.strip()
            if piece and piece not in out:
                out.append(piece)
    except Exception:  # noqa: BLE001 — a malformed env var must not blind the gate
        pass
    return tuple(out)


def automark_enabled() -> bool:
    """Is collection-time pty detection active? NEVER raises."""
    try:
        raw = str(os.environ.get(DISABLE_ENV_VAR, "")).strip().lower()
        return raw in ("", "0", "false", "no", "off")
    except Exception:  # noqa: BLE001
        return True


def _fingerprint(path: str) -> "Tuple[int, int]":
    """(mtime_ns, size) — cheap, and it changes whenever the content does."""
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def _scan_source(source: str) -> bool:
    """Does this source drive a terminal? AST first, text as the floor.

    The AST pass answers the import and attribute questions precisely. The
    literal pass answers the embedded-child-script question, which no import
    analysis can. A file that fails to parse still gets the literal pass --
    a `SyntaxError` in a test file is a broken test, not permission to stop
    gating it.
    """
    import ast

    modules = _env_extend(MODULES_ENV_VAR, DEFAULT_PTY_MODULES)
    calls = _env_extend("", DEFAULT_PTY_CALLS)
    helpers = _env_extend(HELPERS_ENV_VAR, DEFAULT_PTY_HELPERS)
    helper_tails = {h.rsplit(".", 1)[-1] for h in helpers}

    def _literal_hit(text: str) -> bool:
        signals = _env_extend(SIGNALS_ENV_VAR, DEFAULT_PTY_SOURCE_SIGNALS)
        return any(sig in text for sig in signals)

    try:
        tree = ast.parse(source)
    except Exception:  # noqa: BLE001 — SyntaxError, RecursionError, ValueError
        return _literal_hit(source)

    for node in ast.walk(tree):
        # `import pty`, `import tests.ui.pty_console`
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name or ""
                root = name.split(".")[0]
                if root in modules or name in helpers:
                    return True
                if name.rsplit(".", 1)[-1] in helper_tails:
                    return True
        # `from pty import openpty`, `from .pty_console import PtyConsole`
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ""
            root = name.split(".")[0]
            if root in modules or name in helpers:
                return True
            if name and name.rsplit(".", 1)[-1] in helper_tails:
                return True
        # `pty.openpty(...)` / `pty.fork(...)` however the module was bound
        elif isinstance(node, ast.Attribute):
            if node.attr in calls:
                base = node.value
                if isinstance(base, ast.Name) and base.id in modules:
                    return True
        # The embedded child script -- seven of the eight live here.
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and _literal_hit(node.value):
                return True

    return False


def module_needs_pty(path: "str") -> bool:
    """Does the test module at ``path`` drive a real terminal? NEVER raises.

    Unreadable or unstattable files answer **False** and are recorded in
    :data:`UNDECIDABLE`. False is the honest answer for "we could not look":
    marking on a failed read would gate tests on the health of this detector
    rather than on what they do, and a detector that fails closed would take
    the suite down with it the first time a path went strange.
    """
    if not path:
        return False
    try:
        fp = _fingerprint(path)
    except Exception as exc:  # noqa: BLE001
        UNDECIDABLE.append((str(path), f"stat failed: {exc}"))
        return False

    cached = _AFFINITY_CACHE.get(path)
    if cached is not None and cached[0] == fp:
        return bool(cached[1])

    try:
        with open(path, "rb") as fh:
            source = fh.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        UNDECIDABLE.append((str(path), f"read failed: {exc}"))
        return False

    try:
        verdict = _scan_source(source)
    except Exception as exc:  # noqa: BLE001 — detection must never kill collection
        UNDECIDABLE.append((str(path), f"scan failed: {exc}"))
        verdict = False

    _AFFINITY_CACHE[path] = (fp, verdict)
    return verdict


def item_path(item: "object") -> str:
    """The filesystem path of a collected item, across pytest versions."""
    try:
        raw = getattr(item, "path", None)
        if raw is None:
            raw = getattr(item, "fspath", None)
        return str(raw) if raw is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def mark_pty_items(items: "object") -> int:
    """Apply the pty marker to every item whose module drives a terminal.

    Returns how many were marked. Mutates items in place; never raises; a
    single unmarkable item never stops the rest.
    """
    if not automark_enabled():
        return 0
    marked = 0
    try:
        mark = getattr(pytest.mark, PTY_MARKER)
    except Exception:  # noqa: BLE001
        return 0
    for item in items or ():
        try:
            if item.get_closest_marker(PTY_MARKER) is not None:
                continue
            # Per-ITEM, never per-module: see the taint section below for the
            # measurement that made module granularity unacceptable.
            if item_needs_pty(item):
                item.add_marker(mark)
                marked += 1
        except Exception:  # noqa: BLE001
            continue
    return marked


def gate_item(item: "object") -> None:
    """Enforce the terminal requirement for one marked item. NEVER raises past pytest.

    Runs at SETUP, before the test body allocates anything. That placement is
    the whole point: it is why a machine without `/dev/ptmx` now produces the
    same verdict for all eight files instead of one raw `OSError` among seven
    clean skips. Delegates the decision to :func:`require_pty` so there is
    still exactly ONE definition of "can this machine give us a terminal".
    """
    try:
        if item.get_closest_marker(PTY_MARKER) is None:
            return
    except Exception:  # noqa: BLE001
        return
    require_pty(getattr(item, "nodeid", "") or "")


# ---------------------------------------------------------------------------
# ITEM-LEVEL AFFINITY — because a module is the wrong unit
# ---------------------------------------------------------------------------
#
# MEASURED, and the reason this section exists: gating at MODULE level turned
# `test_region_layout_mount.py` from "4 errored, 5 passed" into "9 skipped".
# Exactly one of its six tests touches a terminal (`test_renders_without_
# geometry_panic`, via the module-level helper `_render_at`); the five in the
# class below it never allocate anything. Skipping those five is the same
# harm as the import-based signal rejected above -- passing tests silently
# converted into skips -- just smaller. A fix that trades one regression for
# a quieter one is not a fix.
#
# So affinity is resolved per TEST, by taint analysis inside the module:
#
#   * a function is DIRECTLY tainted if its own subtree allocates a terminal
#     (`pty.openpty`/`pty.fork`), imports `pty`, embeds a child script whose
#     text does, or references a name already known to be tainted;
#   * taint PROPAGATES through calls to fixpoint, so a test that calls a
#     helper that calls a helper that allocates is caught;
#   * a module-level `import pty` taints NOTHING by itself. An import is not a
#     use, and treating it as one is what over-gated the five.
#
# Fixtures are functions, so they are covered by the same pass: a test whose
# parameter names a tainted fixture inherits its taint.

def _tainted_seed_names(tree: "object", helpers: "Tuple[str, ...]") -> "set":
    """Module-level names that carry terminal-driving content.

    Two kinds, both of which the call graph then propagates:

    * a string constant holding an embedded child script (`_SCRIPT = '''...
      pty.fork() ...'''`) -- the shape seven of the eight files use;
    * a binding introduced by importing a pty-owning helper, under whatever
      alias it was given.
    """
    import ast

    helper_tails = {h.rsplit(".", 1)[-1] for h in helpers}
    seeds = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            val = node.value.value
            if isinstance(val, str) and _literal_signals_hit(val):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        seeds.add(tgt.id)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in helpers or mod.rsplit(".", 1)[-1] in helper_tails:
                for alias in node.names:
                    seeds.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name or ""
                if name in helpers or name.rsplit(".", 1)[-1] in helper_tails:
                    seeds.add(alias.asname or name.split(".")[0])
    return seeds


def _literal_signals_hit(text: str) -> bool:
    """Does this text drive a terminal? Used for embedded child scripts."""
    try:
        signals = _env_extend(SIGNALS_ENV_VAR, DEFAULT_PTY_SOURCE_SIGNALS)
        return any(sig in text for sig in signals)
    except Exception:  # noqa: BLE001
        return False


def _function_defs(tree: "object") -> "dict":
    """Every function in the module, by bare name, including methods.

    Keyed on the bare name because that is what a pytest node id gives us.
    On a name collision across classes the entries are kept as a LIST and the
    taint verdict is the OR of them -- conservative in the only direction
    that cannot silently un-gate a terminal test.
    """
    import ast

    out: "dict" = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, []).append(node)
    return out


def _direct_taint(fn: "object", seeds: "set") -> bool:
    """Does this function itself reach a terminal? NEVER raises."""
    import ast

    modules = _env_extend(MODULES_ENV_VAR, DEFAULT_PTY_MODULES)
    calls = DEFAULT_PTY_CALLS
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and node.attr in calls:
            base = node.value
            if isinstance(base, ast.Name) and base.id in modules:
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.name or "").split(".")[0] in modules:
                    return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in modules:
                return True
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and _literal_signals_hit(node.value):
                return True
        elif isinstance(node, ast.Name) and node.id in seeds:
            return True
    return False


def _callee_names(fn: "object") -> "set":
    """Names this function calls, plus its parameter names.

    Parameters are included because a pytest fixture arrives as one, and a
    fixture that allocates a terminal must taint the tests that request it.
    """
    import ast

    names = set()
    try:
        for arg in list(getattr(fn.args, "args", []) or []) + \
                list(getattr(fn.args, "kwonlyargs", []) or []):
            names.add(arg.arg)
    except Exception:  # noqa: BLE001
        pass
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def _analyze_module(source: str) -> "Tuple[bool, set]":
    """(module_touches_pty, {tainted function names}). NEVER raises.

    The boolean is the conservative module-wide answer, kept for callers that
    have no test name to resolve. The set is the precise one.
    """
    import ast

    try:
        tree = ast.parse(source)
    except Exception:  # noqa: BLE001 — unparseable: fall back to the text floor
        return (_literal_signals_hit(source), set())

    helpers = _env_extend(HELPERS_ENV_VAR, DEFAULT_PTY_HELPERS)
    seeds = _tainted_seed_names(tree, helpers)
    funcs = _function_defs(tree)

    tainted = set(seeds)
    for name, defs in funcs.items():
        if any(_direct_taint(d, seeds) for d in defs):
            tainted.add(name)

    # Propagate through the call graph to fixpoint. Bounded by the number of
    # functions, so it always terminates; a cycle simply stops adding.
    changed = True
    guard = 0
    while changed and guard < len(funcs) + 2:
        changed = False
        guard += 1
        for name, defs in funcs.items():
            if name in tainted:
                continue
            for d in defs:
                if _callee_names(d) & tainted:
                    tainted.add(name)
                    changed = True
                    break

    module_wide = bool(tainted) or _scan_source(source)
    return (module_wide, tainted)


def _module_analysis(path: str) -> "Tuple[bool, set]":
    """Cached :func:`_analyze_module` for one path. NEVER raises."""
    key = ("analysis", path)
    try:
        fp = _fingerprint(path)
    except Exception as exc:  # noqa: BLE001
        UNDECIDABLE.append((str(path), f"stat failed: {exc}"))
        return (False, set())
    cached = _AFFINITY_CACHE.get(key)
    if cached is not None and cached[0] == fp:
        return cached[1]
    try:
        with open(path, "rb") as fh:
            source = fh.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        UNDECIDABLE.append((str(path), f"read failed: {exc}"))
        return (False, set())
    try:
        result = _analyze_module(source)
    except Exception as exc:  # noqa: BLE001
        UNDECIDABLE.append((str(path), f"analysis failed: {exc}"))
        result = (False, set())
    _AFFINITY_CACHE[key] = (fp, result)
    return result


def _item_function_name(item: "object") -> str:
    """The bare function name behind a node id.

    `test_x[200]` -> `test_x`; `TestC::test_y` -> `test_y`. Parametrisation
    and class nesting are presentation, not identity.
    """
    try:
        name = str(getattr(item, "name", "") or "")
    except Exception:  # noqa: BLE001
        return ""
    if "[" in name:
        name = name.split("[", 1)[0]
    if "::" in name:
        name = name.rsplit("::", 1)[-1]
    return name.strip()


def item_needs_pty(item: "object") -> bool:
    """Does THIS test drive a terminal? NEVER raises.

    Falls back to the module-wide verdict only when the function cannot be
    resolved -- an unresolvable name must not silently un-gate a test that
    allocates.
    """
    path = item_path(item)
    if not path:
        return False
    module_wide, tainted = _module_analysis(path)
    if not module_wide:
        return False
    fname = _item_function_name(item)
    if not fname:
        return True
    if fname in tainted:
        return True
    # A resolvable name that is simply not tainted is a real negative: the
    # module drives a terminal somewhere, this test does not.
    try:
        import ast
        with open(path, "rb") as fh:
            src = fh.read().decode("utf-8", errors="replace")
        if fname in _function_defs(ast.parse(src)):
            return False
    except Exception:  # noqa: BLE001
        pass
    return True
