"""Cadence Slice 4 — launchd User Agent installer regression
spine.

Pins per operator binding 2026-05-06:

  * --launchd-dry-run renders a valid plist
  * Plist invokes run_live_fire_graduation_soak.sh wrapper
    (single env-block source of truth — same as cron path)
  * StartInterval derived from CRON_SCHEDULE via canonical
    cadence_manifest.derive_interval_hint_s — single source
    of cadence-string truth
  * Plist sets JARVIS_CADENCE_KIND=launchd in
    EnvironmentVariables so the wrapper's preflight stamps
    the cadence_health row correctly
  * Label is the canonical com.jarvis.live-fire-soak
  * StandardOut/ErrorPath point at .jarvis/live_fire_soak_logs/
  * --remove-launchd removes plist + unloads (idempotent)
  * Existing 4 Phase 9 env vars MUST appear in the script
    file (entry-point parity contract)
  * --help mentions both cron and launchd paths

Verifies (16 tests).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _installer_path() -> Path:
    return (
        _repo_root() / "scripts" / "install_live_fire_soak_cron.sh"
    )


def _run_installer(args, env_overrides=None):
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(_installer_path())] + list(args),
        capture_output=True, text=True, env=env,
        cwd=str(_repo_root()),
    )


# ---------------------------------------------------------------------------
# --launchd-dry-run
# ---------------------------------------------------------------------------


def test_launchd_dry_run_succeeds():
    r = _run_installer(["--launchd-dry-run"])
    assert r.returncode == 0


def test_launchd_dry_run_renders_valid_plist_header():
    r = _run_installer(["--launchd-dry-run"])
    assert "<?xml version=" in r.stdout
    assert "<plist version=" in r.stdout
    assert "</plist>" in r.stdout


def test_launchd_plist_label_is_canonical():
    r = _run_installer(["--launchd-dry-run"])
    assert "com.jarvis.live-fire-soak" in r.stdout


def test_launchd_plist_invokes_wrapper_script():
    """Plist MUST invoke run_live_fire_graduation_soak.sh —
    NOT a duplicate env block."""
    r = _run_installer(["--launchd-dry-run"])
    assert "run_live_fire_graduation_soak.sh" in r.stdout


def test_launchd_plist_does_not_inline_env_block():
    """The plist MUST NOT redeclare the 4 Phase 9 env vars —
    those live in the wrapper exclusively (single source of
    truth). Defense against env-block duplication drift."""
    r = _run_installer(["--launchd-dry-run"])
    # Walk the plist body specifically (after the XML header).
    plist_idx = r.stdout.find("<?xml")
    assert plist_idx >= 0
    plist_body = r.stdout[plist_idx:]
    # JARVIS_GRADUATION_LEDGER_ENABLED in the plist body would
    # mean we're duplicating the wrapper's env block.
    assert (
        "JARVIS_GRADUATION_LEDGER_ENABLED"
        not in plist_body
    ), (
        "plist MUST NOT inline JARVIS_GRADUATION_LEDGER_"
        "ENABLED — wrapper carries it"
    )
    assert (
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED"
        not in plist_body
    )


def test_launchd_plist_sets_cadence_kind_env():
    """JARVIS_CADENCE_KIND=launchd MUST appear in
    EnvironmentVariables so the wrapper's preflight stamps
    cadence_health rows correctly."""
    r = _run_installer(["--launchd-dry-run"])
    # Should appear inside an EnvironmentVariables dict
    assert "JARVIS_CADENCE_KIND" in r.stdout
    assert ">launchd<" in r.stdout


def test_launchd_start_interval_derived_from_cron_schedule():
    """CRON_SCHEDULE='0 */12 * * *' → StartInterval=43200."""
    r = _run_installer(
        ["--launchd-dry-run"],
        env_overrides={"CRON_SCHEDULE": "0 */12 * * *"},
    )
    assert "<integer>43200</integer>" in r.stdout


def test_launchd_start_interval_default_8h():
    """Default CRON_SCHEDULE='0 */8 * * *' → 28800s."""
    r = _run_installer(["--launchd-dry-run"])
    assert "<integer>28800</integer>" in r.stdout


def test_launchd_log_paths_point_at_canonical_dir():
    r = _run_installer(["--launchd-dry-run"])
    assert "live_fire_soak_logs/launchd.stdout.log" in r.stdout
    assert "live_fire_soak_logs/launchd.stderr.log" in r.stdout


def test_launchd_run_at_load_false():
    """RunAtLoad=False — no surprise immediate fire when
    operator runs --launchd. They invoke --once for first
    proof."""
    r = _run_installer(["--launchd-dry-run"])
    assert "<key>RunAtLoad</key>" in r.stdout
    # Locate the RunAtLoad block + check its value
    idx = r.stdout.find("<key>RunAtLoad</key>")
    section = r.stdout[idx:idx + 100]
    assert "<false/>" in section


# ---------------------------------------------------------------------------
# --help text mentions both paths
# ---------------------------------------------------------------------------


def test_help_mentions_launchd_path():
    r = _run_installer(["--help"])
    assert "--launchd" in r.stdout
    assert "--remove-launchd" in r.stdout


def test_help_recommends_launchd_on_macos():
    r = _run_installer(["--help"])
    assert "Launchd" in r.stdout or "launchd" in r.stdout


def test_help_still_documents_cron_path():
    """Don't lose existing operator muscle memory — --install
    / --once / --remove still documented."""
    r = _run_installer(["--help"])
    for flag in ("--install", "--once", "--remove", "--status"):
        assert flag in r.stdout


# ---------------------------------------------------------------------------
# Phase 9 env-vars-in-installer pin (existing contract)
# ---------------------------------------------------------------------------


def test_phase9_env_vars_still_present_in_installer():
    """The existing test_install_live_fire_soak_cron.py asserts
    all 4 Phase 9 env vars appear in install_live_fire_soak_cron.sh.
    Slice 4's launchd path moved env vars to the wrapper, but
    the cron path (build_cron_block) still inlines them. Pin
    the textual presence so the contract holds."""
    text = _installer_path().read_text(encoding="utf-8")
    for var in (
        "JARVIS_GRADUATION_LEDGER_ENABLED",
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED",
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT",
        "JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED",
    ):
        assert var in text


# ---------------------------------------------------------------------------
# --remove-launchd idempotency
# ---------------------------------------------------------------------------


def test_remove_launchd_when_not_installed_no_op():
    """--remove-launchd when no plist installed should not
    error — graceful no-op."""
    # Use a temp HOME so we don't touch the operator's real
    # ~/Library/LaunchAgents.
    import tempfile
    with tempfile.TemporaryDirectory() as fake_home:
        fake_la = Path(fake_home) / "Library" / "LaunchAgents"
        # Don't create it — simulate missing
        r = _run_installer(
            ["--remove-launchd"],
            env_overrides={"HOME": fake_home},
        )
        assert r.returncode == 0
        assert "not present" in r.stdout or "not present" in r.stderr


# ---------------------------------------------------------------------------
# Wrapper composition contract
# ---------------------------------------------------------------------------


def test_launchd_path_composes_canonical_wrapper():
    """The launchd path's plist invokes the SAME wrapper as the
    cron path — single source of truth for env block + preflight
    invocation."""
    text = _installer_path().read_text(encoding="utf-8")
    # The launchd plist builder must reference the wrapper
    assert "WRAPPER_SCRIPT" in text
    assert "run_live_fire_graduation_soak.sh" in text
