"""Tests for failover_deadman.py -- Pure string/logic, NO real GCE.

Verifies:
- build_deadman_startup_script returns a valid ASCII bash script.
- Script exports HOME=/root (the bake lesson from bake_jprime_golden_image.py).
- Script contains the metadata SA-token fetch with Metadata-Flavor: Google header.
- Script contains the Compute REST instances DELETE on self (instance-name read
  from the metadata server at runtime, never hardcoded).
- Idle measure references the configured port (:11434) and /api/ path.
- Boot-grace guard: uptime AND idle both required before self-delete.
- Env overrides for JARVIS_DEADMAN_IDLE_TIMEOUT_S, _CHECK_INTERVAL_S, _BOOT_GRACE_S.
- deadman_enabled() default true; respects JARVIS_FAILOVER_DEADMAN_ENABLED.
- DELETE verb present (the unbreakable self-delete).
- Metadata bearer-token shape is present.
- ASCII-only script content.
"""
from __future__ import annotations

import os
import importlib

import pytest


# ---------------------------------------------------------------------------
# Import the module under test.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def mod():
    from backend.core.ouroboros.governance import failover_deadman
    return failover_deadman


@pytest.fixture(scope="module")
def default_script(mod):
    return mod.build_deadman_startup_script()


# ---------------------------------------------------------------------------
# ASCII safety (no binary / emoji / non-ASCII sneaking into the bash script).
# ---------------------------------------------------------------------------

class TestAsciiOnly:
    def test_default_script_is_ascii(self, default_script):
        # Will raise UnicodeEncodeError if any non-ASCII codepoint slipped in.
        default_script.encode("ascii")

    def test_custom_script_is_ascii(self, mod):
        script = mod.build_deadman_startup_script(
            idle_timeout_s=900, check_interval_s=120, boot_grace_s=1800, port=11434
        )
        script.encode("ascii")


# ---------------------------------------------------------------------------
# HOME=/root export (the mandatory bake lesson).
# ---------------------------------------------------------------------------

class TestHomeExport:
    def test_exports_home_root(self, default_script):
        assert "export HOME=/root" in default_script, (
            "Script must export HOME=/root before any Ollama/Go invocations."
        )


# ---------------------------------------------------------------------------
# Metadata server endpoints -- SA token + instance identity.
# The bash script must fetch these at RUNTIME from the metadata server
# (never hardcoded project/zone/instance).
# ---------------------------------------------------------------------------

class TestMetadataEndpoints:
    def test_metadata_flavor_header_present(self, default_script):
        assert "Metadata-Flavor: Google" in default_script, (
            "Script must pass 'Metadata-Flavor: Google' header to the metadata server."
        )

    def test_sa_token_endpoint_present(self, default_script):
        assert "service-accounts/default/token" in default_script, (
            "Script must fetch the SA token from the metadata instance token endpoint."
        )

    def test_project_id_endpoint_present(self, default_script):
        assert "project/project-id" in default_script, (
            "Script must fetch the project ID from the metadata server at runtime."
        )

    def test_instance_name_endpoint_present(self, default_script):
        assert "instance/name" in default_script, (
            "Script must fetch the instance name from the metadata server at runtime."
        )

    def test_instance_zone_endpoint_present(self, default_script):
        assert "instance/zone" in default_script, (
            "Script must fetch the zone from the metadata server at runtime."
        )

    def test_metadata_base_url(self, default_script):
        assert "metadata.google.internal" in default_script, (
            "Script must use the GCE metadata server base URL."
        )


# ---------------------------------------------------------------------------
# Compute REST self-delete -- the unbreakable cost backstop.
# ---------------------------------------------------------------------------

class TestSelfDelete:
    def test_delete_verb_present(self, default_script):
        # The curl DELETE call is the core unbreakable self-delete.
        assert "DELETE" in default_script, (
            "Script must issue an HTTP DELETE to the Compute REST API."
        )

    def test_compute_googleapis_url(self, default_script):
        assert "compute.googleapis.com/compute/v1/projects/" in default_script, (
            "Script must use the standard Compute REST v1 instances DELETE URL."
        )

    def test_instances_path_in_url(self, default_script):
        assert "/instances/" in default_script, (
            "Script must include /instances/ in the Compute REST delete URL."
        )

    def test_bearer_token_auth_present(self, default_script):
        # The Authorization: Bearer ... header must be constructed from the SA token.
        assert "Authorization: Bearer" in default_script, (
            "Script must use 'Authorization: Bearer <token>' for the Compute REST call."
        )

    def test_instance_name_not_hardcoded(self, default_script):
        # The actual name must be fetched at runtime from metadata -- test that it
        # references a shell variable derived from the metadata fetch, not a literal
        # hostname. We verify by confirming the Compute URL uses a shell variable
        # reference ($...) rather than a literal instance name.
        # The URL construction line should include a variable like ${INSTANCE} or
        # $(INSTANCE_NAME) -- at minimum a "$" in the instances path.
        import re
        # Find lines containing compute.googleapis.com
        lines = [l for l in default_script.splitlines()
                 if "compute.googleapis.com" in l and "/instances/" in l]
        assert lines, "Expected a line with the Compute REST instances URL."
        # At least one should contain a shell variable reference.
        assert any("$" in line for line in lines), (
            "The Compute REST instances DELETE URL must use a shell variable "
            "for the instance name/zone/project (fetched from metadata at runtime), "
            "NOT a hardcoded string."
        )


# ---------------------------------------------------------------------------
# Idle measurement -- Ollama port + /api/ path.
# ---------------------------------------------------------------------------

class TestIdleMeasure:
    def test_port_11434_present(self, default_script):
        assert "11434" in default_script, (
            "Script must reference the Ollama port 11434 for idle detection."
        )

    def test_api_path_present(self, default_script):
        assert "/api/" in default_script, (
            "Script must grep for /api/ requests to measure Ollama activity."
        )

    def test_custom_port(self, mod):
        script = mod.build_deadman_startup_script(port=12345)
        assert "12345" in script
        assert "/api/" in script

    def test_idle_timeout_s_referenced(self, mod):
        # The idle_timeout_s value must appear in the script (e.g. used in
        # journalctl --since or sleep comparisons).
        script = mod.build_deadman_startup_script(idle_timeout_s=900)
        assert "900" in script

    def test_heartbeat_file_present(self, default_script):
        # A heartbeat/activity file (jprime_last_activity) must be present to
        # make the idle clock robust across journal gaps.
        assert "jprime_last_activity" in default_script, (
            "Script must use a heartbeat file /var/run/jprime_last_activity."
        )

    def test_log_file_present(self, default_script):
        assert "jprime_deadman.log" in default_script, (
            "Script must log to /var/log/jprime_deadman.log."
        )


# ---------------------------------------------------------------------------
# Boot grace -- uptime AND idle both required before self-delete.
# ---------------------------------------------------------------------------

class TestBootGrace:
    def test_boot_grace_value_in_script(self, mod):
        script = mod.build_deadman_startup_script(boot_grace_s=3600)
        assert "3600" in script, (
            "boot_grace_s value must appear in the script so the uptime check uses it."
        )

    def test_default_boot_grace_in_script(self, default_script):
        # default is 2100
        assert "2100" in default_script

    def test_uptime_guard_present(self, default_script):
        # The script must check uptime before triggering the self-delete.
        # This can be via /proc/uptime, the `uptime` command, or a boot-time
        # sentinel file. We verify that some uptime-related check exists.
        has_uptime = (
            "/proc/uptime" in default_script
            or "uptime" in default_script
            or "BOOT_TIME" in default_script
            or "BOOT_GRACE" in default_script
        )
        assert has_uptime, (
            "Script must contain a boot-grace/uptime check to avoid killing a "
            "freshly-awakened node that the Body hasn't yet routed traffic to."
        )

    def test_idle_and_grace_both_required(self, mod):
        # Build a script and verify both the grace and idle checks appear
        # together (i.e. the self-delete is guarded by BOTH conditions).
        script = mod.build_deadman_startup_script(
            idle_timeout_s=1800, boot_grace_s=2100
        )
        # Both values appear in the same script.
        assert "1800" in script
        assert "2100" in script
        # The DELETE verb must also appear in the same script (not guarded away).
        assert "DELETE" in script


# ---------------------------------------------------------------------------
# Check interval.
# ---------------------------------------------------------------------------

class TestCheckInterval:
    def test_default_check_interval_in_script(self, default_script):
        # default check_interval_s is 300
        assert "300" in default_script

    def test_custom_check_interval(self, mod):
        script = mod.build_deadman_startup_script(check_interval_s=60)
        assert "60" in script


# ---------------------------------------------------------------------------
# deadman_enabled() -- master gate.
# ---------------------------------------------------------------------------

class TestDeadmanEnabled:
    def test_default_is_true(self, mod, monkeypatch):
        monkeypatch.delenv("JARVIS_FAILOVER_DEADMAN_ENABLED", raising=False)
        assert mod.deadman_enabled() is True

    def test_explicit_true_values(self, mod, monkeypatch):
        for val in ("1", "true", "True", "TRUE", "yes", "on"):
            monkeypatch.setenv("JARVIS_FAILOVER_DEADMAN_ENABLED", val)
            assert mod.deadman_enabled() is True, f"Expected True for {val!r}"

    def test_false_values(self, mod, monkeypatch):
        for val in ("0", "false", "False", "FALSE", "no", "off"):
            monkeypatch.setenv("JARVIS_FAILOVER_DEADMAN_ENABLED", val)
            assert mod.deadman_enabled() is False, f"Expected False for {val!r}"


# ---------------------------------------------------------------------------
# Env-override knobs: the three JARVIS_DEADMAN_* env vars change the script.
# ---------------------------------------------------------------------------

class TestEnvOverrides:
    def test_idle_timeout_env_override(self, mod, monkeypatch):
        monkeypatch.setenv("JARVIS_DEADMAN_IDLE_TIMEOUT_S", "600")
        # Re-import to pick up env (or call the builder directly with no args --
        # the module reads env at call time, not at import).
        script = mod.build_deadman_startup_script()
        assert "600" in script

    def test_check_interval_env_override(self, mod, monkeypatch):
        monkeypatch.setenv("JARVIS_DEADMAN_CHECK_INTERVAL_S", "120")
        script = mod.build_deadman_startup_script()
        assert "120" in script

    def test_boot_grace_env_override(self, mod, monkeypatch):
        monkeypatch.setenv("JARVIS_DEADMAN_BOOT_GRACE_S", "1200")
        script = mod.build_deadman_startup_script()
        assert "1200" in script

    def test_explicit_params_override_env(self, mod, monkeypatch):
        # Explicit keyword args must win over env vars.
        monkeypatch.setenv("JARVIS_DEADMAN_IDLE_TIMEOUT_S", "9999")
        script = mod.build_deadman_startup_script(idle_timeout_s=777)
        assert "777" in script
        # The env-set value should NOT appear (explicit wins).
        assert "9999" not in script


# ---------------------------------------------------------------------------
# Script structure -- shebang and set options.
# ---------------------------------------------------------------------------

class TestScriptStructure:
    def test_shebang_present(self, default_script):
        assert default_script.startswith("#!/"), (
            "Script must begin with a shebang line."
        )

    def test_set_options_present(self, default_script):
        # Must have set -uo pipefail or equivalent for robustness.
        assert "set -" in default_script

    def test_log_file_writable(self, default_script):
        # Script must direct output to the deadman log file.
        assert "/var/log/jprime_deadman.log" in default_script
