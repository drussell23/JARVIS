---
title: Project Slice198 Sovereign Ignition
modules: []
status: merged
source: project_slice198_sovereign_ignition.md
---

**Slice 198 — Sovereign Ignition Protocol (MERGED #69437, main `a62c21df79`, 2026-06-10).**

**Why:** [[project-slice197-autonomous-graduation]] graduated M10 but live verify found it half-wired — `m10_arch_proposer_enabled=True` yet `cadence_enabled=False`, protection bundle (taste+orange) dark. Graduated proposer + no trigger = dead engine.

**How to apply:** Three-state pattern extended to 3 more sub-flags (explicit env wins, `=0` supreme kill switch, unset → graduation-gated arming): `cadence_runner.cadence_enabled` → `m10_cadence_ignited()` (= is_autonomously_unlocked); `architectural_taste_layer.master_enabled` → `taste_layer_armed()`; `orange_pr_reviewer.is_orange_pr_enabled` → `orange_pr_armed()`. Arming fns in `m10_autonomous_graduation.py`: `taste_layer_assertion_passes` (synthetic micro-proposal via master-independent `assess_file` scorer w/ source_override — responsive, no git/model; PASSES in-container), `orange_pr_assertion_passes` (gh-binary + git-work-tree + remote preflight, NO push; FAILS in gitless container — honest fail-closed). **KEY HONEST FINDING: the soak CONTAINER has no `.git` and no `gh` CLI (GH_TOKEN in env but no binary/repo) → orange-PR + auto-commit + taste-profiling CANNOT complete the "ship a proposal" path in-container. M10 can mine/synthesize/validate but NOT commit/PR from the gitless container. The durable §41.6 "proposals shipped" + "OV-signed commits" evidence rows REQUIRE a git+gh host (GCP migration target or local non-container) — not the Docker soak.** Also: DROPPED the is_autonomously_unlocked 30s negative cache (persisted-file short-circuit makes True sticky+cheap; removal = ignition fires the millisecond criteria go healthy). §33.1 taste invariant `_validate_master_default_false` UPDATED (not weakened) to pin unset-path-routes-through-taste_layer_armed. Boundary gate untouched (re-pinned). 21 tests; 319+ regression. taste assess_file is master-INDEPENDENT (good test seam). See [[project-slice193-observability-registry]].
