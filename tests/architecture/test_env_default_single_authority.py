"""Architecture pin: one environment variable, ONE default.

Two places resolving the same ``JARVIS_*`` variable with different defaults is
not a style problem — it is two authorities, and the operator only ever sees
the second one. It shipped twice in this repository and both were found by
sweeping for it, not by reading the code:

    JARVIS_GOVERNED_TOOL_MAX_ROUNDS   engine 15 · boot panel 10
        Unset — the default case — the panel announced a safety ceiling the
        tool loop did not use.

    JARVIS_L2_ENABLED                 engine "true" · boot panel ""
        Unset, the engine ran L2 and the panel reported it absent. The two
        also disagreed about `1`, `yes` and `on`: not a mismatched default,
        two different definitions of the same word.

This pin makes a third instance fail CI naming the file:line of both sides.

WHY AST AND NOT A REGEX
-----------------------
The sweep that found those two was a regex over ``environ.get("VAR", "d")``.
It would have missed ``os.getenv``, any call split across lines, and — most
importantly — ``environ.get(SOME_CONST, "d")``, which is exactly the shape the
FIXES introduced (``L2_ENABLED_ENV = "JARVIS_L2_ENABLED"``). A literal-only
scanner would be blind in precisely the code written to end the problem.

So this walks the AST, and resolves module-level string constants before
deciding whether a call names a JARVIS variable.

WHY DEFAULTS ARE NORMALISED BEFORE THEY ARE COMPARED
----------------------------------------------------
``"120"`` and ``"120.0"`` are the same timeout; ``"600"`` and ``"600.0"`` are
the same deadline. Failing on those would train everyone to waive, and a
check people route around is worse than no check. Only differences that could
produce different behaviour are reported.

WHY THE WAIVERS CANNOT ROT
--------------------------
A waiver list normally decays into a place to silence failures. This one is
self-cleaning: a waiver for a variable that NO LONGER conflicts fails the test
too. A conflict cannot be waived without a reason, and a waiver cannot outlive
the conflict it excuses.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Set, Tuple

_REPO = Path(__file__).resolve().parents[2]

#: The executable surface this invariant governs.
_ROOT = _REPO / "backend" / "core" / "ouroboros"

#: Only this family. A third-party variable may legitimately carry different
#: defaults in different subsystems; these are ours and mean one thing.
_PREFIX = "JARVIS_"

#: Call targets that resolve an environment variable with a fallback, matched
#: on their TAIL rather than their full dotted path.
#:
#: The first version of this pin matched exact paths — ('os','environ','get')
#: and friends — and a self-test injecting `import os as _o` walked straight
#: past it. Alias-blind, which made the check a thing that reported success
#: without measuring: precisely the defect it exists to catch, in the code
#: written to catch it.
#:
#: Tail-matching is alias-proof and also covers `from os import environ` and
#: `from os import getenv` for free. `environ.get` and `getenv` are
#: unambiguous enough that a false positive is not a realistic concern.
_GETTER_TAILS = (("environ", "get"), ("getenv",))


def _is_env_getter(func: ast.AST) -> bool:
    path = _dotted(func)
    return any(path[-len(tail):] == tail for tail in _GETTER_TAILS if path)

#: Conflicts that are real but correct, each with the reason it is correct.
#: A waiver whose conflict has been fixed FAILS — see the module docstring.
_WAIVED: Dict[str, str] = {
    # ---- Verified equivalent: different spellings, one behaviour ----------
    "JARVIS_VENV_DIR": (
        "Python sites use '~/...' with expanduser; trinity_installer also "
        "embeds '$HOME/...' into a generated bash launcher, where $HOME "
        "expands at runtime and '~' inside double quotes would not. Different "
        "consumers, correctly different defaults."
    ),
    "JARVIS_RISK_CEILING": (
        "serpent_flow's default is a human sentence — '(not set — using "
        "per-op classification)' — rendered when no ceiling is configured. "
        "The governance sites default to '' and branch on emptiness. A "
        "display string is not a competing value."
    ),
    "JARVIS_BACKLOG_URGENCY_HINT_ENABLED": (
        "'' and 'false' are both FALSY under every truthiness helper in this "
        "repository, so the feature is off either way. One spelling would be "
        "tidier; neither can produce different behaviour."
    ),
    "JARVIS_MEMORY_REPUTATION_BIAS_ENABLED": (
        "'' and 'false' are both falsy; the feature is off under either. See "
        "JARVIS_BACKLOG_URGENCY_HINT_ENABLED."
    ),
    "JARVIS_PARANOIA_MODE": (
        "'0' and '' are both falsy; paranoia is off under either. The flag is "
        "documented as a shortcut for a minimum risk tier and is read as a "
        "boolean at every site."
    ),
    "JARVIS_PROJECT_ROOT": (
        "'' and '.' both resolve to the working directory: the '' sites pair "
        "the lookup with an `or`/falsy branch that substitutes cwd, and '.' "
        "IS cwd. Same directory by two routes."
    ),
    "JARVIS_REPO_PATH": (
        "'' and '.' both resolve to the working directory, as with "
        "JARVIS_PROJECT_ROOT. The widest conflict by call count and the least "
        "consequential."
    ),

    # ---- KNOWN DEFECTS: real disagreements, each needs its own change -----
    # Waived so this pin stays green for NEW regressions. Every entry here is
    # a to-do, and the self-cleaning check above deletes it the day it is
    # fixed. None of these were introduced by the work that added this file.
    "JARVIS_GOVERNANCE_MODE": (
        "KNOWN DEFECT. health_cortex defaults to 'governed' while "
        "remote_status and integration default to 'sandbox'. Unset, the "
        "health cortex believes one tier while the surface that REPORTS "
        "governance mode externally announces the other. The most "
        "safety-relevant instance found; needs an owner to decide which is "
        "correct before a resolver can be written."
    ),
    "JARVIS_PRIME_API_KEY": (
        "KNOWN DEFECT. engine.py defaults to 'sk-local-jarvis-key'; "
        "integration.py defaults to 'sk-local-jarvis' in two places. If "
        "either default is ever actually used, one of the two authenticates "
        "and the other does not."
    ),
    "JARVIS_PRIME_API_BASE": (
        "KNOWN DEFECT. Two different local endpoints — ':8000/v1' and "
        "':8080' — differ in BOTH port and path prefix, so a caller taking "
        "the wrong default reaches nothing rather than the wrong thing."
    ),
    "JARVIS_PRIME_HOST": (
        "KNOWN DEFECT. Three defaults: '', 'localhost', and a hardcoded "
        "public IP. A subsystem falling back to the IP when another falls "
        "back to localhost is a silent split-brain about where J-Prime is."
    ),
    "JARVIS_CHANNEL_PORT": (
        "KNOWN DEFECT. event_channel binds 8099; ov_doctor defaults to '' "
        "when probing it. A diagnostic that resolves a port differently from "
        "the server it diagnoses can report a healthy channel as absent."
    ),
    "JARVIS_TEST_DIR_NAMES": (
        "KNOWN DEFECT. test_source_attribution defaults to 'tests' while "
        "repair_engine, repair_sandbox, reverse_dep_resolver, "
        "target_stratification and test_runner default to 'tests,test'. "
        "Attribution would not look in a 'test/' directory the rest of the "
        "system searches — a coverage gap, not a cosmetic one."
    ),
    "JARVIS_APP_NAME": (
        "KNOWN DEFECT (or a deliberate rename mid-flight): 'jarvis' in one "
        "place and 'trinity' in another. Needs an owner to say which name "
        "the product answers to."
    ),
    "JARVIS_BRAIN_POLICY_PATH": (
        "KNOWN DEFECT. context_pruner defaults to the real policy file; "
        "dream_engine defaults to ''. Since every model reference is meant "
        "to resolve through brain_selection_policy.yaml, a site that "
        "silently has no policy path is worth an owner's attention."
    ),
    "JARVIS_DW_REASONING_EFFORT": (
        "KNOWN DEFECT, same file twice: '' and 'none'. Not provably "
        "equivalent — '' is falsy while 'none' is a truthy string, so a site "
        "testing presence rather than value would branch differently."
    ),
    "JARVIS_MIN_EXPLORATION_CALLS": (
        "KNOWN DEFECT, tracked separately. orchestrator/validate_runner/"
        "providers read a flat '2' while exploration_floor.py exists to be "
        "the single complexity-scaled authority and none of them import it. "
        "exploration_floor's own docstring records this happening once "
        "already. Fixing it means threading complexity context into three "
        "governance call sites — a real change, not a rename."
    ),
}


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------

def _dotted(node: ast.AST) -> Tuple[str, ...]:
    """`os.environ.get` -> ('os','environ','get'). () when not a plain path."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return tuple(reversed(parts))
    return ()


def _module_constants(tree: ast.AST) -> Dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings.

    Load-bearing: the fixes for both shipped defects introduced exactly this
    shape, so a scanner that only understood literals would be blind to the
    code written to prevent the problem recurring.
    """
    out: Dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                out[target.id] = node.value.value
    return out


def _literal(node: ast.AST, consts: Dict[str, str]) -> "str | None":
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _normalise(default: str) -> str:
    """Collapse spellings that cannot produce different behaviour.

    Numeric strings compare as numbers, so '120' and '120.0' are one default.
    Everything else compares on its stripped, case-folded text: a resolver
    that lowercases before testing makes 'TRUE' and 'true' the same word.
    """
    text = default.strip()
    try:
        return f"num:{float(text)}"
    except ValueError:
        return f"str:{text.casefold()}"


def _resolutions() -> Dict[str, Dict[str, Set[str]]]:
    """{var: {normalised_default: {"file:line", ...}}} across the surface."""
    found: Dict[str, Dict[str, Set[str]]] = {}
    for path in sorted(_ROOT.rglob("*.py")):
        # Cheap gate before the expensive one. Parsing every module under
        # ouroboros costs ~74s; the overwhelming majority mention no JARVIS_
        # variable at all, and a substring test rejects them in microseconds.
        # A pin nobody runs because it is slow is a pin that catches nothing.
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _PREFIX not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        consts = _module_constants(tree)
        rel = path.relative_to(_REPO)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) != 2:
                continue
            if not _is_env_getter(node.func):
                continue
            var = _literal(node.args[0], consts)
            default = _literal(node.args[1], consts)
            if not var or default is None or not var.startswith(_PREFIX):
                continue
            found.setdefault(var, {}).setdefault(
                _normalise(default), set(),
            ).add(f"{rel}:{node.lineno}")
    return found


def conflicts() -> Dict[str, Dict[str, Set[str]]]:
    """Variables resolved with more than one behaviourally distinct default."""
    return {v: d for v, d in _resolutions().items() if len(d) > 1}


# ---------------------------------------------------------------------------
# the pin
# ---------------------------------------------------------------------------

def test_no_environment_variable_has_two_defaults() -> None:
    unwaived = {v: d for v, d in conflicts().items() if v not in _WAIVED}
    if not unwaived:
        return

    report = ["", "An environment variable is resolved with two defaults.",
              "The operator only ever sees the second one.", ""]
    for var, defaults in sorted(unwaived.items()):
        report.append(f"  {var}")
        for norm, sites in sorted(defaults.items()):
            shown = norm.split(":", 1)[1]
            report.append(f"      default {shown!r}")
            for site in sorted(sites):
                report.append(f"          {site}")
        report.append("")
    report.append("Give the variable ONE resolver and have every consumer read")
    report.append("it. If the difference is genuinely correct, waive it in")
    report.append("_WAIVED with the reason it is correct.")
    raise AssertionError("\n".join(report))


def test_every_waiver_still_excuses_a_real_conflict() -> None:
    """A waiver that outlives its conflict is how an allowlist becomes a
    graveyard. Fixing a variable must also require deleting its excuse."""
    live = set(conflicts())
    stale = sorted(set(_WAIVED) - live)
    assert not stale, (
        "These variables no longer conflict — delete their waivers:\n  "
        + "\n  ".join(stale)
    )


def test_every_waiver_carries_a_reason() -> None:
    """'Because it failed' is not a reason. The text is the review."""
    thin = sorted(v for v, why in _WAIVED.items() if len(why.strip()) < 40)
    assert not thin, f"waivers without a substantive reason: {thin}"


# ---------------------------------------------------------------------------
# the scanner itself, because a blind scanner passes everything
# ---------------------------------------------------------------------------

def test_it_sees_getenv_and_not_only_environ_get() -> None:
    src = '''
import os
A = os.environ.get("JARVIS_X", "1")
B = os.getenv("JARVIS_X", "2")
'''
    tree = ast.parse(src)
    consts = _module_constants(tree)
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and len(node.args) == 2:
            if _is_env_getter(node.func):
                seen.add(_literal(node.args[1], consts))
    assert seen == {"1", "2"}, seen


def test_it_resolves_a_module_constant_as_the_variable_name() -> None:
    """The shape BOTH fixes introduced. A literal-only scanner would be blind
    in exactly the code written to end this defect."""
    tree = ast.parse('L2_ENABLED_ENV = "JARVIS_L2_ENABLED"\n')
    assert _module_constants(tree)["L2_ENABLED_ENV"] == "JARVIS_L2_ENABLED"


def test_numeric_spellings_are_not_reported_as_conflicts() -> None:
    assert _normalise("120") == _normalise("120.0") == _normalise(" 120 ")


def test_case_only_differences_are_not_conflicts() -> None:
    assert _normalise("TRUE") == _normalise("true")


def test_genuinely_different_defaults_are_still_distinct() -> None:
    assert _normalise("10") != _normalise("15")
    assert _normalise("") != _normalise("true")


def test_the_two_defects_this_pin_exists_for_are_fixed() -> None:
    """Regression, not documentation. If either variable reacquires a second
    default the pin must go red — and these two prove it can."""
    live = conflicts()
    for var in ("JARVIS_GOVERNED_TOOL_MAX_ROUNDS", "JARVIS_L2_ENABLED"):
        assert var not in live, f"{var} has two defaults again: {live[var]}"


def test_an_aliased_import_cannot_hide_a_resolution() -> None:
    """`import os as _o` defeated the first version of this pin.

    A self-test injected a second default through an aliased import and the
    check stayed green — a scanner that reports success without measuring,
    which is the exact defect it exists to catch. Tail-matching closed it, and
    this keeps it closed.
    """
    src = (
        "import os as _o\n"
        "from os import environ, getenv\n"
        'A = _o.environ.get("JARVIS_X", "1")\n'
        'B = environ.get("JARVIS_X", "2")\n'
        'C = getenv("JARVIS_X", "3")\n'
        'D = _o.getenv("JARVIS_X", "4")\n'
    )
    tree = ast.parse(src)
    consts = _module_constants(tree)
    seen = {
        _literal(n.args[1], consts)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and len(n.args) == 2
        and _is_env_getter(n.func)
    }
    assert seen == {"1", "2", "3", "4"}, seen
