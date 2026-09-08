"""One execution envelope, derived — and no launcher allowed to transcribe it.

``soak26.sh`` carried ~40 ``export`` lines; ``cockpit_interactive.sh`` carried
the presentation flags and NONE of the execution budgets, so a ``/goal
sanction`` typed into the cockpit ran the production pipeline on DEFAULT
budgets. Copy-pasting the block into the second script would have fixed that
run and guaranteed the next drift.

These tests pin three things: the budgets are DERIVED (one wall clock, two
ratios) and reproduce the hand-tuned soak26 numbers exactly; the cockpit and
the soak get IDENTICAL execution budgets; and no launcher re-hardcodes a value
this module owns.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.production_envelope import (
    PROFILES,
    build,
    export_lines,
    hydrate,
)

@pytest.fixture(autouse=True)
def _hydrated():
    """The envelope derives the pool size from the lane, and the lane may only
    be read from a LOADED environment (init_guard). Production declares this at
    the boot seam."""
    from backend.core.ouroboros.governance.init_guard import (
        mark_hydrated, reset_for_tests,
    )
    reset_for_tests()
    mark_hydrated()
    yield
    reset_for_tests()


REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "scripts" / "soaks" / "cockpit_interactive.sh"

#: Budgets that must be identical in every profile — a cockpit that cannot do
#: production work is the problem this module exists to solve.
EXECUTION_KEYS = (
    "JARVIS_PIPELINE_TIMEOUT_S",
    "JARVIS_GENERATION_TIMEOUT_S",
    "JARVIS_GEN_TIMEOUT_STANDARD_S",
    "JARVIS_BG_WORKER_OP_TIMEOUT_S",
    "JARVIS_BG_POOL_SIZE",
    "JARVIS_BG_QUEUE_SIZE",
    "JARVIS_VALIDATION_RESERVE_ENABLED",
    "JARVIS_VALIDATION_RESERVE_COLD_S",
    "JARVIS_WORK_ORDER_SENSOR_ENABLED",
    "JARVIS_ALLOW_ROADMAP_REVISIT",
)


# --------------------------------------------------------------------------
# Derivation — and the soak26 regression anchor
# --------------------------------------------------------------------------

def test_the_soak_profile_reproduces_the_hand_tuned_soak26_budgets():
    """soak26.sh ran wall=9000 and set 4968 / 3726 / 4968 by hand. If the
    derivation reproduces them exactly, the ratios ARE the relation those
    numbers encoded — and this is the anchor that proves the refactor changed
    no behaviour."""
    env = build("soak", wall_s=9000).as_env()
    assert env["JARVIS_PIPELINE_TIMEOUT_S"] == "4968"
    assert env["JARVIS_GENERATION_TIMEOUT_S"] == "3726"
    assert env["JARVIS_GEN_TIMEOUT_STANDARD_S"] == "3726"
    assert env["JARVIS_BG_WORKER_OP_TIMEOUT_S"] == "4968"


@pytest.mark.parametrize("wall", [600, 1800, 3600, 9000, 43200])
def test_generation_never_exceeds_its_own_pipeline_budget(wall):
    """A generation budget larger than the pipeline it runs inside is a
    deadline the op can never meet — invisible until a soak burns on it.
    Written as literals in three scripts, that is one careless edit away."""
    e = build("soak", wall_s=wall)
    assert e.generation_timeout_s < e.pipeline_timeout_s
    assert e.pipeline_timeout_s <= wall
    assert e.bg_worker_op_timeout_s == e.pipeline_timeout_s


@pytest.mark.parametrize("wall", [600, 3600, 9000])
def test_the_approval_deadline_expires_before_the_pipeline_does(wall):
    """Phase 3's invariant. An op shed by its own approval gate records WHY;
    one killed by the pipeline clock just vanishes."""
    e = build("cockpit", wall_s=wall)
    assert e.approval_deadline_s < e.pipeline_timeout_s
    assert e.as_env()["JARVIS_APPROVAL_DEADLINE_S"] == str(e.approval_deadline_s)


def test_budgets_move_together_when_the_wall_changes():
    small, large = build("soak", wall_s=1800), build("soak", wall_s=9000)
    assert large.pipeline_timeout_s > small.pipeline_timeout_s
    assert large.generation_timeout_s > small.generation_timeout_s
    assert large.approval_deadline_s > small.approval_deadline_s


# --------------------------------------------------------------------------
# The cockpit is the same organism
# --------------------------------------------------------------------------

def test_cockpit_and_soak_share_identical_execution_budgets():
    soak = build("soak", wall_s=3600).as_env()
    cockpit = build("cockpit", wall_s=3600).as_env()
    for key in EXECUTION_KEYS:
        assert soak[key] == cockpit[key], (
            f"{key} differs between profiles — the cockpit could not then do "
            "the production work the soak does, which is the whole point"
        )


def test_the_cockpit_profile_adds_presentation_without_changing_execution():
    soak = build("soak", wall_s=3600).as_env()
    cockpit = build("cockpit", wall_s=3600).as_env()
    assert set(soak).issubset(set(cockpit))
    assert cockpit["JARVIS_OV_PRESENTATION"] == "cockpit"
    assert "JARVIS_OV_PRESENTATION" not in soak


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_every_profile_is_air_gapped(profile):
    """The 22 branches escaped from HEADLESS soaks, so the headless profile is
    exactly the one that must carry the air-gap too."""
    env = build(profile).as_env()
    assert env["JARVIS_REMOTE_PUSH_AIRGAP"] == "true"
    assert env["JARVIS_ORANGE_PR_ENABLED"] == "false"


def test_booleans_render_lowercase():
    """Every reader in this repo compares against ("1","true","yes","on").
    "True" survives that only by accident of .lower()."""
    env = build("soak").as_env()
    assert env["JARVIS_PIPELINE_DEADLINE_AT_START"] == "true"
    assert env["JARVIS_DOC_STALENESS_ENABLED"] == "false"
    assert not any(v in ("True", "False") for v in env.values())


def test_an_unknown_profile_degrades_to_soak_rather_than_raising():
    """A launcher typo must not stop the organism booting."""
    assert build("cockpti").profile == "soak"


# --------------------------------------------------------------------------
# Operator intent wins — identically in Python and in bash
# --------------------------------------------------------------------------

def test_hydrate_never_clobbers_a_value_the_operator_set():
    env = {"JARVIS_BG_POOL_SIZE": "99", "JARVIS_PIPELINE_TIMEOUT_S": "123"}
    applied, overridden = hydrate("soak", environ=env)
    assert env["JARVIS_BG_POOL_SIZE"] == "99"
    assert env["JARVIS_PIPELINE_TIMEOUT_S"] == "123"
    assert set(overridden) == {"JARVIS_BG_POOL_SIZE", "JARVIS_PIPELINE_TIMEOUT_S"}
    assert "JARVIS_BG_POOL_SIZE" not in applied


def test_hydrate_fills_what_nobody_spoke_for():
    env = {}
    applied, overridden = hydrate("cockpit", environ=env)
    assert not overridden
    assert env["JARVIS_BG_POOL_SIZE"] == "6"
    assert len(applied) == len(build("cockpit").as_env())


def test_an_empty_string_counts_as_unset():
    """`export FOO=` in a launcher means "I did not decide", not "use empty"."""
    env = {"JARVIS_BG_POOL_SIZE": ""}
    hydrate("soak", environ=env)
    assert env["JARVIS_BG_POOL_SIZE"] == "6"


def test_shell_export_lines_give_bash_the_same_precedence(tmp_path):
    lines = export_lines("cockpit", wall_s=3600)
    assert 'export JARVIS_BG_POOL_SIZE="${JARVIS_BG_POOL_SIZE:-6}"' in lines
    # Every emitted line must use the :- form, or bash and Python disagree
    # about who wins and the launcher silently overrides the operator.
    for line in lines.splitlines():
        if line.startswith("export "):
            assert ":-" in line, f"not override-safe: {line}"


def test_shell_output_is_valid_bash():
    import subprocess
    lines = export_lines("cockpit")
    rc = subprocess.run(["bash", "-n"], input=lines, text=True,
                        capture_output=True).returncode
    assert rc == 0, "emitted export lines are not parseable bash"


# --------------------------------------------------------------------------
# Anti-drift: no launcher may re-hardcode what this module owns
# --------------------------------------------------------------------------

def test_the_cockpit_launcher_transcribes_no_envelope_value():
    """The regression this whole module prevents. If someone pastes an
    ``export JARVIS_BG_POOL_SIZE=6`` back into the launcher, the two sources
    exist again and the next edit splits them."""
    if not LAUNCHER.exists():  # pragma: no cover - layout guard
        pytest.skip("launcher not present")
    owned = set(build("cockpit").as_env())
    # The air-gap pair is deliberately re-stated in the launcher so an
    # operator sees the zero-push posture without opening a Python module.
    # They are pinned to the SAME value, so they cannot drift into a conflict.
    allowed = {"JARVIS_REMOTE_PUSH_AIRGAP", "JARVIS_ORANGE_PR_ENABLED"}
    offenders = []
    for raw in LAUNCHER.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        m = re.match(r"^export\s+([A-Z_][A-Z0-9_]*)=", line)
        if not m:
            continue
        name = m.group(1)
        if name in owned and name not in allowed:
            offenders.append(line)
    assert not offenders, (
        "the launcher hardcodes values production_envelope owns — that is the "
        "drift this module exists to remove:\n  " + "\n  ".join(offenders)
    )


def test_the_launcher_actually_sources_the_envelope():
    """A launcher that transcribes nothing AND sources nothing would pass the
    test above while running on defaults — the exact bug being fixed."""
    if not LAUNCHER.exists():  # pragma: no cover
        pytest.skip("launcher not present")
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "production_envelope" in text
    assert "--profile cockpit" in text
    assert 'eval "$ENVELOPE"' in text


def test_the_launcher_refuses_rather_than_running_on_defaults():
    """If the envelope cannot be built the cockpit must NOT start. Booting on
    default budgets is precisely the silent failure being removed."""
    if not LAUNCHER.exists():  # pragma: no cover
        pytest.skip("launcher not present")
    text = LAUNCHER.read_text(encoding="utf-8")
    # Anchor on the COMMAND, not the first prose mention of the module name.
    assert "if ! ENVELOPE=" in text, "the envelope build is not guarded by a conditional"
    tail = text[text.index("if ! ENVELOPE="):]
    fence = tail[:tail.index('eval "$ENVELOPE"')]
    assert "die " in fence, (
        "the envelope build has no refusal path — a failed build would fall "
        "through to a cockpit running on default budgets"
    )
