"""Regression spine for §40 Wave 3 #5 — MCP Output Scanner
(SemanticGuardian ↔ MCP boundary substrate).

Covers:

* §33.1 cognitive substrate default-FALSE
* Closed 3-value :class:`McpScanVerdict` taxonomy
* Closed 5-value :class:`CredentialKind` taxonomy
* All 5 canonical redact_secrets credential shapes detected via
  the composed Tier -1 pipeline (no parallel regex set)
* Pattern-delta detection — credentials pre-existing in prior
  conversation are NOT flagged
* MCP server whitelist predicate — empty allows all, populated
  enforces case-insensitive membership
* Findings bounded at max_findings env knob
* §33.5 frozen artifacts + to_dict projection
* 6 AST pin canonical-source pass + 6 synthetic regressions
* FlagRegistry seeds auto-discovered
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import (
    mcp_output_scanner as scanner,
)
from backend.core.ouroboros.governance.mcp_output_scanner import (
    CredentialKind,
    MCP_OUTPUT_SCANNER_SCHEMA_VERSION,
    McpScanFinding,
    McpScanReport,
    McpScanVerdict,
    _ENV_MASTER,
    _ENV_SERVER_WHITELIST,
    _LABEL_TO_KIND,
    _coerce_label,
    _enumerate_findings,
    _extract_labels,
    format_scan_panel,
    is_server_whitelisted,
    master_enabled,
    max_findings,
    scan_mcp_output,
    whitelisted_servers,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for env in (
        _ENV_MASTER,
        _ENV_SERVER_WHITELIST,
        "JARVIS_MCP_OUTPUT_SCANNER_MAX_FINDINGS",
    ):
        monkeypatch.delenv(env, raising=False)
    yield


@pytest.fixture
def master_on(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")


# ---------------------------------------------------------------------------
# Credential test fixtures (matching conversation_bridge regex set)
# ---------------------------------------------------------------------------


# 5 canonical credential shapes for hermetic testing. NOTE: these
# are SYNTHETIC strings matching the regex shape; they are NOT
# real credentials.
_FAKE_OPENAI = "sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_FAKE_SLACK = "xoxb-aaaaaaaaaa"
_FAKE_AWS = "AKIAIOSFODNN7EXAMPLE"  # 16 chars after AKIA
_FAKE_GITHUB = "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_FAKE_PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\nABCDEFGH\n"
    "-----END RSA PRIVATE KEY-----"
)


# ---------------------------------------------------------------------------
# §33.1 master flag
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_false(self):
        assert master_enabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
    def test_truthy(self, monkeypatch, truthy):
        monkeypatch.setenv(_ENV_MASTER, truthy)
        assert master_enabled() is True


# ---------------------------------------------------------------------------
# Env knob clamping
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_max_findings_default(self):
        assert max_findings() == 64

    def test_max_findings_clamped_low(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_MCP_OUTPUT_SCANNER_MAX_FINDINGS", "0",
        )
        assert max_findings() == 1

    def test_max_findings_clamped_high(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_MCP_OUTPUT_SCANNER_MAX_FINDINGS", "999999",
        )
        assert max_findings() == 10_000


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


class TestTaxonomies:
    def test_verdict_3_values(self):
        assert {v.value for v in McpScanVerdict} == {
            "clean", "credential_found", "disabled",
        }

    def test_credential_kind_5_values(self):
        assert {k.value for k in CredentialKind} == {
            "openai_key", "slack_token", "aws_key",
            "private_key", "github_token",
        }

    def test_label_to_kind_covers_all_5(self):
        assert set(_LABEL_TO_KIND.keys()) == {
            "openai-key", "slack-token", "aws-access-key",
            "private-key-block", "github-token",
        }

    def test_coerce_label_known(self):
        assert _coerce_label("openai-key") is CredentialKind.OPENAI_KEY
        assert _coerce_label("slack-token") is CredentialKind.SLACK_TOKEN
        assert _coerce_label("aws-access-key") is CredentialKind.AWS_KEY
        assert (
            _coerce_label("private-key-block")
            is CredentialKind.PRIVATE_KEY
        )
        assert (
            _coerce_label("github-token")
            is CredentialKind.GITHUB_TOKEN
        )

    def test_coerce_label_unknown(self):
        assert _coerce_label("unknown") is None
        assert _coerce_label("") is None

    def test_coerce_label_case_insensitive(self):
        assert (
            _coerce_label("OPENAI-KEY") is CredentialKind.OPENAI_KEY
        )


# ---------------------------------------------------------------------------
# Whitelist predicate
# ---------------------------------------------------------------------------


class TestServerWhitelist:
    def test_empty_whitelist_allows_all(self):
        assert is_server_whitelisted("github") is True
        assert is_server_whitelisted("anything") is True
        assert is_server_whitelisted("") is True

    def test_populated_whitelist_enforces(self, monkeypatch):
        monkeypatch.setenv(_ENV_SERVER_WHITELIST, "github,drive")
        assert is_server_whitelisted("github") is True
        assert is_server_whitelisted("drive") is True
        assert is_server_whitelisted("slack") is False

    def test_whitelist_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(_ENV_SERVER_WHITELIST, "GitHub")
        assert is_server_whitelisted("github") is True
        assert is_server_whitelisted("GITHUB") is True

    def test_whitelist_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv(
            _ENV_SERVER_WHITELIST, " github , drive ",
        )
        wl = whitelisted_servers()
        assert "github" in wl
        assert "drive" in wl

    def test_whitelisted_servers_returns_frozenset(self):
        # Defensive — operator-visible accessor returns frozenset
        assert isinstance(whitelisted_servers(), frozenset)


# ---------------------------------------------------------------------------
# scan_mcp_output — every credential shape detected
# ---------------------------------------------------------------------------


class TestCredentialDetection:
    """The scanner composes canonical redact_secrets; each of
    the 5 canonical shapes MUST be detected end-to-end."""

    def test_openai_key_detected(self, master_on):
        r = scan_mcp_output(f"oops {_FAKE_OPENAI} leaked")
        assert r.verdict is McpScanVerdict.CREDENTIAL_FOUND
        kinds = {f.kind for f in r.findings}
        assert CredentialKind.OPENAI_KEY in kinds

    def test_slack_token_detected(self, master_on):
        r = scan_mcp_output(f"token: {_FAKE_SLACK}")
        assert r.verdict is McpScanVerdict.CREDENTIAL_FOUND
        kinds = {f.kind for f in r.findings}
        assert CredentialKind.SLACK_TOKEN in kinds

    def test_aws_key_detected(self, master_on):
        r = scan_mcp_output(f"key: {_FAKE_AWS}")
        assert r.verdict is McpScanVerdict.CREDENTIAL_FOUND
        kinds = {f.kind for f in r.findings}
        assert CredentialKind.AWS_KEY in kinds

    def test_github_token_detected(self, master_on):
        r = scan_mcp_output(f"gh token: {_FAKE_GITHUB}")
        assert r.verdict is McpScanVerdict.CREDENTIAL_FOUND
        kinds = {f.kind for f in r.findings}
        assert CredentialKind.GITHUB_TOKEN in kinds

    def test_private_key_block_detected(self, master_on):
        r = scan_mcp_output(_FAKE_PRIVATE_KEY)
        assert r.verdict is McpScanVerdict.CREDENTIAL_FOUND
        kinds = {f.kind for f in r.findings}
        assert CredentialKind.PRIVATE_KEY in kinds

    def test_multiple_credentials_in_one_output(self, master_on):
        r = scan_mcp_output(
            f"both leak: {_FAKE_OPENAI} and {_FAKE_GITHUB}",
        )
        kinds = {f.kind for f in r.findings}
        assert CredentialKind.OPENAI_KEY in kinds
        assert CredentialKind.GITHUB_TOKEN in kinds


# ---------------------------------------------------------------------------
# Pattern-delta detection
# ---------------------------------------------------------------------------


class TestPatternDelta:
    def test_new_credential_flagged_as_delta(self, master_on):
        prior = "Earlier text — no credentials here"
        new = f"MCP brought in: {_FAKE_OPENAI}"
        r = scan_mcp_output(
            new, prior_conversation=prior,
        )
        assert r.verdict is McpScanVerdict.CREDENTIAL_FOUND
        assert r.delta_mode is True
        # The openai_key finding is NEW
        f = next(
            f for f in r.findings
            if f.kind is CredentialKind.OPENAI_KEY
        )
        assert f.is_pattern_delta is True

    def test_pre_existing_credential_not_flagged_in_delta_mode(
        self, master_on,
    ):
        """Credentials that ALREADY existed in prior conversation
        are dropped from delta-mode findings (not re-flagging)."""
        prior = f"Prior: {_FAKE_OPENAI}"
        new = f"MCP repeated: {_FAKE_OPENAI}"
        r = scan_mcp_output(
            new, prior_conversation=prior,
        )
        # In delta mode, pre-existing kinds excluded
        assert r.delta_mode is True
        kinds = {f.kind for f in r.findings}
        # openai_key was in prior → filtered out
        assert CredentialKind.OPENAI_KEY not in kinds
        # No NEW credentials → CLEAN
        assert r.verdict is McpScanVerdict.CLEAN

    def test_mixed_delta_keeps_new_drops_old(self, master_on):
        prior = f"Prior: {_FAKE_OPENAI}"
        new = (
            f"MCP repeated {_FAKE_OPENAI} but ALSO {_FAKE_GITHUB}"
        )
        r = scan_mcp_output(
            new, prior_conversation=prior,
        )
        kinds = {f.kind for f in r.findings}
        # openai_key pre-existing → excluded
        assert CredentialKind.OPENAI_KEY not in kinds
        # github_token NEW → included
        assert CredentialKind.GITHUB_TOKEN in kinds
        assert r.verdict is McpScanVerdict.CREDENTIAL_FOUND

    def test_delta_mode_marker_set_on_report(self, master_on):
        r = scan_mcp_output(
            "clean text", prior_conversation="also clean",
        )
        assert r.delta_mode is True

    def test_non_delta_mode_when_prior_none(self, master_on):
        r = scan_mcp_output("clean text")
        assert r.delta_mode is False


# ---------------------------------------------------------------------------
# Verdict cascade
# ---------------------------------------------------------------------------


class TestVerdictCascade:
    def test_master_off_returns_disabled(self):
        r = scan_mcp_output(f"oops {_FAKE_OPENAI}")
        assert r.verdict is McpScanVerdict.DISABLED
        assert r.master_enabled is False

    def test_master_on_empty_text_returns_clean(self, master_on):
        r = scan_mcp_output("")
        assert r.verdict is McpScanVerdict.CLEAN

    def test_master_on_whitespace_only_returns_clean(
        self, master_on,
    ):
        r = scan_mcp_output("   \n  ")
        assert r.verdict is McpScanVerdict.CLEAN

    def test_master_on_clean_text_returns_clean(self, master_on):
        r = scan_mcp_output(
            "MCP tool returned: project=foo branch=main",
        )
        assert r.verdict is McpScanVerdict.CLEAN
        assert len(r.findings) == 0

    def test_master_on_credential_returns_credential_found(
        self, master_on,
    ):
        r = scan_mcp_output(f"creds: {_FAKE_OPENAI}")
        assert r.verdict is McpScanVerdict.CREDENTIAL_FOUND
        assert len(r.findings) >= 1


# ---------------------------------------------------------------------------
# Non-string / defensive
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_none_input_returns_clean(self, master_on):
        r = scan_mcp_output(None)  # type: ignore[arg-type]
        assert r.verdict is McpScanVerdict.CLEAN

    def test_bytes_decoded(self, master_on):
        # str() on bytes → "b'...'" form which has no credential
        # shape; just verify NO crash
        r = scan_mcp_output(b"raw bytes")  # type: ignore[arg-type]
        assert r.master_enabled is True


# ---------------------------------------------------------------------------
# Server name diagnostics
# ---------------------------------------------------------------------------


class TestServerNameDiagnostic:
    def test_whitelisted_server_no_marker(self, master_on):
        r = scan_mcp_output(
            f"sensitive {_FAKE_OPENAI}",
            server_name="github",
        )
        # No whitelist enforced → no marker
        assert "not_whitelisted" not in r.diagnostic

    def test_non_whitelisted_server_marker_added(
        self, monkeypatch, master_on,
    ):
        monkeypatch.setenv(_ENV_SERVER_WHITELIST, "github")
        r = scan_mcp_output(
            f"sensitive {_FAKE_OPENAI}",
            server_name="slack",
        )
        # Slack not in allowlist → marker
        assert "not_whitelisted:slack" in r.diagnostic


# ---------------------------------------------------------------------------
# §33.5 frozen artifacts
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_finding_to_dict(self):
        f = McpScanFinding(
            kind=CredentialKind.OPENAI_KEY,
            label="openai-key",
            occurrences_in_output=3,
            is_pattern_delta=True,
            source_label="mcp_test",
        )
        d = f.to_dict()
        assert d["kind"] == "openai_key"
        assert d["occurrences_in_output"] == 3
        assert d["is_pattern_delta"] is True
        assert d["schema_version"] == (
            MCP_OUTPUT_SCANNER_SCHEMA_VERSION
        )

    def test_report_to_dict_shape(self, master_on):
        r = scan_mcp_output(f"oops {_FAKE_OPENAI}")
        d = r.to_dict()
        expected = {
            "scanned_at_unix", "master_enabled", "verdict",
            "findings", "bytes_scanned", "bytes_redacted",
            "delta_mode", "source_label", "diagnostic",
            "elapsed_s", "schema_version",
        }
        assert set(d.keys()) == expected
        assert d["verdict"] == "credential_found"

    def test_findings_bounded(self, master_on, monkeypatch):
        # max_findings clamps results — feed 100 different kinds
        monkeypatch.setenv(
            "JARVIS_MCP_OUTPUT_SCANNER_MAX_FINDINGS", "2",
        )
        # All 5 kinds → 5 distinct labels
        text = (
            f"{_FAKE_OPENAI} {_FAKE_SLACK} {_FAKE_AWS} "
            f"{_FAKE_GITHUB} {_FAKE_PRIVATE_KEY}"
        )
        r = scan_mcp_output(text)
        # Capped at 2 findings
        assert len(r.findings) == 2


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestInternalHelpers:
    def test_extract_labels_empty(self):
        assert _extract_labels("") == frozenset()

    def test_extract_labels_returns_kinds(self, master_on):
        labels = _extract_labels(f"{_FAKE_OPENAI} and {_FAKE_GITHUB}")
        assert "openai-key" in labels
        assert "github-token" in labels

    def test_enumerate_findings_caps_at_max(self):
        # Build synthetic redacted text with many distinct kinds
        redacted = " ".join(
            f"[REDACTED:{label}]"
            for label in (
                "openai-key", "github-token", "aws-access-key",
            )
        )
        findings = _enumerate_findings(redacted, "test", None)
        assert len(findings) == 3
        # Source label propagates
        assert all(f.source_label == "test" for f in findings)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TestRenderer:
    def test_master_off_disabled_marker(self):
        out = format_scan_panel(text="anything")
        assert "disabled" in out

    def test_master_on_panel_renders(self, master_on):
        out = format_scan_panel(
            text=f"{_FAKE_OPENAI}",
            source_label="mcp_test",
        )
        assert "MCP Output Scan" in out
        assert "openai_key" in out


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_source():
    src = Path(scanner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    return src, tree


@pytest.fixture
def pins():
    return scanner.register_shipped_invariants()


class TestAstPinsCanonicalPass:
    def test_6_pins_registered(self, pins):
        assert len(pins) == 6
        names = {p.invariant_name for p in pins}
        assert names == {
            "mcp_output_scanner_verdict_taxonomy_closed",
            "mcp_output_scanner_kind_taxonomy_closed",
            "mcp_output_scanner_label_map_coverage",
            "mcp_output_scanner_authority_asymmetry",
            "mcp_output_scanner_master_default_false",
            "mcp_output_scanner_composes_canonical",
        }

    @pytest.mark.parametrize(
        "pin_name",
        [
            "mcp_output_scanner_verdict_taxonomy_closed",
            "mcp_output_scanner_kind_taxonomy_closed",
            "mcp_output_scanner_label_map_coverage",
            "mcp_output_scanner_authority_asymmetry",
            "mcp_output_scanner_master_default_false",
            "mcp_output_scanner_composes_canonical",
        ],
    )
    def test_pin_passes(self, canonical_source, pins, pin_name):
        src, tree = canonical_source
        pin = next(
            p for p in pins if p.invariant_name == pin_name
        )
        assert not pin.validate(tree, src)


class TestAstPinsSynthetic:
    def test_verdict_pin_fires(self, pins):
        synthetic = """
import enum
class McpScanVerdict(str, enum.Enum):
    CLEAN = "clean"
    # MISSING: credential_found, disabled
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "mcp_output_scanner_verdict_taxonomy_closed"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_kind_pin_fires_on_drift(self, pins):
        synthetic = """
import enum
class CredentialKind(str, enum.Enum):
    OPENAI_KEY = "openai_key"
    SLACK_TOKEN = "slack_token"
    AWS_KEY = "aws_key"
    PRIVATE_KEY = "private_key"
    GITHUB_TOKEN = "github_token"
    EXTRA = "extra_unexpected"
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "mcp_output_scanner_kind_taxonomy_closed"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "drift" in violations[0]

    def test_label_map_pin_fires_on_missing(self, pins):
        synthetic = """
_LABEL_TO_KIND = {
    "openai-key": None,
    "github-token": None,
    # MISSING: slack-token, aws-access-key, private-key-block
}
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "mcp_output_scanner_label_map_coverage"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "slack-token" in violations[0]

    def test_authority_pin_fires(self, pins):
        synthetic = (
            "from backend.core.ouroboros.governance.mcp_tool_client "
            "import x\n"
        )
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "mcp_output_scanner_authority_asymmetry"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "mcp_tool_client" in violations[0]

    def test_master_pin_fires_on_default_true(self, pins):
        synthetic = """
def master_enabled():
    return _flag("FOO", default=True)
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "mcp_output_scanner_master_default_false"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_composes_pin_fires_on_missing_redact_secrets(self, pins):
        synthetic = "x = 1\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "mcp_output_scanner_composes_canonical"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        # Must mention both compose requirements
        assert any(
            "redact_secrets" in v for v in violations
        )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


class TestFlagSeeds:
    def test_seeds_auto_discovered(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        names = {f.name for f in reg.list_all()}
        for expected in [
            "JARVIS_MCP_OUTPUT_SCANNER_ENABLED",
            "JARVIS_MCP_SERVER_WHITELIST",
            "JARVIS_MCP_OUTPUT_SCANNER_MAX_FINDINGS",
        ]:
            assert expected in names

    def test_master_safety_default_false(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        spec = next(
            f for f in reg.list_all()
            if f.name == "JARVIS_MCP_OUTPUT_SCANNER_ENABLED"
        )
        assert spec.default is False
        assert spec.category.value == "safety"


# ---------------------------------------------------------------------------
# Canonical reuse — NO parallel regex set
# ---------------------------------------------------------------------------


class TestCanonicalReuse:
    def test_scanner_uses_redact_secrets_via_label_markers(self):
        """The scanner MUST compose conversation_bridge.redact_secrets
        verbatim — we verify by checking the labels found align
        with redact_secrets' label vocabulary."""
        from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
            redact_secrets,
        )
        text = (
            f"{_FAKE_OPENAI} {_FAKE_SLACK} {_FAKE_AWS} "
            f"{_FAKE_GITHUB} {_FAKE_PRIVATE_KEY}"
        )
        redacted, n = redact_secrets(text)
        # All 5 labels appear in the redacted output
        assert "[REDACTED:openai-key]" in redacted
        assert "[REDACTED:slack-token]" in redacted
        assert "[REDACTED:aws-access-key]" in redacted
        assert "[REDACTED:github-token]" in redacted
        assert "[REDACTED:private-key-block]" in redacted
        # And the scanner's label_to_kind covers each
        for label in (
            "openai-key", "slack-token", "aws-access-key",
            "github-token", "private-key-block",
        ):
            assert label in _LABEL_TO_KIND

    def test_scanner_does_not_define_parallel_regex(self):
        """Source AST check — scanner MUST NOT define its own
        credential regex set."""
        src = Path(scanner.__file__).read_text(encoding="utf-8")
        # No re.compile lines for credential patterns
        # (only the [REDACTED:label] marker regex is allowed)
        assert "sk-[A-Za-z0-9]" not in src
        assert "AKIA[0-9A-Z]" not in src
        assert "ghp_" not in src or "ghp_aaaa" in src  # only in docs


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_all_exports_present(self):
        for name in scanner.__all__:
            assert getattr(scanner, name) is not None

    def test_schema_version(self):
        assert MCP_OUTPUT_SCANNER_SCHEMA_VERSION.startswith(
            "mcp_output_scanner.",
        )
