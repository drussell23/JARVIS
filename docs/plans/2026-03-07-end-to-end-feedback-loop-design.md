# End-to-End Feedback Loop Completion — Design

## Problem

The GCP-first routing and outcome detection are implemented, but four gaps prevent live end-to-end operation:

1. `GoogleWorkspaceAgent` lacks `get_message_labels()` — outcome detection silently no-ops.
2. Training capture notifications are not wired into the runner outcome flow.
3. `test_disabled_flag_skips_entirely` fails due to incomplete test mock.
4. `ExperienceQueueProcessor` starts opportunistically from agent_runtime, not under supervisor lifecycle authority.

Additional hardening requirements from review:
- Outcome idempotency keys to prevent duplicates from overlapping polling windows.
- Notifications must be post-commit (after durable enqueue), not pre-commit.
- Import-path discipline (`from core.*` in email triage, never `from backend.core.*`).

## Design

### Fix 1: `get_message_labels()` on GoogleWorkspaceAgent

Add `_get_message_labels_sync(message_id) -> Set[str]` as a peer to `_fetch_unread_sync`. Route through `_execute_with_retry` (same circuit breaker + auth refresh + thread executor pattern). Uses `messages().get(format='minimal')` which returns only `id`, `threadId`, `labelIds` — minimal payload.

Public async method: `get_message_labels(message_id) -> Set[str]`.

### Fix 2: Outcome idempotency keys

In `_enqueue_to_reactor_core`, compute `content_hash = hashlib.md5(f"{msg_id}:{outcome}:{label_hash}".encode()).hexdigest()` and pass as `metadata={"content_hash": hash}` to `enqueue_experience()`. The ExperienceDataQueue already deduplicates on `content_hash`.

### Fix 3: Durable training notification in runner

In `runner.py`, after `check_outcomes_for_cycle` returns captured outcomes, call `build_training_capture_message(captured)`. Dispatch via notifier at LOW urgency. The notification fires after `record_outcome` → `_enqueue_to_reactor_core` has already succeeded (the capture list only contains successfully recorded outcomes).

### Fix 4: Test mock fix

Add `_triage_disabled_logged`, `_triage_pressure_skip_count`, `_experience_processor_started`, `_experience_processor` to the `object.__new__()` test setup in `test_agent_runtime_integration.py`.

### Fix 5: Supervisor-managed processor lifecycle

Add `_start_experience_queue_processor()` in `unified_supervisor.py` during startup, registered via `_background_tasks.append()`. The agent_runtime idempotent guard (`_experience_processor_started`) prevents double-start.

## Execution Order

1. `get_message_labels()` — enables downstream flow
2. Outcome idempotency — safety before volume
3. Training notification wiring — depends on 1+2
4. Test mock fix — independent
5. Supervisor processor lifecycle — independent

## Out of Scope

- Per-brain canary/shadow rollout (reactor-core repo)
- Per-brain eval gates (reactor-core repo)
- Queue backpressure limits (existing 100MB cap sufficient)
- Import-path refactor (follow existing convention, no new risk)
