"""Phase 9 Slice 3 — wrapper + cron + harness env-propagation regression spine.

Pins the operator-visible surfaces of the synthetic-workload arc:

  * ``DEFAULT_SEED_INTENTS_PER_SOAK = 3`` constant present in
    live_fire_soak.py
  * ``_build_env_for_flag`` injects ``OUROBOROS_BATTLE_SEED_INTENTS``
    when not already set; preserves operator override when set
  * Wrapper script ``run_live_fire_graduation_soak.sh`` exports
    the var (with operator-override-preservation shell pattern)
  * Cron generator + crontab example both inline the var on the
    cron line (subprocess inheritance)
  * Single-source-of-truth pin: all 3 entry points carry the
    new var (mirrors the Wave 3 hygiene pattern for the original
    4 boolean flags)

Verifies (12 tests).
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_repo_root() / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Module constant + behavior
# ---------------------------------------------------------------------------


def test_default_seed_intents_per_soak_constant():
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        DEFAULT_SEED_INTENTS_PER_SOAK,
    )
    assert DEFAULT_SEED_INTENTS_PER_SOAK == 3


def test_build_env_for_flag_sets_seed_intents_when_unset():
    """When parent env has no OUROBOROS_BATTLE_SEED_INTENTS,
    `_build_env_for_flag` injects the harness default."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        DEFAULT_SEED_INTENTS_PER_SOAK,
        get_default_harness,
    )
    harness = get_default_harness()
    # Use a real flag name from CADENCE_POLICY.
    flag_name = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(
            "OUROBOROS_BATTLE_SEED_INTENTS", None,
        )
        env = harness._build_env_for_flag(flag_name)
    assert (
        env.get("OUROBOROS_BATTLE_SEED_INTENTS")
        == str(DEFAULT_SEED_INTENTS_PER_SOAK)
    )


def test_build_env_for_flag_preserves_operator_override():
    """Operator-set OUROBOROS_BATTLE_SEED_INTENTS=N MUST flow
    through to subprocess unchanged. The harness default is
    only applied when the var is unset."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        get_default_harness,
    )
    harness = get_default_harness()
    flag_name = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    with patch.dict(
        os.environ,
        {"OUROBOROS_BATTLE_SEED_INTENTS": "7"},
    ):
        env = harness._build_env_for_flag(flag_name)
    assert env.get("OUROBOROS_BATTLE_SEED_INTENTS") == "7"


# ---------------------------------------------------------------------------
# Single-source-of-truth pins — all 3 entry points carry the new var
# ---------------------------------------------------------------------------


def test_wrapper_exports_seed_intents():
    text = _read("scripts/run_live_fire_graduation_soak.sh")
    assert "OUROBOROS_BATTLE_SEED_INTENTS" in text, (
        "wrapper script missing OUROBOROS_BATTLE_SEED_INTENTS"
    )
    # Operator-override-preservation shell pattern:
    # `export VAR="${VAR:-3}"` honors operator value when set.
    assert (
        'OUROBOROS_BATTLE_SEED_INTENTS="${OUROBOROS_BATTLE_SEED_INTENTS:-3}"'  # noqa: E501
        in text
    ), (
        "wrapper MUST use operator-override-preservation shell "
        "pattern: export OUROBOROS_BATTLE_SEED_INTENTS="
        "\"${OUROBOROS_BATTLE_SEED_INTENTS:-3}\""
    )


def test_cron_installer_inlines_seed_intents():
    """Cron entry generator must include the new var inline on
    the same line as the python invocation (subprocess
    inheritance via cron's environment)."""
    text = _read("scripts/install_live_fire_soak_cron.sh")
    # Find the `cat` heredoc block defining the cron entry.
    assert "OUROBOROS_BATTLE_SEED_INTENTS=3" in text, (
        "install_live_fire_soak_cron.sh cron entry missing "
        "OUROBOROS_BATTLE_SEED_INTENTS=3"
    )
    # And the --once path (run_once function) must also include it.
    once_block_start = text.index("def run_once") if (
        "def run_once" in text
    ) else text.index("run_once()")
    once_block = text[once_block_start:]
    assert "OUROBOROS_BATTLE_SEED_INTENTS" in once_block, (
        "--once code path missing OUROBOROS_BATTLE_SEED_INTENTS"
    )


def test_crontab_example_inlines_seed_intents():
    text = _read("scripts/crontab-live-fire.example")
    # Find the cron line containing the python3 invocation.
    cron_lines = [
        line for line in text.split("\n")
        if "python3" in line
        and "live_fire_graduation_soak.py" in line
    ]
    assert cron_lines, (
        "crontab example missing python3 invocation"
    )
    for line in cron_lines:
        assert "OUROBOROS_BATTLE_SEED_INTENTS=3" in line, (
            f"crontab line missing OUROBOROS_BATTLE_SEED_INTENTS"
            f"=3: {line[:120]}"
        )


@pytest.mark.parametrize("rel_path", [
    "scripts/run_live_fire_graduation_soak.sh",
    "scripts/install_live_fire_soak_cron.sh",
    "scripts/crontab-live-fire.example",
])
def test_seed_intents_present_in_every_entry_point(rel_path):
    """Single-source-of-truth pin: all 3 entry points carry the
    new env var. Mirrors the Wave 3 hygiene pattern that
    enforced the original 4 boolean flags. Adding a 5th cadence
    env var requires updating ALL files."""
    text = _read(rel_path)
    assert "OUROBOROS_BATTLE_SEED_INTENTS" in text, (
        f"{rel_path} missing OUROBOROS_BATTLE_SEED_INTENTS — "
        f"entry points MUST stay in sync"
    )


# ---------------------------------------------------------------------------
# Documentation-quality pins — operator can grep for the gap-closure
# ---------------------------------------------------------------------------


def test_crontab_example_documents_seed_intents_purpose():
    """Crontab example header MUST explain WHY the var is set
    (closes the headless zero-ops blocker), so a future operator
    grepping the file understands the load-bearing role of this
    seemingly-innocuous int."""
    text = _read("scripts/crontab-live-fire.example")
    assert "synthetic workload" in text.lower(), (
        "crontab example must document the role of "
        "OUROBOROS_BATTLE_SEED_INTENTS in plain English"
    )
    assert "cadence_synthetic" in text or (
        "Phase 9 Slice 3" in text
    ), (
        "crontab example must reference the source token or "
        "the slice for grep-discoverability"
    )


# ---------------------------------------------------------------------------
# Defense-in-depth: factory cap honored end-to-end
# ---------------------------------------------------------------------------


def test_seed_intents_3_under_factory_cap():
    """The harness default (3) MUST be safely under the factory's
    hard cap (default 16). Defense-in-depth: a misconfigured
    operator override CANNOT exceed the factory cap regardless
    of what env they set."""
    from backend.core.ouroboros.governance.graduation.phase_9_synthetic_workload import (  # noqa: E501
        seed_intents_max,
    )
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        DEFAULT_SEED_INTENTS_PER_SOAK,
    )
    assert DEFAULT_SEED_INTENTS_PER_SOAK <= seed_intents_max()


def test_factory_caps_extreme_operator_override():
    """If operator sets OUROBOROS_BATTLE_SEED_INTENTS=999 (oops),
    the factory cap (default 16, max 64) catches it. Validates
    the layered defense: env passes through subprocess → harness
    reads → factory caps."""
    from backend.core.ouroboros.governance.graduation.phase_9_synthetic_workload import (  # noqa: E501
        build_synthetic_envelopes, seed_intents_max,
    )
    envs = build_synthetic_envelopes(n=999, repo="r")
    assert len(envs) == seed_intents_max()
    assert len(envs) <= 64


# ---------------------------------------------------------------------------
# Wave 3 hygiene continuity — original 4-var pin still passes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", [
    "scripts/run_live_fire_graduation_soak.sh",
    "scripts/install_live_fire_soak_cron.sh",
    "scripts/crontab-live-fire.example",
])
def test_wave3_original_4_vars_still_present(rel_path):
    """Continuity: Slice 3 added a 5th var but MUST NOT have
    dropped any of the original 4 Wave 3 hygiene-pinned vars."""
    text = _read(rel_path)
    for var in (
        "JARVIS_GRADUATION_LEDGER_ENABLED",
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED",
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT",
        "JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED",
    ):
        assert var in text, (
            f"{rel_path} dropped Wave 3-pinned var {var}"
        )
