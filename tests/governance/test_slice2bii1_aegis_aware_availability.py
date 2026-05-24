"""Slice 2B-ii.1 — Aegis-aware provider availability gates.

# What this slice closes

The Aegis Detonation soak ``bt-2026-05-24-222008`` proved 3 of 4
target signals (env scrub, daemon bind, Slice 12AH synthetic noop) but
**Signal #3 (health_probe lease + /v1/messages forwarding) never fired**
because the providers self-disabled at boot:

  1. ``aegis_preflight()`` correctly evicted ANTHROPIC_API_KEY +
     DOUBLEWORD_API_KEY at T+2s.
  2. The harness then imported ``doubleword_provider.py``; the module-
     level ``_DW_API_KEY = os.environ.get("DOUBLEWORD_API_KEY", "")``
     evaluated to ``""``.
  3. ``DoublewordProvider.is_available`` returned ``bool(self._api_key)
     → False``; provider was marked unavailable; ``health_probe()`` was
     never invoked.
  4. Equivalent gate at ``governed_loop_service.py:3259``
     (``if self._config.claude_api_key:``) refused to even *construct*
     the ``ClaudeProvider`` once the env was scrubbed.

This is a Catch-22: Aegis succeeds at confiscating credentials, but the
providers' "available?" predicates only know how to look at the local
environment — they don't know that Aegis HAS the credentials and will
inject them server-side.

# Fix (Slice 2B-ii.1)

Three gates updated to compose ``aegis.client.is_enabled()`` as an
OR-fallback when the local key is absent:

  * ``doubleword_provider.py``: ``is_available`` returns
    ``_aegis_is_enabled() or bool(self._api_key)``.
  * ``governed_loop_service.py:3259``: ``if self._config.claude_api_key
    or _aegis_is_enabled():`` (also coerces ``None → ""`` at the
    ClaudeProvider construction site so ``self._api_key`` is always
    a string).
  * ``governed_loop_service.py:3293``: ``if _dw_api_key or
    _aegis_is_enabled():``.

# Operator binding honored

  * No hardcoding — the gate composes the existing
    ``aegis.client.is_enabled()`` predicate (read at call time so
    the test monkeypatch path works).
  * No silent fallback — when Aegis is enabled but the daemon
    later fails, the provider still raises through the normal
    bridge-failure path (Slice 2B-ii's ``acquire_call_lease()``
    raises AegisClientError). The availability gate only enables
    the provider's existence; runtime lease failures still surface.
  * Single seam — every "is this provider available?" gate flows
    through one composed predicate (no parallel logic).

# Test surface

  * AST pin: every gating site flows through ``is_enabled()``
    OR a local key check (NOT a bare local-key check).
  * Spine: monkeypatch ``aegis.client.is_enabled → True`` +
    ``api_key=""`` → ``DoublewordProvider.is_available is True``.
  * Spine: monkeypatch ``aegis.client.is_enabled → False`` +
    ``api_key=""`` → ``DoublewordProvider.is_available is False``
    (preserves disabled-state behavior).
  * Spine: monkeypatch ``aegis.client.is_enabled → True`` +
    ``api_key="real-key"`` → ``DoublewordProvider.is_available is True``
    (Aegis-enabled doesn't suppress legacy path).
  * Spine: the governed_loop_service gate logic — extracted as a
    pure helper for testability — returns True when either local
    key is present OR Aegis is enabled.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

import pytest

# Modules under construction — imports must work post-implementation
from backend.core.ouroboros.governance import doubleword_provider as dw_mod


# ──────────────────────────────────────────────────────────────────────
# Helpers — AST walkers
# ──────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]
OUROBOROS_PKG = REPO_ROOT / "backend" / "core" / "ouroboros"
DW_FILE = OUROBOROS_PKG / "governance" / "doubleword_provider.py"
GLS_FILE = OUROBOROS_PKG / "governance" / "governed_loop_service.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PIN #1 — DW is_available composes Aegis
# ──────────────────────────────────────────────────────────────────────

def test_ast_pin_dw_is_available_composes_aegis() -> None:
    """The ``DoublewordProvider.is_available`` property body must
    reference ``is_enabled`` (the Aegis predicate) — bare
    ``bool(self._api_key)`` is forbidden.
    """
    tree = _parse(DW_FILE)
    found = False
    body_source = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "is_available":
            found = True
            body_source = ast.unparse(node)
            break
    assert found, "DoublewordProvider.is_available not found"
    assert "is_enabled" in body_source, (
        f"is_available body does not reference is_enabled — Aegis "
        f"fallback missing.\nBody:\n{body_source}"
    )


# ──────────────────────────────────────────────────────────────────────
# AST PIN #2 — governed_loop_service gates compose Aegis
# ──────────────────────────────────────────────────────────────────────

def test_ast_pin_governed_loop_service_gates_compose_aegis() -> None:
    """Both provider-construction gates in governed_loop_service.py
    must compose ``is_enabled`` as an OR-fallback. The original bare
    forms ``if self._config.claude_api_key:`` and ``if _dw_api_key:``
    self-disable under Aegis env scrub.

    Approach: source-level scan for the gate substrings + assert each
    appears in a context where ``is_enabled`` is referenced within a
    short window after the gate.
    """
    src = GLS_FILE.read_text()
    # Find each gating substring and assert is_enabled appears in the
    # same ~3-line window (immediately before/after the if-line).
    lines = src.splitlines()
    gates = (
        "if self._config.claude_api_key",
        "if _dw_api_key",
    )
    offenders: List[str] = []
    for gate in gates:
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if not stripped.startswith(gate):
                continue
            # Search a ~1-line window (the gate line itself; multiline OR
            # forms collapse onto one line).
            window = line
            if "is_enabled" not in window:
                offenders.append(f"line {i+1}: {line.strip()}")
    assert not offenders, (
        "Bare provider-availability gates without Aegis fallback — "
        "providers will self-disable under Aegis env scrub.\n"
        "Offenders:\n  " + "\n  ".join(offenders)
    )


# ──────────────────────────────────────────────────────────────────────
# Spine fixtures
# ──────────────────────────────────────────────────────────────────────

class _DummyProviderState:
    """Minimal stand-in for ClaudeProviderState.fresh() / DW state.
    Avoids dragging the full state-quarantine machinery into a
    pure-availability unit test."""

    def __init__(self) -> None:
        self.total_batches = 0
        self.failed_batches = 0
        self.total_latency_s = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.daily_spend = 0.0
        self.daily_spend_date = None


# ──────────────────────────────────────────────────────────────────────
# SPINE #1 — Aegis enabled + empty api_key → available
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def aegis_env_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set both env vars that ``aegis.client.is_enabled()`` reads
    (the canonical predicate the production code uses). Also lets
    the bridge's ``dw_aegis_base_url()`` resolve cleanly during
    DW constructor — without this, the bridge's defensive empty-URL
    check raises during ``DoublewordProvider()`` instantiation in
    tests that exercise the Aegis-enabled path."""
    monkeypatch.setenv("JARVIS_AEGIS_URL", "http://aegis-test:9999")
    monkeypatch.setenv("JARVIS_AEGIS_BOOTSTRAP_PSK", "test-psk")


@pytest.fixture
def aegis_env_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_AEGIS_URL", raising=False)
    monkeypatch.delenv("JARVIS_AEGIS_BOOTSTRAP_PSK", raising=False)
    monkeypatch.delenv("DOUBLEWORD_BASE_URL", raising=False)


def test_dw_is_available_true_when_aegis_enabled_and_no_local_key(
    aegis_env_enabled: None,
) -> None:
    """When Aegis is enabled (real key confiscated to daemon), the
    provider must report available even with empty local key."""
    # Construct provider with empty key (mimics post-scrub harness state)
    provider = dw_mod.DoublewordProvider(api_key="")
    assert provider.is_available is True, (
        "DoublewordProvider self-disabled under Aegis enabled + empty "
        "local key — health_probe will never fire, Signal #3 unobtainable"
    )


# ──────────────────────────────────────────────────────────────────────
# SPINE #2 — Aegis disabled + empty api_key → unavailable
# ──────────────────────────────────────────────────────────────────────

def test_dw_is_available_false_when_aegis_disabled_and_no_local_key(
    aegis_env_disabled: None,
) -> None:
    """When Aegis is disabled and no local key is present, provider
    must report unavailable (preserves legacy behavior — Aegis isn't
    silently masking a real config error)."""
    provider = dw_mod.DoublewordProvider(api_key="")
    assert provider.is_available is False


# ──────────────────────────────────────────────────────────────────────
# SPINE #3 — Aegis enabled + local key present → available
# ──────────────────────────────────────────────────────────────────────

def test_dw_is_available_true_when_aegis_enabled_and_local_key_present(
    aegis_env_enabled: None,
) -> None:
    """Aegis enabled doesn't suppress legacy keys — the gate is
    purely additive (OR-fallback, never overriding a real key)."""
    provider = dw_mod.DoublewordProvider(api_key="sk-real-dw-key")
    assert provider.is_available is True


# ──────────────────────────────────────────────────────────────────────
# SPINE #4 — Aegis disabled + local key present → available (legacy)
# ──────────────────────────────────────────────────────────────────────

def test_dw_is_available_legacy_path_unchanged(
    aegis_env_disabled: None,
) -> None:
    """The legacy disabled-Aegis + real-key path must produce
    byte-identical behavior."""
    provider = dw_mod.DoublewordProvider(api_key="sk-legacy-dw-key")
    assert provider.is_available is True


# ──────────────────────────────────────────────────────────────────────
# SPINE #5 — governed_loop_service gate helper (extracted for testability)
# ──────────────────────────────────────────────────────────────────────

def test_provider_construction_gate_helper_aegis_enabled_no_key(
    aegis_env_enabled: None,
) -> None:
    """The helper that decides whether to construct ClaudeProvider /
    DoublewordProvider in governed_loop_service.py must compose Aegis
    in the same shape as DW.is_available — for symmetry + AST pinning.
    """
    from backend.core.ouroboros.governance import governed_loop_service as gls_mod

    helper = getattr(gls_mod, "_provider_construction_gate", None)
    assert helper is not None, (
        "governed_loop_service._provider_construction_gate helper "
        "missing — gate logic must be extracted as a pure callable "
        "for AST pinning + unit testing (avoid bare inline checks at "
        "the construction sites)."
    )
    # Empty/None local key + Aegis enabled → True (provider should be built)
    assert helper(local_api_key="") is True
    assert helper(local_api_key=None) is True


def test_provider_construction_gate_helper_aegis_disabled_no_key(
    aegis_env_disabled: None,
) -> None:
    """Without Aegis + without local key, helper returns False."""
    from backend.core.ouroboros.governance import governed_loop_service as gls_mod

    helper = gls_mod._provider_construction_gate
    assert helper(local_api_key="") is False
    assert helper(local_api_key=None) is False


def test_provider_construction_gate_helper_legacy_path(
    aegis_env_disabled: None,
) -> None:
    """Local key present + Aegis disabled = legacy path; gate True."""
    from backend.core.ouroboros.governance import governed_loop_service as gls_mod

    helper = gls_mod._provider_construction_gate
    assert helper(local_api_key="sk-real-key") is True


# ──────────────────────────────────────────────────────────────────────
# SPINE #6 — ClaudeProvider tolerates empty api_key under Aegis
# ──────────────────────────────────────────────────────────────────────

def test_claude_provider_tolerates_empty_api_key_under_aegis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClaudeProvider must accept api_key="" without raising at
    construction (so the construction-site coercion None → "" is safe).
    The actual credential injection happens server-side via Aegis."""
    from backend.core.ouroboros.governance.providers import ClaudeProvider

    # Construct with empty key — must not raise. The bridge handles
    # the actual transport when _ensure_client is called.
    provider = ClaudeProvider(api_key="")
    assert provider is not None
    assert getattr(provider, "_api_key", None) == ""
