---
title: Project Iron Gate Pushb
modules: [docs/architecture/OUROBOROS.md]
status: historical
source: project_iron_gate_pushB.md
---

Four-gate deterministic perimeter push targeting the C+ → B letter grade jump on O+V.

Motivation: after the first autonomous APPLY (Apr 10), the grade was C+. Three gaps drove the D+ intelligence rating: (1) tool calls not enforced before patch, (2) L2 repair disabled by default, (3) no format check on non-code files — the `rapidفuzz` Unicode typo slipped through to `pip install` because requirements.txt short-circuited validation.

## What landed

1. **Iron Gate — Exploration-first hard fail** (`orchestrator.py` ~line 1398, post-GENERATE).
   Was a soft warning; now a hard RuntimeError that flows through the existing GENERATE_RETRY loop with targeted feedback: "you MUST call read_file/search_code at least 2 times before returning a patch". Trivial ops bypass the gate. Env: `JARVIS_EXPLORATION_GATE` (default `true`), `JARVIS_MIN_EXPLORATION_CALLS` (default `2`).

2. **Iron Gate — ASCII strictness** (`orchestrator.py` same block).
   Deterministic O(n) scan over every candidate's `full_content`. Any codepoint > 127 hard-fails the generation with targeted "re-emit with 7-bit ASCII" feedback. Catches the `rapidفuzz` class, smart-quotes, Cyrillic look-alikes. Env: `JARVIS_ASCII_GATE` (default `true`).

3. **L2 repair enabled by default** (`repair_engine.py:87, 115`).
   Flipped `RepairBudget.enabled: bool = True` and the env default from `"false"` to `"true"`. Accepts `false|0|no|off` as opt-out. Tests in `test_repair_engine.py` and `test_governed_loop_l2.py` updated to match new default. Verified with `pytest` — 4+7 = 11 green. Boot banner confirms `[GovernedLoop] L2 RepairEngine wired: max_iterations=5, timebox=120.0s`.

4. **Pre-APPLY config format gate** (`orchestrator._validate_config_file_format`, new staticmethod).
   Replaces the `"validation skipped: non-code file"` short-circuit with a real format check. Handles `requirements*.txt` (line-by-line PEP 508 parsing + non-ASCII check), `*.json` / `package.json` (json.loads), `*.yaml` / `*.yml` (yaml.safe_load when PyYAML is present). Returns a failure_class="build" ValidationResult on reject so the existing retry cascade handles it.

## Docs updated
- `CLAUDE.md` — L2 enabled-by-default, new "Iron Gate" subsystem paragraph, exploration-first wording hardened
- `README.md` — env-var table updated (L2=true, new JARVIS_EXPLORATION_GATE / JARVIS_MIN_EXPLORATION_CALLS / JARVIS_ASCII_GATE rows)
- `docs/architecture/OUROBOROS.md` — both L2 env-var tables flipped to `true`

## Verification
- `pytest test_repair_engine.py::TestRepairBudget` → 4 passed
- `pytest test_governed_loop_l2.py::TestGovernedLoopConfigRepairBudget` → 7 passed
- Synthetic Iron Gate smoke test (direct staticmethod invocation):
  - rapidفuzz typo → rejected with U+0641 message
  - Smart-quote typo → rejected with U+201D message
  - Invalid JSON → rejected with line/col
  - URL/VCS requirements → pass
  - PEP 508 environment markers → pass
- Battle test boot banner shows `L2 Repair Engine ON` and `bash + web + tests + L2` in Venom info

## Not yet verified in-flight
Battle test cascaded on pre-existing DW+Claude generation failures (DW RT budget exhaustion, Claude JSON parse errors) so no candidate reached the Iron Gate in this session. The gate code path is proven via the smoke test. The cascade itself is a separate pre-existing issue — zombie battle tests from 7:39 PM were competing for budget; killed both processes before wrap-up.

**Why:** Closed three of the four gaps identified in the O+V assessment (exploration enforcement, L2 default, pre-APPLY config validation). The ASCII gate is net-new and prevents the exact failure mode from the first autonomous APPLY.
**How to apply:** The new grade target is B. The three highest-value remaining gaps for B → A- are (1) multi-file coordinated generation, (2) real sensor implementations (not stubs), (3) direction inference from git history + manifesto.
