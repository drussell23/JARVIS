---
title: Project Flag Registry Api Drift
modules: [backend/core/ouroboros/governance/flag_registry.py]
status: historical
source: project_flag_registry_api_drift.md
---

Surfaced during v33 capability soak boot (2026-05-28, post-Slice-37
merge c2de840edc on main). At least 20+ call sites across O+V
governance modules invoke `FlagRegistry.register(name=..., ...)`
passing keyword args directly, but the canonical signature at
`backend/core/ouroboros/governance/flag_registry.py:267` is:

```python
def register(self, spec: FlagSpec, *, override: bool = True) -> None
```

The `name` kwarg doesn't exist — callers must construct
`FlagSpec(name=..., kind=..., default=..., ...)` first and pass it
positionally. Every miscall raises `TypeError: FlagRegistry.register()
got an unexpected keyword argument 'name'`.

**Why it's non-fatal**: Every caller wraps its `register_flags()`
with a `try: ... except Exception: logger.debug("seeding failed
(non-fatal)")` pattern. Boot continues past the seeding phase. The
FlagRegistry simply doesn't get the seed entry — flags still work
via env-var access, only `/help flags`/typo-detection lose coverage
for unseeded flags.

**Known affected files** (sample from v33 boot):
- `autonomy_command_bus_bridge.py` (3 sites: poll-interval / ledger-path / verbose)
- `component_tool_scope.py:492`
- `error_classifier.py:381`
- `execution_graph_progress_bridge.py` (3 sites: master / verbose / ledger-path)
- `execution_monitor_bridge.py` (2 sites: master / ledger-path)
- `operation_mode.py:292`
- `verification/multi_prior_dispatch.py:603`
- `verification/multi_prior_graduation_contract.py` (3 sites)
- `verification/multi_prior_observer.py` (3 sites)
- `verification/multi_prior_planning.py` (2 sites)
- `verification/multi_prior_runner.py:872`
- Plus more (P94AdversarialCorpus, etc.)

**Scope**: orthogonal to Slice 37 / Slice 36 / current arc. The
fix is mechanical: each `register(name=N, kind=K, default=D, ...)`
becomes `register(FlagSpec(name=N, kind=K, default=D, ...))`.

**Why not now**: Slice 37 is closing a capability-bar blocker; the
log spam is purely cosmetic. A dedicated micro-slice (sed-script
across the ~20 files, single AST-pin asserting all `register()` calls
take a FlagSpec arg) can land any time without competing for arc
attention.

Related: [[project_flag_registry_graduation]].
