"""Slice 125 — Aegis credential bootstrap + health probe.

Proves the root invariant: the funded provider key reaches the env BEFORE the
Aegis snapshot, explicit exports win, malformed/missing .env fails clean with no
secret leak, and the Aegis-routed probe maps a 402 to
``aegis_credential_injection_failed`` (NOT ``provider_out_of_credits``).
"""

from __future__ import annotations

from backend.core.ouroboros.aegis import credential_env_loader as L
from backend.core.ouroboros.aegis.credential_probe import (
    CredentialVerdict as V,
    classify_credential_probe,
    is_fatal,
)

_FAKE_KEY = "sk-FAKE-doubleword-test-key-0123456789"


class TestEnvLoader:
    def test_dotenv_fills_absent_key_before_snapshot(self, tmp_path):
        dotenv = tmp_path / ".env"
        dotenv.write_text(f"DOUBLEWORD_API_KEY={_FAKE_KEY}\nOTHER=ignored\n")
        env = {}  # simulates the pre-snapshot process env WITHOUT the key
        rep = L.load_provider_credentials(dotenv_path=str(dotenv), env=env)
        assert env["DOUBLEWORD_API_KEY"] == _FAKE_KEY  # now present before snapshot
        assert "DOUBLEWORD_API_KEY" in rep.loaded
        assert rep.ok and rep.dotenv_present
        # Non-allowlisted keys are NOT injected.
        assert "OTHER" not in env

    def test_explicit_export_wins_over_dotenv(self, tmp_path):
        dotenv = tmp_path / ".env"
        dotenv.write_text(f"DOUBLEWORD_API_KEY={_FAKE_KEY}\n")
        env = {"DOUBLEWORD_API_KEY": "sk-EXPLICIT-operator-export"}
        rep = L.load_provider_credentials(dotenv_path=str(dotenv), env=env)
        assert env["DOUBLEWORD_API_KEY"] == "sk-EXPLICIT-operator-export"  # not overwritten
        assert "DOUBLEWORD_API_KEY" in rep.already_present
        assert "DOUBLEWORD_API_KEY" not in rep.loaded

    def test_missing_dotenv_is_clean_noop(self, tmp_path):
        env = {}
        rep = L.load_provider_credentials(dotenv_path=str(tmp_path / "nope.env"), env=env)
        assert rep.ok and not rep.dotenv_present and not rep.loaded
        assert env == {}

    def test_malformed_dotenv_no_crash_no_leak(self, tmp_path):
        dotenv = tmp_path / ".env"
        # Garbage + a would-be shell injection line — must NOT execute, must parse safely.
        dotenv.write_text("this is not valid\n$(rm -rf /)\nDOUBLEWORD_API_KEY=" + _FAKE_KEY + "\n`evil`\n")
        env = {}
        rep = L.load_provider_credentials(dotenv_path=str(dotenv), env=env)
        # The valid credential line is still extracted; the junk lines are ignored.
        assert env.get("DOUBLEWORD_API_KEY") == _FAKE_KEY
        assert rep.ok  # garbled lines are skipped, not fatal

    def test_quoted_values_unwrapped(self, tmp_path):
        dotenv = tmp_path / ".env"
        dotenv.write_text(f'DOUBLEWORD_API_KEY="{_FAKE_KEY}"\nANTHROPIC_API_KEY=\'sk-ant-x\'\n')
        env = {}
        L.load_provider_credentials(dotenv_path=str(dotenv), env=env)
        assert env["DOUBLEWORD_API_KEY"] == _FAKE_KEY
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-x"

    def test_export_prefix_handled(self, tmp_path):
        dotenv = tmp_path / ".env"
        dotenv.write_text(f"export DOUBLEWORD_API_KEY={_FAKE_KEY}\n")
        env = {}
        L.load_provider_credentials(dotenv_path=str(dotenv), env=env)
        assert env["DOUBLEWORD_API_KEY"] == _FAKE_KEY


class TestRedaction:
    def test_fingerprint_never_reveals_key(self):
        fp = L.fingerprint(_FAKE_KEY)
        assert fp.startswith("sha256:")
        # No substring of the raw key appears in the fingerprint.
        assert _FAKE_KEY not in fp
        assert _FAKE_KEY[4:20] not in fp

    def test_report_format_has_no_raw_secret(self, tmp_path):
        dotenv = tmp_path / ".env"
        dotenv.write_text(f"DOUBLEWORD_API_KEY={_FAKE_KEY}\n")
        env = {}
        rep = L.load_provider_credentials(dotenv_path=str(dotenv), env=env)
        line = L.format_report(rep)
        assert _FAKE_KEY not in line          # the log line never carries the secret
        assert "DOUBLEWORD_API_KEY" in line   # but names + fingerprints are fine
        assert "sha256:" in line


class TestProbeClassification:
    def test_402_via_aegis_is_injection_failure_not_out_of_credits(self):
        # The exact failure that cost four soaks: funded key works direct (200),
        # Aegis path 402. Must be injection failure, NOT provider_out_of_credits.
        assert classify_credential_probe(200, 402) is V.AEGIS_CREDENTIAL_INJECTION_FAILED
        assert classify_credential_probe(200, 401) is V.AEGIS_CREDENTIAL_INJECTION_FAILED

    def test_direct_402_is_operator_problem(self):
        assert classify_credential_probe(402, 402) is V.OPERATOR_CREDENTIAL_PROBLEM
        assert classify_credential_probe(401, None) is V.OPERATOR_CREDENTIAL_PROBLEM

    def test_both_ok(self):
        assert classify_credential_probe(200, 200) is V.OK

    def test_transport(self):
        assert classify_credential_probe(None, None) is V.TRANSPORT_PROBLEM
        assert classify_credential_probe(200, None) is V.TRANSPORT_PROBLEM
        assert classify_credential_probe(503, 200) is V.TRANSPORT_PROBLEM

    def test_fatal_verdicts_halt_soak(self):
        assert is_fatal(V.AEGIS_CREDENTIAL_INJECTION_FAILED) is True
        assert is_fatal(V.OPERATOR_CREDENTIAL_PROBLEM) is True
        assert is_fatal(V.OK) is False
        assert is_fatal(V.TRANSPORT_PROBLEM) is False  # transient — don't hard-fail


class TestConflictDetection:
    """Slice 126.5 — a stale env credential differing from the funded .env must
    be surfaced LOUDLY (the ghost that cost five soaks), with NO secret leak."""

    def test_conflict_surfaced_redacted(self, tmp_path):
        dotenv = tmp_path / ".env"
        dotenv.write_text("DOUBLEWORD_API_KEY=sk-FUNDED-correct\n")
        env = {"DOUBLEWORD_API_KEY": "sk-STALE-ghost"}  # a sibling-.env shadow
        rep = L.load_provider_credentials(dotenv_path=str(dotenv), env=env)
        assert "DOUBLEWORD_API_KEY" in rep.conflicts
        line = L.format_report(rep)
        assert "CONFLICT" in line
        assert "sk-FUNDED" not in line and "sk-STALE" not in line  # redacted
        # Explicit env still wins (we don't silently overwrite) — we WARN.
        assert env["DOUBLEWORD_API_KEY"] == "sk-STALE-ghost"

    def test_no_conflict_when_values_match(self, tmp_path):
        dotenv = tmp_path / ".env"
        dotenv.write_text("DOUBLEWORD_API_KEY=sk-SAME\n")
        env = {"DOUBLEWORD_API_KEY": "sk-SAME"}
        rep = L.load_provider_credentials(dotenv_path=str(dotenv), env=env)
        assert rep.conflicts == {}
