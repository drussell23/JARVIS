"""Slice 19a — Pure provider isolation gate: JARVIS_PROVIDER_CLAUDE_DISABLED.

Operator binding (verbatim 2026-05-26):

  "i want to hold off using Claude's API because we know it'll work and
   i want to run the soak with only using DW's API's because i want to
   understand how external API works and etc. does that make sense?
   because DW is the primary"

# Architectural choice

When ``JARVIS_PROVIDER_CLAUDE_DISABLED=true`` AND
``_provider_construction_gate`` is called with ``provider_name="claude"``,
the gate returns False. ClaudeProvider is NEVER constructed.
``self._fallback`` stays None. ``candidate_generator``'s cascade
naturally degrades to ``all_providers_exhausted`` on DW failures
instead of routing to a non-existent fallback.

IMMEDIATE-routed ops (per Manifesto §5: voice_human, IDE test_failure,
runtime_health) FAIL VISIBLY under DW-only mode because §5 specifies
Claude-direct routing for human-reflex ops. Per operator binding:
"if an unrelated process tries to call an IMMEDIATE reflex action
while Claude is intentionally pulled, it must fail visibly to
maintain absolute system observability."

SWE-Bench-Pro ops are unaffected because Slice 10A (PR #58161)
downgrades them to STANDARD route which uses DW primary.

# Slice 19a discipline

* Gate is provider-aware via the new ``provider_name`` kwarg
* Disable knob ONLY honored for ``provider_name="claude"``
* DW gating (and any future provider's gating) ignores it
* Truthy parse uses the canonical frozenset ``{true, 1, yes, on}``
  (case-insensitive). Any other value falls through to legacy
  behavior — defensive fail-closed against accidental "no" / "off"
  being interpreted as disable.

# Test surface (2 AST pins + 6 spine)
"""

from __future__ import annotations

import ast
import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GLS_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "governed_loop_service.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_gate_function_carries_provider_name_kwarg() -> None:
    """``_provider_construction_gate`` MUST carry the ``provider_name``
    kwarg with default empty string (preserves legacy callers)."""
    src = GLS_FILE.read_text()
    tree = ast.parse(src, filename=str(GLS_FILE))
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_provider_construction_gate"
        ):
            kwonly_names = [a.arg for a in node.args.kwonlyargs]
            assert "provider_name" in kwonly_names, (
                "Slice 19a: _provider_construction_gate missing "
                "provider_name kwarg — Claude-specific disable inert"
            )
            # Find the JARVIS_PROVIDER_CLAUDE_DISABLED env read inside
            body_src = ast.unparse(node)
            assert "JARVIS_PROVIDER_CLAUDE_DISABLED" in body_src, (
                "Gate body does NOT consult JARVIS_PROVIDER_CLAUDE_DISABLED "
                "— Slice 19a disable knob inert"
            )
            # ast.unparse normalizes to single quotes; accept either.
            assert (
                'provider_name == "claude"' in body_src
                or "provider_name == 'claude'" in body_src
            ), (
                "Gate body lacks Claude-only guard — disable could "
                "leak to other providers"
            )
            found = True
            break
    assert found, "_provider_construction_gate not found"
    # Slice 19a attribution
    assert "Slice 19a" in src
    assert "JARVIS_PROVIDER_CLAUDE_DISABLED" in src


def test_ast_pin_claude_call_site_passes_provider_name() -> None:
    """The ClaudeProvider construction call site MUST pass
    ``provider_name="claude"`` to ``_provider_construction_gate``.
    Without this, the disable knob never fires (gate doesn't know
    it's gating Claude)."""
    src = GLS_FILE.read_text()
    tree = ast.parse(src, filename=str(GLS_FILE))
    # Find all calls to _provider_construction_gate
    found_claude_call = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_provider_construction_gate"
        ):
            # Inspect kwargs for provider_name="claude"
            for kw in node.keywords:
                if (
                    kw.arg == "provider_name"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "claude"
                ):
                    found_claude_call = True
                    break
            if found_claude_call:
                break
    assert found_claude_call, (
        "No call to _provider_construction_gate with "
        "provider_name=\"claude\" — Slice 19a disable knob orphaned"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 6
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def stubbed_aegis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the aegis client import so gate tests don't depend on
    heavy daemon boot. The fake module has is_enabled() returning False
    by default; tests can monkey-patch it per case."""
    fake_aegis = types.ModuleType("backend.core.ouroboros.aegis.client")
    fake_aegis.is_enabled = lambda: False
    fake_aegis_pkg = types.ModuleType("backend.core.ouroboros.aegis")
    monkeypatch.setitem(sys.modules, "backend.core.ouroboros.aegis", fake_aegis_pkg)
    monkeypatch.setitem(sys.modules, "backend.core.ouroboros.aegis.client", fake_aegis)


def test_spine_claude_with_key_legacy_behavior(
    stubbed_aegis, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the disable env, Claude with API key gates True
    (byte-equivalent to pre-Slice-19a)."""
    from backend.core.ouroboros.governance.governed_loop_service import (
        _provider_construction_gate,
    )
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    assert _provider_construction_gate(
        local_api_key="sk-test", provider_name="claude",
    ) is True


def test_spine_claude_with_disable_env_blocked(
    stubbed_aegis, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With disable env true, Claude gating returns False even with
    API key present — Slice 19a's core contract."""
    from backend.core.ouroboros.governance.governed_loop_service import (
        _provider_construction_gate,
    )
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    assert _provider_construction_gate(
        local_api_key="sk-test", provider_name="claude",
    ) is False


def test_spine_dw_unaffected_by_claude_disable_env(
    stubbed_aegis, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DW provider's gate call IGNORES the Claude-specific disable
    env. DW must still construct normally when its API key is set."""
    from backend.core.ouroboros.governance.governed_loop_service import (
        _provider_construction_gate,
    )
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    # provider_name="doubleword" must IGNORE the Claude env
    assert _provider_construction_gate(
        local_api_key="dw-key", provider_name="doubleword",
    ) is True
    # Even with empty provider_name (legacy callers), Claude env ignored
    assert _provider_construction_gate(
        local_api_key="some-key",
    ) is True


def test_spine_truthy_env_variants_all_disable(
    stubbed_aegis, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All canonical truthy variants disable Claude: true / True / 1 /
    yes / on / ON. Case-insensitive."""
    from backend.core.ouroboros.governance.governed_loop_service import (
        _provider_construction_gate,
    )
    for val in ("true", "True", "TRUE", "1", "yes", "Yes", "on", "ON"):
        monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", val)
        assert _provider_construction_gate(
            local_api_key="sk-test", provider_name="claude",
        ) is False, f"Truthy variant {val!r} failed to disable Claude"


def test_spine_falsy_env_variants_preserve_legacy(
    stubbed_aegis, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falsy/empty/junk env values preserve legacy behavior (Claude
    still constructed). Defensive fail-closed posture against
    accidental 'no'/'off' being interpreted as disable."""
    from backend.core.ouroboros.governance.governed_loop_service import (
        _provider_construction_gate,
    )
    for val in ("false", "False", "0", "no", "off", "", "junk", "maybe"):
        monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", val)
        assert _provider_construction_gate(
            local_api_key="sk-test", provider_name="claude",
        ) is True, (
            f"Falsy/junk variant {val!r} unexpectedly disabled Claude "
            "(should preserve legacy True)"
        )


def test_spine_disable_env_with_aegis_on_still_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 19a disable wins EVEN over Aegis-enabled state. The
    operator's explicit attestation supersedes the daemon-key-held
    inference. (This is the v14 detonation contract.)"""
    # Stub Aegis as enabled (the daemon scenario)
    fake_aegis = types.ModuleType("backend.core.ouroboros.aegis.client")
    fake_aegis.is_enabled = lambda: True
    fake_aegis_pkg = types.ModuleType("backend.core.ouroboros.aegis")
    monkeypatch.setitem(
        sys.modules, "backend.core.ouroboros.aegis", fake_aegis_pkg,
    )
    monkeypatch.setitem(
        sys.modules, "backend.core.ouroboros.aegis.client", fake_aegis,
    )
    from backend.core.ouroboros.governance.governed_loop_service import (
        _provider_construction_gate,
    )
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    # Even with Aegis on (key in daemon) + Claude disable env true:
    # Slice 19a's explicit operator attestation wins
    assert _provider_construction_gate(
        local_api_key="", provider_name="claude",
    ) is False, (
        "Slice 19a disable env did NOT override Aegis-on path — "
        "DW-only soak intent will be violated"
    )
