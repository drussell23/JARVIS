"""
Tests for scripts/migrate_memory_topics.py — MEM-3 validation.

Covers: domain classification, modules extraction, frontmatter shape,
feedback-skip logic, and idempotent re-run behaviour.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the script under test
# ---------------------------------------------------------------------------
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "migrate_memory_topics.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("migrate_memory_topics", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mmt = _load_script()


# ---------------------------------------------------------------------------
# Helper — build a tiny fake memory file
# ---------------------------------------------------------------------------
_FAKE_SOVEREIGN = textwrap.dedent("""\
    ---
    name: project_sovereign_test
    description: Sovereign test memory entry
    ---

    # Sovereign Cross-Repo Mutator Test

    This is a test. MERGED PR #99999.

    See `backend/core/ouroboros/governance/providers.py` for implementation.
    Also `scripts/some_script.py` is relevant.
    And `extensions/vscode-jarvis/src/index.ts` for IDE integration.
""")

_FAKE_INTAKE = textwrap.dedent("""\
    ---
    name: project_a1_intake_test
    ---

    # A1 Intake Dispatch Test

    The intake DLQ is at backend/core/ouroboros/governance/intake_dlq.py
    and the router at backend/core/ouroboros/governance/urgency_router.py.
    PR #00001 OPEN (not yet merged).
""")


# ---------------------------------------------------------------------------
# classify_domain
# ---------------------------------------------------------------------------
class TestClassifyDomain:
    def test_sovereign_prefix(self):
        domain = mmt.classify_domain("project_sovereign_egress_interceptor", "")
        assert domain == "sovereign"

    def test_sovereign_cross_repo(self):
        domain = mmt.classify_domain("project_sovereign_cross_repo_mutator", "")
        assert domain == "sovereign"

    def test_swarm_by_keyword(self):
        domain = mmt.classify_domain("project_sovereign_swarm", "")
        assert domain == "swarm"

    def test_omni_integration_goes_to_swarm(self):
        domain = mmt.classify_domain("project_omni_integration_mas", "")
        assert domain == "swarm"

    def test_dw_goes_to_providers(self):
        domain = mmt.classify_domain("project_dw_reasoning_capability_profiler", "")
        assert domain == "providers"

    def test_a1_goes_to_intake(self):
        domain = mmt.classify_domain("project_a1_intake_dispatch", "")
        assert domain == "intake"

    def test_oracle_prefix(self):
        domain = mmt.classify_domain("project_oracle_cache_oom_hardening", "")
        assert domain == "oracle"

    def test_slice_goes_to_slices(self):
        domain = mmt.classify_domain("project_slice_47_v43_soak", "")
        assert domain == "slices"

    def test_phase_goes_to_ouroboros(self):
        domain = mmt.classify_domain("project_phase_b_subagent_roadmap", "")
        assert domain == "ouroboros"

    def test_gap_goes_to_ouroboros(self):
        domain = mmt.classify_domain("project_gap_4_review_branch", "")
        assert domain == "ouroboros"

    def test_wave_goes_to_ouroboros(self):
        domain = mmt.classify_domain("project_wave2_phaserunner_slice1", "")
        assert domain == "ouroboros"

    def test_vision_sensor(self):
        domain = mmt.classify_domain("project_vision_sensor_verify_arc", "")
        assert domain == "vision"

    def test_v2_goes_to_battle_test(self):
        domain = mmt.classify_domain("project_v2_89_venom_v2_observability_substrate", "")
        assert domain == "battle_test"

    def test_memory_user_preference(self):
        domain = mmt.classify_domain("project_user_preference_memory", "")
        assert domain == "memory"

    def test_soak_goes_to_battle_test(self):
        domain = mmt.classify_domain("project_soak_v5_findings", "")
        assert domain == "battle_test"

    def test_infra_launchd(self):
        domain = mmt.classify_domain("project_jarvis_launchd_keepalive", "")
        assert domain == "infra"


# ---------------------------------------------------------------------------
# extract_modules
# ---------------------------------------------------------------------------
class TestExtractModules:
    def test_extracts_backend_path(self):
        body = "See `backend/core/ouroboros/governance/providers.py` for details."
        mods = mmt.extract_modules(body)
        assert any("providers.py" in m for m in mods), f"Expected providers.py in {mods}"

    def test_extracts_scripts_path(self):
        body = "Run `scripts/migrate_memory_topics.py` to migrate."
        mods = mmt.extract_modules(body)
        assert any("migrate_memory_topics.py" in m for m in mods), f"Expected script in {mods}"

    def test_deduplicates(self):
        body = (
            "backend/core/ouroboros/governance/providers.py mentioned twice. "
            "backend/core/ouroboros/governance/providers.py again."
        )
        mods = mmt.extract_modules(body)
        count = sum(1 for m in mods if "providers.py" in m)
        assert count == 1, f"Expected dedup, got {mods}"

    def test_cap_respected(self):
        lines = [f"backend/module_{i}/file_{i}.py" for i in range(20)]
        body = " ".join(lines)
        mods = mmt.extract_modules(body, cap=5)
        assert len(mods) <= 5

    def test_empty_body(self):
        mods = mmt.extract_modules("")
        assert mods == []


# ---------------------------------------------------------------------------
# Frontmatter derivation
# ---------------------------------------------------------------------------
class TestFrontmatter:
    def test_title_from_heading(self):
        body = "# My Cool Title\n\nSome content."
        title = mmt.derive_title(body, "project_my_cool")
        assert title == "My Cool Title"

    def test_title_fallback_to_stem(self):
        body = "No heading here."
        title = mmt.derive_title(body, "project_my_cool_thing")
        assert "Project My Cool Thing" in title or "project_my_cool_thing" in title.lower()

    def test_status_merged(self):
        assert mmt.derive_status("PR #123 MERGED, main abc123") == "merged"

    def test_status_graduated(self):
        assert mmt.derive_status("Arc GRADUATED on 2026-04-20.") == "merged"

    def test_status_open(self):
        # OPEN takes priority; "not yet merged" should NOT trigger merged
        assert mmt.derive_status("PR #123 OPEN (not yet merged)") == "open"

    def test_status_historical(self):
        assert mmt.derive_status("Some old historical note.") == "historical"

    def test_build_frontmatter_shape(self):
        fm = mmt.build_frontmatter(
            title="Test Title",
            modules=["backend/foo.py", "scripts/bar.py"],
            status="merged",
            source="project_test.md",
        )
        assert fm.startswith("---\n")
        assert "title: Test Title" in fm
        assert "modules: [backend/foo.py, scripts/bar.py]" in fm
        assert "status: merged" in fm
        assert "source: project_test.md" in fm
        assert fm.rstrip().endswith("---")

    def test_build_frontmatter_empty_modules(self):
        fm = mmt.build_frontmatter("T", [], "historical", "x.md")
        assert "modules: []" in fm


# ---------------------------------------------------------------------------
# strip_existing_frontmatter
# ---------------------------------------------------------------------------
class TestStripFrontmatter:
    def test_strips_yaml_fm(self):
        raw = "---\nname: foo\n---\n\n# Body"
        result = mmt.strip_existing_frontmatter(raw)
        assert result.startswith("# Body")
        assert "name: foo" not in result

    def test_no_fm_unchanged(self):
        raw = "# Body\nNo frontmatter."
        result = mmt.strip_existing_frontmatter(raw)
        assert result == raw


# ---------------------------------------------------------------------------
# should_migrate
# ---------------------------------------------------------------------------
class TestShouldMigrate:
    def test_project_file_migrates(self):
        assert mmt.should_migrate("project_sovereign_swarm.md") is True

    def test_feedback_skipped(self):
        assert mmt.should_migrate("feedback_commit_hygiene.md") is False

    def test_memory_skipped(self):
        assert mmt.should_migrate("MEMORY.md") is False

    def test_non_arch_other_skipped(self):
        assert mmt.should_migrate("derek-job-search-profile.md") is False
        assert mmt.should_migrate("user_role.md") is False

    def test_non_md_skipped(self):
        assert mmt.should_migrate("project_something.txt") is False

    def test_draft_scope_migrates(self):
        assert mmt.should_migrate("draft_p0_5_scope.md") is True


# ---------------------------------------------------------------------------
# End-to-end: migrate_file on a tmp fixture
# ---------------------------------------------------------------------------
class TestMigrateFileE2E:
    def test_sovereign_fixture(self, tmp_path):
        src = tmp_path / "project_sovereign_test.md"
        src.write_text(_FAKE_SOVEREIGN, encoding="utf-8")
        dest_root = tmp_path / "out"

        result = mmt.migrate_file(src, dest_root, dry_run=False)

        assert result is not None
        domain, dest_path, title = result
        assert domain == "sovereign"
        assert "sovereign" in dest_path
        content = Path(dest_path).read_text(encoding="utf-8")
        # frontmatter present
        assert content.startswith("---\n")
        assert "title:" in content
        assert "modules:" in content
        assert "status: merged" in content
        assert "source: project_sovereign_test.md" in content
        # original body preserved
        assert "Sovereign Cross-Repo Mutator Test" in content
        # modules extracted
        assert "providers.py" in content or "backend/" in content

    def test_intake_fixture(self, tmp_path):
        src = tmp_path / "project_a1_intake_test.md"
        src.write_text(_FAKE_INTAKE, encoding="utf-8")
        dest_root = tmp_path / "out"

        result = mmt.migrate_file(src, dest_root, dry_run=False)

        assert result is not None
        domain, dest_path, title = result
        assert domain == "intake"
        content = Path(dest_path).read_text(encoding="utf-8")
        assert "status: open" in content

    def test_feedback_not_migrated(self, tmp_path):
        src = tmp_path / "feedback_commit_hygiene.md"
        src.write_text("# Feedback\nSome notes.", encoding="utf-8")
        dest_root = tmp_path / "out"

        result = mmt.migrate_file(src, dest_root, dry_run=False)
        assert result is None
        # Destination dir should not be created at all
        assert not (dest_root / "misc" / "feedback_commit_hygiene.md").exists()

    def test_idempotent_rerun(self, tmp_path):
        src = tmp_path / "project_sovereign_test.md"
        src.write_text(_FAKE_SOVEREIGN, encoding="utf-8")
        dest_root = tmp_path / "out"

        mmt.migrate_file(src, dest_root, dry_run=False)
        # Second run must not raise and must produce same content
        result2 = mmt.migrate_file(src, dest_root, dry_run=False)
        assert result2 is not None
        domain, dest_path, _ = result2
        content = Path(dest_path).read_text(encoding="utf-8")
        assert content.startswith("---\n")
