#!/usr/bin/env python3
"""
migrate_memory_topics.py — MEM-3 deterministic memory migration script.

Migrates architectural project_*.md (and selected other .md) files from the
Claude harness memory directory into docs/memory_topics/<domain>/<name>.md,
prepending structured frontmatter and generating an INDEX.md.

Usage:
    python3 scripts/migrate_memory_topics.py [--dry-run] [--source DIR] [--dest DIR]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------
_DEFAULT_SOURCE = os.path.expanduser(
    "~/.claude/projects/-Users-djrussell23-Documents-repos-JARVIS-AI-Agent/memory"
)
_DEFAULT_DEST = str(
    Path(__file__).resolve().parent.parent / "docs" / "memory_topics"
)

# ---------------------------------------------------------------------------
# Domain classifier — ordered rules, FIRST MATCH wins.
# Each entry: (match_fn, domain_str)
# ---------------------------------------------------------------------------
def _prefix(p: str):
    """Return a predicate that checks whether the stem starts with p."""
    def _fn(stem: str, _body: str) -> bool:
        return stem.startswith(p)
    return _fn


def _any_kw(*kws: str):
    """Return a predicate that checks whether the stem contains any keyword."""
    def _fn(stem: str, _body: str) -> bool:
        return any(k in stem for k in kws)
    return _fn


def _body_kw(*kws: str):
    """Return a predicate that checks whether the body contains any keyword."""
    def _fn(_stem: str, body: str) -> bool:
        lower = body.lower()
        return any(k.lower() in lower for k in kws)
    return _fn


# Rules are evaluated in order; first match wins.
# stem = filename without extension (e.g. "project_sovereign_swarm")
DOMAIN_RULES: list[tuple] = [
    # ---- slices (numbered/lettered slice arcs) ----
    (_prefix("project_slice"), "slices"),

    # ---- swarm (multi-agent swarm + omni integration) ----
    (_any_kw("sovereign_swarm", "omni_integration_mas", "sovereign_fleet", "elastic_fanout",
             "agent_message_bus", "ephemeral_sandbox", "deadlock_breaker"), "swarm"),

    # ---- sovereign (cross-repo governance, command node, egress, self-termination…) ----
    (_prefix("project_sovereign_"), "sovereign"),
    (_any_kw("sovereign_egress", "sovereign_exec", "sovereign_execution",
             "sovereign_command", "sovereign_cross_repo", "sovereign_telemetry",
             "sovereign_evidence", "sovereign_resilience", "sovereign_state_propagation",
             "sovereign_sentinel", "sovereign_self_termination",
             "gitops_identity", "cross_repo_scope"), "sovereign"),

    # ---- providers (DW, Claude, J-Prime failover, routing) ----
    (_prefix("project_dw_"), "providers"),
    (_any_kw("failover_lifecycle", "jprime_local", "predictive_provider",
             "zero_waste_predictive", "provider_quarantine", "provider_resilience",
             "epistemic_feedback_lane", "dw_reasoning", "dw_economic",
             "dw_event_driven", "dw_completion", "dw_sovereignty",
             "dw_phantom", "containerized_dw"), "providers"),

    # ---- intake (A1 sensors, intake router, admission gate, DLQ, WAL) ----
    (_prefix("project_a1_"), "intake"),
    (_any_kw("intake_dispatch", "intake_layer", "admission_gate",
             "sensor_governor", "deep_analysis_sensor", "github_issue_cooldown",
             "goal_relevance_scoring", "convergence_watchdog", "epistemic_context_matrix",
             "async_yield_matrix", "sovereign_state_propagation"), "intake"),

    # ---- oracle (semantic index, oracle cache, production oracle) ----
    (_prefix("project_oracle_"), "oracle"),
    (_any_kw("production_oracle", "oracle_cache", "oracle_sqlite",
             "oracle_to_auto", "oracle_ipc", "vendor_oracle",
             "cluster_intelligence", "codebase_character_digest",
             "semantic_inference", "module_discovery"), "oracle"),

    # ---- memory (user prefs, last session, conversation bridge) ----
    (_any_kw("user_preference_memory", "last_session_summary",
             "conversation_bridge", "session_history",
             "ov_devmemory"), "memory"),

    # ---- vision (vision sensor, VLA) ----
    (_any_kw("vision_sensor", "vision_repl"), "vision"),

    # ---- battle_test (harness, serpent, TUI, soaks, CC parity) ----
    (_prefix("project_v2_"), "battle_test"),
    (_prefix("project_v3_"), "battle_test"),
    (_any_kw("battle_test", "harness_epic", "serpent_split", "soak_v",
             "cc_parity", "pass_b_soak", "pass_c_graduation",
             "v45_preflight", "v46_preflight", "v57_hybrid",
             "v33_capability", "rubric_soak", "phase10_audit",
             "section_36_brutal", "section_37", "section_38", "section_39",
             "tier1_capability", "swe_bench", "swebp", "swebench",
             "eval2_macro", "first_container", "validation_infra"), "battle_test"),

    # ---- infra (GCP, docker, launchd, hardware, repos) ----
    (_any_kw("jarvis_launchd", "local_hardware", "containerized",
             "multi_repo_sharding", "repo_git_pr",
             "async_shutdown_race", "asyncio_audit",
             "operation_timeline_rewind", "artifact_janitor",
             "cursor_agent_git_ban", "followup_intake_wal",
             "followup_partial_summary", "followup_provider_retry",
             "followup_harness_idle", "followup_idle_timeout",
             "followup_stale_test", "followup_graduation_runbook",
             "followup_seed_exploration", "known_preexisting_test",
             "validation_infra_flakiness"), "infra"),

    # ---- ouroboros (core governance pipeline, phases, gaps, waves, moves, sections) ----
    (_prefix("project_phase_"), "ouroboros"),
    (_prefix("project_phase"), "ouroboros"),
    (_prefix("project_gap_"), "ouroboros"),
    (_prefix("project_wave"), "ouroboros"),
    (_prefix("project_move_"), "ouroboros"),
    (_prefix("project_section"), "ouroboros"),
    (_prefix("project_pass_"), "ouroboros"),
    (_prefix("project_reverse_russian"), "ouroboros"),
    (_prefix("project_priority_"), "ouroboros"),
    (_prefix("project_vector_"), "ouroboros"),
    (_prefix("project_w2_"), "ouroboros"),
    (_prefix("project_f1_"), "ouroboros"),
    (_prefix("project_s6_"), "ouroboros"),
    (_prefix("project_upgrade_"), "ouroboros"),
    (_any_kw("direction_inferrer", "flag_registry", "sensor_governor_plan",
             "manifesto", "ouroboros_direction", "ouroboros_session",
             "phase_b_subagent", "phase_b_step", "phase_c_general",
             "phase_c_semantic", "phase_cost",
             "iron_gate", "inline_prompt_gate",
             "autonomous_loop", "followup_battle_test",
             "followup_f1", "followup_f2", "followup_f5",
             "repair_context", "autonomous_loop_implements",
             "bg_spec", "lifecycle_hooks",
             "candidate_generator_defect", "persistent_intelligence",
             "wall_clock_watchdog", "wallclock_watchdog",
             "async_shutdown", "exploration_ledger",
             "multifile_enforcement", "no_verify_phase",
             "json_extractor", "exhaustion_watcher",
             "loop_shadow_mode", "operator_commit",
             "op_lifecycle_stream", "repl_dispatch_registry",
             "observability_route_registry", "priority_dispatch_audit",
             "skill_registry", "autonomous_graduation",
             "cognitive_graduation", "graduation_crucible",
             "rsi_convergence", "phd_strategic",
             "roadmap_orchestrator", "prd_hygiene", "prd_section",
             "north_star_galaxy", "manifesto_v5",
             "autonomous_loop_implements", "mission_inferrer",
             "m9_curiosity", "m10_architecture",
             "ouroboros_checkpoint", "lean_prompt",
             "ov_vs_claude", "sbt_probe",
             "anti_venom", "venom_hardening",
             "aegis", "section_28", "section_35",
             "problem_7_plan", "recovery_guidance",
             "cleanup_arc", "repair_engine",
             "move_2", "move_3", "move_4", "move_5", "move_6", "move_7", "move_8",
             "flag_registry_api", "flag_registry_plan",
             "ticket_4", "subagent_freeform",
             "worktree_isolation", "spec2_cybernetic",
             "stage_1_6_park", "draft_p0_5",
             "p0_5"), "ouroboros"),

    # ---- misc (catch-all) ----
]


def classify_domain(stem: str, body: str) -> str:
    """Return the domain string for this file."""
    for predicate, domain in DOMAIN_RULES:
        if predicate(stem, body):
            return domain
    return "misc"


# ---------------------------------------------------------------------------
# Path extraction (modules: field)
# ---------------------------------------------------------------------------
_PATH_RE = re.compile(
    r"(?:^|[ \t(`'\",])"
    r"((?:backend|scripts|extensions|tests|docs)"
    r"/[A-Za-z0-9_/.\-]+)"
    r"(?=[ \t\n`'\",:)\]>]|$)",
    re.MULTILINE,
)
_PATH_CLEAN_RE = re.compile(r"[`',\)\]>:]+$")
# Only keep paths that look like real source files or meaningful dirs
_PATH_VALID_SUFFIX_RE = re.compile(
    r"(\.py|\.ts|\.js|\.yaml|\.yml|\.json|\.sh|\.md)$"
    r"|/[a-zA-Z_][a-zA-Z0-9_]*$"  # ends on a module/package name
    r"|/[a-zA-Z_][a-zA-Z0-9_\-]*\.[a-zA-Z]{2,4}$"  # ends on a file
)


def extract_modules(body: str, cap: int = 12) -> list[str]:
    """Extract unique backend/scripts/extensions/tests paths from body text."""
    raw = _PATH_RE.findall(body)
    seen: list[str] = []
    seen_set: set[str] = set()
    for p in raw:
        p = _PATH_CLEAN_RE.sub("", p).strip()
        # Skip very short or obviously partial matches
        if len(p) < 8 or "/" not in p:
            continue
        # Must look like a real file or meaningful directory endpoint
        if not _PATH_VALID_SUFFIX_RE.search(p):
            # Allow paths that have at least 2 segments
            parts = p.split("/")
            if len(parts) < 3:
                continue
        # Normalise: strip leading ./
        p = p.lstrip("./")
        if p not in seen_set:
            seen_set.add(p)
            seen.append(p)
        if len(seen) >= cap:
            break
    return seen


# ---------------------------------------------------------------------------
# Frontmatter derivation
# ---------------------------------------------------------------------------
_HEADING_RE = re.compile(r"^#\s+(.+)", re.MULTILINE)
_EXISTING_FM_RE = re.compile(r"^---\s*\n.*?^---\s*\n", re.DOTALL | re.MULTILINE)

# Status detection — order matters: check OPEN before MERGED to avoid
# "not yet merged" triggering the MERGED pattern.
# MERGED: requires "MERGED" preceded by "PR #N" OR followed by nothing bad,
# OR "GRADUATED" anywhere. Explicitly excludes "NOT merged" / "not yet merged".
_MERGED_POSITIVE_RE = re.compile(
    r"MERGED\s+PR\s+#?\d+"           # "MERGED PR #NNN"
    r"|PR\s+#?\d+\s+MERGED"          # "PR #NNN MERGED"
    r"|\bGRADUATED\b",               # "GRADUATED" anywhere
    re.IGNORECASE,
)
_OPEN_RE = re.compile(
    r"\bPR\s+#?\d+\s+OPEN\b"
    r"|\bPR\s+open\b"
    r"|\bopen\s+PR\b",
    re.IGNORECASE,
)


def derive_title(body_no_fm: str, stem: str) -> str:
    """Extract title from first # heading or fall back to humanised stem."""
    m = _HEADING_RE.search(body_no_fm)
    if m:
        return m.group(1).strip()
    # Humanise stem
    return stem.replace("_", " ").replace("-", " ").title()


def derive_status(body_no_fm: str) -> str:
    if _OPEN_RE.search(body_no_fm):
        return "open"
    if _MERGED_POSITIVE_RE.search(body_no_fm):
        return "merged"
    return "historical"


def strip_existing_frontmatter(raw: str) -> str:
    """Remove the memory-system YAML frontmatter if present (starts with ---)."""
    if raw.startswith("---"):
        # Find the closing ---
        end = raw.find("\n---", 3)
        if end != -1:
            # Skip past the closing --- and optional newline
            after = raw[end + 4:]
            return after.lstrip("\n")
    return raw


def build_frontmatter(title: str, modules: list[str], status: str, source: str) -> str:
    mods_str = ", ".join(modules) if modules else ""
    mods_yaml = f"[{mods_str}]"
    return textwrap.dedent(f"""\
        ---
        title: {title}
        modules: {mods_yaml}
        status: {status}
        source: {source}
        ---
        """)


# ---------------------------------------------------------------------------
# File-level decision: should this file be migrated?
# ---------------------------------------------------------------------------
# Non-architectural "other" files to skip
_NON_ARCH_SKIP = frozenset({
    "derek-job-search-profile.md",
    "user_role.md",
    "project-comma-openpilot-engagement.md",
    "MEMORY.md",
})


def should_migrate(filename: str) -> bool:
    """Return True if the file should be migrated to docs/memory_topics/."""
    name = os.path.basename(filename)
    if name in _NON_ARCH_SKIP:
        return False
    if name.startswith("feedback_"):
        return False
    if name == "MEMORY.md":
        return False
    if not name.endswith(".md"):
        return False
    return True


# ---------------------------------------------------------------------------
# Main migration logic
# ---------------------------------------------------------------------------

def migrate_file(
    src_path: Path,
    dest_root: Path,
    dry_run: bool = False,
) -> Optional[tuple[str, str, str]]:
    """
    Migrate a single file.

    Returns (domain, dest_path_str, title) or None if skipped.
    """
    filename = src_path.name
    if not should_migrate(filename):
        return None

    raw = src_path.read_text(encoding="utf-8", errors="replace")
    body_no_fm = strip_existing_frontmatter(raw)

    stem = src_path.stem  # without .md
    domain = classify_domain(stem, body_no_fm)
    modules = extract_modules(body_no_fm)
    title = derive_title(body_no_fm, stem)
    status = derive_status(body_no_fm)
    source = filename

    fm = build_frontmatter(title, modules, status, source)
    dest_content = fm + "\n" + body_no_fm

    dest_dir = dest_root / domain
    dest_path = dest_dir / filename

    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(dest_content, encoding="utf-8")

    return (domain, str(dest_path), title)


def generate_index(
    migrated: list[tuple[str, str, str]],  # (domain, dest_path, title)
    dest_root: Path,
    dry_run: bool = False,
) -> None:
    """Generate docs/memory_topics/INDEX.md grouped by domain."""
    # Group by domain
    by_domain: dict[str, list[tuple[str, str]]] = {}
    for domain, dest_path, title in sorted(migrated):
        rel = Path(dest_path).relative_to(dest_root)
        modules_line = ""
        # Read the generated file to extract modules for the index line
        if not dry_run and Path(dest_path).exists():
            content = Path(dest_path).read_text(encoding="utf-8")
            m = re.search(r"^modules:\s*\[([^\]]*)\]", content, re.MULTILINE)
            if m and m.group(1).strip():
                mods = [x.strip() for x in m.group(1).split(",") if x.strip()]
                if mods:
                    # Show just the filenames for brevity
                    short_mods = [Path(mod).name for mod in mods[:3]]
                    modules_line = " — modules: " + ", ".join(short_mods)
        by_domain.setdefault(domain, []).append((str(rel), title, modules_line))

    lines = ["# Memory Topics Index\n", "Auto-generated by `scripts/migrate_memory_topics.py`.\n"]
    domain_order = [
        "sovereign", "swarm", "ouroboros", "slices", "providers",
        "intake", "oracle", "memory", "vision", "battle_test", "infra", "misc",
    ]
    all_domains = sorted(set(by_domain.keys()))
    ordered = [d for d in domain_order if d in by_domain]
    remaining = [d for d in all_domains if d not in ordered]
    for domain in ordered + remaining:
        entries = by_domain.get(domain, [])
        if not entries:
            continue
        lines.append(f"\n## {domain}/\n")
        for rel, title, mods in sorted(entries, key=lambda x: x[0]):
            lines.append(f"- [{title}]({rel}){mods}\n")

    index_path = dest_root / "INDEX.md"
    if not dry_run:
        index_path.write_text("".join(lines), encoding="utf-8")
        print(f"  Generated INDEX.md ({index_path})")
    else:
        print(f"  [dry-run] Would generate INDEX.md at {index_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate architectural memory files into docs/memory_topics/."
    )
    parser.add_argument(
        "--source",
        default=_DEFAULT_SOURCE,
        help="Source memory directory (default: %(default)s)",
    )
    parser.add_argument(
        "--dest",
        default=_DEFAULT_DEST,
        help="Destination directory (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without writing files.",
    )
    args = parser.parse_args(argv)

    source_dir = Path(args.source)
    dest_root = Path(args.dest)

    if not source_dir.is_dir():
        print(f"ERROR: source directory does not exist: {source_dir}", file=sys.stderr)
        return 1

    if not args.dry_run:
        dest_root.mkdir(parents=True, exist_ok=True)

    # Collect all .md files in source
    all_files = sorted(source_dir.glob("*.md"))
    print(f"Source: {source_dir} ({len(all_files)} .md files total)")

    migrated: list[tuple[str, str, str]] = []
    skipped_feedback = 0
    skipped_other = 0
    domain_counts: dict[str, int] = {}

    for src_path in all_files:
        name = src_path.name
        # Categorise skip reason for reporting
        if name.startswith("feedback_"):
            skipped_feedback += 1
            continue
        if name in _NON_ARCH_SKIP:
            skipped_other += 1
            if args.dry_run:
                print(f"  [skip non-arch] {name}")
            continue
        if name == "MEMORY.md":
            skipped_other += 1
            continue
        if not name.endswith(".md"):
            skipped_other += 1
            continue

        result = migrate_file(src_path, dest_root, dry_run=args.dry_run)
        if result:
            domain, dest_path, title = result
            migrated.append(result)
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            if args.dry_run:
                print(f"  [dry-run] {name} -> {domain}/{name}  (title: {title[:60]})")

    # Generate index
    generate_index(migrated, dest_root, dry_run=args.dry_run)

    # Summary
    print(f"\n{'[DRY-RUN] ' if args.dry_run else ''}Migration summary:")
    print(f"  Total migrated:        {len(migrated)}")
    print(f"  Skipped feedback_*:    {skipped_feedback}")
    print(f"  Skipped non-arch/meta: {skipped_other}")
    print(f"  Per-domain counts:")
    domain_order = [
        "sovereign", "swarm", "ouroboros", "slices", "providers",
        "intake", "oracle", "memory", "vision", "battle_test", "infra", "misc",
    ]
    for d in domain_order:
        if d in domain_counts:
            print(f"    {d:20s}: {domain_counts[d]}")
    for d in sorted(domain_counts):
        if d not in domain_order:
            print(f"    {d:20s}: {domain_counts[d]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
