---
title: Tab completion arbiter — one key, two completion sources
modules: [backend/core/ouroboros/battle_test/completion_arbiter.py, backend/core/ouroboros/battle_test/repl_completion.py, backend/core/ouroboros/cli/ov.py, backend/core/ouroboros/cli/ov_demo.py, backend/core/ouroboros/battle_test/keymap.py]
status: active
source: session 2026-07-31, operator screenshot
---

# Tab completion arbiter

**Report:** the `ov` prompt shows grey ghost text (`wha` → `what is O+V?`) and
Tab does nothing.

## Root cause: nobody arbitrated

`PromptSession` is built with BOTH a `completer` and an `auto_suggest`. Those
are two independent sources and prompt_toolkit gives them different keys: Tab
drives the COMPLETER, `→`/`c-e` accept the SUGGESTION. The verb completer only
fires on `/` and the mention completer on `@`, so on prose it correctly yields
nothing — and Tab, bound to the completer alone, correctly does nothing.

Every piece behaved as designed. The missing piece was an arbiter deciding
which source Tab means right now.

**Not** "bind Tab to accept the suggestion" — that trades one broken half for
the other and stops `/mem` → `/memory`. Tab became a dispatcher over a ladder:
menu open → navigate · completion territory → complete · suggestion at end →
accept · empty buffer → palette · else → indent.

## Why the ladder decides by CONTEXT

The obvious implementation (ask the completer, fall through if empty) is
impossible: the completer is THREADED, so asking is async by construction and
a key handler awaiting it blocks the keystroke it services. So rung 2 uses
`repl_completion.completion_would_trigger` — and BOTH completers were
refactored to consume that same predicate. Two copies of "is this completion
territory" would drift the first time a trigger char was added, and the
symptom would be this exact bug again. AST-pinned by test.

## Two bugs only LIVE FIRE caught

33 decision-level tests passed while both were live. A real `PromptSession`
over `create_pipe_input()` + `DummyOutput` found them in one run.

1. **The suggestion race.** `Buffer.suggestion` is populated by an ASYNC task.
   A human typing wins that race; a paste, a fast typist, or a busy loop does
   not — Tab arrives with `suggestion=None`, indents, and the inserted
   whitespace also corrupts the prefix the pending suggestion would match.
   Fix: fall back to asking `buffer.auto_suggest.get_suggestion()`
   SYNCHRONOUSLY. That is the same work the async task would do, done now
   instead of maybe-already. `ThreadedAutoSuggest` keeps the sync method.

2. **`bind_action` treats `default_keys` elements as ALTERNATIVES.** Passing
   `("escape", "f")` bound bare `escape` AND bare `f` — hijacking the letter f
   mid-word and the Escape key `overlay_arbiter` depends on. Chords are ONE
   space-joined string: `("escape f",)`.

## Surface parity

Mounted in `ov.py::_client_extra_bindings` — the ONE action set both attach
surfaces share, so binding at either call site would fix the surface someone
looked at and leave the other dead. `ov_demo.py` mounts its own. Both pinned
by test, because a capability on one surface and not the other is the class
that left `search_rows` dark on the shipping client.

Remappable via `keymap.bind_action` (`chat:smart_complete`,
`chat:accept_suggestion_word`) — Tab is a default, not a constant. Master
`JARVIS_COMPLETION_ARBITER_ENABLED` (default true).

38 tests incl. 4 live-fire; 708 green across completion/keymap surfaces.
⚠️ Not yet confirmed by the operator in a real terminal.
