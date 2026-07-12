# Slice 7 — Subset Coverage Semantics for Resolved-Attribution Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the MultiFileCoverageGate accept a partial-coverage candidate (e.g. the correct source-only repair) when the op's scope came from a *resolved* Slice-6 test→source attribution — killing the Run #17 blocker (`multi_file_coverage_insufficient covers 1/2` → `only_noop_read_only_completions_no_mutation`) — plus the Slice-6 final-review fast-follow: a lock-free freshness probe in `prewarm_module_map`.

**Architecture:** A resolved-attribution TestFailure scope is **permissive** (either locus may be the fix target — that is the whole point of Slice 6), not an exhaustive change-set. The gate (`multi_file_coverage_gate.check_candidate`) grows an optional `intake_evidence_json` keyword; when the evidence carries `attribution.status == "resolved"` (only the Slice-6 bridge stamps that), covering **≥1** target file suffices. Zero-target coverage is still rejected; ⊆ containment stays enforced by the pre-existing `file_scope_mismatch` guard (`doubleword_provider.py:2507`); plain multi-file change-set ops (refactors etc., no resolved attribution) keep the strict superset demand byte-identically. The evidence is threaded from `ctx.intake_evidence_json` at **BOTH** gate call sites — inline `orchestrator.py` and extracted `phase_runners/generate_runner.py` (the Slice-6 T5 wired-but-inert lesson) — and an AST wiring pin makes deleting either wire a red test.

**Tech Stack:** Python 3.9+ stdlib only (`json`, `ast`, `os`, `threading`). No new dependencies. `from __future__ import annotations` everywhere. All new behavior env-gated with default-true master `JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED`, fail-CLOSED to the pre-Slice-7 strict behavior on any fault.

## Global Constraints

- Python 3.9+ — no `asyncio.timeout`; `from __future__ import annotations` required in all files.
- No hardcoded model names; every tunable reads an env var with a sensible default.
- New flag `JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED` — BOOL, default `true`.
- Fail-CLOSED discipline: any evidence-parse/import fault in the gate ⇒ strict superset demand (pre-Slice-7 behavior), never a waiver.
- The subset waiver must land on BOTH GATE call paths (inline orchestrator + extracted generate_runner) — Slice-6 T5 lesson; Task 4's AST pin enforces this structurally.
- Commit style: conventional commits, `git add` only named files (never `-A`/`.`), footer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Run tests per-file with `python3 -m pytest <file> -v` from the repo root `/Users/djrussell23/Documents/repos/JARVIS-AI-Agent`.

---

### Task 1: `attribution_status` evidence helper (single evidence parser)

**Files:**
- Modify: `backend/core/ouroboros/governance/intent/test_source_attribution.py` (imports at ~line 26; predicate section after `scope_gate_enabled`, ~line 344)
- Test: `tests/governance/intent/test_source_attribution.py` (append — this file already unit-tests the module and imports it as `tsa`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `attribution_status(intake_evidence_json: str) -> str` — pure, fail-soft (`""` on absent/malformed), returns e.g. `"resolved"`/`"unresolved"`/`"disabled"`/`""`. Task 2 imports this. Also private `_attribution_dict(intake_evidence_json: str) -> Dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

Append to the test file:

```python
class TestAttributionStatus:
    """Slice 7 — single evidence parser consumed by the coverage gate."""

    def test_resolved(self):
        j = json.dumps({"attribution": {"status": "resolved"}})
        assert attribution_status(j) == "resolved"

    def test_unresolved(self):
        j = json.dumps({"attribution": {"status": "unresolved"}})
        assert attribution_status(j) == "unresolved"

    def test_absent_attribution_block(self):
        assert attribution_status(json.dumps({"other": 1})) == ""

    def test_empty_and_none_ish(self):
        assert attribution_status("") == ""
        assert attribution_status("{}") == ""

    def test_malformed_json(self):
        assert attribution_status("{not json") == ""

    def test_non_dict_evidence(self):
        assert attribution_status("[1, 2]") == ""

    def test_non_dict_attribution_value(self):
        assert attribution_status(json.dumps({"attribution": "resolved"})) == ""
```

Ensure the test file imports `json` and adds `attribution_status` to its existing `from backend.core.ouroboros.governance.intent.test_source_attribution import (...)` block.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/intent/test_source_attribution.py -v -k TestAttributionStatus`
Expected: FAIL / collection error with `ImportError: cannot import name 'attribution_status'`

- [ ] **Step 3: Implement the helper and refactor the existing predicate onto it**

In `test_source_attribution.py`:

(a) Extend the typing import (line 26):

```python
from typing import Any, Dict, Optional, Sequence, Set, Tuple
```

(b) Insert immediately after `scope_gate_enabled()` (before `unattributed_test_scope_violation`):

```python
def _attribution_dict(intake_evidence_json: str) -> Dict[str, Any]:
    """Fail-soft parse of the Slice-6 evidence block: returns the
    ``attribution`` dict from an op's intake evidence JSON, or ``{}`` on
    absent / non-JSON / non-dict shapes. Never raises."""
    try:
        evidence = json.loads(intake_evidence_json or "{}")
        attribution = evidence.get("attribution") or {}
    except (ValueError, TypeError, AttributeError):
        return {}
    return attribution if isinstance(attribution, dict) else {}


def attribution_status(intake_evidence_json: str) -> str:
    """``attribution.status`` from an op's intake evidence JSON, ``""``
    when absent or malformed. The single evidence parser shared by the
    scope gate (Slice 6) and the coverage-gate subset waiver (Slice 7)."""
    return str(_attribution_dict(intake_evidence_json).get("status", ""))
```

(c) In `unattributed_test_scope_violation`, replace the parse block

```python
    try:
        evidence = json.loads(intake_evidence_json or "{}")
        attribution = evidence.get("attribution") or {}
        status = str(attribution.get("status", ""))
    except (ValueError, TypeError, AttributeError):
        return None
```

with:

```python
    attribution = _attribution_dict(intake_evidence_json)
    status = str(attribution.get("status", ""))
```

(The downstream `attribution.get("test_locus", "")` / `attribution.get("reason", "unknown")` reads are unchanged — `_attribution_dict` always returns a dict.)

- [ ] **Step 4: Run the module's full test files to verify green (helper + no regression in the refactored predicate)**

Run: `python3 -m pytest tests/governance/intent/ tests/governance/test_attribution_scope_gate.py -v`
Expected: PASS (all — including the pre-existing `unattributed_test_scope_violation` cases)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intent/test_source_attribution.py tests/governance/intent/test_source_attribution.py
git commit -m "feat(slice7): attribution_status evidence helper — single fail-soft parser for the Slice-6 evidence block

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Subset semantics in `multi_file_coverage_gate.check_candidate`

**Files:**
- Modify: `backend/core/ouroboros/governance/multi_file_coverage_gate.py` (env consts ~line 50; new helpers after `is_enabled`; `check_candidate` signature + reject block, lines 141–179)
- Test: `tests/test_ouroboros_governance/test_multi_file_coverage_gate.py` (append)

**Interfaces:**
- Consumes: `attribution_status(intake_evidence_json: str) -> str` from Task 1 (lazy import, fail-closed).
- Produces: `check_candidate(candidate, target_files, project_root=None, *, intake_evidence_json: str = "") -> Optional[Tuple[str, List[str]]]` — new keyword-only param, default `""` keeps every existing caller/test byte-identical. New `subset_coverage_enabled() -> bool` and private `_attribution_resolved(intake_evidence_json: str) -> bool`. Task 3 wires the new kwarg at both call sites.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ouroboros_governance/test_multi_file_coverage_gate.py` (also add `import json` to its imports and `subset_coverage_enabled` to the gate import block):

```python
# ---------------------------------------------------------------------------
# Slice 7 — subset semantics for resolved-attribution scope (Run #17)
# ---------------------------------------------------------------------------

_SOURCE = "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py"
_TEST = "tests/governance/a1_ignition_vector/test_leaf_predicates.py"
_RESOLVED = json.dumps({"attribution": {
    "status": "resolved", "test_locus": _TEST, "method": "direct_import",
}})
_UNRESOLVED = json.dumps({"attribution": {
    "status": "unresolved", "reason": "no_first_party_source_imports",
}})


class TestSubsetCoverageSemantics:
    """Run #17 blocker: attributed scope is PERMISSIVE (either locus may
    be the fix target), not an exhaustive change-set. Resolved
    attribution ⇒ covering >=1 target suffices; everything else keeps
    the strict superset demand byte-identically."""

    def test_resolved_source_only_candidate_passes(self):
        """THE Run #17 repro: correct source-only repair on a 2-file scope."""
        cand = {"file_path": _SOURCE, "full_content": "def clamp01(x):\n    return min(1.0, max(0.0, x))\n"}
        assert check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        ) is None

    def test_resolved_test_only_candidate_passes(self):
        """The test itself may legitimately be the fix target — the
        unresolved-only scope gate (Slice 6 T5) intentionally does not
        fire on resolved attribution, so the coverage gate must not
        re-block it either."""
        cand = {"file_path": _TEST, "full_content": "def test_clamp01():\n    assert True\n"}
        assert check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        ) is None

    def test_resolved_full_coverage_still_passes(self):
        cand = {"files": [
            {"file_path": _SOURCE, "full_content": "a = 1\n"},
            {"file_path": _TEST, "full_content": "b = 2\n"},
        ]}
        assert check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        ) is None

    def test_resolved_zero_target_coverage_still_rejected(self):
        """Subset never means 'anything goes' — a candidate covering NO
        target file is still rejected even with resolved attribution."""
        cand = {"file_path": "backend/somewhere/else.py", "full_content": "x = 1\n"}
        result = check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        )
        assert result is not None
        reason, missing = result
        assert reason.startswith(REASON_PREFIX)
        assert set(missing) == {_SOURCE, _TEST}

    def test_unresolved_attribution_keeps_superset_demand(self):
        cand = {"file_path": _SOURCE, "full_content": "x = 1\n"}
        result = check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_UNRESOLVED,
        )
        assert result is not None and result[0].startswith(REASON_PREFIX)

    def test_absent_evidence_keeps_superset_demand(self):
        """Default kwarg — every pre-Slice-7 caller is byte-identical."""
        cand = {"file_path": _SOURCE, "full_content": "x = 1\n"}
        assert check_candidate(cand, [_SOURCE, _TEST]) is not None

    def test_malformed_evidence_fail_closed_strict(self):
        cand = {"file_path": _SOURCE, "full_content": "x = 1\n"}
        result = check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json="{not json",
        )
        assert result is not None

    def test_subset_master_off_keeps_superset_demand(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED", "false")
        cand = {"file_path": _SOURCE, "full_content": "x = 1\n"}
        result = check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        )
        assert result is not None


class TestSubsetCoverageEnabled:
    def test_default_is_on(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED", raising=False)
        assert subset_coverage_enabled() is True

    @pytest.mark.parametrize("val", ["false", "FALSE", "0", "no", "off"])
    def test_explicit_off(self, monkeypatch, val):
        monkeypatch.setenv("JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED", val)
        assert subset_coverage_enabled() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_multi_file_coverage_gate.py -v -k "Subset"`
Expected: FAIL — `ImportError: cannot import name 'subset_coverage_enabled'`

- [ ] **Step 3: Implement the gate change**

In `multi_file_coverage_gate.py`:

(a) After `_ENV_MULTI_GEN = "JARVIS_MULTI_FILE_GEN_ENABLED"` (line 51) add:

```python
_ENV_SUBSET = "JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED"
```

(b) After `is_enabled()` (line 71) add:

```python
def subset_coverage_enabled() -> bool:
    """Slice 7 master switch (default ON): resolved-attribution
    TestFailure scope is judged with subset semantics — covering >=1
    target file suffices. OFF restores the pre-Slice-7 strict superset
    demand for every op."""
    raw = os.environ.get(_ENV_SUBSET, "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


def _attribution_resolved(intake_evidence_json: str) -> bool:
    """True iff the op's intake evidence carries ``attribution.status ==
    "resolved"`` — only the Slice-6 test→source attribution bridge stamps
    that status, so it is a sufficient discriminator for a permissive
    (either-locus-may-be-the-fix) TestFailure scope. Fail-CLOSED to
    ``False``: any import/parse fault keeps the strict superset demand,
    i.e. the pre-Slice-7 behavior."""
    if not intake_evidence_json:
        return False
    try:
        from backend.core.ouroboros.governance.intent.test_source_attribution import (  # noqa: E501
            attribution_status,
        )
        return attribution_status(intake_evidence_json) == "resolved"
    except Exception:  # noqa: BLE001 — waiver is a relaxation, never fatal
        return False
```

(c) Change the `check_candidate` signature (line 141):

```python
def check_candidate(
    candidate: Dict[str, Any],
    target_files: Sequence[str],
    project_root: Optional[Path] = None,
    *,
    intake_evidence_json: str = "",
) -> Optional[Tuple[str, List[str]]]:
```

(d) Between `if not missing: return None` (lines 164–165) and the `reason = (...)` build (line 167), insert:

```python
    covered_targets = len(normalized_targets) - len(missing)
    if (
        covered_targets >= 1
        and subset_coverage_enabled()
        and _attribution_resolved(intake_evidence_json)
    ):
        # Slice 7 (Run #17): a resolved-attribution TestFailure scope is
        # PERMISSIVE — target_files names the candidate fix loci (source
        # AND test), not an exhaustive change-set. Demanding full
        # coverage here rejected the correct source-only repair
        # ("covers 1/2") and killed the op. Covering >=1 target
        # suffices; ⊆ containment is file_scope_mismatch's job, and
        # true multi-file change-set goals (no resolved attribution)
        # keep the strict superset demand above.
        logger.info(
            "[MultiFileCoverageGate] subset-coverage waiver: resolved "
            "attribution — candidate covers %d/%d target file(s)",
            covered_targets,
            len(normalized_targets),
        )
        return None
```

(e) Update the module docstring Contract section: after the bullet ending "…rejected with every target listed as missing." add:

```
- Slice 7 exception: when ``intake_evidence_json`` carries
  ``attribution.status == "resolved"`` (Slice-6 test→source bridge) and
  ``JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED`` is not falsy, covering
  at least ONE target file passes — attributed scope is permissive
  (either locus may be the fix target), not an exhaustive change-set.
  Zero-coverage candidates are still rejected.
```

- [ ] **Step 4: Run the full gate test file to verify green (new + all pre-existing strict cases)**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_multi_file_coverage_gate.py -v`
Expected: PASS (29 pre-existing + 10 new)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/multi_file_coverage_gate.py tests/test_ouroboros_governance/test_multi_file_coverage_gate.py
git commit -m "feat(slice7): subset coverage semantics for resolved-attribution scope (Run #17 blocker)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Wire `intake_evidence_json` at BOTH gate call sites + AST wiring pin

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py:6644-6650` (the `_mf_check(...)` call)
- Modify: `backend/core/ouroboros/governance/phase_runners/generate_runner.py:1527-1533` (the `_mf_check(...)` call)
- Test: `tests/test_ouroboros_governance/test_multi_file_coverage_gate.py` (append wiring-pin class)

**Interfaces:**
- Consumes: `check_candidate(..., intake_evidence_json=...)` from Task 2; `ctx.intake_evidence_json` (already populated at op creation; `getattr` default `""` for legacy ops).
- Produces: both live GENERATE paths forward the evidence. Nothing downstream changes shape.

- [ ] **Step 1: Write the failing AST wiring pin**

Append to `tests/test_ouroboros_governance/test_multi_file_coverage_gate.py`:

```python
# ---------------------------------------------------------------------------
# Slice 7 — wiring pins (the T5 wired-but-inert lesson, structurally enforced)
# ---------------------------------------------------------------------------

import ast as _ast

_GOV = Path(__file__).resolve().parents[2] / "backend" / "core" / "ouroboros" / "governance"


def _mf_check_call_nodes(path: Path):
    tree = _ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, _ast.Name) else getattr(fn, "attr", "")
            if name == "_mf_check":
                out.append(node)
    return out


class TestBothGatePathsForwardEvidence:
    """Slice 6 shipped a Critical where the extracted runner (the
    shipping default) lacked the new gate. This pin makes 'one path
    wired, the other inert' a red test forever."""

    @pytest.mark.parametrize("rel", [
        "orchestrator.py",
        "phase_runners/generate_runner.py",
    ])
    def test_call_site_passes_intake_evidence(self, rel):
        calls = _mf_check_call_nodes(_GOV / rel)
        assert calls, f"no _mf_check(...) call found in {rel}"
        for call in calls:
            kwargs = {k.arg for k in call.keywords}
            assert "intake_evidence_json" in kwargs, (
                f"{rel}: _mf_check call does not forward "
                "intake_evidence_json — subset semantics inert on this path"
            )
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_multi_file_coverage_gate.py -v -k Forward`
Expected: FAIL on both parametrized cases (`intake_evidence_json` not in kwargs)

- [ ] **Step 3: Wire both call sites**

`orchestrator.py` — replace lines 6646–6650:

```python
                            _mf_result = _mf_check(
                                _cand,
                                ctx.target_files,
                                self._config.project_root,
                                intake_evidence_json=getattr(
                                    ctx, "intake_evidence_json", "",
                                ) or "",
                            )
```

`phase_runners/generate_runner.py` — replace lines 1529–1533:

```python
                        _mf_result = _mf_check(
                            _cand,
                            ctx.target_files,
                            orch._config.project_root,
                            intake_evidence_json=getattr(
                                ctx, "intake_evidence_json", "",
                            ) or "",
                        )
```

- [ ] **Step 4: Run the pin + import-sanity check**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_multi_file_coverage_gate.py -v && python3 -c "import backend.core.ouroboros.governance.orchestrator, backend.core.ouroboros.governance.phase_runners.generate_runner; print('imports ok')"`
Expected: PASS + `imports ok`

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py backend/core/ouroboros/governance/phase_runners/generate_runner.py tests/test_ouroboros_governance/test_multi_file_coverage_gate.py
git commit -m "feat(slice7): forward intake evidence to the coverage gate on BOTH GENERATE paths + AST wiring pin

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Lock-free freshness probe in `prewarm_module_map` (Slice-6 final-review fast-follow)

**Files:**
- Modify: `backend/core/ouroboros/governance/intent/test_source_attribution.py:156-160` (the probe at the top of `prewarm_module_map`)
- Test: `tests/governance/intent/test_source_attribution.py` (append; same file as Task 1 — it already has an autouse fixture clearing `tsa._MAP_CACHE` and uses `@pytest.mark.asyncio` for the existing prewarm tests)

**Interfaces:**
- Consumes: module globals `_MAP_CACHE`, `_MAP_CACHE_LOCK`, `_module_map_ttl_s()`.
- Produces: no API change — `prewarm_module_map(repo_root)` signature/behavior identical, but the warm-path probe no longer touches `_MAP_CACHE_LOCK`.

**Why:** `_build_and_cache_module_map` holds `_MAP_CACHE_LOCK` across the ~7s `build_module_to_path` rglob when running in an executor thread. `prewarm_module_map` runs ON the event loop, and its current probe is `with _MAP_CACHE_LOCK:` — so a concurrent in-flight build blocks the loop for up to the full crawl, reintroducing exactly the sync-FS-on-loop class C1 closed. CPython dict reads are atomic; a stale lock-free read is benign (worst case we offload and the single-flight builder inside the lock dedups the crawl).

- [ ] **Step 1: Write the failing test**

Append to the Task-1 test file (the file already imports the module as `tsa` and marks async tests `@pytest.mark.asyncio`; ensure `import time` is present):

```python
class _ForbiddenLock:
    """Trips the moment anything on the probe path touches the lock."""

    def __enter__(self):
        raise AssertionError("prewarm warm-path probe must not take _MAP_CACHE_LOCK")

    def __exit__(self, *args):
        return False

    def acquire(self, *args, **kwargs):
        raise AssertionError("prewarm warm-path probe must not take _MAP_CACHE_LOCK")

    def release(self):
        pass


@pytest.mark.asyncio
async def test_prewarm_warm_probe_never_touches_lock(monkeypatch):
    """Slice 7 fast-follow (Slice-6 final review): an in-flight executor
    build holds _MAP_CACHE_LOCK for the full ~7s crawl; probing under
    the lock would block the event loop for exactly that long."""
    root = "/definitely/fake/slice7-root"
    monkeypatch.setitem(
        tsa._MAP_CACHE, root, (time.monotonic(), {"m": "m.py"}),
    )
    monkeypatch.setattr(tsa, "_MAP_CACHE_LOCK", _ForbiddenLock())
    # Old code raises AssertionError from the `with _MAP_CACHE_LOCK:`
    # probe; fixed code returns without ever touching the lock.
    await tsa.prewarm_module_map(root)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/governance/intent/test_source_attribution.py -v -k prewarm_warm_probe`
Expected: FAIL with `AssertionError: prewarm warm-path probe must not take _MAP_CACHE_LOCK`

- [ ] **Step 3: Make the probe lock-free**

In `prewarm_module_map`, replace:

```python
    now = time.monotonic()
    with _MAP_CACHE_LOCK:
        hit = _MAP_CACHE.get(repo_root)
        if hit is not None and now - hit[0] < _module_map_ttl_s():
            return  # already warm — no crawl, on- or off-loop
```

with:

```python
    now = time.monotonic()
    # Lock-free probe (Slice 7 fast-follow): an in-flight executor build
    # holds _MAP_CACHE_LOCK for the full ~7s crawl, so probing under the
    # lock would block the event loop for exactly that long. CPython
    # dict reads are atomic; a stale read is benign — the offload lands
    # in the single-flight builder, which dedups inside the lock.
    hit = _MAP_CACHE.get(repo_root)
    if hit is not None and now - hit[0] < _module_map_ttl_s():
        return  # already warm — no crawl, on- or off-loop
```

Also update the docstring's fail-soft paragraph tail (optional, one line): note the freshness probe itself is lock-free.

- [ ] **Step 4: Run the attribution test files to verify green**

Run: `python3 -m pytest tests/governance/intent/ -v`
Expected: PASS (all, including the pre-existing prewarm/single-flight cases)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intent/test_source_attribution.py tests/governance/intent/test_source_attribution.py
git commit -m "fix(slice7): lock-free freshness probe in prewarm_module_map — in-flight executor build must not block the loop

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: End-to-end pin — real Run-#17 evidence through the real gate

**Files:**
- Modify: `tests/governance/intent/test_attribution_e2e_leaf_predicates.py` (append)

**Interfaces:**
- Consumes: `TestWatcher.process_failures` (real signal evidence), `check_candidate(..., intake_evidence_json=...)` from Task 2.
- Produces: the Run-#17 scenario pinned against the REAL repo — regression here means autonomous test-failure repair is structurally dead again at the coverage gate.

- [ ] **Step 1: Write the failing test**

Append (add `import json` and the gate import to the file's imports):

```python
def test_run17_source_only_candidate_passes_coverage_gate() -> None:
    """THE Run #17 blocker, pinned end-to-end: the REAL signal evidence
    (resolved attribution) through the REAL coverage gate must accept
    the correct source-only repair on the [source, test] scope."""
    from backend.core.ouroboros.governance.multi_file_coverage_gate import (
        REASON_PREFIX,
        check_candidate,
    )

    w = TestWatcher(repo="jarvis", repo_path=_REPO)
    f = TestFailure(
        test_id=f"{_TEST}::test_clamp01",
        file_path=_TEST,
        error_text="AssertionError: clamp01(2.0) != 1.0",
    )
    w.process_failures([f])
    signals = w.process_failures([f])
    assert len(signals) == 1
    evidence_json = json.dumps(signals[0].evidence)

    source_only = {"file_path": _SOURCE, "full_content": "x = 1\n"}
    assert check_candidate(
        source_only,
        list(signals[0].target_files),
        Path(_REPO),
        intake_evidence_json=evidence_json,
    ) is None

    # Strictness preserved: same candidate WITHOUT the evidence is
    # still rejected (plain multi-file change-set semantics).
    rejected = check_candidate(
        source_only, list(signals[0].target_files), Path(_REPO),
    )
    assert rejected is not None and rejected[0].startswith(REASON_PREFIX)
```

- [ ] **Step 2: Run to verify current state**

Run: `python3 -m pytest tests/governance/intent/test_attribution_e2e_leaf_predicates.py -v`
Expected: PASS if Tasks 2–3 are complete (this task pins integration, TDD-red was exercised at Task 2). If it FAILS, the gate or watcher contract drifted — stop and diagnose before committing.

- [ ] **Step 3: Commit**

```bash
git add tests/governance/intent/test_attribution_e2e_leaf_predicates.py
git commit -m "test(slice7): pin the Run #17 scenario end-to-end — real resolved evidence unlocks the source-only repair

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Docs — flag registry seed, CLAUDE.md, memory topic, ledger

**Files:**
- Modify: `backend/core/ouroboros/governance/flag_registry_seed.py` (append to the Slice-6 flag block at ~line 5299; update its `— 4 flags` header comment to `— 5 flags`)
- Modify: `CLAUDE.md` (the `Test→Source Attribution Bridge — Slice 6` bullet)
- Modify: `docs/memory_topics/intake/project_slice6_test_source_attribution.md` (append a Slice 7 section)
- Modify: `.superpowers/sdd/progress.md` (append Slice 7 entry)

**Interfaces:**
- Consumes: everything above (documentation only — no runtime changes).
- Produces: flag discoverable via `/help flags`; CLAUDE.md truth updated.

- [ ] **Step 1: Add the FlagSpec**

Append inside the Slice-6 block of `flag_registry_seed.py` (after the `JARVIS_ATTRIBUTION_MODULE_MAP_TTL_S` entry), mirroring the neighboring entries' shape exactly:

```python
    FlagSpec(
        name="JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 7: the MultiFileCoverageGate judges resolved-"
            "attribution TestFailure scope with SUBSET semantics — a "
            "candidate covering >=1 of the attributed target files "
            "(e.g. the source-only repair) passes instead of being "
            "rejected multi_file_coverage_insufficient. Zero-coverage "
            "candidates are still rejected; ops without resolved "
            "attribution keep the strict full-coverage demand. OFF "
            "restores pre-Slice-7 strictness for every op."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/multi_file_coverage_gate.py",
        example="true",
        since="2026-07-11",
        posture_relevance=_HARDEN_CRITICAL,
    ),
```

And change the block header comment `# Test->Source Attribution Bridge (Slice 6) — 4 flags` to `— 5 flags (Slice 7 added subset coverage)`.

- [ ] **Step 2: Update CLAUDE.md**

In the `**Test→Source Attribution Bridge — Slice 6**` bullet, after the sentence about the gate living on BOTH GATE paths, insert:

```
Slice 7: MultiFileCoverageGate applies SUBSET semantics to resolved-attribution scope (candidate covering ≥1 attributed target passes — the Run-17 `covers 1/2` blocker; zero-coverage still rejected, non-attributed multi-file ops keep the superset demand; evidence forwarded on BOTH GENERATE paths with an AST wiring pin). Master: `JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED` default-TRUE.
```

- [ ] **Step 3: Append the Slice 7 section to the memory topic + ledger entry**

Memory topic — append a short section: what Run #17 proved (attribution live, gate blocker), the subset-semantics design (permissive scope vs exhaustive change-set), fail-closed discipline, the lock-free prewarm probe, and the wiring-pin pattern. Ledger — one `Slice 7: complete (...)` block in `.superpowers/sdd/progress.md` naming commits + review outcomes.

- [ ] **Step 4: Sanity-run the flag registry tests**

Run: `python3 -m pytest tests/governance/test_flag_registry_seed_truth.py -q`
Expected: PASS (seed entries validate against the real source files)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/flag_registry_seed.py CLAUDE.md docs/memory_topics/intake/project_slice6_test_source_attribution.md .superpowers/sdd/progress.md
git commit -m "docs(slice7): subset coverage flag registry entry + CLAUDE.md + memory topic

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Whole-slice verification sweep

**Files:** none new — verification only.

- [ ] **Step 1: Run the full touched-surface test battery**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_multi_file_coverage_gate.py tests/governance/intent/ tests/governance/test_attribution_scope_gate.py -v
```

Expected: all PASS (known pre-existing collection errors elsewhere in the tree — merkle/phase11/l2-fixtures — are out of scope; do not chase them).

- [ ] **Step 2: Grep-audit the wired-but-inert invariant one more time (belt for the AST pin)**

```bash
grep -n "intake_evidence_json" backend/core/ouroboros/governance/orchestrator.py | grep -A0 -B0 "" | head -5
grep -c "intake_evidence_json" backend/core/ouroboros/governance/phase_runners/generate_runner.py
```

Expected: generate_runner count ≥ 1 (was 0 before this slice at the gate site — confirm the hit is the `_mf_check` call).

- [ ] **Step 3: Report**

Summarize: tests green (counts), both paths wired, flag registered. The slice is then ready for the whole-branch review + Run #18 re-fire (`--max-wall-seconds 2400`, headless) — Run #18 is conducted from the main session, not inside this plan.

---

## Verification for the arc (post-plan, main session)

Run #18 acceptance (not a plan task — operator-conducted): re-fire the isomorphic A1 driver; watch for `[Attribution] … (direct_import)` → sig-op scope `[source, test]` → **no** `multi_file_coverage_insufficient` → APPLY on the source → `pass_rate=1.0` → AutoCommit → `proven=true`.
