"""Regression spine — Operator Commit Authority (OCA) Slice 1.

Mirrors ``test_governance_manifest.py`` structure: isolation fixture,
master-flag matrix, closed taxonomy, grant lifecycle, IDE-no-env path,
concurrent append, AST pins (canonical pass + synthetic regression),
FlagRegistry seeds.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import (
    operator_commit_authority as oca,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point grant ledger + secret at tmp; clear OCA env."""
    for env in (
        "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED",
        "JARVIS_COMMIT_GRANT_DEFAULT_TTL_S",
        "JARVIS_COMMIT_GRANT_PLAN_TTL_S",
        "JARVIS_COMMIT_AUTHORITY_GRANTS_PATH",
        "JARVIS_COMMIT_AUTHORITY_SECRET_PATH",
        "JARVIS_COMMIT_AUTHORITY_ENABLE_FILE",
        "JARVIS_LEDGER_SOVEREIGNTY_ENABLED",
        "JARVIS_GOVERNANCE_MANIFEST_ENABLED",
        "JARVIS_OPERATION_MODE",
        "JARVIS_OPERATION_MODE_ENABLED",
        # The legacy shell token must be irrelevant — never set it.
        "JARVIS_AUTHORIZE_COMMIT_TOKEN",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_GRANTS_PATH",
        str(tmp_path / "grants.jsonl"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_SECRET_PATH",
        str(tmp_path / "secret"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_ENABLE_FILE",
        str(tmp_path / "enabled"),
    )
    yield


def _on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true"
    )


def _ctx(tmp_path, **kw):
    kw.setdefault("channel", "ide")
    kw.setdefault("repo_root", str(tmp_path))
    return oca.CommitAuthorityContext(**kw)


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_false(self):
        assert oca.master_enabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "TRUE"])
    def test_truthy(self, monkeypatch, truthy):
        monkeypatch.setenv(
            "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", truthy
        )
        assert oca.master_enabled() is True

    def test_off_returns_disabled(self, tmp_path):
        v = oca.verify_pre_commit(_ctx(tmp_path))
        assert v.verdict is oca.CommitAuthorityVerdict.DISABLED
        assert v.authorized() is True  # DISABLED = legacy pass-through


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


class TestTaxonomy:
    def test_verdict_exactly_8(self):
        assert {v.value for v in oca.CommitAuthorityVerdict} == {
            "authorized",
            "denied_no_grant",
            "denied_expired",
            "denied_scope",
            "denied_governance_drift",
            "denied_sovereignty",
            "disabled",
            "channel_unknown",
        }

    def test_channel_exactly_5(self):
        assert {c.value for c in oca.CommitChannel} == {
            "repl",
            "cli",
            "ide",
            "daemon",
            "autonomous",
        }

    @pytest.mark.parametrize(
        "raw,exp",
        [
            ("ide", oca.CommitChannel.IDE),
            ("REPL", oca.CommitChannel.REPL),
            (" cli ", oca.CommitChannel.CLI),
            ("bogus", None),
            ("", None),
        ],
    )
    def test_channel_parse(self, raw, exp):
        assert oca.CommitChannel.parse(raw) is exp

    def test_is_authorized_predicate(self):
        V = oca.CommitAuthorityVerdict
        assert oca.is_authorized_verdict(V.AUTHORIZED)
        assert oca.is_authorized_verdict(V.DISABLED)
        assert not oca.is_authorized_verdict(V.DENIED_NO_GRANT)
        assert not oca.is_authorized_verdict(V.CHANNEL_UNKNOWN)
        assert not oca.is_authorized_verdict("garbage")


# ---------------------------------------------------------------------------
# Dataclass roundtrip (§33.5)
# ---------------------------------------------------------------------------


class TestRoundtrip:
    def test_grant_roundtrip_lossless(self):
        g = oca.CommitGrant(
            grant_id="abc",
            issued_at_unix=100.0,
            expires_at_unix=200.0,
            repo_root_sha256="deadbeef",
            branch="main",
            channel="ide",
            scopes=("docs/", "src/"),  # already canonical (sorted)
            operator_label="derek",
            governance_amend=True,
        )
        back = oca.CommitGrant.from_dict(g.to_dict())
        assert back == g

    def test_scopes_normalized_for_stable_hmac(self):
        """Scope order must not change the signed payload — two
        grants differing only in scope order sign identically (the
        HMAC is over sorted scopes)."""
        a = oca.CommitGrant.from_dict(
            oca.CommitGrant(
                grant_id="x",
                issued_at_unix=1.0,
                expires_at_unix=2.0,
                repo_root_sha256="f",
                branch="",
                channel="ide",
                scopes=("src/", "docs/"),
                operator_label="d",
            ).to_dict()
        )
        b = oca.CommitGrant.from_dict(
            oca.CommitGrant(
                grant_id="x",
                issued_at_unix=1.0,
                expires_at_unix=2.0,
                repo_root_sha256="f",
                branch="",
                channel="ide",
                scopes=("docs/", "src/"),
                operator_label="d",
            ).to_dict()
        )
        assert a == b
        # Idempotent: a normalized grant round-trips exactly.
        assert oca.CommitGrant.from_dict(a.to_dict()) == a

    def test_from_dict_rejects_empty_id(self):
        assert oca.CommitGrant.from_dict({"grant_id": ""}) is None

    def test_verdict_result_to_dict(self, tmp_path):
        v = oca.verify_pre_commit(_ctx(tmp_path))
        d = v.to_dict()
        assert d["verdict"] == "disabled"
        assert "schema_version" in d


# ---------------------------------------------------------------------------
# Channel-unknown fail-closed
# ---------------------------------------------------------------------------


def test_unknown_channel_denied(monkeypatch, tmp_path):
    _on(monkeypatch)
    v = oca.verify_pre_commit(_ctx(tmp_path, channel="banana"))
    assert v.verdict is oca.CommitAuthorityVerdict.CHANNEL_UNKNOWN


# ---------------------------------------------------------------------------
# Operator grant lifecycle
# ---------------------------------------------------------------------------


class TestGrantLifecycle:
    def test_no_grant_denied(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        v = oca.verify_pre_commit(_ctx(tmp_path))
        assert v.verdict is oca.CommitAuthorityVerdict.DENIED_NO_GRANT

    def test_issue_then_authorized_no_shell_env(
        self, monkeypatch, tmp_path
    ):
        """The core acceptance scenario: a grant file alone (no
        JARVIS_AUTHORIZE_COMMIT_TOKEN export) authorizes an IDE
        commit."""
        _on(monkeypatch)
        out = oca.issue_grant(
            channel="ide",
            operator_label="derek",
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        assert out.ok and out.grant_id
        assert "JARVIS_AUTHORIZE_COMMIT_TOKEN" not in __import__(
            "os"
        ).environ
        v = oca.verify_pre_commit(
            _ctx(tmp_path, channel="ide", now_unix=1100.0)
        )
        assert v.verdict is oca.CommitAuthorityVerdict.AUTHORIZED
        assert v.matched_grant_id == out.grant_id

    def test_expired_grant_denied(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        oca.issue_grant(
            channel="ide",
            operator_label="d",
            ttl_s=60,
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        v = oca.verify_pre_commit(
            _ctx(tmp_path, channel="ide", now_unix=999_999.0)
        )
        assert v.verdict is oca.CommitAuthorityVerdict.DENIED_EXPIRED

    def test_scope_miss_denied(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        oca.issue_grant(
            channel="ide",
            operator_label="d",
            scopes=["src/"],
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        v = oca.verify_pre_commit(
            _ctx(
                tmp_path,
                channel="ide",
                staged_files=("docs/readme.md",),
                now_unix=1100.0,
            )
        )
        assert v.verdict is oca.CommitAuthorityVerdict.DENIED_SCOPE

    def test_scope_hit_authorized(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        oca.issue_grant(
            channel="ide",
            operator_label="d",
            scopes=["src/"],
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        v = oca.verify_pre_commit(
            _ctx(
                tmp_path,
                channel="ide",
                staged_files=("src/app/main.py",),
                now_unix=1100.0,
            )
        )
        assert v.verdict is oca.CommitAuthorityVerdict.AUTHORIZED

    def test_channel_mismatch_not_matched(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        oca.issue_grant(
            channel="ide",
            operator_label="d",
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        v = oca.verify_pre_commit(
            _ctx(tmp_path, channel="cli", now_unix=1100.0)
        )
        assert v.verdict is oca.CommitAuthorityVerdict.DENIED_NO_GRANT

    def test_wrong_repo_root_not_matched(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        oca.issue_grant(
            channel="ide",
            operator_label="d",
            repo_root=tmp_path / "repoA",
            now_unix=1000.0,
        )
        v = oca.verify_pre_commit(
            oca.CommitAuthorityContext(
                channel="ide",
                repo_root=str(tmp_path / "repoB"),
                now_unix=1100.0,
            )
        )
        assert v.verdict is oca.CommitAuthorityVerdict.DENIED_NO_GRANT

    def test_forged_signature_treated_as_no_grant(
        self, monkeypatch, tmp_path
    ):
        _on(monkeypatch)
        oca.issue_grant(
            channel="ide",
            operator_label="d",
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        ledger = tmp_path / "grants.jsonl"
        rec = json.loads(ledger.read_text().splitlines()[0])
        rec["grant"]["expires_at_unix"] = 9_999_999_999.0  # tamper
        ledger.write_text(json.dumps(rec) + "\n")
        v = oca.verify_pre_commit(
            _ctx(tmp_path, channel="ide", now_unix=1100.0)
        )
        assert v.verdict is oca.CommitAuthorityVerdict.DENIED_NO_GRANT

    def test_revoke_by_id(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        out = oca.issue_grant(
            channel="ide",
            operator_label="d",
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        assert oca.revoke_grants(grant_id=out.grant_id) == 1
        v = oca.verify_pre_commit(
            _ctx(tmp_path, channel="ide", now_unix=1100.0)
        )
        assert v.verdict is oca.CommitAuthorityVerdict.DENIED_NO_GRANT

    def test_revoke_all(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        oca.issue_grant(
            channel="ide",
            operator_label="d",
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        assert oca.revoke_grants(revoke_all=True, now_unix=1050.0) == 1
        v = oca.verify_pre_commit(
            _ctx(tmp_path, channel="ide", now_unix=1100.0)
        )
        assert v.verdict is oca.CommitAuthorityVerdict.DENIED_NO_GRANT

    def test_consume_one_shot(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        out = oca.issue_grant(
            channel="ide",
            operator_label="d",
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        assert oca.consume_grant(out.grant_id) is True
        v = oca.verify_pre_commit(
            _ctx(tmp_path, channel="ide", now_unix=1100.0)
        )
        assert v.verdict is oca.CommitAuthorityVerdict.DENIED_NO_GRANT

    def test_latest_grant_wins_concurrent_append(
        self, monkeypatch, tmp_path
    ):
        _on(monkeypatch)
        oca.issue_grant(
            channel="ide",
            operator_label="first",
            ttl_s=60,
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        out2 = oca.issue_grant(
            channel="ide",
            operator_label="second",
            ttl_s=3600,
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        lines = (tmp_path / "grants.jsonl").read_text().splitlines()
        assert len(lines) == 2
        v = oca.verify_pre_commit(
            _ctx(tmp_path, channel="ide", now_unix=1200.0)
        )
        assert v.verdict is oca.CommitAuthorityVerdict.AUTHORIZED
        assert v.matched_grant_id == out2.grant_id

    def test_issue_requires_operator_label(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        out = oca.issue_grant(
            channel="ide", operator_label="  ", repo_root=tmp_path
        )
        assert out.ok is False and "operator_label" in out.error

    def test_issue_rejects_unknown_channel(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        out = oca.issue_grant(
            channel="zzz", operator_label="d", repo_root=tmp_path
        )
        assert out.ok is False and "channel" in out.error

    def test_no_secret_fail_closed(self, monkeypatch, tmp_path):
        """Operator channel with no secret bootstrapped → deny."""
        _on(monkeypatch)
        v = oca.verify_pre_commit(_ctx(tmp_path, channel="cli"))
        assert v.verdict is oca.CommitAuthorityVerdict.DENIED_NO_GRANT
        assert not (tmp_path / "secret").exists()


# ---------------------------------------------------------------------------
# Autonomous channel — delegated to ledger_sovereignty
# ---------------------------------------------------------------------------


class TestAutonomousChannel:
    def test_sovereignty_off_authorized_without_grant(
        self, monkeypatch, tmp_path
    ):
        _on(monkeypatch)
        v = oca.verify_pre_commit(
            _ctx(tmp_path, channel="autonomous")
        )
        assert v.verdict is oca.CommitAuthorityVerdict.AUTHORIZED

    def test_sovereignty_on_unowned_denied(
        self, monkeypatch, tmp_path
    ):
        _on(monkeypatch)
        monkeypatch.setenv("JARVIS_LEDGER_SOVEREIGNTY_ENABLED", "true")
        v = oca.verify_pre_commit(
            _ctx(tmp_path, channel="autonomous")
        )
        assert (
            v.verdict is oca.CommitAuthorityVerdict.DENIED_SOVEREIGNTY
        )

    def test_sovereignty_on_owned_authorized(
        self, monkeypatch, tmp_path
    ):
        _on(monkeypatch)
        monkeypatch.setenv("JARVIS_LEDGER_SOVEREIGNTY_ENABLED", "true")
        from backend.core.ouroboros.governance import (
            ledger_sovereignty as ls,
        )
        ls.mark_owned(
            tmp_path, session_id="s1", branch_name="b1"
        )
        v = oca.verify_pre_commit(
            _ctx(tmp_path, channel="autonomous")
        )
        assert v.verdict is oca.CommitAuthorityVerdict.AUTHORIZED


# ---------------------------------------------------------------------------
# Governance drift gate (composes governance_manifest)
# ---------------------------------------------------------------------------


class TestGovernanceGate:
    def test_drift_without_amend_denied(
        self, monkeypatch, tmp_path
    ):
        _on(monkeypatch)
        from backend.core.ouroboros.governance import (
            governance_manifest as gm,
        )

        class _Cmp:
            verdict = gm.ManifestVerdict.DRIFT

        monkeypatch.setattr(
            gm, "verify_governance_state", lambda **k: _Cmp()
        )
        oca.issue_grant(
            channel="ide",
            operator_label="d",
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        v = oca.verify_pre_commit(
            _ctx(
                tmp_path,
                channel="ide",
                staged_files=(
                    "backend/core/ouroboros/governance/x.py",
                ),
                now_unix=1100.0,
            )
        )
        assert (
            v.verdict
            is oca.CommitAuthorityVerdict.DENIED_GOVERNANCE_DRIFT
        )
        assert v.governance_verdict == "drift"

    def test_drift_with_amend_grant_authorized(
        self, monkeypatch, tmp_path
    ):
        _on(monkeypatch)
        from backend.core.ouroboros.governance import (
            governance_manifest as gm,
        )

        class _Cmp:
            verdict = gm.ManifestVerdict.DRIFT

        monkeypatch.setattr(
            gm, "verify_governance_state", lambda **k: _Cmp()
        )
        oca.issue_grant(
            channel="ide",
            operator_label="d",
            governance_amend=True,
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        v = oca.verify_pre_commit(
            _ctx(
                tmp_path,
                channel="ide",
                staged_files=(
                    "backend/core/ouroboros/governance/x.py",
                ),
                now_unix=1100.0,
            )
        )
        assert v.verdict is oca.CommitAuthorityVerdict.AUTHORIZED


# ---------------------------------------------------------------------------
# Adaptive TTL (composes operation_mode, read-only)
# ---------------------------------------------------------------------------


class TestAdaptiveTtl:
    def test_plan_mode_shortens_grant(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        monkeypatch.setenv("JARVIS_COMMIT_GRANT_DEFAULT_TTL_S", "3600")
        monkeypatch.setenv("JARVIS_COMMIT_GRANT_PLAN_TTL_S", "120")
        monkeypatch.setenv("JARVIS_OPERATION_MODE", "plan")
        out = oca.issue_grant(
            channel="ide",
            operator_label="d",
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        assert out.expires_at_unix == 1000.0 + 120

    def test_explicit_ttl_overrides_adaptive(
        self, monkeypatch, tmp_path
    ):
        _on(monkeypatch)
        monkeypatch.setenv("JARVIS_OPERATION_MODE", "plan")
        out = oca.issue_grant(
            channel="ide",
            operator_label="d",
            ttl_s=300,
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        assert out.expires_at_unix == 1000.0 + 300

    def test_ttl_clamped(self, monkeypatch, tmp_path):
        _on(monkeypatch)
        out = oca.issue_grant(
            channel="ide",
            operator_label="d",
            ttl_s=10**9,
            repo_root=tmp_path,
            now_unix=0.0,
        )
        assert out.expires_at_unix == float(oca._MAX_TTL_S)


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_source():
    src = Path(oca.__file__).read_text(encoding="utf-8")
    return src, ast.parse(src)


@pytest.fixture
def pins():
    return oca.register_shipped_invariants()


class TestAstPinsCanonicalPass:
    def test_6_pins_registered(self, pins):
        assert {p.invariant_name for p in pins} == {
            "operator_commit_authority_verdict_taxonomy_closed",
            "operator_commit_authority_channel_taxonomy_closed",
            "operator_commit_authority_authority_asymmetry",
            "operator_commit_authority_master_default_false",
            "operator_commit_authority_composes_canonical",
            "operator_commit_authority_subprocess_discipline",
        }

    @pytest.mark.parametrize(
        "pin_name",
        [
            "operator_commit_authority_verdict_taxonomy_closed",
            "operator_commit_authority_channel_taxonomy_closed",
            "operator_commit_authority_authority_asymmetry",
            "operator_commit_authority_master_default_false",
            "operator_commit_authority_composes_canonical",
            "operator_commit_authority_subprocess_discipline",
        ],
    )
    def test_pin_passes_on_canonical(
        self, canonical_source, pins, pin_name
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins if p.invariant_name == pin_name
        )
        assert not pin.validate(tree, src)


class TestAstPinsSyntheticRegression:
    def _pin(self, pins, name):
        return next(
            p for p in pins if p.invariant_name == name
        )

    def test_verdict_drift_fires(self, pins):
        src = (
            "import enum\n"
            "class CommitAuthorityVerdict(str, enum.Enum):\n"
            "    EXTRA = 'extra'\n"
        )
        v = self._pin(
            pins,
            "operator_commit_authority_verdict_taxonomy_closed",
        ).validate(ast.parse(src), src)
        assert v

    def test_channel_drift_fires(self, pins):
        src = (
            "import enum\n"
            "class CommitChannel(str, enum.Enum):\n"
            "    REPL = 'repl'\n"
        )
        v = self._pin(
            pins,
            "operator_commit_authority_channel_taxonomy_closed",
        ).validate(ast.parse(src), src)
        assert v

    def test_authority_import_fires(self, pins):
        src = (
            "from backend.core.ouroboros.governance.auto_committer "
            "import x\n"
        )
        v = self._pin(
            pins,
            "operator_commit_authority_authority_asymmetry",
        ).validate(ast.parse(src), src)
        assert v and "auto_committer" in v[0]

    def test_master_default_true_fires(self, pins):
        src = (
            "def master_enabled():\n"
            "    return _flag('X', default=True)\n"
        )
        v = self._pin(
            pins,
            "operator_commit_authority_master_default_false",
        ).validate(ast.parse(src), src)
        assert v

    def test_composes_missing_fires(self, pins):
        src = "x = 1\n"
        v = self._pin(
            pins,
            "operator_commit_authority_composes_canonical",
        ).validate(ast.parse(src), src)
        assert v

    def test_subprocess_shell_true_fires(self, pins):
        src = "import subprocess\nsubprocess.run(['git'], shell=True)\n"
        v = self._pin(
            pins,
            "operator_commit_authority_subprocess_discipline",
        ).validate(ast.parse(src), src)
        assert v

    def test_subprocess_missing_timeout_fires(self, pins):
        src = "import subprocess\nsubprocess.run(['git'])\n"
        v = self._pin(
            pins,
            "operator_commit_authority_subprocess_discipline",
        ).validate(ast.parse(src), src)
        assert v


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeds:
    def test_seeds_register(self):
        captured = []

        class _Reg:
            def register(self, spec):
                captured.append(spec.name)

        n = oca.register_flags(_Reg())
        assert n == 6  # Slice 3 #0 added the enable-file seed
        assert "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED" in captured
        assert "JARVIS_COMMIT_AUTHORITY_ENABLE_FILE" in captured

    def test_master_seed_default_false(self):
        captured = {}

        class _Reg:
            def register(self, spec):
                captured[spec.name] = spec

        oca.register_flags(_Reg())
        master = captured["JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED"]
        assert master.default is False


# ---------------------------------------------------------------------------
# Slice 3 #0 — persistent file-based master enable (Cursor SCM fix)
# ---------------------------------------------------------------------------


class TestPersistentEnable:
    def test_default_off_no_env_no_file(self):
        assert oca.persistent_enabled() is False
        assert oca.master_enabled() is False

    def test_enable_authority_turns_master_on_without_env(self):
        # No JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED in env at all —
        # this is exactly the Cursor-SCM (no shell env) scenario.
        assert oca.enable_authority("cursor-test") is True
        assert oca.persistent_enabled() is True
        assert oca.master_enabled() is True

    def test_enable_requires_label(self):
        assert oca.enable_authority("   ") is False
        assert oca.persistent_enabled() is False

    def test_disable_reverts_to_default_false(self):
        oca.enable_authority("x")
        assert oca.master_enabled() is True
        assert oca.disable_authority() is True
        assert oca.persistent_enabled() is False
        assert oca.master_enabled() is False

    def test_tampered_enable_file_is_rejected(self, tmp_path):
        oca.enable_authority("x")
        ef = tmp_path / "enabled"
        raw = json.loads(ef.read_text())
        raw["record"]["operator_label"] = "ATTACKER"  # tamper
        ef.write_text(json.dumps(raw))
        # signature no longer matches recomputed payload -> off
        assert oca.persistent_enabled() is False
        assert oca.master_enabled() is False

    def test_empty_file_does_not_enable(self, tmp_path):
        (tmp_path / "enabled").write_text("")
        assert oca.persistent_enabled() is False

    def test_enabled_false_record_does_not_enable(self, tmp_path):
        oca.enable_authority("x")
        ef = tmp_path / "enabled"
        raw = json.loads(ef.read_text())
        raw["record"]["enabled"] = False
        ef.write_text(json.dumps(raw))
        assert oca.persistent_enabled() is False

    def test_enable_file_without_secret_is_fail_closed(
        self, tmp_path
    ):
        oca.enable_authority("x")
        (tmp_path / "secret").unlink()  # secret gone -> unverifiable
        assert oca.persistent_enabled() is False
        assert oca.master_enabled() is False

    def test_env_flag_still_independently_enables(self, monkeypatch):
        # No enable file; env path must still work (back-compat).
        assert oca.persistent_enabled() is False
        monkeypatch.setenv(
            "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true"
        )
        assert oca.master_enabled() is True

    def test_verify_pre_commit_honors_persistent_enable_no_env(
        self, tmp_path
    ):
        # The end-to-end Cursor-SCM proof at substrate level: NO env
        # master flag, persistent enable + signed grant -> AUTHORIZED.
        assert "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED" not in (
            __import__("os").environ
        )
        assert oca.enable_authority("cursor-scm") is True
        out = oca.issue_grant(
            channel="ide",
            operator_label="cursor-scm",
            repo_root=tmp_path,
            now_unix=1000.0,
        )
        assert out.ok
        v = oca.verify_pre_commit(
            _ctx(tmp_path, channel="ide", now_unix=1100.0)
        )
        assert v.verdict is oca.CommitAuthorityVerdict.AUTHORIZED

    def test_enable_file_path_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom" / "oca.enabled"
        monkeypatch.setenv(
            "JARVIS_COMMIT_AUTHORITY_ENABLE_FILE", str(custom)
        )
        assert oca.enable_file_path() == custom.resolve() or str(
            oca.enable_file_path()
        ) == str(custom)
        assert oca.enable_authority("x") is True
        assert custom.exists()

    def test_master_default_false_ast_pin_still_passes(self):
        import ast as _ast

        src = Path(oca.__file__).read_text(encoding="utf-8")
        pin = next(
            p
            for p in oca.register_shipped_invariants()
            if p.invariant_name
            == "operator_commit_authority_master_default_false"
        )
        assert not pin.validate(_ast.parse(src), src)
