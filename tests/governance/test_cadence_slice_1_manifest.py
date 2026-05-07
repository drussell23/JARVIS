"""Cadence Slice 1 — manifest substrate regression spine.

Pins per operator binding 2026-05-06:

  * derive_interval_hint_s parses cron 5-field expressions
    correctly for all common installer-emitted patterns:
      - ``0 */N * * *`` (every N hours)
      - ``0 H1,H2,H3 * * *`` (fixed-hour list — worst-case gap)
      - single literal hour
      - ``*`` everywhere → minute-floor clamp
  * Defensive: blank / garbage / 6-field user-prefix / negative
    intervals → 0 (caller falls back to override)
  * Floor 60s + ceiling 7d clamps applied
  * Launchd derivation passes through StartInterval seconds
  * write_manifest atomic (temp + os.replace)
  * write_manifest validates schedule_kind ∈ {cron, launchd}
  * read_manifest defensive on missing / malformed
  * §33.5 versioned-artifact-contract round-trip
  * AST pins fire on synthetic regressions

Verifies (32 tests).
"""
from __future__ import annotations

import ast
import json
import os
import tempfile
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# derive_interval_hint_s — cron-spec parser
# ---------------------------------------------------------------------------


def test_derive_every_8_hours():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s,
    )
    assert derive_interval_hint_s("0 */8 * * *") == 8 * 3600


def test_derive_every_12_hours():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s,
    )
    assert derive_interval_hint_s("0 */12 * * *") == 12 * 3600


def test_derive_fixed_hour_list_worst_case_gap():
    """0 6,14,22 * * * = 8/8/8h gaps → worst case 8h."""
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s,
    )
    assert (
        derive_interval_hint_s("0 6,14,22 * * *") == 8 * 3600
    )


def test_derive_uneven_hour_list_picks_max_gap():
    """0 6,9,22 * * * = 3h, 13h, 8h gaps → worst case 13h."""
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s,
    )
    assert (
        derive_interval_hint_s("0 6,9,22 * * *") == 13 * 3600
    )


def test_derive_single_fire_per_day_returns_24h():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s,
    )
    assert (
        derive_interval_hint_s("0 6 * * *") == 24 * 3600
    )


def test_derive_every_minute_clamped_to_floor():
    """``* * * * *`` would be 60s — floor enforced."""
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s,
    )
    assert derive_interval_hint_s("* * * * *") == 60


def test_derive_blank_returns_zero():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s,
    )
    assert derive_interval_hint_s("") == 0
    assert derive_interval_hint_s("   ") == 0


def test_derive_garbage_returns_zero():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s,
    )
    assert derive_interval_hint_s("not a cron expr") == 0
    assert derive_interval_hint_s("0 */abc * * *") == 0
    assert derive_interval_hint_s("foo bar baz") == 0


def test_derive_too_few_fields_returns_zero():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s,
    )
    assert derive_interval_hint_s("0 */8") == 0


def test_derive_handles_six_field_user_prefix():
    """System crontabs sometimes carry a leading user name as
    a 6th field. Heuristic strips it."""
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s,
    )
    # `root 0 */8 * * *` — strip `root` prefix
    assert (
        derive_interval_hint_s("root 0 */8 * * *") == 8 * 3600
    )


def test_derive_negative_step_returns_zero():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s,
    )
    assert derive_interval_hint_s("0 */-1 * * *") == 0


def test_derive_never_raises_on_malformed():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s,
    )
    # Pile of bad inputs
    for s in (None, "", "garbage", "0 */0 * * *", "0 */ * * *"):
        try:
            v = derive_interval_hint_s(s)  # type: ignore
            assert isinstance(v, int)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"raised on {s!r}: {exc}")


# ---------------------------------------------------------------------------
# derive_interval_hint_s_from_launchd_interval
# ---------------------------------------------------------------------------


def test_launchd_43200():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s_from_launchd_interval,
    )
    assert (
        derive_interval_hint_s_from_launchd_interval(43200)
        == 43200
    )


def test_launchd_zero_returns_zero():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s_from_launchd_interval,
    )
    assert (
        derive_interval_hint_s_from_launchd_interval(0) == 0
    )


def test_launchd_below_floor_clamps():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s_from_launchd_interval,
    )
    assert (
        derive_interval_hint_s_from_launchd_interval(30) == 60
    )


def test_launchd_above_ceiling_clamps():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s_from_launchd_interval,
    )
    one_year = 365 * 24 * 3600
    one_week = 7 * 24 * 3600
    assert (
        derive_interval_hint_s_from_launchd_interval(one_year)
        == one_week
    )


def test_launchd_garbage_returns_zero():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        derive_interval_hint_s_from_launchd_interval,
    )
    assert (
        derive_interval_hint_s_from_launchd_interval(
            "not_int",  # type: ignore
        )
        == 0
    )


# ---------------------------------------------------------------------------
# write_manifest / read_manifest round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_manifest(monkeypatch, tmp_path):
    target = tmp_path / "cadence_manifest.json"
    monkeypatch.setenv(
        "JARVIS_CADENCE_MANIFEST_PATH", str(target),
    )
    yield target


def test_write_cron_manifest_round_trip(tmp_manifest):
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        read_manifest, write_manifest,
    )
    ok, detail = write_manifest(
        schedule_kind="cron",
        schedule_string="0 */12 * * *",
        extras={
            "cost_cap_usd": "0.50",
            "wall_cap_s": "2400",
        },
    )
    assert ok is True
    assert detail == "ok"
    m = read_manifest()
    assert m is not None
    assert m.schedule_kind == "cron"
    assert m.schedule_string == "0 */12 * * *"
    assert m.interval_hint_s == 12 * 3600
    assert m.extras["cost_cap_usd"] == "0.50"


def test_write_launchd_manifest_round_trip(tmp_manifest):
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        read_manifest, write_manifest,
    )
    ok, detail = write_manifest(
        schedule_kind="launchd",
        schedule_string="43200",
        extras={"plist_path": "~/Library/LaunchAgents/foo.plist"},
    )
    assert ok is True
    m = read_manifest()
    assert m.schedule_kind == "launchd"
    assert m.interval_hint_s == 43200


def test_write_manifest_explicit_interval_override(
    tmp_manifest,
):
    """Caller-provided interval_hint_s overrides the cron-spec
    parser. Clamp ceiling (7 days) still applies."""
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        read_manifest, write_manifest,
    )
    # Override well below ceiling — passes through verbatim
    ok, _ = write_manifest(
        schedule_kind="cron",
        schedule_string="0 */8 * * *",
        interval_hint_s=12345,
    )
    assert ok is True
    m = read_manifest()
    assert m.interval_hint_s == 12345


def test_write_manifest_override_above_ceiling_clamps(
    tmp_manifest,
):
    """Override > 7d ceiling clamps to 7d."""
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        read_manifest, write_manifest,
    )
    huge = 14 * 24 * 3600  # 14 days
    ok, _ = write_manifest(
        schedule_kind="cron",
        schedule_string="0 */8 * * *",
        interval_hint_s=huge,
    )
    assert ok is True
    m = read_manifest()
    assert m.interval_hint_s == 7 * 24 * 3600


def test_write_manifest_rejects_unknown_kind(tmp_manifest):
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        write_manifest,
    )
    ok, detail = write_manifest(
        schedule_kind="systemd",
        schedule_string="OnCalendar=*-*-* 0/12:00:00",
    )
    assert ok is False
    assert "unknown_schedule_kind" in detail


def test_read_manifest_returns_none_when_missing(tmp_manifest):
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        read_manifest,
    )
    assert read_manifest() is None


def test_read_manifest_defensive_on_garbage(tmp_manifest):
    """Corrupt JSON → None, NOT raised."""
    tmp_manifest.write_text("not_json", encoding="utf-8")
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        read_manifest,
    )
    assert read_manifest() is None


def test_read_manifest_defensive_on_unknown_kind(tmp_manifest):
    """JSON valid but kind unknown → None."""
    tmp_manifest.write_text(
        json.dumps({
            "schema_version": "cadence_manifest.1",
            "schedule_kind": "weird_systemd_thing",
            "schedule_string": "x",
            "interval_hint_s": 0,
        }),
        encoding="utf-8",
    )
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        read_manifest,
    )
    assert read_manifest() is None


def test_write_atomic_via_tmp_replace(tmp_manifest):
    """Atomic semantics — tmp file removed after replace."""
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        write_manifest,
    )
    write_manifest(
        schedule_kind="cron",
        schedule_string="0 */8 * * *",
    )
    assert tmp_manifest.exists()
    tmp_path = Path(str(tmp_manifest) + ".tmp")
    assert not tmp_path.exists()


# ---------------------------------------------------------------------------
# §33.5 Versioned-Artifact-Contract round-trip
# ---------------------------------------------------------------------------


def test_artifact_to_dict_from_dict_round_trip():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        CADENCE_MANIFEST_SCHEMA_VERSION,
        CadenceManifest,
    )
    m = CadenceManifest(
        schema_version=CADENCE_MANIFEST_SCHEMA_VERSION,
        schedule_kind="cron",
        schedule_string="0 */8 * * *",
        interval_hint_s=28800,
        installed_at_iso="2026-05-06T07:00:00Z",
        installed_at_epoch=1234567890.0,
        installer_version="1.0",
        extras={"cost": "0.50"},
    )
    rt = CadenceManifest.from_dict(m.to_dict())
    assert rt is not None
    assert rt.schedule_kind == "cron"
    assert rt.interval_hint_s == 28800
    assert rt.extras["cost"] == "0.50"


def test_artifact_from_dict_unknown_kind_returns_none():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        CadenceManifest,
    )
    assert CadenceManifest.from_dict(
        {"schedule_kind": "weird"},
    ) is None


def test_artifact_from_dict_handles_garbage():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        CadenceManifest,
    )
    assert CadenceManifest.from_dict("not a dict") is None  # type: ignore
    assert CadenceManifest.from_dict({}) is None


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_2():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "cadence_manifest_authority_asymmetry",
        "cadence_manifest_versioned_artifact_compliance",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/graduation/"
        "cadence_manifest.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.iron_gate "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_versioned_artifact_pin_fires_on_missing_schema_version():
    from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
from dataclasses import dataclass
@dataclass(frozen=True)
class CadenceManifest:
    schedule_kind: str
    def to_dict(self): return {}
    @classmethod
    def from_dict(cls, p): return None
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "versioned_artifact_compliance" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("schema_version" in v for v in violations)


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance.graduation import (
        cadence_manifest,
    )
    expected = {
        "CADENCE_MANIFEST_SCHEMA_VERSION",
        "CadenceManifest",
        "derive_interval_hint_s",
        "derive_interval_hint_s_from_launchd_interval",
        "manifest_path",
        "read_manifest",
        "register_shipped_invariants",
        "write_manifest",
    }
    assert set(cadence_manifest.__all__) == expected


# ---------------------------------------------------------------------------
# CLI integration — write-cadence-manifest subcommand
# ---------------------------------------------------------------------------


def test_cli_subcommand_registered():
    """live_fire_graduation_soak.py CLI must register the
    write-cadence-manifest subcommand (installer-invokable
    seam)."""
    target = (
        _repo_root() / "scripts/live_fire_graduation_soak.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "write-cadence-manifest" in source
    assert "cmd_write_cadence_manifest" in source
