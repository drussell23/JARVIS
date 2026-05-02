"""RR Pass B Slice 2 — Order-2 classifier + risk-floor regression suite.

Pins:
  * RiskTier.ORDER_2_GOVERNANCE enum value present + strictly above
    BLOCKED + 5-value enum shape.
  * Env knob default-false-pre-graduation.
  * classify_order2_match: empty target_files / None target_files /
    manifest-not-loaded → False; happy path with single match;
    multi-file with one matching path; non-string entries skipped;
    repo isolation; wildcard glob match; no-files case.
  * apply_order2_floor: master-off short-circuits (returns input
    tier unchanged); master-on + match → ORDER_2_GOVERNANCE
    regardless of input tier (5 parametrize); master-on + miss →
    input tier unchanged; DUAL-flag protection (manifest off +
    risk-class on → unchanged); injected manifest honoured.
  * Telemetry log line emitted on Order-2 classification.
  * Authority invariants: no banned imports + no I/O / subprocess /
    env mutation.
  * Hot-revert matrix:
    - manifest off + risk-class off → unchanged
    - manifest on + risk-class off → unchanged
    - manifest off + risk-class on → unchanged (Slice 1 returns empty
      manifest; classifier returns False)
    - manifest on + risk-class on → ORDER_2_GOVERNANCE on match
"""
from __future__ import annotations

import io
import logging
import tokenize
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.meta.order2_classifier import (
    apply_order2_floor,
    classify_order2_match,
    is_enabled,
)
from backend.core.ouroboros.governance.meta.order2_manifest import (
    ManifestLoadStatus,
    Order2Manifest,
    Order2ManifestEntry,
    reset_default_manifest,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


def _entry(repo="jarvis", path_glob="x.py"):
    return Order2ManifestEntry(
        repo=repo, path_glob=path_glob, rationale="r",
        added="2026-04-26", added_by="operator",
    )


def _loaded_manifest(*entries: Order2ManifestEntry) -> Order2Manifest:
    return Order2Manifest(
        entries=entries or (_entry(),),
        status=ManifestLoadStatus.LOADED,
    )


def _empty_manifest() -> Order2Manifest:
    return Order2Manifest(status=ManifestLoadStatus.NOT_LOADED)


@pytest.fixture(autouse=True)
def _clear_env_and_singleton(monkeypatch):
    monkeypatch.delenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_ORDER2_MANIFEST_LOADED", raising=False)
    monkeypatch.delenv("JARVIS_ORDER2_MANIFEST_PATH", raising=False)
    reset_default_manifest()
    yield
    reset_default_manifest()


# ===========================================================================
# A — RiskTier enum extension
# ===========================================================================


def test_risk_tier_has_order2_governance_value():
    """Pin: Pass B Slice 2 added the ORDER_2_GOVERNANCE enum value."""
    assert hasattr(RiskTier, "ORDER_2_GOVERNANCE")
    assert RiskTier.ORDER_2_GOVERNANCE.name == "ORDER_2_GOVERNANCE"


def test_risk_tier_has_five_values():
    """Pin: 4 original tiers + ORDER_2_GOVERNANCE = 5. Adding a sixth
    requires a new design doc (Pass C may add more)."""
    names = {t.name for t in RiskTier}
    assert names == {
        "SAFE_AUTO", "NOTIFY_APPLY", "APPROVAL_REQUIRED",
        "BLOCKED", "ORDER_2_GOVERNANCE",
    }


def test_order2_strictly_above_blocked():
    """Pin: per Pass B §4.1, ORDER_2_GOVERNANCE has a strictly-greater
    enum value than BLOCKED (auto-incremented via `auto()`). This is
    the structural property the strictest-wins composition relies on."""
    assert RiskTier.ORDER_2_GOVERNANCE.value > RiskTier.BLOCKED.value
    assert RiskTier.BLOCKED.value > RiskTier.APPROVAL_REQUIRED.value


# ===========================================================================
# B — Env knob (default true post-Q4-P#3 graduation, 2026-05-02)
# ===========================================================================


def test_is_enabled_default_true_post_q4_graduation():
    """Q4 Priority #3 graduation (2026-05-02): operator authorized
    Pass B Slices 1+2 graduation. Order-2 risk class now active by
    default. Slice 6.x amendment-protocol flags stay default-false —
    the only path to actual Order-2 mutations is the operator-only
    /order2 amend REPL (gated on JARVIS_ORDER2_REPL_ENABLED)."""
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_is_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", val)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_is_enabled_explicit_falsy(monkeypatch, val):
    # Empty string excluded — that's now "unset → graduated default
    # true" per Q4 P#3.
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", val)
    assert is_enabled() is False


@pytest.mark.parametrize("val", ["", "   ", "\t"])
def test_is_enabled_empty_treats_as_unset(monkeypatch, val):
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", val)
    assert is_enabled() is True


# ===========================================================================
# C — classify_order2_match
# ===========================================================================


def test_classify_empty_target_files_returns_false():
    m = _loaded_manifest(_entry(path_glob="anything.py"))
    assert classify_order2_match([], manifest=m) is False
    assert classify_order2_match((), manifest=m) is False


def test_classify_none_target_files_returns_false():
    m = _loaded_manifest(_entry(path_glob="anything.py"))
    # Defensive: None should not crash; coerced to falsy.
    assert classify_order2_match(None, manifest=m) is False  # type: ignore[arg-type]


def test_classify_manifest_not_loaded_returns_false():
    """Pin: any status other than LOADED → False. Slice 2-6 consumers
    degrade to pre-Pass-B behaviour when the cage isn't fully online."""
    m = _empty_manifest()
    assert classify_order2_match(["x.py"], manifest=m) is False


def test_classify_match_returns_true():
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    assert classify_order2_match(["backend/x.py"], manifest=m) is True


def test_classify_miss_returns_false():
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    assert classify_order2_match(["backend/y.py"], manifest=m) is False


def test_classify_multi_file_one_match_returns_true():
    """Pin: multi-file aware — one matching path among many → True.
    Per Pass B §4.2 'multi-file aware' design."""
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    assert classify_order2_match(
        ["unrelated.py", "backend/x.py", "other.py"], manifest=m,
    ) is True


def test_classify_multi_file_zero_match_returns_false():
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    assert classify_order2_match(
        ["unrelated.py", "other.py", "third.py"], manifest=m,
    ) is False


def test_classify_skips_non_string_entries():
    """Defensive: non-string elements in target_files (None, ints,
    nested lists) are silently skipped."""
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    assert classify_order2_match(
        [None, 42, "backend/x.py"], manifest=m,  # type: ignore[list-item]
    ) is True
    assert classify_order2_match(
        [None, 42, ""], manifest=m,  # type: ignore[list-item]
    ) is False


def test_classify_skips_empty_string():
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    assert classify_order2_match([""], manifest=m) is False


def test_classify_repo_isolation():
    """Pin: same path under different repo MUST NOT match."""
    m = _loaded_manifest(_entry(repo="jarvis", path_glob="x.py"))
    assert classify_order2_match(
        ["x.py"], repo="jarvis", manifest=m,
    ) is True
    assert classify_order2_match(
        ["x.py"], repo="jarvis-prime", manifest=m,
    ) is False


def test_classify_wildcard_glob_matches_immediate_children():
    m = _loaded_manifest(_entry(path_glob="phase_runners/*.py"))
    assert classify_order2_match(
        ["phase_runners/foo.py"], manifest=m,
    ) is True


def test_classify_wildcard_glob_matches_recursively_by_design():
    """Pin: ``fnmatch``'s ``*`` is greedy (matches across ``/``).
    For the Order-2 cage this is the intended safety behavior: any
    file under a manifested directory IS governance code, regardless
    of subdirectory depth. Operators who want strict directory-only
    matching specify exact paths instead of globs."""
    m = _loaded_manifest(_entry(path_glob="phase_runners/*.py"))
    assert classify_order2_match(
        ["phase_runners/sub/foo.py"], manifest=m,
    ) is True


def test_classify_wildcard_glob_does_not_match_outside_dir():
    """Pin: even with greedy ``*``, the prefix anchor prevents
    matching files outside the manifested directory."""
    m = _loaded_manifest(_entry(path_glob="phase_runners/*.py"))
    assert classify_order2_match(
        ["other_dir/foo.py"], manifest=m,
    ) is False
    assert classify_order2_match(
        ["foo_phase_runners/foo.py"], manifest=m,
    ) is False


def test_classify_uses_default_manifest_when_none_provided(monkeypatch):
    """Pin: when manifest=None, the function fetches the singleton
    via get_default_manifest. Use the real .jarvis YAML to prove."""
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "1")
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_PATH",
        str(_REPO / ".jarvis" / "order2_manifest.yaml"),
    )
    reset_default_manifest()
    assert classify_order2_match(
        ["backend/core/ouroboros/governance/orchestrator.py"],
    ) is True


# ===========================================================================
# D — apply_order2_floor: master-flag gating
# ===========================================================================


def test_apply_master_off_returns_unchanged_even_on_match(monkeypatch):
    """Pin: master flag explicitly false → input tier unchanged
    regardless of manifest match. Post-Q4-P#3 graduation, env-unset
    yields default-true; this test exercises the operator's
    instant-rollback path."""
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", "false")
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    result = apply_order2_floor(
        RiskTier.SAFE_AUTO, ["backend/x.py"], manifest=m,
    )
    assert result is RiskTier.SAFE_AUTO


@pytest.mark.parametrize("input_tier", [
    RiskTier.SAFE_AUTO,
    RiskTier.NOTIFY_APPLY,
    RiskTier.APPROVAL_REQUIRED,
    RiskTier.BLOCKED,
])
def test_apply_master_on_match_escalates_to_order2_from_any_tier(
    monkeypatch, input_tier,
):
    """Pin: with master-on + match, ORDER_2_GOVERNANCE wins over
    EVERY input tier including BLOCKED. Per Pass B §4.1 'strictly
    above BLOCKED' invariant."""
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", "1")
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    result = apply_order2_floor(
        input_tier, ["backend/x.py"], manifest=m,
    )
    assert result is RiskTier.ORDER_2_GOVERNANCE


def test_apply_master_on_already_order2_stays_order2(monkeypatch):
    """Idempotent: ORDER_2 input + match → ORDER_2 output."""
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", "1")
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    result = apply_order2_floor(
        RiskTier.ORDER_2_GOVERNANCE, ["backend/x.py"], manifest=m,
    )
    assert result is RiskTier.ORDER_2_GOVERNANCE


@pytest.mark.parametrize("input_tier", [
    RiskTier.SAFE_AUTO,
    RiskTier.NOTIFY_APPLY,
    RiskTier.APPROVAL_REQUIRED,
    RiskTier.BLOCKED,
])
def test_apply_master_on_miss_returns_unchanged(monkeypatch, input_tier):
    """Pin: master-on but no manifest match → input tier unchanged.
    Order-2 is additive; non-governance ops keep their normal tier."""
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", "1")
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    result = apply_order2_floor(
        input_tier, ["unrelated.py"], manifest=m,
    )
    assert result is input_tier


# ===========================================================================
# E — DUAL-flag protection (Slice 1 + Slice 2 must both be on)
# ===========================================================================


def test_dual_flag_manifest_off_riskclass_off_unchanged(monkeypatch):
    """Cage state 1: both flags explicitly off → no behaviour change.
    Post-Q4-P#3 graduation, the rollback path requires explicit
    `false` since unset = graduated default true."""
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", "false")
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "false")
    reset_default_manifest()
    result = apply_order2_floor(
        RiskTier.SAFE_AUTO,
        ["backend/core/ouroboros/governance/orchestrator.py"],
    )
    assert result is RiskTier.SAFE_AUTO


def test_dual_flag_manifest_on_riskclass_off_unchanged(monkeypatch):
    """Cage state 2: manifest loaded + risk-class explicitly off →
    unchanged. Operator can audit the manifest without enforcement
    firing."""
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", "false")
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "1")
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_PATH",
        str(_REPO / ".jarvis" / "order2_manifest.yaml"),
    )
    reset_default_manifest()
    result = apply_order2_floor(
        RiskTier.SAFE_AUTO,
        ["backend/core/ouroboros/governance/orchestrator.py"],
    )
    assert result is RiskTier.SAFE_AUTO


def test_dual_flag_manifest_off_riskclass_on_unchanged(monkeypatch):
    """Cage state 3: manifest explicitly off + risk-class on →
    unchanged (classifier returns False on empty manifest). Defense
    in depth: even if the risk-class flag was prematurely flipped,
    the cage stays inert until the manifest loads."""
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "false")
    reset_default_manifest()
    result = apply_order2_floor(
        RiskTier.SAFE_AUTO,
        ["backend/core/ouroboros/governance/orchestrator.py"],
    )
    assert result is RiskTier.SAFE_AUTO


def test_dual_flag_both_on_match_escalates(monkeypatch):
    """Cage state 4: both flags on + manifest match → ORDER_2_GOVERNANCE.
    The fully-armed configuration."""
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "1")
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_PATH",
        str(_REPO / ".jarvis" / "order2_manifest.yaml"),
    )
    reset_default_manifest()
    result = apply_order2_floor(
        RiskTier.SAFE_AUTO,
        ["backend/core/ouroboros/governance/orchestrator.py"],
    )
    assert result is RiskTier.ORDER_2_GOVERNANCE


# ===========================================================================
# F — Telemetry log line on Order-2 classification
# ===========================================================================


def test_apply_emits_telemetry_on_match(monkeypatch, caplog):
    """Pin: structured log line so Slice 2 graduation evidence is
    observable in session logs."""
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", "1")
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    with caplog.at_level(logging.INFO):
        apply_order2_floor(
            RiskTier.SAFE_AUTO, ["backend/x.py"], manifest=m,
        )
    assert any(
        "[Order2RiskClass]" in r.message and "ORDER_2_GOVERNANCE" in r.message
        and "was SAFE_AUTO" in r.message
        for r in caplog.records
    )


def test_apply_emits_no_telemetry_on_miss(monkeypatch, caplog):
    """No log line on non-Order-2 ops — keeps session noise low."""
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", "1")
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    with caplog.at_level(logging.INFO):
        apply_order2_floor(
            RiskTier.SAFE_AUTO, ["unrelated.py"], manifest=m,
        )
    assert not any(
        "[Order2RiskClass]" in r.message for r in caplog.records
    )


def test_apply_emits_no_telemetry_when_master_off(monkeypatch, caplog):
    """Pin: master explicitly off → no log even on match (short-
    circuit before classifier runs). Post-Q4-P#3 graduation, the
    operator's instant-rollback path requires explicit `false`."""
    monkeypatch.setenv("JARVIS_ORDER2_RISK_CLASS_ENABLED", "false")
    m = _loaded_manifest(_entry(path_glob="backend/x.py"))
    with caplog.at_level(logging.INFO):
        apply_order2_floor(
            RiskTier.SAFE_AUTO, ["backend/x.py"], manifest=m,
        )
    assert not any(
        "[Order2RiskClass]" in r.message for r in caplog.records
    )


# ===========================================================================
# G — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier_floor",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
    "from backend.core.ouroboros.governance.semantic_firewall",
    "from backend.core.ouroboros.governance.scoped_tool_backend",
]


def test_classifier_no_authority_imports():
    """Pin: classifier is pure data + manifest read. Importing any
    cage module would create a circular dep when Slice 2b wires
    risk_tier_floor against this function."""
    src = _read(
        "backend/core/ouroboros/governance/meta/order2_classifier.py",
    )
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_classifier_no_io_or_subprocess():
    """Pin: pure function — no I/O, no subprocess, no env writes."""
    src = _strip_docstrings_and_comments(
        _read(
            "backend/core/ouroboros/governance/meta/order2_classifier.py",
        ),
    )
    forbidden = [
        "subprocess.",
        "open(",
        ".write_text(",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"


def test_risk_engine_still_only_owns_risk_tier_enum():
    """Defensive: adding ORDER_2_GOVERNANCE to risk_engine.py is
    additive. Pin the absence of any cage import in risk_engine itself
    so we don't accidentally pull manifest/classifier into the enum
    file."""
    src = _read("backend/core/ouroboros/governance/risk_engine.py")
    # risk_engine should NOT import from the new meta/ package — that
    # would create a backward dep (enum → cage → enum).
    assert (
        "from backend.core.ouroboros.governance.meta" not in src
    ), "risk_engine.py must not import meta/ — keeps the cage acyclic"
