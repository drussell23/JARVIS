"""Regression spine for §40 Wave 1 #8 — Adversarial Autobiography.

Closes the operator's Wave 1 #8 ship. Covers:

* §33.1 master flag default-FALSE + truthy alternation
* Closed 4-value :class:`AutobiographyFinding` taxonomy
* Closed 3-value :class:`ProbeOutcome` taxonomy
* Per-commit probe outcomes (MATCH/NO_MATCH/UNKNOWN)
* Aggregate findings (CORPUS_ESCAPE/CLEAN/NO_COMMITS/DISABLED)
* Hermetic git log + git show via injected runners
* Composes canonical sources (P9.4 corpus + ov_signature_substring)
  — no parallel state, no hardcoded patterns
* 5 AST pin canonical-source pass + 5 synthetic regressions
* §33.5 frozen report to_dict projection completeness
* Auto-discovered REPL via §32.11 Slice 4 + §33.3 naming-cage
* FlagRegistry seeds auto-discovered (5 specs)
* SSE event registered in canonical broker
* IDE GET route handler exists on canonical router
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import (
    adversarial_autobiography as aa,
)
from backend.core.ouroboros.governance.adversarial_autobiography import (
    ADVERSARIAL_AUTOBIOGRAPHY_SCHEMA_VERSION,
    AutobiographyFinding,
    AutobiographyReport,
    CommitAutobiographyAudit,
    ProbeMatch,
    ProbeOutcome,
    _ENV_MASTER,
    _audit_one_commit,
    _is_autonomous_commit,
    _parse_git_log,
    _probe_commit_against_entry,
    audit_autobiography,
    commit_scan_max,
    escape_alert_threshold,
    finding_glyph,
    format_autobiography_panel,
    format_commit_audit,
    master_enabled,
    persistence_enabled,
    reset_cache_for_tests,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_autobiography(monkeypatch):
    reset_cache_for_tests()
    for env in (
        _ENV_MASTER,
        "JARVIS_AUTOBIOGRAPHY_COMMIT_SCAN_MAX",
        "JARVIS_AUTOBIOGRAPHY_ESCAPE_ALERT_THRESHOLD",
        "JARVIS_AUTOBIOGRAPHY_PERSISTENCE_ENABLED",
        "JARVIS_AUTOBIOGRAPHY_LEDGER_PATH",
    ):
        monkeypatch.delenv(env, raising=False)
    yield
    reset_cache_for_tests()


def _fake_runner(stdout_text: str, returncode: int = 0):
    class _R:
        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout_text
            self.stderr = ""
    def runner(*args, **kwargs):
        return _R()
    return runner


def _make_git_log_output(commits: List[dict]) -> str:
    parts: List[str] = []
    for c in commits:
        parts.append("__OV_AUTOBIO__")
        parts.append(c["hash"])
        parts.append(str(c["time"]))
        parts.append(c["body"])
        parts.append("__END_HEADER__")
    return "\n".join(parts) + "\n"


def _ov_body(text: str = "fix: stuff") -> str:
    """Compose a commit body with the canonical OV signature."""
    return (
        f"{text}\n\n"
        "Ouroboros+Venom [O+V] — Autonomous Self-Development Engine\n"
    )


# ---------------------------------------------------------------------------
# §33.1 master flag default-FALSE
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_false(self):
        assert master_enabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
    def test_truthy(self, monkeypatch, truthy):
        monkeypatch.setenv(_ENV_MASTER, truthy)
        assert master_enabled() is True

    def test_persistence_short_circuits_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_AUTOBIOGRAPHY_PERSISTENCE_ENABLED", "true",
        )
        # Master off → persistence gated off
        assert persistence_enabled() is False

    def test_persistence_default_true_when_master_on(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        assert persistence_enabled() is True

    def test_persistence_explicit_off(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        monkeypatch.setenv(
            "JARVIS_AUTOBIOGRAPHY_PERSISTENCE_ENABLED", "false",
        )
        assert persistence_enabled() is False


# ---------------------------------------------------------------------------
# Env knob clamping
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_scan_max_default(self):
        assert commit_scan_max() == 200

    def test_scan_max_clamped_low(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_AUTOBIOGRAPHY_COMMIT_SCAN_MAX", "1",
        )
        assert commit_scan_max() == 5

    def test_scan_max_clamped_high(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_AUTOBIOGRAPHY_COMMIT_SCAN_MAX", "999999999",
        )
        assert commit_scan_max() == 10_000

    def test_scan_max_bad_falls_back(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_AUTOBIOGRAPHY_COMMIT_SCAN_MAX", "not-int",
        )
        assert commit_scan_max() == 200

    def test_escape_threshold_default(self):
        assert escape_alert_threshold() == 1

    def test_escape_threshold_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_AUTOBIOGRAPHY_ESCAPE_ALERT_THRESHOLD", "3",
        )
        assert escape_alert_threshold() == 3


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


class TestFindingTaxonomy:
    def test_exactly_4_values(self):
        values = {f.value for f in AutobiographyFinding}
        assert values == {
            "corpus_escape",
            "corpus_clean",
            "corpus_no_commits",
            "corpus_disabled",
        }

    def test_each_has_glyph(self):
        for f in AutobiographyFinding:
            assert finding_glyph(f) != "?"

    def test_unknown_glyph_returns_question(self):
        assert finding_glyph("bogus") == "?"
        assert finding_glyph(None) == "?"


class TestProbeOutcomeTaxonomy:
    def test_exactly_3_values(self):
        values = {o.value for o in ProbeOutcome}
        assert values == {"match", "no_match", "unknown"}


# ---------------------------------------------------------------------------
# §33.5 frozen artifacts
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_probe_match_to_dict_full_shape(self):
        m = ProbeMatch(
            entry_id="p9.4.001",
            category="quine_shape",
            pattern_excerpt="def test_foo(): ...",
            matched_line="+def test_foo(): assert True",
        )
        d = m.to_dict()
        assert set(d.keys()) == {
            "entry_id", "category", "pattern_excerpt",
            "matched_line", "schema_version",
        }
        assert d["schema_version"] == (
            ADVERSARIAL_AUTOBIOGRAPHY_SCHEMA_VERSION
        )

    def test_audit_to_dict_full_shape(self):
        m = ProbeMatch(
            entry_id="p9.4.001", category="quine_shape",
            pattern_excerpt="x", matched_line="y",
        )
        audit = CommitAutobiographyAudit(
            commit_hash="abc",
            commit_time_unix=1700000000,
            finding=AutobiographyFinding.CORPUS_ESCAPE,
            entries_probed=25,
            matches=(m,),
            diagnostic="found 1",
        )
        d = audit.to_dict()
        assert d["commit_hash"] == "abc"
        assert d["finding"] == "corpus_escape"
        assert d["entries_probed"] == 25
        assert len(d["matches"]) == 1

    def test_report_to_dict_full_shape(self):
        r = AutobiographyReport(
            audited_at_unix=1.0,
            master_enabled=True,
            finding=AutobiographyFinding.CORPUS_CLEAN,
            commits_audited=5,
            escape_count=0,
            clean_count=5,
            per_category_escape={},
            per_entry_escape={},
            cage_health_ratio=1.0,
            elapsed_s=0.1,
            diagnostic="ok",
        )
        d = r.to_dict()
        expected_keys = {
            "audited_at_unix", "master_enabled", "finding",
            "commits_audited", "escape_count", "clean_count",
            "per_category_escape", "per_entry_escape",
            "cage_health_ratio", "elapsed_s", "diagnostic",
            "schema_version",
        }
        assert set(d.keys()) == expected_keys

    def test_report_diagnostic_clamped(self):
        r = AutobiographyReport(
            audited_at_unix=1.0,
            master_enabled=True,
            finding=AutobiographyFinding.CORPUS_CLEAN,
            commits_audited=0,
            escape_count=0,
            clean_count=0,
            per_category_escape={},
            per_entry_escape={},
            cage_health_ratio=0.0,
            elapsed_s=0.0,
            diagnostic="x" * 1000,
        )
        d = r.to_dict()
        assert len(d["diagnostic"]) == 512


# ---------------------------------------------------------------------------
# Git log parser (pure function, hermetic)
# ---------------------------------------------------------------------------


class TestGitLogParser:
    def test_empty_returns_empty_tuple(self):
        assert _parse_git_log("") == ()
        assert _parse_git_log("    \n  ") == ()

    def test_malformed_chunk_skipped(self):
        # Missing time field
        out = "__OV_AUTOBIO__\nabcdef\n__END_HEADER__\n"
        assert _parse_git_log(out) == ()

    def test_well_formed_chunk_parsed(self):
        raw = _make_git_log_output([
            {"hash": "abc1", "time": 1700000000, "body": "fix"},
        ])
        parsed = _parse_git_log(raw)
        assert len(parsed) == 1
        assert parsed[0].commit_hash == "abc1"
        assert parsed[0].commit_time_unix == 1700000000

    def test_multiple_chunks_parsed(self):
        raw = _make_git_log_output([
            {"hash": "a", "time": 1, "body": "x"},
            {"hash": "b", "time": 2, "body": "y"},
            {"hash": "c", "time": 3, "body": "z"},
        ])
        parsed = _parse_git_log(raw)
        assert [c.commit_hash for c in parsed] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Autonomous commit detection
# ---------------------------------------------------------------------------


class TestAutonomousDetection:
    def test_signature_present_detected(self):
        body = _ov_body("fix: thing")
        sig = (
            "Ouroboros+Venom [O+V] — "
            "Autonomous Self-Development Engine"
        )
        assert _is_autonomous_commit(body, sig) is True

    def test_signature_absent_not_detected(self):
        body = "fix: thing\nSigned-off-by: Derek\n"
        sig = (
            "Ouroboros+Venom [O+V] — "
            "Autonomous Self-Development Engine"
        )
        assert _is_autonomous_commit(body, sig) is False


# ---------------------------------------------------------------------------
# Per-entry probe — pure function
# ---------------------------------------------------------------------------


def _fake_entry(entry_id: str, category_value: str, pattern: str):
    """Build a duck-typed AdversarialEntry-shaped object for hermetic
    probe testing without instantiating the canonical class."""
    from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
        AdversarialCategory,
        AdversarialEntry,
        ExpectedVerdict,
    )
    cat = next(
        (c for c in AdversarialCategory if c.value == category_value),
        AdversarialCategory.QUINE_SHAPE,
    )
    return AdversarialEntry(
        entry_id=entry_id,
        category=cat,
        expected_verdict=ExpectedVerdict.REJECT_AT_VALIDATE,
        pattern=pattern,
        rationale="hermetic test fixture",
    )


class TestProbe:
    def test_match_in_added_lines(self):
        diff = (
            "diff --git a/x.py b/x.py\n"
            "+++ b/x.py\n"
            "--- a/x.py\n"
            "+def malicious():\n"
            "+    pass\n"
        )
        entry = _fake_entry(
            "p9.4.test", "quine_shape", "def malicious():",
        )
        outcome, match = _probe_commit_against_entry(diff, entry)
        assert outcome is ProbeOutcome.MATCH
        assert match is not None
        assert match.entry_id == "p9.4.test"
        assert "malicious" in match.matched_line

    def test_no_match_when_pattern_absent(self):
        diff = (
            "+++ b/x.py\n"
            "+def safe():\n"
            "+    return 1\n"
        )
        entry = _fake_entry(
            "p9.4.test", "quine_shape", "def malicious():",
        )
        outcome, match = _probe_commit_against_entry(diff, entry)
        assert outcome is ProbeOutcome.NO_MATCH
        assert match is None

    def test_removed_lines_dont_count_as_match(self):
        # Pattern only present in REMOVED lines (lines with -)
        # should NOT trigger a match — that's the cage WORKING
        # post-hoc, not an escape.
        diff = (
            "+++ b/x.py\n"
            "-def malicious():\n"
            "-    pass\n"
        )
        entry = _fake_entry(
            "p9.4.test", "quine_shape", "def malicious():",
        )
        outcome, _ = _probe_commit_against_entry(diff, entry)
        assert outcome is ProbeOutcome.NO_MATCH

    def test_unknown_placeholder_returns_unknown(self):
        # Entries whose materialize_pattern returns the
        # unsubstituted placeholder token signal a corpus bug;
        # the probe should return UNKNOWN, not raise.
        entry = _fake_entry(
            "p9.4.test", "credential_introduced",
            "<P9.4.999_UNKNOWN_PLACEHOLDER>",
        )
        outcome, match = _probe_commit_against_entry(
            "+anything", entry,
        )
        assert outcome is ProbeOutcome.UNKNOWN
        assert match is None


# ---------------------------------------------------------------------------
# Per-commit audit
# ---------------------------------------------------------------------------


class TestCommitAudit:
    def test_clean_commit(self, monkeypatch):
        from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
            CORPUS,
        )
        commit = aa._RawCommit(
            commit_hash="abc1",
            commit_time_unix=1700000000,
            body=_ov_body(),
        )
        clean_diff = "+++ b/x.py\n+x = 1\n+y = 2\n"
        audit = _audit_one_commit(
            Path("/tmp"), commit, CORPUS,
            git_show_runner=_fake_runner(clean_diff),
        )
        assert audit.finding is AutobiographyFinding.CORPUS_CLEAN
        assert audit.commit_hash == "abc1"
        assert len(audit.matches) == 0
        assert audit.entries_probed > 0


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


class TestAggregator:
    def test_master_off_returns_disabled(self):
        report = audit_autobiography()
        assert report.master_enabled is False
        assert (
            report.finding is AutobiographyFinding.CORPUS_DISABLED
        )
        assert report.commits_audited == 0

    def test_master_on_no_commits_returns_no_commits(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        # Empty git log → no commits to audit
        report = audit_autobiography(
            git_log_runner=_fake_runner(""),
            git_show_runner=_fake_runner(""),
        )
        assert report.master_enabled is True
        assert (
            report.finding is AutobiographyFinding.CORPUS_NO_COMMITS
        )

    def test_master_on_human_commits_only_no_commits(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        raw = _make_git_log_output([
            {
                "hash": "h1", "time": 1700000000,
                "body": "operator commit\n",  # no OV signature
            },
        ])
        report = audit_autobiography(
            git_log_runner=_fake_runner(raw),
            git_show_runner=_fake_runner("+harmless"),
        )
        # Parsed but filtered out by signature check
        assert (
            report.finding is AutobiographyFinding.CORPUS_NO_COMMITS
        )

    def test_master_on_clean_ov_commits_returns_clean(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        raw = _make_git_log_output([
            {
                "hash": "ov1", "time": 1700000000,
                "body": _ov_body("fix: clean"),
            },
            {
                "hash": "ov2", "time": 1700000100,
                "body": _ov_body("feat: also clean"),
            },
        ])
        clean_diff = "+++ b/x.py\n+harmless = True\n"
        report = audit_autobiography(
            git_log_runner=_fake_runner(raw),
            git_show_runner=_fake_runner(clean_diff),
        )
        assert report.master_enabled is True
        assert report.finding is AutobiographyFinding.CORPUS_CLEAN
        assert report.commits_audited == 2
        assert report.escape_count == 0
        assert report.clean_count == 2
        assert report.cage_health_ratio == 1.0

    def test_escape_detected_when_pattern_lands(self, monkeypatch):
        """If a known P9.4 pattern appears in an OV commit's added
        lines, finding is CORPUS_ESCAPE."""
        monkeypatch.setenv(_ENV_MASTER, "true")
        # First corpus entry's pattern
        from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
            CORPUS, materialize_pattern,
        )
        first = CORPUS[0]
        pat = materialize_pattern(first)
        # Sanity check: corpus has a real first entry
        assert pat and not pat.startswith("<")

        # Build a fake commit diff that adds this exact pattern
        diff_lines = ["+++ b/x.py"]
        for line in pat.splitlines():
            diff_lines.append(f"+{line}")
        diff = "\n".join(diff_lines) + "\n"

        raw = _make_git_log_output([
            {
                "hash": "leaked1", "time": 1700000000,
                "body": _ov_body("oops: leaked pattern"),
            },
        ])
        report = audit_autobiography(
            git_log_runner=_fake_runner(raw),
            git_show_runner=_fake_runner(diff),
        )
        assert report.master_enabled is True
        assert report.finding is AutobiographyFinding.CORPUS_ESCAPE
        assert report.escape_count == 1
        assert report.clean_count == 0
        assert report.cage_health_ratio == 0.0
        assert first.entry_id in report.per_entry_escape

    def test_cache_within_ttl(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        r1 = audit_autobiography(
            git_log_runner=_fake_runner(""),
            git_show_runner=_fake_runner(""),
        )
        r2 = audit_autobiography(
            git_log_runner=_fake_runner(""),
            git_show_runner=_fake_runner(""),
        )
        assert r1 is r2

    def test_force_refresh_bypasses_cache(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        r1 = audit_autobiography(
            git_log_runner=_fake_runner(""),
            git_show_runner=_fake_runner(""),
        )
        r2 = audit_autobiography(
            git_log_runner=_fake_runner(""),
            git_show_runner=_fake_runner(""),
            force_refresh=True,
        )
        assert r1 is not r2


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


class TestRenderers:
    def test_panel_master_off(self):
        report = audit_autobiography()  # master off
        out = format_autobiography_panel(report)
        assert "disabled" in out

    def test_panel_master_on_clean(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        report = audit_autobiography(
            git_log_runner=_fake_runner(""),
            git_show_runner=_fake_runner(""),
        )
        out = format_autobiography_panel(report)
        assert "Adversarial Autobiography" in out
        assert "commits_audited" in out

    def test_commit_audit_render(self):
        audit = CommitAutobiographyAudit(
            commit_hash="abc12345xyz",
            commit_time_unix=1700000000,
            finding=AutobiographyFinding.CORPUS_CLEAN,
            entries_probed=25,
            matches=(),
            diagnostic="clean",
        )
        out = format_commit_audit(audit)
        assert "abc12345xyz"[:12] in out
        assert "corpus_clean" in out


# ---------------------------------------------------------------------------
# AST pins canonical-source pass + synthetic regressions
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_source():
    path = Path(aa.__file__)
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    return src, tree


@pytest.fixture
def pins():
    return aa.register_shipped_invariants()


class TestAstPinsCanonicalPass:
    def test_5_pins_registered(self, pins):
        assert len(pins) == 5
        names = {p.invariant_name for p in pins}
        assert names == {
            "adversarial_autobiography_master_default_false",
            "adversarial_autobiography_authority_asymmetry",
            "adversarial_autobiography_finding_taxonomy_closed",
            "adversarial_autobiography_probe_outcome_taxonomy",
            "adversarial_autobiography_composes_canonical",
        }

    def test_master_default_false_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "adversarial_autobiography_master_default_false"
        )
        assert not pin.validate(tree, src)

    def test_authority_asymmetry_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "adversarial_autobiography_authority_asymmetry"
        )
        assert not pin.validate(tree, src)

    def test_finding_taxonomy_passes(self, canonical_source, pins):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "adversarial_autobiography_finding_taxonomy_closed"
        )
        assert not pin.validate(tree, src)

    def test_probe_outcome_taxonomy_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "adversarial_autobiography_probe_outcome_taxonomy"
        )
        assert not pin.validate(tree, src)

    def test_composes_canonical_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "adversarial_autobiography_composes_canonical"
        )
        assert not pin.validate(tree, src)


class TestAstPinsSyntheticRegression:
    def test_master_pin_fires_on_premature_flip(self, pins):
        synthetic = """
def master_enabled():
    return _flag("FOO", default=True)
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "adversarial_autobiography_master_default_false"
        )
        assert pin.validate(tree, synthetic)

    @pytest.mark.parametrize(
        "module",
        [
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.semantic_guardian",
            (
                "backend.core.ouroboros.governance."
                "adversarial_reviewer_service"
            ),
        ],
    )
    def test_authority_pin_fires_on_forbidden_import(
        self, pins, module,
    ):
        synthetic = f"from {module} import x\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "adversarial_autobiography_authority_asymmetry"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert any(module in v for v in violations)

    def test_finding_pin_fires_on_missing_value(self, pins):
        synthetic = """
import enum
class AutobiographyFinding(str, enum.Enum):
    CORPUS_ESCAPE = "corpus_escape"
    CORPUS_CLEAN = "corpus_clean"
    CORPUS_NO_COMMITS = "corpus_no_commits"
    # MISSING: CORPUS_DISABLED
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "adversarial_autobiography_finding_taxonomy_closed"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "missing" in violations[0]

    def test_probe_outcome_pin_fires_on_drift(self, pins):
        synthetic = """
import enum
class ProbeOutcome(str, enum.Enum):
    MATCH = "match"
    NO_MATCH = "no_match"
    UNKNOWN = "unknown"
    EXTRA = "extra"
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "adversarial_autobiography_probe_outcome_taxonomy"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "drift" in violations[0]

    def test_composes_canonical_pin_fires_on_missing(self, pins):
        synthetic = "x = 1\ny = 2\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "adversarial_autobiography_composes_canonical"
        )
        violations = pin.validate(tree, synthetic)
        # All three compose checks fire
        assert violations
        assert any("p9_4_adversarial_corpus" in v for v in violations)
        assert any(
            "materialize_pattern" in v for v in violations
        )
        assert any(
            "ov_signature_substring" in v for v in violations
        )


# ---------------------------------------------------------------------------
# Canonical source smokes
# ---------------------------------------------------------------------------


class TestCanonicalSmokes:
    def test_p9_4_corpus_loadable(self):
        from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
            CORPUS, corpus_size, materialize_pattern,
        )
        assert corpus_size() == len(CORPUS) >= 1
        # First entry's pattern materializes to a non-placeholder
        # string (sanity check on the canonical builder).
        pat = materialize_pattern(CORPUS[0])
        assert isinstance(pat, str) and len(pat) > 0

    def test_ov_signature_loadable(self):
        from backend.core.ouroboros.governance.auto_committer import (
            ov_signature_substring,
        )
        sig = ov_signature_substring()
        assert "Ouroboros" in sig

    def test_sse_event_registered(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_AUTOBIOGRAPHY_AUDIT_COMPLETED,
            _VALID_EVENT_TYPES,
        )
        assert (
            EVENT_TYPE_AUTOBIOGRAPHY_AUDIT_COMPLETED
            in _VALID_EVENT_TYPES
        )

    def test_ide_handler_exists(self):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        assert hasattr(
            IDEObservabilityRouter, "_handle_adversarial_autobiography",
        )
        assert hasattr(
            IDEObservabilityRouter,
            "_autobiography_master_enabled",
        )


# ---------------------------------------------------------------------------
# REPL — §32.11 Slice 4 auto-discovery
# ---------------------------------------------------------------------------


class TestRepl:
    def test_match_canonical_verb(self):
        from backend.core.ouroboros.governance.autobiography_repl import (  # noqa: E501
            dispatch_autobiography_command,
        )
        r = dispatch_autobiography_command("/autobiography help")
        assert r.matched is True
        assert r.ok is True

    def test_help_bypasses_master_gate(self):
        from backend.core.ouroboros.governance.autobiography_repl import (  # noqa: E501
            dispatch_autobiography_command,
        )
        # Master off — help still works
        r = dispatch_autobiography_command("/autobiography help")
        assert r.matched is True
        assert r.ok is True

    def test_status_blocked_when_master_off(self):
        from backend.core.ouroboros.governance.autobiography_repl import (  # noqa: E501
            dispatch_autobiography_command,
        )
        r = dispatch_autobiography_command("/autobiography status")
        assert r.matched is True
        assert r.ok is False
        assert "disabled" in r.text

    def test_status_works_master_on(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        from backend.core.ouroboros.governance.autobiography_repl import (  # noqa: E501
            dispatch_autobiography_command,
        )
        r = dispatch_autobiography_command("/autobiography status")
        assert r.matched is True
        assert r.ok is True

    def test_corpus_subcommand_works(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        from backend.core.ouroboros.governance.autobiography_repl import (  # noqa: E501
            dispatch_autobiography_command,
        )
        r = dispatch_autobiography_command("/autobiography corpus")
        assert r.matched is True
        assert r.ok is True
        assert "categories covered" in r.text

    def test_commit_requires_hash(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        from backend.core.ouroboros.governance.autobiography_repl import (  # noqa: E501
            dispatch_autobiography_command,
        )
        r = dispatch_autobiography_command("/autobiography commit")
        assert r.matched is True
        assert r.ok is False
        assert "required" in r.text

    def test_escapes_subcommand(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        from backend.core.ouroboros.governance.autobiography_repl import (  # noqa: E501
            dispatch_autobiography_command,
        )
        r = dispatch_autobiography_command("/autobiography escapes")
        assert r.matched is True
        assert r.ok is True

    def test_unknown_subcommand(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        from backend.core.ouroboros.governance.autobiography_repl import (  # noqa: E501
            dispatch_autobiography_command,
        )
        r = dispatch_autobiography_command("/autobiography bogus")
        assert r.matched is True
        assert r.ok is False
        assert "unknown" in r.text.lower()

    def test_unmatched(self):
        from backend.core.ouroboros.governance.autobiography_repl import (  # noqa: E501
            dispatch_autobiography_command,
        )
        r = dispatch_autobiography_command("/something_else")
        assert r.matched is False


class TestNamingCageAutoDiscovery:
    def test_verb_in_registry(self):
        from backend.core.ouroboros.battle_test import (
            repl_dispatch_registry as rdr,
        )
        rdr.reset_registry_for_tests()
        rdr.prime_registry()
        assert "autobiography" in rdr.list_verbs()

    def test_dispatcher_routes(self):
        from backend.core.ouroboros.battle_test import (
            repl_dispatch_registry as rdr,
        )
        rdr.reset_registry_for_tests()
        rdr.prime_registry()
        outcome = rdr.try_dispatch("/autobiography help")
        assert outcome.matched is True
        assert outcome.ok is True


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeds:
    def test_all_5_seeds_auto_discovered(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        names = {f.name for f in reg.list_all()}
        for expected in [
            "JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED",
            "JARVIS_AUTOBIOGRAPHY_COMMIT_SCAN_MAX",
            "JARVIS_AUTOBIOGRAPHY_ESCAPE_ALERT_THRESHOLD",
            "JARVIS_AUTOBIOGRAPHY_PERSISTENCE_ENABLED",
            "JARVIS_AUTOBIOGRAPHY_LEDGER_PATH",
        ]:
            assert expected in names, f"missing seed: {expected}"

    def test_master_seed_default_false_safety_category(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        spec = next(
            f for f in reg.list_all()
            if f.name == "JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED"
        )
        assert spec.default is False
        assert spec.category.value == "safety"


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_all_exports_present(self):
        for name in aa.__all__:
            assert getattr(aa, name) is not None

    def test_schema_version(self):
        assert ADVERSARIAL_AUTOBIOGRAPHY_SCHEMA_VERSION.startswith(
            "adversarial_autobiography.",
        )
