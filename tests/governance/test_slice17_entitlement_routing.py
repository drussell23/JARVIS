# tests/governance/test_slice17_entitlement_routing.py
"""Slice 17 — entitlement vs capability in the DW routing layer.

Run-25c autopsy: O+V entered 19 GENERATE phases and produced ZERO code at
$0.00. Root cause was a three-stage laundering of one HTTP status:

  1. ``Qwen/Qwen3.5-397B-A17B-FP8-dottxt`` returns **403** on the real-time
     endpoint ("blocked by a routing rule") — an AUTHORIZATION fact about the
     account, verified live against the DW API.
  2. The transport profiler recorded ANY batch dispatch as an IMMORTAL
     batch-only CAPABILITY tag, so the 403 became "this model needs batch" —
     and so did every healthy model that ever happened to take the batch path
     (the plain 397B serves RT in 1.6s; gpt-oss-120b in 0.7s — both fossilized).
  3. Batch then aged out on the 300s breaker, and the bandit folded that
     TimeoutError into the model's generation-QUALITY posterior — teaching it
     to down-rank models that generate perfectly well and up-rank an
     unreachable one, until the ladder starved to "exhausted all 1 DW models".

These tests pin the structural repair: a refused transport is EXCLUDED, never
downgraded; a demotion always decays and is invalidated by live success; and an
environment fault never touches a quality score.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.bandit_router import (
    FAULT_ENTITLEMENT,
    FAULT_QUALITY,
    FAULT_TIMEOUT,
    FAULT_TRANSPORT,
    BanditRouter,
    classify_fault,
    is_infra_fault,
)
from backend.core.ouroboros.governance.dw_transport_profile import (
    TRANSPORT_BATCH,
    TRANSPORT_REALTIME,
    TransportProfile,
)

DOTTXT = "Qwen/Qwen3.5-397B-A17B-FP8-dottxt"   # 403s on RT (measured live)
PLAIN_397B = "Qwen/Qwen3.5-397B-A17B-FP8"      # serves RT in 1.6s (measured)
FLASH = "deepseek-ai/DeepSeek-V4-Flash"        # serves RT in 15s, 870 tokens


@pytest.fixture()
def profile(monkeypatch: pytest.MonkeyPatch) -> TransportProfile:
    monkeypatch.setenv(
        "JARVIS_DW_TRANSPORT_PROFILE_STATE_PATH",
        tempfile.mktemp(suffix=".json"),
    )
    return TransportProfile(autosave=False)


# ---------------------------------------------------------------------------
# Mandate 1 — a 403 is an entitlement fact, NEVER a capability claim
# ---------------------------------------------------------------------------

def test_403_excludes_the_transport_and_never_batch_demotes(
    profile: TransportProfile,
) -> None:
    """The Run-25c bug in one assertion: a 403 on RT must not imply batch."""
    profile.record_unavailable(DOTTXT, TRANSPORT_REALTIME, status=403)

    assert profile.is_unavailable(DOTTXT, TRANSPORT_REALTIME) is True
    # THE regression: the denial must not be laundered into a capability tag.
    assert profile.is_batch_only(DOTTXT) is False
    # Batch entitlement is independent — DW accepted our batch upload (201).
    assert profile.is_unavailable(DOTTXT, TRANSPORT_BATCH) is False


def test_denial_is_per_transport_not_per_model(
    profile: TransportProfile,
) -> None:
    profile.record_unavailable(DOTTXT, TRANSPORT_REALTIME, status=403)
    profile.record_unavailable(FLASH, TRANSPORT_BATCH, status=403)

    assert profile.is_unavailable(DOTTXT, TRANSPORT_REALTIME) is True
    assert profile.is_unavailable(DOTTXT, TRANSPORT_BATCH) is False
    assert profile.is_unavailable(FLASH, TRANSPORT_BATCH) is True
    assert profile.is_unavailable(FLASH, TRANSPORT_REALTIME) is False


# ---------------------------------------------------------------------------
# Mandate 2 — no immortal demotions; live success invalidates the fossil
# ---------------------------------------------------------------------------

def test_rt_success_invalidates_stale_batch_only_and_denial(
    profile: TransportProfile,
) -> None:
    """The plain 397B was fossil-tagged batch-only yet answers RT in 1.6s.
    One successful RT call must drop BOTH stale demotions."""
    profile.record_batch_only(PLAIN_397B)
    profile.record_unavailable(PLAIN_397B, TRANSPORT_REALTIME, status=403)
    assert profile.is_batch_only(PLAIN_397B) is True
    assert profile.is_unavailable(PLAIN_397B, TRANSPORT_REALTIME) is True

    profile.record_rt_success(PLAIN_397B)

    assert profile.is_batch_only(PLAIN_397B) is False
    assert profile.is_unavailable(PLAIN_397B, TRANSPORT_REALTIME) is False


def test_batch_only_tag_is_never_immortal(
    monkeypatch: pytest.MonkeyPatch, profile: TransportProfile,
) -> None:
    """TTL=0 used to mean IMMORTAL. It must now coerce to the finite default —
    a demotion is a hypothesis about a live endpoint, not a life sentence."""
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_PROFILE_TTL_S", "0")
    from backend.core.ouroboros.governance import dw_transport_profile as mod

    assert mod._profile_ttl_s() > 0.0
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_PROFILE_TTL_S", "-5")
    assert mod._profile_ttl_s() > 0.0
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_PROFILE_TTL_S", "garbage")
    assert mod._profile_ttl_s() > 0.0


def test_expired_demotions_decay_on_read(
    monkeypatch: pytest.MonkeyPatch, profile: TransportProfile,
) -> None:
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_PROFILE_TTL_S", "0.01")
    monkeypatch.setenv("JARVIS_DW_ENTITLEMENT_TTL_S", "0.01")
    profile.record_batch_only(PLAIN_397B)
    profile.record_unavailable(DOTTXT, TRANSPORT_REALTIME, status=403)

    import time as _t
    _t.sleep(0.05)

    assert profile.is_batch_only(PLAIN_397B) is False
    assert profile.is_unavailable(DOTTXT, TRANSPORT_REALTIME) is False


# ---------------------------------------------------------------------------
# Mandate 3 — infra faults must not contaminate the quality posterior
# ---------------------------------------------------------------------------

class _Err403(Exception):
    status_code = 403


class _Err500(Exception):
    status_code = 500


def test_classify_fault_separates_environment_from_quality() -> None:
    assert classify_fault(_Err403()) == FAULT_ENTITLEMENT
    assert classify_fault(_Err500()) == FAULT_TRANSPORT
    assert classify_fault(TimeoutError()) == FAULT_TIMEOUT
    # Conservative: an unidentifiable failure stays QUALITY, so a genuinely
    # bad model can never escape its posterior by being mislabelled infra.
    assert classify_fault(ValueError("bad json")) == FAULT_QUALITY
    assert classify_fault(None) == FAULT_QUALITY

    assert is_infra_fault(FAULT_ENTITLEMENT) is True
    assert is_infra_fault(FAULT_TIMEOUT) is True
    assert is_infra_fault(FAULT_QUALITY) is False


def test_infra_fault_leaves_posterior_untouched() -> None:
    bandit = BanditRouter(state_path=Path(tempfile.mktemp(suffix=".json")))

    for fault in (FAULT_ENTITLEMENT, FAULT_TRANSPORT, FAULT_TIMEOUT):
        bandit.record_outcome(FLASH, success=False, fault_class=fault)

    arm = bandit.snapshot()[FLASH]
    # THE regression: three infrastructure failures, posterior unmoved.
    assert arm["alpha"] == 1.0
    assert arm["beta"] == 1.0
    assert arm["infra_faults"] == 3.0


def test_quality_failure_still_moves_the_posterior() -> None:
    bandit = BanditRouter(state_path=Path(tempfile.mktemp(suffix=".json")))
    bandit.record_outcome(FLASH, success=False, fault_class=FAULT_QUALITY)
    arm = bandit.snapshot()[FLASH]
    assert arm["beta"] > 1.0

    bandit.record_outcome(FLASH, success=True, latency_s=1.0)
    assert bandit.snapshot()[FLASH]["alpha"] > 1.0


def test_unranked_default_still_penalizes_bare_failures() -> None:
    """No fault_class supplied → legacy behavior (quality). Callers that have
    not been taught the taxonomy must not silently gain infra immunity."""
    bandit = BanditRouter(state_path=Path(tempfile.mktemp(suffix=".json")))
    bandit.record_outcome(FLASH, success=False)
    assert bandit.snapshot()[FLASH]["beta"] > 1.0


# ---------------------------------------------------------------------------
# Mandate 4 — the ladder verifies entitlement instead of burning an op on it
# ---------------------------------------------------------------------------

def test_ladder_bypasses_rt_denied_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_DW_TRANSPORT_PROFILE_STATE_PATH",
        tempfile.mktemp(suffix=".json"),
    )
    from backend.core.ouroboros.governance.dw_transport_profile import (
        get_transport_profile,
    )
    from backend.core.ouroboros.governance.provider_topology import (
        _entitlement_filtered,
    )

    get_transport_profile().record_unavailable(
        DOTTXT, TRANSPORT_REALTIME, status=403,
    )
    out = _entitlement_filtered("background", (DOTTXT, FLASH, PLAIN_397B))

    assert DOTTXT not in out
    assert out == (FLASH, PLAIN_397B)


def test_all_denied_empties_the_ladder_instead_of_dispatching_a_known_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run-26 correction.

    The filter's first draft returned the UNFILTERED ladder when every model was
    denied, reasoning that starving a route was worse. That was wrong, and it
    showed up live: BACKGROUND's ladder held exactly one model (``-dottxt``,
    already condemned by the entitlement probe), so the "never starve" rule
    handed it straight back and the route kept dispatching into a guaranteed 403.

    Emptying the ladder is the honest signal — the cascade matrix already knows
    what to do with "DW cannot serve this route" (queue for BG/SPEC, cascade to
    Claude for STANDARD/COMPLEX). A poisoned ladder just burns the op.
    """
    monkeypatch.setenv(
        "JARVIS_DW_TRANSPORT_PROFILE_STATE_PATH",
        tempfile.mktemp(suffix=".json"),
    )
    from backend.core.ouroboros.governance.dw_transport_profile import (
        get_transport_profile,
    )
    from backend.core.ouroboros.governance.provider_topology import (
        _entitlement_filtered,
    )

    profile = get_transport_profile()
    ladder = (DOTTXT, FLASH)
    for model in ladder:
        profile.record_unavailable(model, TRANSPORT_REALTIME, status=403)

    assert _entitlement_filtered("background", ladder) == ()
    assert _entitlement_filtered("background", ()) == ()


def test_single_model_denied_ladder_empties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact Run-26 BACKGROUND shape: a one-model ladder holding the 403."""
    monkeypatch.setenv(
        "JARVIS_DW_TRANSPORT_PROFILE_STATE_PATH",
        tempfile.mktemp(suffix=".json"),
    )
    from backend.core.ouroboros.governance.dw_transport_profile import (
        get_transport_profile,
    )
    from backend.core.ouroboros.governance.provider_topology import (
        _entitlement_filtered,
    )

    get_transport_profile().record_unavailable(
        DOTTXT, TRANSPORT_REALTIME, status=403,
    )
    assert _entitlement_filtered("background", (DOTTXT,)) == ()
