---
title: Gap #4 — IDE-native ReviewBranch closure (2026-05-04)
modules: [backend/core/ouroboros/battle_test/diff_archive.py, backend/core/ouroboros/governance/review_branch_manager.py, backend/core/ouroboros/governance/review_coordinator.py, backend/core/ouroboros/governance/ide_observability_stream.py, backend/core/ouroboros/battle_test/serpent_flow.py]
status: historical
source: project_gap_4_review_branch.md
---

# Gap #4 — IDE-native ReviewBranch closure (2026-05-04)

6-slice arc closing the "transient diff preview" gap from the
SerpentFlow / LiveDashboard UX audit, but reframed mid-arc when the
operator pointed out that REPL `/expand` is not a code-review surface
— what's needed is **IDE-native diff parity** (Claude Code / Cursor
pattern). The revised arc replaces the 5s Rich overlay → auto-apply
path with non-destructive git preview branches that VS Code's source
control panel surfaces natively, plus a DiffArchive lifecycle audit
trail and operator decision REPL verbs.

## Slices

* **Slice 1** — `diff_archive.py` (~530 LOC): thread-safe FIFO ring
  with monotonic ``d-N`` refs (NEVER reused, mirrors Gap #2 Slice 3
  ``BoundedBodyStore``); frozen ``ArchivedDiff`` dataclass with full
  lifecycle metadata; closed 5-value ``DiffOutcome`` (``PENDING`` /
  ``APPLIED`` / ``REJECTED`` / ``SUPERSEDED`` / ``FAILED``) +
  closed 4-value ``VerifyOutcome``; mutating API
  (``add`` / ``mark_applied`` / ``mark_verified`` /
  ``attach_review_branch``) with **terminal-frozen** semantics
  (mark_* on a terminal entry is a no-op); rich query API
  (``find_by_op_id`` / ``find_by_file`` / ``find_by_outcome``).
  62 tests.

* **Slice 2** — `review_branch_manager.py` (~750 LOC): the
  load-bearing **non-destructive git plumbing**. Uses
  ``hash-object`` + ``read-tree`` (with temp ``GIT_INDEX_FILE``) +
  ``update-index`` + ``write-tree`` + ``commit-tree`` + ``branch``
  to create ``ouroboros/preview/{op-id}`` branches **without
  touching the working tree, HEAD, or operator's index**. Closed
  5-value ``ReviewState`` enum; closed ``CreateOutcome`` /
  ``AcceptOutcome`` / ``RejectOutcome`` taxonomies. ``accept()``
  uses ``merge --ff-only`` (refuses on dirty tree or non-fast-forward —
  SUPERSEDED state for mid-review races). 39 tests including real
  git plumbing against temp repos.

* **Slice 3** — `review_coordinator.py` (~700 LOC) + minimal
  orchestrator hook (~40 LOC): the integration façade that joins
  ``DiffArchive`` + ``ReviewBranchManager`` + per-op
  ``asyncio.Event`` rendezvous + cancel-check polling into a single
  ``coordinate_review()`` entry point. Master flag
  ``JARVIS_REVIEW_BRANCH_ENABLED`` (default false during slice).
  Default timeout 300s → auto-EXPIRE → auto-REJECT (operator must
  explicitly accept). ``=0`` opts out entirely (legacy auto-apply).
  Closed 5-value ``ReviewDecision`` enum with
  ``implies_apply`` predicate. Orchestrator hook at the
  ``notify_apply_diff`` site (line 6685) routes through coordinator
  when flag enabled; legacy 5s overlay path preserved verbatim
  below the master-flag guard for byte-identical rollback. 41 tests.

* **Slice 4** — SSE events + REPL verbs (~140 LOC across
  ``ide_observability_stream.py`` + ``serpent_flow.py``):
  4 new event types (``review_branch_created`` / ``accepted`` /
  ``rejected`` / ``expired``) following the existing
  ``plan_pending`` / ``approved`` / ``rejected`` / ``expired``
  4-event vocabulary pattern. ``publish_review_branch_event``
  helper. ReviewCoordinator's ``_publish_state_event`` hook fires
  on every lifecycle transition. Three new REPL verbs:
  ``/accept <op-id>``, ``/reject <op-id>``, ``/review`` (with
  optional op-id substring filter). 10 tests.

* **Slice 5 — DEFERRED**. The Python side is complete: branches
  surface in VS Code's native source control panel automatically
  (no extension code required), SSE events fire for any subscriber,
  REPL verbs work. The custom ``jarvis.openPendingReview``
  command + webview is polish on top — operators have a complete
  IDE-native review UX without it. Pickup work: subscribe to the
  4 new SSE event types in ``extensions/vscode-jarvis/`` and add
  a notification + ``vscode.diff(headUri, branchUri)`` command.

* **Slice 6** — Graduation: master flag
  ``JARVIS_REVIEW_BRANCH_ENABLED`` flipped default-TRUE; module-owned
  ``register_flags(registry) -> 4`` (auto-discovered via the
  governance entry in ``_FLAG_PROVIDER_PACKAGES``); module-owned
  ``register_shipped_invariants() -> 4 ShippedCodeInvariant``
  pinning (1) ``ReviewState`` 5-value taxonomy frozen,
  (2) ``DiffOutcome`` 5-value + ``VerifyOutcome`` 4-value taxonomies
  frozen, (3) **bug-fix regression pin** on the orchestrator hook
  presence + ``coordinate_review`` call site, (4) ``SerpentREPL``
  exposes ``_handle_accept`` / ``_handle_reject`` / ``_handle_review``
  AND dispatches ``/accept`` + ``/reject`` lines to them. 15 graduation
  tests including end-to-end production-seed-boot resolution +
  AST-pin synthetic-positive coverage.

## Numbers

* **167 / 167 green** across the 5 active slices on first integrated run
* **0 regressions** in adjacent code (orchestrator imports cleanly;
  the 4 pre-existing battle_test failures from Gap #2 are unchanged)
* ~2,160 LOC substrate + ~1,400 LOC tests
* 4 FlagSpec seeds; 4 ShippedCodeInvariant pins; 1 memory file

## Architectural properties

* **Non-destructive git plumbing** — review branches created without
  touching the working tree, HEAD, or operator's index. Operator's
  editor state never flickers.
* **No silent ref reuse** — `BoundedBodyStore` (Gap #2) +
  `DiffArchive` (this arc) both guarantee monotonic counters that
  never reset, even after eviction. A printed ref always resolves
  to the same artifact or `None`, never a different one.
* **Terminal-frozen lifecycle** — once a `DiffOutcome` /
  `VerifyOutcome` reaches a terminal value, subsequent calls with
  a different outcome are silently ignored. Audit-trail integrity.
* **Layered non-destructive contract** — `ReviewBranchManager` checks
  preconditions (clean tree, attached HEAD) at create AND at accept;
  refuses to operate on dirty state. Mid-review races (operator
  committed something) → SUPERSEDED state, never silent corruption.
* **Auto-reject on timeout (safer default)** — 300s default window;
  if operator doesn't engage, the change is REJECTED, not silently
  applied. Operators who want the legacy auto-apply UX set
  `JARVIS_REVIEW_TIMEOUT_S=0`.
* **VS Code-native diff** — the branch existing in git IS the IDE
  integration. No custom extension code required for basic
  operation; VS Code's source control panel surfaces it automatically.

## Why-nots (deliberately deferred)

* **Slice 5 — VS Code custom command + webview**: the polished
  operator UX (notification → click → side-by-side diff with
  Accept/Reject buttons → POST to backend). Deferred because
  the Python side is fully functional without it; operators
  already have IDE-native review via VS Code's source control
  panel + REPL accept/reject. Pickup is ~150 lines of
  TypeScript in `extensions/vscode-jarvis/`.
* **HTTP POST `/review/<op-id>/accept|reject` endpoints**: needed
  by Slice 5's webview button; not needed when operator drives
  via REPL. Same scope discipline as above.
* **Per-hunk accept/reject**: VS Code's native diff already
  supports this via gutter actions; we don't need to reimplement
  Cursor's hunk picker.
* **3-way merge UI**: not in scope. Fast-forward only; conflict
  → SUPERSEDED state surfaces it, operator manually resolves.
* **Persistent disk archive**: in-memory only. The orchestrator's
  existing `dump_full_diff` (env-gated) still works for retrospective
  on-disk evidence.

## Reused architectural assets

| Existing asset | Reuse |
|---|---|
| `BoundedBodyStore` (Gap #2 Slice 3) | Monotonic-ref ring pattern (DiffArchive parallels it) |
| `OrangePRReviewer` | Branch-naming convention + commit message structure (preview branches mirror review branches but local-only) |
| `WorktreeManager` | Subprocess shim pattern (`asyncio.to_thread` + capture_output + structured returncode) |
| `IDEStreamRouter` event vocabulary | New 4-event group follows the `plan_pending/approved/rejected/expired` template |
| `flag_registry_seed` discovery | Module-owned `register_flags()` auto-discovered from `_FLAG_PROVIDER_PACKAGES` |
| `shipped_code_invariants` discovery | Module-owned `register_shipped_invariants()` auto-discovered |
| `SerpentREPL` `_handle_*` pattern | New verbs follow the existing `_handle_risk` / `_handle_budget` / `_handle_cancel` shape |
