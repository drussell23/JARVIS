"""
Task #96 spine — H1 falsification gate (http client mode).

v14-rev13 graduation soak (PRD §40.7.10-stage1.6-slice3-v14rev13) proved
D2 budget invariant holds (no 12× overshoot) but surfaced a new top
line: 4 stream terminations with ``first_token=NEVER bytes_received=0``
on ``thinking=on`` streams, all root-caused (via asyncio.leak stack
trace) to ``httpcore.ConnectTimeout`` inside ``messages.stream()``'s TCP
connect.

Step 2 isolation probe (operator-approved 2026-05-14) proved this is
NOT product behavior — ``AsyncAnthropic()`` defaults from the same
shell env stream the first event in 1.08–1.34s consistently.  The
harness differs from the probe on three axes (custom http_client,
Limits, concurrency).

This module gates the H1 falsification experiment via
``JARVIS_CLAUDE_HTTP_CLIENT_MODE``.  Default ``custom`` preserves
production byte-identically.  ``stdlib_default`` drops the custom
http_client kwarg — reproducing the Step 2 probe's client shape
exactly — while keeping ``max_retries=0`` (D2 invariant) and the D2
per-request timeout override.  One soak (v14-rev14) flips to
``stdlib_default`` for measurement; the decision tree afterwards
forward-ports what we actually need rather than leaving "no custom
client" as the permanent story.

This spine pins:

  * Closed 2-value taxonomy (``custom`` and ``stdlib_default``).
  * Resolver returns ``custom`` for unknown / empty / unset values
    (operator binding: no behavior change without explicit measurement).
  * Both code branches present in ``_ensure_client`` (AST scan).
  * Both branches preserve ``max_retries=0`` — D2 invariant.
  * ``stdlib_default`` branch does NOT construct a custom http_client.
  * FlagRegistry seed present with default ``custom`` + Category.SAFETY
    (operator binding: protected behavior gate, not casual tuning).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_PROVIDERS_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "providers.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "flag_registry_seed.py"
)


# ---------------------------------------------------------------------------
# Closed taxonomy + resolver behavior
# ---------------------------------------------------------------------------


def _import_resolver():
    from backend.core.ouroboros.governance.providers import (
        _resolve_http_client_mode,
    )
    return _resolve_http_client_mode


def _import_constants():
    from backend.core.ouroboros.governance.providers import (
        _CLAUDE_HTTP_CLIENT_MODE_CUSTOM,
        _CLAUDE_HTTP_CLIENT_MODE_STDLIB_DEFAULT,
        _CLAUDE_HTTP_CLIENT_MODES,
    )
    return (
        _CLAUDE_HTTP_CLIENT_MODE_CUSTOM,
        _CLAUDE_HTTP_CLIENT_MODE_STDLIB_DEFAULT,
        _CLAUDE_HTTP_CLIENT_MODES,
    )


def test_taxonomy_is_closed_two_values():
    """Closed taxonomy frozen at exactly two members.  Adding new modes
    requires updating this pin + the dispatch branch in
    ``_ensure_client`` together — prevents accidental third-mode
    feature creep without explicit operator binding."""
    custom, stdlib, modes = _import_constants()
    assert custom == "custom"
    assert stdlib == "stdlib_default"
    assert isinstance(modes, frozenset), (
        "Closed taxonomy MUST be a frozenset (immutable); "
        "list/set could be mutated at runtime"
    )
    assert modes == {"custom", "stdlib_default"}, (
        "Taxonomy is intentionally closed at 2 values.  Adding new "
        "modes requires updating spine + dispatch together."
    )


@pytest.mark.parametrize("env_value,expected", [
    # Defaults
    ("custom", "custom"),
    ("stdlib_default", "stdlib_default"),
    # Case insensitive
    ("CUSTOM", "custom"),
    ("Stdlib_Default", "stdlib_default"),
    # Whitespace stripped
    ("  custom  ", "custom"),
    ("\tstdlib_default\n", "stdlib_default"),
    # Unknown → fallback to custom (operator binding: no silent
    # behavior change without explicit measurement)
    ("typo_mode", "custom"),
    ("", "custom"),
    ("custom_extra", "custom"),
    ("anthropic_default", "custom"),  # plausible typo → safe fallback
])
def test_resolver_decision_table(env_value, expected, monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUDE_HTTP_CLIENT_MODE", env_value)
    fn = _import_resolver()
    assert fn() == expected, (
        f"Resolver mismatch: env={env_value!r} → expected={expected!r} "
        f"got={fn()!r}"
    )


def test_resolver_defaults_to_custom_when_env_unset(monkeypatch):
    """Operator binding: unset env MUST default to ``custom``
    (production-preserving)."""
    monkeypatch.delenv("JARVIS_CLAUDE_HTTP_CLIENT_MODE", raising=False)
    fn = _import_resolver()
    assert fn() == "custom"


# ---------------------------------------------------------------------------
# AST pins — both branches present in _ensure_client
# ---------------------------------------------------------------------------


def test_ast_pin_ensure_client_dispatches_on_mode():
    """``_ensure_client`` MUST consult ``_resolve_http_client_mode`` and
    dispatch on the result."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    # The call + dispatch is the chokepoint — pin its exact shape
    assert "_client_mode = _resolve_http_client_mode()" in src, (
        "_ensure_client MUST call _resolve_http_client_mode() and "
        "store in _client_mode for branch dispatch"
    )
    assert (
        "if _client_mode == _CLAUDE_HTTP_CLIENT_MODE_STDLIB_DEFAULT:"
        in src
    ), (
        "_ensure_client MUST dispatch on the stdlib_default constant "
        "(not a string literal — single source of truth)"
    )


def test_ast_pin_stdlib_default_drops_custom_http_client():
    """The ``stdlib_default`` branch MUST construct AsyncAnthropic
    WITHOUT the ``http_client=`` kwarg (reproducing the probe shape)
    AND must preserve ``max_retries=0`` (D2 invariant)."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    # Locate the stdlib_default branch by its master comment
    branch_marker = "H1 measurement mode"
    idx = src.find(branch_marker)
    assert idx > 0, (
        "stdlib_default branch MUST be documented with 'H1 "
        "measurement mode' comment for grep-discoverability"
    )
    # Within ~1500 chars of the marker (the branch body)
    branch_body = src[idx:idx + 1500]
    # MUST construct AsyncAnthropic
    assert "anthropic.AsyncAnthropic(" in branch_body
    # MUST preserve max_retries=0 (D2 invariant)
    assert "max_retries=0" in branch_body, (
        "stdlib_default branch MUST preserve max_retries=0 — "
        "D2 invariant: single retry authority is _call_with_backoff"
    )
    # MUST NOT pass http_client= in this branch
    # Find the AsyncAnthropic call inside this branch and verify
    # http_client is absent
    _async_call_idx = branch_body.find("anthropic.AsyncAnthropic(")
    _call_segment = branch_body[_async_call_idx:_async_call_idx + 400]
    assert "http_client=" not in _call_segment, (
        "stdlib_default's AsyncAnthropic construction MUST NOT pass "
        "http_client= (reproduces Step 2 probe shape exactly)"
    )


def test_ast_pin_custom_branch_still_constructs_http_client():
    """The default ``custom`` branch MUST still construct the custom
    ``httpx.AsyncClient`` + Limits — production preservation pin."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    # Look for the custom branch's httpx.AsyncClient construction
    assert "httpx.AsyncClient(" in src
    assert "httpx.Limits(" in src
    # The custom branch's AsyncAnthropic call MUST pass http_client=
    assert "http_client=_http_client," in src, (
        "Default 'custom' branch MUST keep http_client= kwarg — "
        "production byte-identical preservation"
    )


def test_ast_pin_both_branches_max_retries_zero():
    """LOAD-BEARING D2 invariant: BOTH branches keep
    ``max_retries=0``.  Without this, the stdlib_default mode would
    accidentally enable SDK internal retries (defaults to 2) — would
    silently re-introduce the pre-D2 retry-stack-on-top-of-outer-budget
    pathology."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    # Count max_retries=0 occurrences — MUST be at least 2 inside the
    # _ensure_client method (one per branch)
    # Use a structural check: walk AST and count Call nodes with
    # max_retries=0 keyword inside _ensure_client.
    tree = ast.parse(src)
    ensure_client_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_ensure_client":
            ensure_client_fn = node
            break
    assert ensure_client_fn is not None
    n_max_retries_zero = 0
    for sub in ast.walk(ensure_client_fn):
        if isinstance(sub, ast.Call):
            for kw in sub.keywords:
                if (
                    kw.arg == "max_retries"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == 0
                ):
                    n_max_retries_zero += 1
    assert n_max_retries_zero == 2, (
        f"D2 invariant: BOTH branches in _ensure_client MUST pass "
        f"max_retries=0 to AsyncAnthropic.  Found {n_max_retries_zero} "
        f"max_retries=0 keyword args; expected 2 (one per branch)."
    )


def test_ast_pin_resolver_is_module_level():
    """``_resolve_http_client_mode`` MUST be a module-level function
    (importable for the spine + AST scanning)."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = [
        n.name for n in tree.body
        if isinstance(n, ast.FunctionDef)
    ]
    assert "_resolve_http_client_mode" in funcs, (
        "_resolve_http_client_mode MUST be module-level "
        "(spine importability + AST discoverability)"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


def test_seed_has_http_client_mode_flag():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_CLAUDE_HTTP_CLIENT_MODE" in src
    idx = src.find("JARVIS_CLAUDE_HTTP_CLIENT_MODE")
    window = src[idx:idx + 2000]
    assert 'default="custom"' in window, (
        "Default MUST be 'custom' per operator binding "
        "(no behavior change for production without measurement)"
    )
    # Category.SAFETY because this gates the production transport
    # configuration — not a casual tuning knob
    assert "Category.SAFETY" in window, (
        "Cap is a protected behavior gate, not casual TUNING — "
        "operator binding: experimental measurement gate"
    )
    assert "providers.py" in window, (
        "Source file MUST point at providers.py"
    )


# ---------------------------------------------------------------------------
# Behavioral guarantee — production byte-identity when env unset
# ---------------------------------------------------------------------------


def test_resolver_default_preserves_production_when_unset(monkeypatch):
    """When ``JARVIS_CLAUDE_HTTP_CLIENT_MODE`` is unset (the production
    default state), the resolver MUST return ``custom`` so the existing
    production path runs.  This is the load-bearing
    backward-compatibility invariant."""
    monkeypatch.delenv("JARVIS_CLAUDE_HTTP_CLIENT_MODE", raising=False)
    fn = _import_resolver()
    custom, _, _ = _import_constants()
    assert fn() == custom


def test_resolver_invalid_falls_back_to_production(monkeypatch):
    """Invalid env values MUST NOT silently flip to stdlib_default —
    must return ``custom``.  Without this, a typo or downstream env-
    composition bug could swap production transport without operator
    knowledge."""
    custom, _, _ = _import_constants()
    fn = _import_resolver()
    for bad in ["nonsense", "True", "0", "1", "yes", "no", "stdlib"]:
        monkeypatch.setenv("JARVIS_CLAUDE_HTTP_CLIENT_MODE", bad)
        assert fn() == custom, (
            f"Invalid value {bad!r} MUST fall back to 'custom' "
            f"(no silent transport swap)"
        )
