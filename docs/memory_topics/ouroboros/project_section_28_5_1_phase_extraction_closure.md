---
title: Project Section 28 5 1 Phase Extraction Closure
modules: [tests/governance/test_phase_runner_extraction_closure.py, orchestrator.py]
status: historical
source: project_section_28_5_1_phase_extraction_closure.md
---

§28.5.1 v9 brutal-review entry "4-phases-not-extracted (CLASSIFY / APPROVE / APPLY / VERIFY)" was authored at a moment when those phases were inline blocks in `orchestrator.py`. Audit 2026-05-05 reveals the actual state has fully closed.

**Why:** the entry stayed `🔴 STILL OPEN` in §35 Open Strategic Moves Registry through Wave 2 + Wave 3 hygiene + Move 7 — the work landed but no one updated the entry. Treating it as still-open would have triggered a redundant Wave 3-style phase-extraction arc that is structurally already done.

**How to apply:** when an entry in §35 (or any open-vector list) names a structural deficiency, BEFORE scoping a closure arc, audit the file system to confirm the deficiency still exists. The Move 8 LLM-driver status reconciliation (Wave 3 Item 1) and this §28.5.1 closure both followed the same pattern: investigation → bytes-pin → docs flip → no new substrate code needed. The pattern is reusable for any "still open" entry whose age exceeds the most recent major arc that touched the area.

**Inventory (all default-true)**:
- CLASSIFY → `CLASSIFYRunner`
- ROUTE → `ROUTERunner`
- CONTEXT_EXPANSION → `ContextExpansionRunner`
- PLAN → `PLANRunner`
- GENERATE → `GENERATERunner`
- VALIDATE → `VALIDATERunner`
- GATE → `GATERunner`
- APPROVE + APPLY + VERIFY → `Slice4bRunner` (combined per Wave 2 architectural decision — APPROVE's tail / cancel-check / DRY_RUN gate runs on every path; APPLY consumes APPROVE local state; VERIFY consumes APPLY local state; separate runners would need 6-way artifact threading; combining preserves inline semantics with one flag + one reindent)
- COMPLETE → `COMPLETERunner`

**Closure artifact**: `tests/governance/test_phase_runner_extraction_closure.py` (31/31 green) pins all 9 master flags default-true + module-existence + `__init__.py` exports + dispatch wiring + Slice4b combined-coverage + directory-shape (exactly 9 modules; deletion or addition fails CI).

**PRD**: §35 entry flipped 🔴 → ✅ CLOSED 2026-05-05; version 2.33 → 2.34.
