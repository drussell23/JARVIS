# Spec 1 — The Sovereign Distillation (Phases A & B)

**Date:** 2026-06-14
**Author:** Derek J. Russell (O+V architect) / Claude
**Target:** `unified_supervisor.py` (102,486 lines, 399 top-level classes)
**Scope:** Phase A (Shadow Resolution) + Phase B (The Purge). Phase C (Cybernetic Reanimation) is a **separate spec** and is explicitly out of scope here.
**Companion spec (later):** `2026-06-14-cybernetic-reanimation-design.md` (Phase C).

---

## 1. Problem Statement

`unified_supervisor.py` is a 102K-line monolith that has accreted two distinct classes of structural rot around a relatively small live kernel:

1. **Shadowed duplicate definitions.** Six class names are each defined **twice at top level**. Python silently keeps the *later* definition; the earlier one is either dead-shadowed or — worse — referenced before the shadow point, so two different concepts share one name. This is a latent correctness hazard.
2. **Mission-misfit "enterprise organs."** A deliberate governance framework (`SystemService` + `CapabilityContract` + `ActivationContract` + per-service kill switches + governance test suites) wraps ~85 services. A subset of these are generic SaaS/web-platform boilerplate (multi-tenancy, i18n, GDPR consent, A/B testing, blue-green/canary deploy, digital signatures) that do not serve a **single-user, sovereign AGI OS**. They are **wired (registered) but mission-irrelevant** — architectural drag, not dead orphans.

This spec eradicates both, establishing an unambiguous, sleek namespace **before** any reanimation work (Phase C) begins.

### Non-Goals (this spec)
- No new capability. No event-bus/activation-bridge work (that is Phase C).
- No reanimation of resilience classes (`SelfHealingOrchestrator`, etc.) — Phase C.
- No removal of mission-relevant governed organs (`MLOpsModelRegistry`, `WorkflowOrchestrator`, `DocumentManagementSystem`, `NotificationHub`, `SessionManager`, `DataLakeManager`, `StreamingAnalyticsEngine`, `LegacyDegradationManager`). Their fit is a *future* judgment, deliberately deferred.
- No file-splitting / module extraction (potential future spec).

---

## 2. Guiding Constraints (from CLAUDE.md + user mandate)

- **Root-cause, no shortcuts.** Renames fix the actual shadowing; un-wiring removes the actual registration — not band-aids.
- **No hardcoding.** No magic constants introduced. (This spec is subtractive; it removes, it does not add tunables.)
- **Leverage existing architecture.** Reuse the existing governance/registry patterns; don't invent parallel ones.
- **Git history is the archive.** Purged code is preserved in history, not commented-out or dead-stored.
- **Every slice independently green.** TDD; prove green before commit; each slice is its own reviewable change.
- **Behavior-preserving by construction.** Phases A & B must not change runtime behavior of anything that survives. Purged organs are already default-off (`event_driven` / kill-switch-gated), so removing them is observationally inert at default config.

---

## 3. Phase A — Shadow Resolution

Six classes are defined twice. Verified facts (line numbers at time of writing):

| Name | Def #1 | Def #2 (wins) | Relationship | Resolution |
|---|---|---|---|---|
| `ZombieProcessInfo` | 22033 | 59656 | Near-identical dataclass; #1 used only at 22239 (inside `ComprehensiveZombieCleanup` #1) | **Delete #1** (falls away with `ComprehensiveZombieCleanup` #1) |
| `ComprehensiveZombieCleanup` | 22051–22340 | 59674–60211 | #2 (v183) fully supersedes #1 (v109.7); all live calls (66136, 76702, 99143) are after #2 | **Delete #1** (dead-shadowed, ~290 lines) |
| `ResourceQuotaManager` | 29612 (OS-level: ulimit/fd/mem monitor) | 47601 (multi-tenant `SystemService`, **wired @67648**) | Genuinely different concepts | **Phase A: rename #1 → `OSResourceQuotaMonitor`** (resolves the shadow). **Phase B: purge #2** (multi-tenant boilerplate, see §4 B2) |
| `Notification` | 37535 (mutable dispatch record) | 55566 (immutable `NamedTuple`) | Different concepts; #1 used @37650 | **Rename #1 → `NotificationDispatchRecord`** + fix ref @37650 |
| `StreamEvent` | 40839 (mutable processing event) | 56937 (`NamedTuple`) | Different concepts; #1 used @40970 | **Rename #1 → `StreamProcessingEvent`** + fix ref @40970 |
| `HealthCheckResult` | 38758 (single-check result) | 58123 (`NamedTuple`, aggregated) | Different concepts; #1 used @38930/38952/38965 | **Rename #1 → `ServiceHealthCheckResult`** + fix 3 refs |

### Important correction (caught during verification)
The **wired** `ResourceQuotaManager` is the *multi-tenant* one (#2 @47601, registered @67648). The dead-shadowed one is the *OS-level monitor* (#1 @29612). Therefore in **Phase A** we **rename and keep the OS-level monitor** (it is genuinely useful and may feed Phase C). The live multi-tenant variant (#2) is itself a **Phase B purge target** (§4 B2) — it is the same multi-tenancy boilerplate theme as `TenantManager` and is misfit for a single-user OS. *(Added beyond the user's literal purge list as a consistency call; flagged for explicit confirmation at review.)*

### Resolution rules
- **Delete only when truly dead-shadowed** (no reference between def #1 and def #2): `ComprehensiveZombieCleanup` #1, and consequently `ZombieProcessInfo` #1.
- **Rename the earlier definition when it is referenced before the shadow point** (renaming is correct; merging is wrong — they are different concepts). Update only the references that fall **between** the two definitions (so the later, winning class keeps its name and all *its* references unchanged).
- Renamed earlier classes stay where they are (no relocation in this spec).

### Regression guard (new test)
Add `tests/unit/core/test_no_shadowed_definitions.py`: parse `unified_supervisor.py` with `ast`, collect all top-level `ClassDef` names, assert **no name appears more than once**. This permanently forecloses the entire bug class.

---

## 4. Phase B — The Purge

Eradicate mission-misfit enterprise organs and dead deprecated classes. These are **wired**, so each removal is a **safe un-wiring** (registration site → class def → header doc → test contract), in that order.

### B1 — `_Deprecated_*` classes (zero references — clean delete)
| Class | Lines | Refs (besides def) |
|---|---|---|
| `_Deprecated_IntelligentCacheManager` | 22357–22727 | 0 |
| `_Deprecated_SpotInstanceResilienceHandler` | 22996–23350 | 0 |
| `_Deprecated_EventSourcingManager` | 46193–46426 | 0 |

Delete class bodies outright. Nothing else references them.

### B2 — Misfit enterprise organs (un-wire, then delete)
Approved purge list. Each requires removing **(a)** its registration, **(b)** its class def, **(c)** any header-comment doc line, **(d)** its governance-test cases.

| Class | Class def | Registration site(s) | Header doc | Notes |
|---|---|---|---|---|
| `TenantManager` | 47923–48210 | `service=TenantManager(config=_config)` @67854 | 47570 | Multi-tenancy — N/A to single-user OS |
| `ABTestingFramework` | 50192–50488 | `service=ABTestingFramework(config=_config)` @67908 | 49624 | **Live impl already exists** at `backend/vision/ab_testing_framework.py` (used by `smart_query_router.py` + model mgmt). Supervisor copy is the redundant duplicate. |
| `LocalizationManager` | 52096–52374 | `service=LocalizationManager(config=_config)` @67899 | 51783 | i18n/l10n — N/A |
| `BlueGreenDeployer` | 39475–39667 | `service=BlueGreenDeployer()` @67762 | 38334 | Web-service deploy pattern — N/A |
| `CanaryReleaseManager` | 39692–39959 | `service=CanaryReleaseManager()` @67771 | 38335 | Web-service deploy pattern — N/A |
| `ConsentManagementSystem` | 57328–57714 | `_organ_specs` entry @89243 (`JARVIS_CONSENT_ENABLED`) | — | GDPR consent — N/A |
| `DigitalSignatureService` | 57756–58111 | `_organ_specs` entry @89244 (`JARVIS_SIGNATURES_ENABLED`) | — | Generic signing — N/A (sovereignty signing already handled by `sovereign_keys`) |
| `ResourceQuotaManager` (multi-tenant #2) | 47601–47900 | `service=ResourceQuotaManager(config=_config)` @67648 | 47570-region | Multi-tenant app quota — N/A. **Removed in B2, not Phase A.** Phase A only renames the OS-level #1. |

> **Line numbers are pre-edit anchors.** Because deletions/renames shift line numbers, the implementation plan executes Phase B **bottom-of-file → top-of-file**, and re-greps each anchor by symbol name immediately before editing. Never trust a stale line number across edits.

### B3 — Associated tests (surgical, not wholesale)
- `tests/unit/supervisor/test_higher_functions_protocol.py` — references `TenantManager`, `ABTestingFramework`, `BlueGreenDeployer`, `CanaryReleaseManager`, `LocalizationManager`. Remove only the cases bound to purged classes; keep the rest.
- `tests/unit/backend/test_enterprise_organ_governance.py` — references `ConsentManagementSystem`, `DigitalSignatureService` **and** `MLOpsModelRegistry` (kept). Remove only the purged-organ test classes; **`MLOpsModelRegistry` governance tests stay**.
- Grep both files post-edit to confirm no dangling import of a purged symbol.

### B4 — Kill-switch hygiene
The env flags for purged organs (`JARVIS_CONSENT_ENABLED`, `JARVIS_SIGNATURES_ENABLED`, and any `JARVIS_*` exclusive to purged organs) become orphaned. Remove their `_organ_specs` entries (B2) and scrub any `FlagRegistry` seed entries / `.env.example` references so the flag registry stays truthful (per "absolute observability"). Do **not** touch flags shared with surviving organs.

---

## 5. Slice Plan (each independently shippable + green)

| Slice | Content | Verification |
|---|---|---|
| **A.1** | Add `test_no_shadowed_definitions.py` (RED — currently 6 dupes), then delete `ComprehensiveZombieCleanup` #1 + `ZombieProcessInfo` #1 | Guard test goes from 6→4 violations; zombie-cleanup tests still green |
| **A.2** | Rename the 4 concept-collision earlier defs (`OSResourceQuotaMonitor`, `NotificationDispatchRecord`, `StreamProcessingEvent`, `ServiceHealthCheckResult`) + fix the 5 in-range refs | Guard test → 0 violations; import-sanity; affected unit tests green |
| **B.1** | Delete 3 `_Deprecated_*` classes | Import-sanity; full module import; no ref breakage |
| **B.2** | Un-wire + delete the 8 misfit organs (bottom→top), scrub registrations + header docs | Module imports; kernel init smoke test; service registry has expected count |
| **B.3** | Surgically prune governance tests + scrub orphaned kill-switch flags | `test_higher_functions_protocol.py` + `test_enterprise_organ_governance.py` green (purged cases gone, kept cases pass) |

Net expected reduction: ~2,500–3,000 lines removed/clarified from the monolith with **zero behavior change at default config**.

---

## 6. Testing & Verification Strategy

- **TDD discipline.** A.1 starts with a failing guard test. Each slice proves green before commit.
- **Behavior-preservation proof.** Module-import sanity (`python3 -c "import unified_supervisor"`) after every slice. Kernel construction smoke test (instantiate `JarvisSystemKernel` config + service registry build) to confirm the registration blocks still execute with the purged entries removed.
- **Blast-radius re-check before each edit.** Re-grep every symbol by name immediately before deleting/renaming (line numbers are stale-by-design across edits).
- **No new flags, no new constants.** This is a subtractive spec; reviewers should see only deletions, renames, and ref fixes.
- **Permanent regression fence.** The shadow-definition guard test stays in the suite forever.

---

## 7. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| A purged organ is referenced by code *outside* the audited set | Pre-edit repo-wide grep per symbol (already run for tests; re-run for `backend/`, `extensions/`, `scripts/` before B.2) |
| Renaming the wrong (winning) definition | Phase A renames only the **earlier** def and only fixes refs **between** the two def points — verified by line ranges in §3 |
| Line-number drift mid-edit corrupts a delete | Bottom→top ordering + symbol re-grep before each edit |
| Governance test file shared with kept organs | Surgical case removal only; `MLOpsModelRegistry` tests explicitly retained (§4 B3) |
| Orphaned kill-switch flags mislead observability | Flag scrub in B.3 |

---

## 8. Definition of Done

- `test_no_shadowed_definitions.py` exists and is green (0 duplicate top-level class names).
- All 8 misfit organs + 3 `_Deprecated_*` classes removed; both registration sites updated; header docs scrubbed.
- Governance tests green with purged cases removed and kept-organ cases passing.
- `import unified_supervisor` clean; kernel construction smoke test passes.
- No orphaned `JARVIS_*` flags for purged organs remain in `_organ_specs` / `FlagRegistry` seed / `.env.example`.
- Net line reduction; zero behavior change at default config.
- Phase C (`Cybernetic Reanimation`) spec authored separately, unblocked by the now-clean namespace.
