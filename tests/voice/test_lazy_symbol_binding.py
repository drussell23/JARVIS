"""A lazily-imported symbol must never be referenced as a bare global.

WHAT THIS CATCHES
-----------------
`speaker_verification_service` defers heavy imports: `_init_ml_components`
imports the audio converters and stores them in the module-level dict
`_audio_converter_funcs`, reachable through `get_audio_converter(name)`. It
deliberately does NOT bind them as module globals — that is the entire point
of deferring the import.

`_convert_audio_for_verification` was nonetheless written against the bare
names. Every conversion strategy in it — quality analysis, async, and the
sync thread-pool fallback — raised `NameError` before converting a byte:

    [VoiceIdentity] unavailable  NameError: name 'AudioConverterConfig'
                                 is not defined

`VoiceIdentity.identify` reports that as UNAVAILABLE and `CapabilityRouter`
reads UNAVAILABLE as "not consent". So speaker verification could not succeed
for any audio, from any caller, since the lazy-loading refactor — and nothing
looked broken, because **a fault on a consent path is indistinguishable from a
refusal unless somebody reads the detail string.** The unit suite was green
throughout; only running the real service against real bytes exposed it.

WHY THE TEST IS STRUCTURAL
---------------------------
Asserting "this one function works" would need the ML stack loaded (~12s) and
would still only cover the four names that happened to be wrong today. The
defect class is "a deferred symbol used as though it were imported", so the
test is written against that class: parse the module and prove no function
reads one of the deferred names out of the global namespace.

A local rebinding is fine and expected — that is exactly the fix
(`x = get_audio_converter('x')`), so a name assigned within the function
before use is not a violation.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE = REPO_ROOT / "backend" / "voice" / "speaker_verification_service.py"

#: The symbols `_init_ml_components` imports into `_audio_converter_funcs`
#: rather than into module globals.
DEFERRED = {
    "prepare_audio_for_stt",
    "prepare_audio_for_stt_async",
    "prepare_audio_with_analysis",
    "AudioConverterConfig",
}


def _bound_locally(fn: ast.AST) -> set:
    """Names the function binds itself: assignment, import, or parameter."""
    bound = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return bound


def _violations():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # `_init_ml_components` is where the deferred import legitimately
        # happens; its own bindings are covered by `_bound_locally`, but name
        # it explicitly so the exemption is a decision rather than an accident.
        if fn.name == "_init_ml_components":
            continue
        local = _bound_locally(fn)
        for node in ast.walk(fn):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id in DEFERRED and node.id not in local):
                out.append((fn.name, node.id, node.lineno))
    return out


def test_no_deferred_symbol_read_from_globals():
    """Reading a deferred symbol as a global is a guaranteed NameError."""
    bad = _violations()
    assert not bad, (
        "These functions read a lazily-imported symbol out of the global "
        "namespace, where it is never bound — each is a NameError at runtime "
        "that surfaces as a consent DENIAL:\n"
        + "\n".join(f"  {fn}() reads {name} at line {line}"
                    for fn, name, line in bad)
        + "\n\nResolve it through the accessor instead:\n"
          "    name = get_audio_converter('name')"
    )


def test_accessor_exists_for_every_deferred_symbol():
    """The fix depends on `get_audio_converter` covering every deferred name.

    If a symbol is added to the lazy import but not to the dict, the accessor
    silently returns None and the caller fails in a new way.
    """
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    missing = DEFERRED - keys
    assert not missing, (
        f"deferred symbols with no entry in _audio_converter_funcs: "
        f"{sorted(missing)} - get_audio_converter() would return None for them"
    )


@pytest.mark.parametrize("name", sorted(DEFERRED))
def test_deferred_symbol_is_actually_importable(name):
    """The converter module really exports what the lazy import promises.

    Guards the other end: a rename in `audio_format_converter` would make the
    deferred import fail at first use, long after the change, in a code path
    whose only visible symptom is a refused unlock.
    """
    import importlib
    mod = importlib.import_module("backend.voice.audio_format_converter")
    assert hasattr(mod, name), (
        f"backend.voice.audio_format_converter has no {name!r}; the lazy "
        f"import in speaker_verification_service would fail at first use"
    )
