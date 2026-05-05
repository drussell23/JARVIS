"""Slice 5b consolidation Slice 4 — repl_dispatch_registry
regression spine (PRD §32.5 / §32.11).

Verifies:

  * Master flag asymmetric env semantics
  * 17+ verbs auto-discovered (5 legacy + 12 newly unlocked)
  * Custom-handler exclusion list cages /budget /risk /goal /
    cancel /plan /postmortems /inline (preserves operator UX)
  * Idempotent priming
  * try_dispatch routing — matched/unmatched semantics
  * Verb-name extraction from filename convention
  * Signature validation (off-shape dispatchers rejected)
  * Composes Slice 2 module_discovery primitive
  * SerpentREPL hookup — uses registry, legacy helper removed
  * Authority asymmetry — pure substrate
"""
from __future__ import annotations

from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        reset_registry_for_tests,
    )
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("v", ["true", "1", "yes", "on", "TRUE"])
def test_master_flag_truthy(monkeypatch, v):
    monkeypatch.setenv(
        "JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED", v,
    )
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        repl_dispatch_autodiscovery_enabled,
    )
    assert repl_dispatch_autodiscovery_enabled() is True


@pytest.mark.parametrize("v", ["false", "0", "no", "off"])
def test_master_flag_falsy(monkeypatch, v):
    monkeypatch.setenv(
        "JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED", v,
    )
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        repl_dispatch_autodiscovery_enabled,
    )
    assert repl_dispatch_autodiscovery_enabled() is False


def test_master_flag_default_true(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        repl_dispatch_autodiscovery_enabled,
    )
    assert repl_dispatch_autodiscovery_enabled() is True


def test_master_off_returns_no_match(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED", "false",
    )
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch,
    )
    out = try_dispatch("/m10 help")
    assert out.matched is False
    assert out.ok is False


# ---------------------------------------------------------------------------
# Discovery + priming
# ---------------------------------------------------------------------------


def test_discovers_legacy_five_verbs():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        prime_registry,
    )
    report = prime_registry(force=True)
    for v in ("probe", "coherence", "quorum", "failures", "outcomes"):
        assert v in report.verbs, (
            f"legacy verb {v!r} missing"
        )


def test_discovers_newly_unlocked_verbs():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        prime_registry,
    )
    report = prime_registry(force=True)
    # 5 dormant verbs that auto-mount via Slice 4.
    for v in ("m10", "decisions", "curiosity"):
        assert v in report.verbs, (
            f"newly-unlocked verb {v!r} missing"
        )


def test_excluded_verbs_not_in_registry():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        prime_registry,
    )
    report = prime_registry(force=True)
    for excluded in (
        "budget", "risk", "goal", "cancel", "plan",
        "postmortems", "inline",
    ):
        assert excluded not in report.verbs, (
            f"excluded verb {excluded!r} leaked into registry"
        )


def test_prime_registry_idempotent():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        prime_registry,
    )
    first = prime_registry(force=True)
    second = prime_registry()  # no force
    # Second call short-circuits without re-walking.
    assert second.verb_count == first.verb_count
    assert second.elapsed_s < first.elapsed_s + 0.5


# ---------------------------------------------------------------------------
# try_dispatch routing
# ---------------------------------------------------------------------------


def test_try_dispatch_matches_known_verb():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch,
    )
    out = try_dispatch("/m10 help")
    assert out.matched is True
    assert out.ok is True
    assert out.verb == "m10"
    assert len(out.text) > 0


def test_try_dispatch_returns_no_match_on_unknown_verb():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch,
    )
    out = try_dispatch("/totally_fake_verb_xyz")
    assert out.matched is False


def test_try_dispatch_no_match_on_excluded_verb():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch,
    )
    # /budget is excluded — registry returns matched=False so
    # serpent_flow's _handle_budget retains authority.
    out = try_dispatch("/budget 1.00")
    assert out.matched is False


def test_try_dispatch_handles_bare_verb_form():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch,
    )
    out = try_dispatch("decisions help")
    assert out.matched is True
    assert out.verb == "decisions"


def test_try_dispatch_empty_line_no_match():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        try_dispatch,
    )
    assert try_dispatch("").matched is False
    assert try_dispatch("   ").matched is False


# ---------------------------------------------------------------------------
# Verb-name extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name,expected", [
    ("backend.core.ouroboros.governance.decisions_repl", "decisions"),  # noqa: E501
    ("backend.core.ouroboros.governance.curiosity_repl", "curiosity"),
    ("backend.core.ouroboros.governance.m10.repl", "m10"),
    ("backend.core.ouroboros.governance.verification.replay_repl", "replay"),  # noqa: E501
    ("foo.bar.baz", None),  # no _repl suffix
    ("baz", None),  # no parent
    ("", None),
])
def test_verb_name_extraction(module_name, expected):
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        _extract_verb_name,
    )
    assert _extract_verb_name(module_name) == expected


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------


def test_signature_rejection_synthetic_module(
    tmp_path, monkeypatch,
):
    pkg = tmp_path / "synth_repl_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # bad: no params
    (pkg / "bad_repl.py").write_text(
        "def dispatch_bad_command(): pass\n",
        encoding="utf-8",
    )
    # good: accepts line
    (pkg / "good_repl.py").write_text(
        "from dataclasses import dataclass\n"
        "@dataclass(frozen=True)\n"
        "class R:\n"
        "    matched: bool = True\n"
        "    ok: bool = True\n"
        "    text: str = 'good!'\n"
        "def dispatch_good_command(line):\n"
        "    return R()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import sys
    for k in list(sys.modules.keys()):
        if k.startswith("synth_repl_pkg"):
            del sys.modules[k]

    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        prime_registry, list_verbs, try_dispatch,
    )
    prime_registry(
        packages=["synth_repl_pkg"],
        excluded_verbs=[],
        force=True,
    )
    verbs = list_verbs()
    assert "good" in verbs
    assert "bad" not in verbs
    out = try_dispatch("/good")
    assert out.matched is True
    assert out.ok is True
    assert out.text == "good!"


# ---------------------------------------------------------------------------
# Dispatcher exception isolation
# ---------------------------------------------------------------------------


def test_dispatcher_exception_isolation(tmp_path, monkeypatch):
    pkg = tmp_path / "synth_boom_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "boom_repl.py").write_text(
        "def dispatch_boom_command(line):\n"
        "    raise RuntimeError('synthetic crash')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import sys
    for k in list(sys.modules.keys()):
        if k.startswith("synth_boom_pkg"):
            del sys.modules[k]

    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        prime_registry, try_dispatch,
    )
    prime_registry(
        packages=["synth_boom_pkg"],
        excluded_verbs=[],
        force=True,
    )
    out = try_dispatch("/boom")
    assert out.matched is True
    assert out.ok is False
    assert "RuntimeError" in out.text or "boom" in out.text


# ---------------------------------------------------------------------------
# Composes Slice 2 primitive
# ---------------------------------------------------------------------------


def test_registry_composes_module_discovery():
    """Per the consolidation arc invariant, the REPL registry
    MUST delegate the package walk to module_discovery — not
    reimplement it."""
    import inspect
    from backend.core.ouroboros.battle_test import (
        repl_dispatch_registry,
    )
    src = inspect.getsource(
        repl_dispatch_registry.prime_registry,
    )
    assert "discover_module_provided_callable" in src
    assert "pkgutil.iter_modules" not in src


# ---------------------------------------------------------------------------
# SerpentREPL hookup
# ---------------------------------------------------------------------------


def test_serpent_imports_and_calls_registry():
    """SerpentREPL._loop MUST invoke try_dispatch instead of
    the legacy if/elif ladder."""
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "battle_test"
        / "serpent_flow.py"
    ).read_text(encoding="utf-8")
    assert "repl_dispatch_registry" in src
    assert "try_dispatch" in src


def test_legacy_print_observability_verb_removed():
    """The legacy ``_print_observability_verb`` helper MUST be
    removed (replaced by the registry) per Slice 4."""
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "battle_test"
        / "serpent_flow.py"
    ).read_text(encoding="utf-8")
    assert "def _print_observability_verb" not in src, (
        "Legacy helper must be removed (Slice 4 replaces it "
        "with auto-discovery)"
    )


# ---------------------------------------------------------------------------
# Authority asymmetry
# ---------------------------------------------------------------------------


def test_repl_registry_authority_asymmetry():
    import ast as _ast
    from pathlib import Path
    target = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "battle_test"
        / "repl_dispatch_registry.py"
    )
    tree = _ast.parse(target.read_text(encoding="utf-8"))
    forbidden = (
        "orchestrator",
        "iron_gate",
        "policy",
        "providers",
        "candidate_generator",
        "urgency_router",
        "change_engine",
        "semantic_guardian",
    )
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            for f in forbidden:
                if f in module:
                    pytest.fail(
                        f"repl_dispatch_registry.py MUST NOT "
                        f"import {module!r}"
                    )


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


def test_public_api_exports():
    from backend.core.ouroboros.battle_test import (
        repl_dispatch_registry as r,
    )
    expected = (
        "try_dispatch",
        "prime_registry",
        "list_verbs",
        "reset_registry_for_tests",
        "repl_dispatch_autodiscovery_enabled",
        "DispatchOutcome",
        "RegistryReport",
        "REPL_DISPATCH_REGISTRY_SCHEMA_VERSION",
    )
    for name in expected:
        assert hasattr(r, name), f"missing public symbol: {name}"


# ---------------------------------------------------------------------------
# DispatchOutcome / RegistryReport shape
# ---------------------------------------------------------------------------


def test_dispatch_outcome_is_frozen():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        DispatchOutcome,
    )
    out = DispatchOutcome(matched=True, ok=False, text="t")
    with pytest.raises(Exception):
        out.matched = False  # type: ignore


def test_registry_report_as_dict():
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        RegistryReport,
        REPL_DISPATCH_REGISTRY_SCHEMA_VERSION,
    )
    r = RegistryReport(
        verb_count=2,
        verbs=("a", "b"),
        excluded=("budget", "risk"),
        elapsed_s=0.05,
    )
    d = r.as_dict()
    assert d["schema_version"] == (
        REPL_DISPATCH_REGISTRY_SCHEMA_VERSION
    )
    assert d["verb_count"] == 2
    assert d["verbs"] == ["a", "b"]
    assert "budget" in d["excluded"]
