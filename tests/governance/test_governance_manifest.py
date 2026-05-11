"""Regression spine for §40 Wave 2 #6 — Hash-Cap on Self-Modification.

Covers:

* §33.1 opt-in workflow — master flag default-**FALSE**
* Closed 5-value :class:`ManifestVerdict` taxonomy
* Pure-function :func:`compute_current_signatures` walks
  governance/ + hashes every .py file deterministically
* :func:`compute_current_manifest` aggregates into one signed
  manifest snapshot with deterministic ``manifest_sha256``
* :func:`load_signed_manifest` defensive on missing / malformed
* :func:`compare_manifests` produces every reachable verdict
* :func:`verify_governance_state` end-to-end composition
* :func:`refresh_signed_manifest` writes manifest atomically
  with operator-required label
* :func:`is_refusal_verdict` centralized predicate
* 5 AST pin canonical-source pass + 5 synthetic regressions
* FlagRegistry seeds auto-discovered
* AutoCommitter integration — pre-commit refusal on drift
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import (
    governance_manifest as gm,
)
from backend.core.ouroboros.governance.governance_manifest import (
    FileSignature,
    GOVERNANCE_MANIFEST_SCHEMA_VERSION,
    ManifestComparison,
    ManifestSnapshot,
    ManifestVerdict,
    RefreshOutcome,
    _ENV_MASTER,
    _ENV_MANIFEST_PATH,
    _compute_manifest_signature,
    _hash_file,
    compare_manifests,
    compute_current_manifest,
    compute_current_signatures,
    is_refusal_verdict,
    load_signed_manifest,
    manifest_path,
    master_enabled,
    max_files_scan,
    refresh_signed_manifest,
    verify_governance_state,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER,
        _ENV_MANIFEST_PATH,
        "JARVIS_GOVERNANCE_MANIFEST_MAX_FILES",
    ):
        monkeypatch.delenv(env, raising=False)
    # Point manifest path into the temp dir for every test so
    # nothing touches the real on-disk baseline.
    manifest_file = tmp_path / "test_manifest.json"
    monkeypatch.setenv(_ENV_MANIFEST_PATH, str(manifest_file))
    yield


# ---------------------------------------------------------------------------
# §33.1 — master flag default-FALSE (opt-in workflow)
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_false(self):
        assert master_enabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
    def test_truthy(self, monkeypatch, truthy):
        monkeypatch.setenv(_ENV_MASTER, truthy)
        assert master_enabled() is True

    @pytest.mark.parametrize(
        "falsy", ["0", "false", "no", "off", "bogus", ""],
    )
    def test_falsy(self, monkeypatch, falsy):
        monkeypatch.setenv(_ENV_MASTER, falsy)
        assert master_enabled() is False


# ---------------------------------------------------------------------------
# Env knob clamping
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_max_files_default(self):
        assert max_files_scan() == 5000

    def test_max_files_clamped_low(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_GOVERNANCE_MANIFEST_MAX_FILES", "1",
        )
        assert max_files_scan() == 10

    def test_max_files_clamped_high(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_GOVERNANCE_MANIFEST_MAX_FILES", "999999999",
        )
        assert max_files_scan() == 100_000

    def test_max_files_bad_falls_back(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_GOVERNANCE_MANIFEST_MAX_FILES", "bogus",
        )
        assert max_files_scan() == 5000


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


class TestVerdictTaxonomy:
    def test_exactly_5_values(self):
        values = {v.value for v in ManifestVerdict}
        assert values == {
            "match", "drift", "missing_manifest",
            "empty_governance", "disabled",
        }


# ---------------------------------------------------------------------------
# is_refusal_verdict predicate
# ---------------------------------------------------------------------------


class TestRefusalPredicate:
    def test_drift_refuses(self):
        assert is_refusal_verdict(ManifestVerdict.DRIFT)

    def test_drift_refuses_string(self):
        assert is_refusal_verdict("drift")

    @pytest.mark.parametrize(
        "verdict",
        [
            ManifestVerdict.MATCH,
            ManifestVerdict.MISSING_MANIFEST,
            ManifestVerdict.EMPTY_GOVERNANCE,
            ManifestVerdict.DISABLED,
        ],
    )
    def test_non_drift_does_not_refuse(self, verdict):
        assert not is_refusal_verdict(verdict)

    def test_none_does_not_refuse(self):
        assert not is_refusal_verdict(None)

    def test_bogus_does_not_refuse(self):
        assert not is_refusal_verdict("bogus")


# ---------------------------------------------------------------------------
# §33.5 frozen artifacts
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_file_signature_to_dict(self):
        s = FileSignature(
            relative_path="a/b.py",
            sha256="a" * 64,
            size_bytes=100,
        )
        assert s.to_dict() == {
            "relative_path": "a/b.py",
            "sha256": "a" * 64,
            "size_bytes": 100,
        }

    def test_file_signature_from_dict(self):
        s = FileSignature.from_dict({
            "relative_path": "x.py",
            "sha256": "b" * 64,
            "size_bytes": 42,
        })
        assert s is not None
        assert s.relative_path == "x.py"
        assert s.sha256 == "b" * 64

    def test_file_signature_from_dict_rejects_bad_sha(self):
        # Non-64-char sha256 → None (defensive)
        s = FileSignature.from_dict({
            "relative_path": "x.py",
            "sha256": "short",
            "size_bytes": 1,
        })
        assert s is None

    def test_file_signature_from_dict_rejects_empty_path(self):
        s = FileSignature.from_dict({
            "relative_path": "",
            "sha256": "a" * 64,
            "size_bytes": 1,
        })
        assert s is None

    def test_manifest_snapshot_round_trip(self):
        snap = ManifestSnapshot(
            schema_version=GOVERNANCE_MANIFEST_SCHEMA_VERSION,
            operator_label="test",
            signed_at_unix=1234.0,
            signatures=(
                FileSignature(
                    relative_path="a.py",
                    sha256="a" * 64,
                    size_bytes=10,
                ),
            ),
            manifest_sha256="b" * 64,
        )
        d = snap.to_dict()
        reparsed = ManifestSnapshot.from_dict(d)
        assert reparsed is not None
        assert reparsed.operator_label == "test"
        assert len(reparsed.signatures) == 1

    def test_manifest_snapshot_lookup(self):
        snap = ManifestSnapshot(
            schema_version="x",
            operator_label="t",
            signed_at_unix=0.0,
            signatures=(
                FileSignature("a.py", "a" * 64, 1),
                FileSignature("b.py", "b" * 64, 1),
            ),
            manifest_sha256="c" * 64,
        )
        assert snap.lookup("a.py").sha256 == "a" * 64
        assert snap.lookup("missing.py") is None
        assert snap.lookup("") is None


# ---------------------------------------------------------------------------
# File hashing
# ---------------------------------------------------------------------------


class TestHashing:
    def test_hash_file_deterministic(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello = 1\n")
        h1 = _hash_file(f)
        h2 = _hash_file(f)
        assert h1 == h2
        assert len(h1) == 64

    def test_hash_file_changes_on_content_change(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("v = 1\n")
        h1 = _hash_file(f)
        f.write_text("v = 2\n")
        h2 = _hash_file(f)
        assert h1 != h2

    def test_hash_file_matches_stdlib(self, tmp_path):
        f = tmp_path / "test.py"
        content = b"some content for hash test\n"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert _hash_file(f) == expected

    def test_hash_file_missing_returns_none(self, tmp_path):
        assert _hash_file(tmp_path / "does_not_exist.py") is None

    def test_hash_file_directory_returns_none(self, tmp_path):
        assert _hash_file(tmp_path) is None

    def test_manifest_signature_deterministic(self):
        sigs = [
            FileSignature("b.py", "b" * 64, 1),
            FileSignature("a.py", "a" * 64, 1),
        ]
        s1 = _compute_manifest_signature(sigs)
        s2 = _compute_manifest_signature(sigs[::-1])
        # Sort-by-path makes ordering irrelevant
        assert s1 == s2

    def test_manifest_signature_changes_on_file_change(self):
        sigs1 = [FileSignature("a.py", "a" * 64, 1)]
        sigs2 = [FileSignature("a.py", "b" * 64, 1)]
        assert (
            _compute_manifest_signature(sigs1)
            != _compute_manifest_signature(sigs2)
        )

    def test_manifest_signature_empty_is_empty_sha(self):
        assert _compute_manifest_signature([]) == (
            hashlib.sha256(b"").hexdigest()
        )


# ---------------------------------------------------------------------------
# compute_current_signatures — walks real governance/
# ---------------------------------------------------------------------------


class TestComputeCurrentSignatures:
    def test_walks_real_governance(self):
        sigs = compute_current_signatures()
        # Real repo should have many governance/ .py files
        assert len(sigs) > 100

    def test_signatures_have_canonical_prefix(self):
        sigs = compute_current_signatures()
        for s in sigs[:10]:
            assert s.relative_path.startswith(
                "backend/core/ouroboros/governance/",
            )
            assert len(s.sha256) == 64
            assert s.size_bytes >= 0

    def test_no_pycache_entries(self):
        sigs = compute_current_signatures()
        for s in sigs:
            assert "__pycache__" not in s.relative_path

    def test_max_files_cap_respected(self):
        sigs = compute_current_signatures(max_files=5)
        assert len(sigs) <= 5

    def test_missing_governance_dir_returns_empty(self, tmp_path):
        sigs = compute_current_signatures(
            governance_dir=tmp_path / "does_not_exist",
        )
        assert sigs == ()


# ---------------------------------------------------------------------------
# compute_current_manifest
# ---------------------------------------------------------------------------


class TestComputeCurrentManifest:
    def test_aggregate_signature_present(self):
        snap = compute_current_manifest("test")
        assert len(snap.manifest_sha256) == 64

    def test_deterministic(self):
        snap1 = compute_current_manifest("test1", now_unix=1.0)
        snap2 = compute_current_manifest("test2", now_unix=2.0)
        # Same file contents → same manifest signature regardless
        # of label or timestamp
        assert snap1.manifest_sha256 == snap2.manifest_sha256

    def test_operator_label_preserved(self):
        snap = compute_current_manifest(
            "pre-M10-flip", now_unix=42.0,
        )
        assert snap.operator_label == "pre-M10-flip"
        assert snap.signed_at_unix == 42.0


# ---------------------------------------------------------------------------
# load_signed_manifest
# ---------------------------------------------------------------------------


class TestLoadSignedManifest:
    def test_missing_returns_none(self, tmp_path):
        result = load_signed_manifest(
            tmp_path / "does_not_exist.json",
        )
        assert result is None

    def test_malformed_json_returns_none(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json {{")
        assert load_signed_manifest(f) is None

    def test_non_dict_returns_none(self, tmp_path):
        f = tmp_path / "list.json"
        f.write_text("[1, 2, 3]")
        assert load_signed_manifest(f) is None

    def test_well_formed_loads(self, tmp_path):
        f = tmp_path / "good.json"
        payload = {
            "schema_version": GOVERNANCE_MANIFEST_SCHEMA_VERSION,
            "operator_label": "test",
            "signed_at_unix": 100.0,
            "signatures": [
                {"relative_path": "a.py", "sha256": "a" * 64,
                 "size_bytes": 1},
            ],
            "manifest_sha256": "b" * 64,
        }
        f.write_text(json.dumps(payload))
        snap = load_signed_manifest(f)
        assert snap is not None
        assert snap.operator_label == "test"
        assert len(snap.signatures) == 1


# ---------------------------------------------------------------------------
# compare_manifests — every verdict reachable
# ---------------------------------------------------------------------------


def _snap(*sigs) -> ManifestSnapshot:
    """Build a test manifest snapshot."""
    file_sigs = tuple(
        FileSignature(rp, sha, sz)
        for rp, sha, sz in sigs
    )
    return ManifestSnapshot(
        schema_version=GOVERNANCE_MANIFEST_SCHEMA_VERSION,
        operator_label="test",
        signed_at_unix=0.0,
        signatures=file_sigs,
        manifest_sha256=_compute_manifest_signature(file_sigs),
    )


class TestCompareManifests:
    def test_missing_signed_returns_missing_manifest(self):
        current = _snap(("a.py", "a" * 64, 1))
        r = compare_manifests(current, None)
        assert r.verdict is ManifestVerdict.MISSING_MANIFEST
        assert r.current_file_count == 1
        assert r.signed_file_count == 0

    def test_empty_current_returns_empty_governance(self):
        signed = _snap(("a.py", "a" * 64, 1))
        current = _snap()
        r = compare_manifests(current, signed)
        assert r.verdict is ManifestVerdict.EMPTY_GOVERNANCE

    def test_identical_returns_match(self):
        sig = ("a.py", "a" * 64, 1)
        r = compare_manifests(_snap(sig), _snap(sig))
        assert r.verdict is ManifestVerdict.MATCH
        assert r.drifted_paths == ()
        assert r.added_paths == ()
        assert r.removed_paths == ()

    def test_hash_drift_detected(self):
        current = _snap(("a.py", "a" * 64, 1))
        signed = _snap(("a.py", "b" * 64, 1))
        r = compare_manifests(current, signed)
        assert r.verdict is ManifestVerdict.DRIFT
        assert "a.py" in r.drifted_paths

    def test_added_files_detected(self):
        current = _snap(
            ("a.py", "a" * 64, 1),
            ("b.py", "b" * 64, 1),
        )
        signed = _snap(("a.py", "a" * 64, 1))
        r = compare_manifests(current, signed)
        assert r.verdict is ManifestVerdict.DRIFT
        assert "b.py" in r.added_paths

    def test_removed_files_detected(self):
        current = _snap(("a.py", "a" * 64, 1))
        signed = _snap(
            ("a.py", "a" * 64, 1),
            ("b.py", "b" * 64, 1),
        )
        r = compare_manifests(current, signed)
        assert r.verdict is ManifestVerdict.DRIFT
        assert "b.py" in r.removed_paths

    def test_target_files_filter_scopes_drift(self):
        """When target_files is supplied, unrelated drift in
        other governance/ files MUST NOT trigger refusal."""
        current = _snap(
            ("a.py", "a" * 64, 1),  # matches signed
            ("b.py", "x" * 64, 1),  # drifted from signed
        )
        signed = _snap(
            ("a.py", "a" * 64, 1),
            ("b.py", "b" * 64, 1),
        )
        # Filter to a.py only — drift on b.py shouldn't fire
        r = compare_manifests(
            current, signed, target_files=["a.py"],
        )
        assert r.verdict is ManifestVerdict.MATCH

    def test_target_files_filter_catches_relevant_drift(self):
        current = _snap(("a.py", "x" * 64, 1))
        signed = _snap(("a.py", "a" * 64, 1))
        r = compare_manifests(
            current, signed, target_files=["a.py"],
        )
        assert r.verdict is ManifestVerdict.DRIFT
        assert "a.py" in r.drifted_paths

    def test_drift_paths_bounded(self):
        # 100 drifted files — must clamp to 32
        current_sigs = [
            (f"f{i}.py", "x" * 64, 1) for i in range(100)
        ]
        signed_sigs = [
            (f"f{i}.py", "a" * 64, 1) for i in range(100)
        ]
        r = compare_manifests(_snap(*current_sigs), _snap(*signed_sigs))
        assert r.verdict is ManifestVerdict.DRIFT
        assert len(r.drifted_paths) == 32


# ---------------------------------------------------------------------------
# verify_governance_state — end-to-end
# ---------------------------------------------------------------------------


class TestVerifyGovernanceState:
    def test_master_off_returns_disabled(self):
        r = verify_governance_state()
        assert r.verdict is ManifestVerdict.DISABLED

    def test_master_on_no_manifest_returns_missing(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = verify_governance_state()
        assert r.verdict is ManifestVerdict.MISSING_MANIFEST

    def test_master_on_baselined_returns_match(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        target = tmp_path / "manifest.json"
        monkeypatch.setenv(_ENV_MANIFEST_PATH, str(target))
        # Baseline the current state
        outcome = refresh_signed_manifest(
            "test-baseline", path=target,
        )
        assert outcome.ok
        # Immediate re-check should MATCH
        r = verify_governance_state()
        assert r.verdict is ManifestVerdict.MATCH

    def test_master_on_drift_detected(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        target = tmp_path / "manifest.json"
        monkeypatch.setenv(_ENV_MANIFEST_PATH, str(target))
        refresh_signed_manifest("baseline", path=target)
        # Corrupt the manifest's first entry's sha
        raw = json.loads(target.read_text())
        assert raw["signatures"]
        raw["signatures"][0]["sha256"] = "f" * 64
        target.write_text(json.dumps(raw))
        # Now the current state diverges from the (corrupted) signed
        r = verify_governance_state()
        assert r.verdict is ManifestVerdict.DRIFT
        assert is_refusal_verdict(r.verdict)


# ---------------------------------------------------------------------------
# refresh_signed_manifest
# ---------------------------------------------------------------------------


class TestRefreshSignedManifest:
    def test_requires_operator_label(self, tmp_path):
        target = tmp_path / "manifest.json"
        outcome = refresh_signed_manifest("", path=target)
        assert outcome.ok is False
        assert "operator_label" in outcome.error.lower()

    def test_writes_manifest_atomically(self, tmp_path):
        target = tmp_path / "manifest.json"
        outcome = refresh_signed_manifest(
            "test-label", path=target,
        )
        assert outcome.ok is True
        assert target.exists()
        # Verify the temp file was cleaned up (atomic rename)
        assert not (target.with_suffix(".json.tmp")).exists()

    def test_written_manifest_round_trips(self, tmp_path):
        target = tmp_path / "manifest.json"
        outcome = refresh_signed_manifest(
            "round-trip-test", path=target,
        )
        assert outcome.ok
        loaded = load_signed_manifest(target)
        assert loaded is not None
        assert loaded.operator_label == "round-trip-test"
        assert loaded.manifest_sha256 == outcome.manifest_sha256

    def test_creates_parent_directories(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "manifest.json"
        outcome = refresh_signed_manifest("test", path=nested)
        assert outcome.ok
        assert nested.exists()


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_source():
    src = Path(gm.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    return src, tree


@pytest.fixture
def pins():
    return gm.register_shipped_invariants()


class TestAstPinsCanonicalPass:
    def test_5_pins_registered(self, pins):
        assert len(pins) == 5
        names = {p.invariant_name for p in pins}
        assert names == {
            "governance_manifest_verdict_taxonomy_closed",
            "governance_manifest_authority_asymmetry",
            "governance_manifest_master_default_false",
            "governance_manifest_composes_boundary_gate",
            "governance_manifest_only_writer_is_refresh",
        }

    @pytest.mark.parametrize(
        "pin_name",
        [
            "governance_manifest_verdict_taxonomy_closed",
            "governance_manifest_authority_asymmetry",
            "governance_manifest_master_default_false",
            "governance_manifest_composes_boundary_gate",
            "governance_manifest_only_writer_is_refresh",
        ],
    )
    def test_pin_passes_on_canonical(
        self, canonical_source, pins, pin_name,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins if p.invariant_name == pin_name
        )
        assert not pin.validate(tree, src)


class TestAstPinsSyntheticRegression:
    def test_verdict_pin_fires_on_drift(self, pins):
        synthetic = """
import enum
class ManifestVerdict(str, enum.Enum):
    MATCH = "match"
    DRIFT = "drift"
    MISSING_MANIFEST = "missing_manifest"
    EMPTY_GOVERNANCE = "empty_governance"
    DISABLED = "disabled"
    EXTRA = "extra"
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "governance_manifest_verdict_taxonomy_closed"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "drift" in violations[0]

    def test_authority_pin_fires(self, pins):
        synthetic = (
            "from backend.core.ouroboros.governance.auto_committer "
            "import ov_signature_substring\n"
        )
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "governance_manifest_authority_asymmetry"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "auto_committer" in violations[0]

    def test_master_pin_fires_on_default_true(self, pins):
        synthetic = """
def master_enabled():
    return _flag("FOO", default=True)
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "governance_manifest_master_default_false"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_composes_boundary_pin_fires_on_missing(self, pins):
        synthetic = "x = 1\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "governance_manifest_composes_boundary_gate"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "governance_boundary_gate" in violations[0]


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeds:
    def test_seeds_auto_discovered(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        names = {f.name for f in reg.list_all()}
        for expected in [
            "JARVIS_GOVERNANCE_MANIFEST_ENABLED",
            "JARVIS_GOVERNANCE_MANIFEST_PATH",
            "JARVIS_GOVERNANCE_MANIFEST_MAX_FILES",
        ]:
            assert expected in names, f"missing seed: {expected}"

    def test_master_seed_safety_default_false(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        spec = next(
            f for f in reg.list_all()
            if f.name == "JARVIS_GOVERNANCE_MANIFEST_ENABLED"
        )
        assert spec.default is False
        assert spec.category.value == "safety"


# ---------------------------------------------------------------------------
# AutoCommitter integration — structural pin
# ---------------------------------------------------------------------------


class TestAutoCommitterIntegration:
    """AutoCommitter MUST compose verify_governance_state +
    is_refusal_verdict pre-commit. Source AST pin guards
    against regression — if the integration disappears, the
    hash-cap silently no-ops."""

    def test_autocommitter_composes_manifest_check(self):
        from backend.core.ouroboros.governance import auto_committer
        src = Path(auto_committer.__file__).read_text(
            encoding="utf-8",
        )
        # Must lazy-import + compose the verifier
        assert "from backend.core.ouroboros.governance.governance_manifest" in src
        assert "verify_governance_state" in src
        assert "is_refusal_verdict" in src
        # Must return governance_manifest_drift skipped reason
        assert "governance_manifest_drift" in src


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_all_exports_present(self):
        for name in gm.__all__:
            assert getattr(gm, name) is not None

    def test_schema_version(self):
        assert GOVERNANCE_MANIFEST_SCHEMA_VERSION.startswith(
            "governance_manifest.",
        )
