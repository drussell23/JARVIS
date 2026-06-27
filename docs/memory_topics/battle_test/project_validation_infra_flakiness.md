---
title: Project Validation Infra Flakiness
modules: [docs/architecture/OUROBOROS.md]
status: historical
source: project_validation_infra_flakiness.md
---

**Status (2026-04-15):** Logged as a backlog follow-up, NOT in scope for the Session U→V commit arc. Session V will work around this via `JARVIS_MAX_VALIDATE_RETRIES=0` (commit `58709f27de`, the env-factory fix). The underlying flakiness needs a separate pass.

**The finding (Session U, op-019d9328):**

Same 4-file candidate, same test target set, two validation passes:

- `iter=0` (15:06:17 → 15:13:40, 7m23s): `failure_class='test'` — LSP type error on `test_test_failure_sensor_dedup.py`. The real model defect.
- `iter=1` (15:13:40 → 15:20:21, 6m41s): `failure_class='infra'` — sandbox/pytest/import transient. Triggered the non-retryable `_early_return_ctx` branch at `orchestrator.py:3642` and advanced ctx to POSTMORTEM with `terminal_reason_code='validation_infra_failure'`. The op died.

**Why this is a bug:**

`'infra'` is designed as a **terminal failure class** — "the sandbox itself is broken, don't retry, escalate." The design assumption is that `'infra'` is durable (if your sandbox can't even set up, retrying won't help). Session U shows the assumption is wrong: `'infra'` can be a **transient** that only fires on one of N re-runs against an unchanged candidate. A single flake condemns a legitimate op.

**Consequences:**

1. **Retries become harmful**, not helpful. The retry loop was designed to catch flaky tests. Instead it rolls dice on whether iter=N returns `'test'` (retryable, routes to L2) or `'infra'` (non-retryable, kills the op). With the default `max_validate_retries=2`, a 4-file op has a non-trivial probability of dying on flake before L2 ever dispatches.
2. **L2 Repair never gets a chance** when iter=0's real critique is masked by iter=1's infra flake. This is why no multi-file op has reached APPLY mode=multi in production across Sessions Q/R/S/T/U.

**The Session V workaround (already shipped in commit `58709f27de`):**

`JARVIS_MAX_VALIDATE_RETRIES=0` — the loop runs exactly once, takes iter=0's genuine `'test'` critique, decrements `validate_retries_remaining` from 0 to -1, and dispatches L2 immediately. The non-deterministic iter=1 never happens.

**What the proper fix looks like (future sprint, NOT in Session V scope):**

Three candidate directions, each falsifiable:

1. **Confirmation-required infra classification**: require `'infra'` to fire on ≥2 consecutive runs before triggering the non-retryable branch. A single-run `'infra'` should demote to `'test'` or `'transient'` and re-run. This matches the semantics of every other "transient until proven durable" signal in the organism (health probes, flake-detection reruns, etc.).

2. **Sandbox isolation hardening**: audit the re-validation code path to understand why the sandbox produces different import/setup states on back-to-back runs of the same candidate. Possible culprits: stale `__pycache__`, temp-dir collision, sys.path pollution from concurrent background ops, pytest-plugin state leak. The test_runner.py re-run path is the first place to look. Session U cost $0.3565 with the worker already up — this should be cheap to reproduce in isolation.

3. **Per-failure-class retry policy**: currently the retry loop treats all non-passing validations the same way until the class is checked for the terminal escalation. A stricter policy would say: `'test'` failure on iter=0 → mandatory L2 dispatch, skip further validation entirely, because the model cannot self-correct a deterministic type/syntax error via pytest re-runs.

**How to apply when investigating:**

1. Grep the session debug log for `[ValidateRetryFSM]` entries and look for `failure_class` drift across iter=0 → iter=1 on the same ctx chain. If drift is observed, this bug is still live.
2. Check whether the `'infra'` class is firing for a genuine sandbox outage or for a transient. Genuine outages show infra failures on CLASSIFY-phase ops too (not just VALIDATE_RETRY); transients only show up on re-validation.
3. If working on the proper fix, do NOT couple it to the `max_validate_retries=0` workaround. The workaround is correct for Session V and probably the next several sessions, but the real fix must survive with retries turned back on (default=2).

**Cross-references:**
- Full Session U postmortem: `docs/architecture/OUROBOROS.md` → Sessions Q–S arc + Follow-up A → Session U subsection (to be written)
- FSM instrumentation that made this finding possible: commit `d6aa78c8ba`
- The Session V workaround commit: `58709f27de` (env-factory for `max_validate_retries`)
