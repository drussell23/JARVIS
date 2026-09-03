#!/usr/bin/env python3
"""Stage work orders for a DPO farming soak — OPERATOR utility, never autonomous.

## Why this does not sign anything

The obvious way to get ops past `self_modification_unsanctioned_source` is
to mint HMAC-signed roadmap goals on demand. `strategy_signer`'s own module
docstring forbids exactly that, by name:

    This exists so the OPERATOR can deliberately attest authorship of a set
    of goals — it is NOT, and must never be, wired into any boot path. A
    roadmap the organism signs over its own goals would be a false
    authenticity claim and the self-authorization anti-pattern the cage
    forbids (operator = zero-order doll, §41.2).

The signature's entire meaning is "a human decided this". Software that
generates the goal AND the attestation produces cryptographically valid
signatures over an authorization nobody granted — a bypass with extra
steps, which is the one thing the farming effort must not become, since
the corpus it produces is what the model is then trained on.

## Why it does not need to

`RiskEngine._SELF_MOD_SENTINELS_BASE` scopes the cage to
`ouroboros/governance/` plus `ouroboros/{daemon,vital_scan,spinal_cord,
rem_sleep,rem_epoch}`; `_KERNEL_SENTINELS_BASE` adds `unified_supervisor`
and `_SECURITY_SENTINELS_BASE` adds `auth/`, `credential`, `secret`,
`token`, `.env`. **A target outside all of those is not self-modification
and requires no authorization at all** — the existing `.jarvis/roadmap.yaml`
says as much in its own note, having deliberately chosen an in-cage target
precisely so it *would* exercise the mechanism.

Farming does not want to exercise the cage. It wants ops that reach
VALIDATE so per-candidate verdicts exist to differentiate siblings. Ordinary
work on ordinary files does that, with the cage fully armed and untouched.

So this tool refuses, structurally, to name a sentinel path — it cannot be
repurposed into a cage bypass even by an operator in a hurry. For genuine
in-cage work, `--governance-target` writes an UNSIGNED roadmap doc and
prints the command for the operator to sign it themselves. It never signs.

## Format traps this encodes (each cost a soak to learn)

* `WorkOrderSensor` reads only the TAIL (`JARVIS_WORK_ORDER_RECENT_N`,
  default 3) of an append-only log — so items go at the BOTTOM.
* Every backticked token is a path candidate. Backtick ONLY the target.
* Prose containing the literal `NEXT:` is parsed as a work item.
* `.jarvis/work_order_seen.json` suppresses re-emission across sessions;
  a re-run needs it cleared or the same order will never fire again.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO = Path(__file__).resolve().parents[1]
PROGRESS = REPO / ".superpowers" / "sdd" / "progress.md"
SEEN = REPO / ".jarvis" / "work_order_seen.json"
ROADMAP_DRAFT = REPO / ".jarvis" / "roadmap.draft.yaml"

#: Re-derived from the live RiskEngine when importable, so this tool cannot
#: drift from the cage it is refusing to touch. The literals are the
#: fallback for a bare checkout, never a second opinion.
_FALLBACK_SENTINELS: Tuple[str, ...] = (
    "ouroboros/governance/", "ouroboros/daemon", "ouroboros/vital_scan",
    "ouroboros/spinal_cord", "ouroboros/rem_sleep", "ouroboros/rem_epoch",
    "unified_supervisor", "auth/", "credential", "secret", "token", ".env",
)


def live_sentinels() -> Tuple[str, ...]:
    """The cage's own path list, read from the engine that enforces it."""
    try:
        sys.path.insert(0, str(REPO))
        from backend.core.ouroboros.governance.risk_engine import RiskEngine
        eng = RiskEngine()
        return tuple(
            eng._self_mod_sentinels()
            + eng._kernel_sentinels()
            + eng._security_sentinels()
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not read live sentinels ({exc}); using fallback")
        return _FALLBACK_SENTINELS


def is_caged(rel_path: str, sentinels: Tuple[str, ...]) -> str:
    """Return the sentinel this path trips, or "" if it trips none."""
    p = rel_path.replace("\\", "/")
    for s in sentinels:
        if s in p:
            return s
    return ""


#: Ordinary, real improvements on small modules well outside every sentinel.
#: Deliberately DOC/TYPE-only: a farming soak wants many ops reaching
#: VALIDATE, not risky diffs. Each names its target ONCE, in backticks.
#: Phrases whose deliverable is INVISIBLE to the value gate.
#:
#: `CandidateValueGate` proves a change cosmetic by comparing `ast.dump()`
#: after `epistemic_shedder._DocstringStripper` -- so comments, formatting
#: and DOCSTRINGS never reach the comparison. A docstring-only patch is
#: therefore cosmetic BY CONSTRUCTION: the op completes as benign
#: `no_op_cosmetic`, which `classify_terminal_reason` maps to
#: ('unknown', 'intent_written', should_train=False).
#:
#: The first farming batch was six documentation tasks. It ran, it cleared
#: the cage, four of seven ops reached APPLY -- and produced 48
#: `no_op_cosmetic` rows out of 74, none trainable. The batch was chosen to
#: avoid the governance cage and accidentally chose the one deliverable the
#: value gate exists to discard. Refused structurally, like a sentinel
#: path, so the mistake cannot be repeated by someone in a hurry.
_COSMETIC_MARKERS: Tuple[str, ...] = (
    "docs only",
    "documentation only",
    "docstrings only",
    "comment only",
    "comments only",
    "change no executable line",
    "change no executable logic",
    "no executable change",
)


def assert_produces_executable_change(target: str, task: str) -> None:
    """Refuse a task whose deliverable cannot survive the value gate.

    Raises rather than warns: farming exists to produce TRAINABLE outcomes,
    and a task that can only yield `no_op_cosmetic` produces none. A warning
    in a staging script is read once and ignored forever.
    """
    low = (task or "").lower()
    hit = next((m for m in _COSMETIC_MARKERS if m in low), "")
    if hit:
        raise ValueError(
            "refusing to stage a cosmetic task for {!r}: contains {!r}. "
            "Docstrings are stripped before the AST comparison that decides "
            "no_op_cosmetic, so this can only terminate as "
            "('unknown', should_train=False) and yields no training signal. "
            "State a change to executable behaviour instead.".format(
                target, hit)
        )


#: Task-text signals for DESIGN FREEDOM -- work that admits more than one
#: correct implementation (a lookup table, a branch chain, a recursive
#: walk, an iterative one). Measured on soak bt-2026-09-02-003459: the
#: canonical tasks (re-raise an exception, swap `datetime.now()` for the
#: tz-aware form) collapsed to ONE structure across three draws at
#: temperatures 0.2/0.70/0.95, while the free-form tasks (a per-type
#: strategy table, a type guard with recursion + list handling, an
#: ok/error flag) drew 2-3 structurally distinct candidates. Sampling
#: cannot manufacture variance a task does not admit, so the batch should
#: LEAD with the work that can pair.
_FREEDOM_SIGNALS: Tuple[str, ...] = (
    "strategy", "table", "mapping", "depend on", "recurs", "depth",
    "bounded", "join", "collect", "handle list", "each element", "guard",
    "distinguish", "structurally", "ladder", "backoff", "retry", "per-",
    "policy", "fallback", "flag", "algorithm", "iterat", "walk",
)
_CANONICAL_SIGNALS: Tuple[str, ...] = (
    "re-raise", "reraise", "timezone", "datetime.now", "import timezone",
    "rename", "log the exception", "exc_info", "lift the hardcoded",
    "named module-level constants", "unused import",
)


def _branch_density(rel: str) -> float:
    """Decision points per definition in the target file, or 0.0.

    A file whose functions already branch a lot leaves more room for a
    different branching structure than a file of straight-line handlers.
    Read from disk with `ast`; never raises."""
    import ast  # noqa: PLC0415

    try:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return 0.0
    defs = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not defs:
        return 0.0
    branches = sum(
        1 for n in ast.walk(tree)
        if isinstance(n, (ast.If, ast.For, ast.While, ast.Try, ast.With,
                          ast.BoolOp, ast.IfExp))
    )
    return branches / len(defs)


def design_freedom_score(rel: str, task: str) -> float:
    """How many correct shapes this task admits, as a sortable number.

    Task text decides most of it (+1 per freedom signal, -1 per canonical
    signal); the target's branch density adds a fraction so that, between
    two similarly worded tasks, the one on the more algorithmic file leads.
    Deterministic and explainable -- the dry run prints it beside each
    order so an operator can see WHY the batch is in this order."""
    low = (task or "").lower()
    score = 0.0
    score += sum(1.0 for s in _FREEDOM_SIGNALS if s in low)
    score -= sum(1.0 for s in _CANONICAL_SIGNALS if s in low)
    score += min(1.0, _branch_density(rel) / 4.0)
    return round(score, 3)


#: Real, small, self-contained defects on NON-cage modules. Each names a
#: concrete behavioural change, so the patch alters the AST and the op can
#: reach a trainable verdict. Grounded by reading the files -- every item
#: below is a defect that exists in the tree today, not invented busywork.
TASKS: List[Tuple[str, str]] = [
    ("backend/api/monitoring_endpoint.py",
     "Fix the error-code bug in control_monitoring: the "
     "HTTPException(status_code=400) raised for an invalid action is thrown "
     "INSIDE the try block, and HTTPException subclasses Exception, so the "
     "broad except catches it and re-raises it as a 500. A client sending an "
     "invalid action receives Internal Server Error instead of Bad Request. "
     "Re-raise HTTPException unchanged before the generic handler runs."),
    ("backend/api/model_status_api.py",
     "Replace every timezone-naive datetime.now() with "
     "datetime.now(timezone.utc) and import timezone, so emitted timestamps "
     "are unambiguous. Naive timestamps in an API response cannot be "
     "compared across hosts."),
    ("backend/api/audio_error_fallback.py",
     "Lift the hardcoded fallback policy (delay_ms 1000, max_retries 3) into "
     "named module-level constants so the retry contract has one definition, "
     "and make the fallback timestamp timezone-aware."),
    ("backend/api/display_routes.py",
     "The broad except handlers return a success-shaped payload carrying an "
     "error string, so a caller cannot distinguish success from failure "
     "without string-matching the body. Give the failure path an explicit "
     "ok/error flag so the two are structurally distinguishable."),
    ("backend/api/sse_contract.py",
     "Make the broad except blocks observable: log the exception with "
     "exc_info before degrading, so a silently-swallowed SSE fault leaves "
     "evidence. Keep behaviour otherwise identical."),
    ("backend/api/clean_vision_response.py",
     "The cleaning steps assume their input is a str; a None or non-str "
     "value raises deep inside instead of being rejected at the boundary. "
     "Add an explicit type guard at the public entry point that returns the "
     "empty result for unusable input."),
    # --- Sibling-entropy harvest batch (2026-09-01). Each of these admits
    # MORE THAN ONE correct implementation (a lookup table, a branch chain,
    # a recursive walk, an iterative one), which is what a GRPO group needs:
    # siblings that differ in STRUCTURE, not in docstring wording.
    ("backend/api/clean_vision_response.py",
     "clean_vision_response recurses into nested dicts with no depth guard, "
     "so a self-referential or deeply nested payload recurses until "
     "RecursionError, and a list payload (e.g. a list of text fragments) is "
     "str()-ified into a Python literal. Add a bounded recursion depth and "
     "handle list inputs by cleaning each element and joining the non-empty "
     "text parts, returning the existing formatting-issue fallback when the "
     "bound is exceeded."),
    ("backend/api/audio_error_fallback.py",
     "handle_audio_error returns one fixed fallback_strategy (retry, 1000ms, "
     "3 attempts) for EVERY error_type, so a not-allowed permission error and "
     "an aborted error are told to retry the same way a transient network "
     "error is. Make the fallback strategy depend on error_type: network "
     "retries with backoff, no-speech retries once with no delay, aborted "
     "and not-allowed do not retry and name the alternative, and an unknown "
     "type gets a conservative single retry. Keep the existing suggestions."),
    ("backend/api/sse_contract.py",
     "eventstream_frame_to_jarviskit reads only the FIRST data: line of a "
     "frame, but the SSE grammar allows a payload to span several data: "
     "lines that the consumer joins with a newline before parsing. A "
     "multi-line frame therefore fails json.loads and is dropped as "
     "unparseable. Collect every data: line in the frame in order and join "
     "them with a newline before decoding, leaving single-line frames "
     "byte-identical in behaviour."),
    # --- Yield batch (2026-09-03). Soak 18 produced the arc's first clean
    # sample and only SIX trainable groups, because nine authored tasks were
    # all the real work that existed: the log carries 427 `2b.1-noop`
    # declines against 112 candidates, the model correctly refusing ambient
    # sensor work that is already done. Breadth, not sampling, is what the
    # corpus was short of. Every item below was read out of the tree with
    # the offending line quoted, and every one admits more than one correct
    # implementation -- a dispatch table or a branch chain, a guard clause
    # or an eager validation, a registry or a single slot -- because a task
    # with one obvious two-line fix draws three identical siblings.
    ("backend/api/simple_context_handler.py",
     "When a command needs the screen and the unlock helper reports "
     "success, the handler waits a fixed two seconds, then runs the "
     "command and rewrites the reply as an unlocked-and-executed success "
     "with screen_unlocked set true, without ever re-checking the lock "
     "state. On a slow or partially-completed unlock the command executes "
     "against a still-locked screen while the caller receives a success- "
     "shaped payload asserting the screen was unlocked. Confirm the screen "
     "actually reached the unlocked state within a bounded wait before "
     "executing the command, and return the existing unlock-failure "
     "response when that confirmation never arrives, instead of trusting a "
     "single fixed delay."),
    ("backend/api/direct_vision_fix.py",
     "The start-monitoring branch calls start_video_streaming a second "
     "time even though the manager's start_monitoring already started it, "
     "and when that call reports failure execution falls through to a "
     "payload with handled, success and monitoring_active all true, so a "
     "caller cannot tell that streaming never started and the reported "
     "error is discarded. It also assumes the streaming call returns a "
     "mapping, raising AttributeError for any analyzer that returns None "
     "or a bool. Drop the redundant second start, produce a distinct "
     "unsuccessful result that carries the reported error when streaming "
     "does not come up, and tolerate a non-mapping return value from the "
     "analyzer."),
    ("backend/api/voice_503_fix.py",
     "Every exit path of the activation endpoint returns HTTP 200 with "
     "status activated, including a malformed request body, a full queue, "
     "and the two-second wait timeout, so a client has no way to "
     "distinguish an activation that actually happened from one that was "
     "dropped or never processed. The timed-out request is also left in "
     "the queue with its future still pending, so the background worker "
     "later resolves an abandoned request and burns a CPU sample doing it. "
     "Keep the endpoint from emitting 503, but make the reported status "
     "and body distinguish accepted, queued, throttled and failed "
     "outcomes, and make a request that is no longer awaited be cancelled "
     "or discarded so the worker does not complete orphaned work."),
    ("backend/api/context_aware_integration.py",
     "The execution callback that runs the user's real command sits inside "
     "the same broad try as all the result-shaping code, which "
     "dereferences the success key directly, so any exception or missing "
     "key raised after the command already executed falls into the "
     "fallback and executes that same command a second time. Side- "
     "effectful commands therefore run twice and the caller only ever sees "
     "the second result, with no indication of the duplication. Restrict "
     "the fallback to the case where the original command demonstrably has "
     "not been executed yet, and turn a malformed or incomplete context "
     "result into an explicit failure response rather than a reason to re- "
     "run the command."),
    ("backend/api/vision_query_bypass.py",
     "Routing decisions are made by bare substring containment, so short "
     "cues match inside unrelated words, count fires on account and open "
     "in fires mid-sentence, and any imperative that happens to contain "
     "browser, windows, application or show me but does not literally "
     "start with one of the nine hardcoded verbs is diverted to vision "
     "analysis instead of being executed, silently dropping the user's "
     "action. The same containment flaw misclassifies query_type and picks "
     "the first application or UI element mentioned regardless of "
     "position. Make cue matching respect word boundaries and require "
     "genuine interrogative or observational form before bypassing command "
     "interpretation, so action requests keep reaching the command path."),
    ("backend/api/hud_local_bridge.py",
     "The loopback check treats an empty or missing host as loopback, so a "
     "request whose client address is unknown or stripped by a proxy is "
     "granted the unauthenticated trust-the-localhost shortcut that is "
     "supposed to be reachable only from this machine. In the other "
     "direction the only normalization performed is stripping an "
     "IPv4-mapped IPv6 prefix, so legitimate local origins such as a "
     "bracketed IPv6 literal, an address carrying a zone identifier, or a "
     "host with a port appended are rejected and the native client is "
     "refused. Make an absent or unrecognizable origin untrusted, and "
     "normalize the supplied host properly so all real loopback forms are "
     "accepted."),
    ("backend/api/screen_control_api.py",
     "The unlock endpoint ignores both the action and the method fields of "
     "the request, it unconditionally drives the keychain implementation, "
     "yet echoes the caller's requested method back in the response, so a "
     "client that asks for the applescript or swift path, or that posts an "
     "action of lock to this endpoint, receives a successful response "
     "naming a method that was never executed. Callers therefore cannot "
     "detect that their requested strategy was silently substituted. "
     "Dispatch on the requested method, reject or explicitly report an "
     "unsupported or contradictory request, and only ever report the "
     "method that actually ran."),
    ("backend/api/vision_ws_endpoint.py",
     "When the idle timeout fires the receive loop breaks out and the "
     "function returns without removing the client from the active "
     "connections registry and without closing the socket, so the dead "
     "entry persists forever. Every later broadcast then tries to send on "
     "that stale socket, and the registry grows without bound across "
     "reconnects. Guarantee that the connection is deregistered and closed "
     "on every way out of the handler, normal exit, idle timeout, "
     "disconnect and unexpected error alike."),
    ("backend/api/self_healing_api.py",
     "The fix endpoint reads the last entry of the healer's fix history "
     "after invoking it and reports that entry's issue and strategy as the "
     "outcome of this request, without checking that the entry was "
     "produced by this call. When the healer appends nothing, because "
     "nothing was wrong or a retry limit blocked the attempt, the response "
     "attributes a previous run's issue type and strategy to the current "
     "request and pairs it with this call's success flag, so the caller "
     "reads a stale diagnosis as fresh. Capture enough state before "
     "invoking the healer to identify which history entries this call "
     "produced, report only those, and return an explicit no-attempt "
     "result otherwise."),
    ("backend/api/display_monitor_api.py",
     "Every endpoint raises a 503 for an uninitialized display monitor "
     "from inside a try block whose broad handler re-wraps any exception "
     "as a 500, so a caller polling for readiness sees an opaque server "
     "error instead of the service-unavailable signal it was given, and an "
     "unimportable display package produces that same indistinguishable "
     "500. Restructure the handlers so a deliberately chosen HTTP status "
     "propagates untouched, an absent or uninitialized monitor is reported "
     "as unavailable, and genuinely unexpected faults keep their 500. "
     "Share the monitor resolution and status mapping across all six "
     "endpoints instead of repeating the same try wrapper in each."),
    ("backend/api/direct_unlock_handler.py",
     "The system screen-lock fallback launches a second interpreter with "
     "no timeout and runs it synchronously from an async call path, so an "
     "unresponsive child process blocks the whole event loop indefinitely "
     "with no way to recover. The daemon health check likewise closes its "
     "probe socket only on the success path, leaking the descriptor "
     "whenever the connect attempt raises. Give every OS resource a "
     "guaranteed release regardless of how the block exits, bound the "
     "external probe in wall-clock time, and keep the blocking work off "
     "the event loop so a stuck probe degrades to a negative answer "
     "instead of freezing the backend."),
    ("backend/api/service_surface.py",
     "Mounting the service surface discards the router mount result "
     "entirely and treats hydration alone as success, so when the "
     "websocket and device SSE router fails to import or fails to mount, "
     "the function still reports success and logs that the device SSE "
     "routes are served, precisely the silently-missing-router failure "
     "this module exists to catch. Make the reported outcome reflect what "
     "actually got mounted as well as what hydrated, so a caller can "
     "distinguish a complete surface from a partial one, and record which "
     "surfaces are missing somewhere an operator can see. Keep both halves "
     "non-fatal; degrade visibly rather than raising."),
    ("backend/api/autonomy_handler.py",
     "The activation sequence appends a step-succeeded string after each "
     "helper and then reports success with all systems online, even though "
     "every helper swallows its own import failure and leaves its flag "
     "false, so a caller receives a success payload and autonomous mode is "
     "set while nothing was actually activated. Have each step surface its "
     "real outcome and let the aggregate response separate full activation "
     "from partial or total failure, including whether autonomous mode "
     "should be entered at all. Emit the reported instants unambiguously "
     "enough to be ordered across machines rather than using the host's "
     "bare local clock."),
    ("backend/api/hive_envelope.py",
     "Both adapters coerce the source timestamp with a float conversion "
     "that raises on a string or any other non-numeric value, dropping an "
     "otherwise valid frame into the catch-all fallback; that fallback "
     "then emits a normal-looking info-severity envelope with no detail, "
     "no event id and no trace, so the feed cannot tell a degraded cast "
     "from a genuine informational event. Make timestamp interpretation "
     "tolerant of the shapes sources actually send and fall back to the "
     "current time only when no usable value exists. Mark envelopes "
     "produced by the failure path so consumers can see that the cast "
     "degraded and why, preserving whatever identifying fields survived "
     "instead of discarding the payload."),
    ("backend/api/progressive_hydration.py",
     "The per-subsystem guard catches BaseException, so cancelling the "
     "background hydration task or interrupting the process is recorded as "
     "an ordinary subsystem error and the loop keeps walking every "
     "remaining loader; shutdown therefore blocks until the whole graph "
     "has been traversed and the cancellation is never observed by whoever "
     "requested it. Nothing bounds how long a single loader may run "
     "either, so one hanging subsystem stalls hydration forever with no "
     "degraded event emitted. Preserve the fail-soft behaviour for genuine "
     "load failures while letting cancellation and interruption end "
     "hydration promptly, and give each loader a bounded time budget whose "
     "expiry degrades that subsystem like any other failure."),
    ("backend/api/loopback_selftest.py",
     "The self-test is documented to never raise, yet the self-heal path "
     "calls the injected recovery engine and imports its helpers outside "
     "any guard, and the classification step reads attributes straight off "
     "the failover result, so a recovery engine that throws or a result "
     "missing those attributes escapes the boot self-test and takes down "
     "the hydration step that scheduled it. Nothing bounds how long the "
     "provider attempts may take either, so a wedged backup provider hangs "
     "boot indefinitely. Make the entry point total: every outward path "
     "must produce a result state, an unexpected fault must be classified "
     "and reported as a surfaced non-fatal failure rather than "
     "propagating, and provider attempts must be time-bounded with the "
     "expiry classified like any other non-answer."),
    ("backend/api/auto_config_endpoint.py",
     "The discovery responses hardcode localhost and the plain http and ws "
     "schemes for every caller and read the port straight out of an "
     "environment variable, so a client on another machine or behind TLS "
     "is handed base, websocket and endpoint URLs it can never reach, "
     "which defeats the entire purpose of an auto-configuration endpoint. "
     "A non-numeric configured port additionally turns the whole response "
     "into an unhandled server error. Derive the advertised host, scheme "
     "and port from what the request actually arrived on, honouring "
     "forwarding headers when present and falling back to configured "
     "values only when the request cannot tell you, keep the websocket "
     "scheme consistent with the http one, and validate the configured "
     "port before it reaches the response."),
    ("backend/api/package_recovery.py",
     "Recovery collapses the requested module to its top-level package "
     "before the allowlist lookup and before probing, and the post-install "
     "verification imports only that top-level name, so a missing "
     "submodule is reported as recovered whenever its parent package "
     "merely imports; the caller then retries, fails with the identical "
     "fault, and the once-per-session ledger now refuses to try again. "
     "Carry the full dotted module name through resolution and "
     "verification so the exact module that was missing is the one proven "
     "importable, while the governed allowlist decision stays keyed on the "
     "installable distribution. A verification that does not actually "
     "restore the requested import must be reported as a distinct non- "
     "recovered outcome rather than as success."),
    ("backend/api/wake_word_api.py",
     "The streaming endpoint installs its per-connection send function "
     "into the single shared service's one callback slot, so a second "
     "client silently steals the stream from the first, and whichever "
     "client disconnects first restores the pre-existing callback and cuts "
     "off every other connected client. Replace the single-slot handoff "
     "with a registration model that lets any number of concurrent streams "
     "receive activations and that removes only the departing connection's "
     "own subscription, leaving whatever callback existed before the first "
     "subscriber intact until the last subscriber leaves. A delivery "
     "failure to one client must not prevent delivery to the rest or leave "
     "a dead subscriber registered."),
    ("backend/api/proactive_monitoring_handler.py",
     "Enabling change reporting returns a payload asserting that "
     "monitoring is active, with prose telling the user the indicator "
     "confirms active watching, regardless of whether the vision "
     "intelligence exists, whether it exposes the multi-space monitoring "
     "entry point, or whether starting it returned false; in all of those "
     "cases no monitoring task is ever created and the caller is told the "
     "opposite of the truth. Report the state actually reached and make an "
     "unavailable or refused start distinguishable in the response, with "
     "narration that matches. The workspace-state read must also stop "
     "returning nothing when its detector attribute is absent, since an "
     "empty state silently prevents the loop from ever holding two states "
     "to compare."),
    ("backend/api/dynamic_cors_handler.py",
     "Preflight and normal responses are emitted with credentials enabled "
     "alongside wildcard values for allowed and exposed headers, a "
     "combination browsers reject outright for credentialed requests, so "
     "every cross-origin call carrying cookies fails even though the "
     "origin was explicitly allowed. Responses whose CORS headers are "
     "computed from the request origin also carry nothing telling caches "
     "that they vary by origin, so a shared cache can hand one origin's "
     "headers to another. Reflect the concrete headers the preflight "
     "actually requested instead of a wildcard, keep the exposed-header "
     "set explicit, and mark origin-dependent responses so any cache keys "
     "on the origin."),
    ("backend/api/rust_api.py",
     "The build endpoint guards concurrency by testing for a lock file and "
     "then separately creating it, so two requests arriving together both "
     "see no lock and both launch the build subprocess, and a lock left "
     "behind by a crashed or killed process makes every later build fail "
     "with a permanent 409 that nothing can clear. Replace the check-then- "
     "create pattern with an acquisition that cannot be interleaved, and "
     "make a lock left by a process that is no longer alive or that is "
     "older than a sane build window reclaimable rather than fatal. Ensure "
     "the caller can tell a build that is genuinely running apart from a "
     "stale marker, and only the request that actually acquired the lock "
     "may release it."),
    ("backend/api/startup_voice_api.py",
     "After successfully queueing an announcement the handlers reach into "
     "the coordinator status dictionary with required-key indexing for "
     "running, queue, size, active_engines, workers and rate_limiter, so a "
     "coordinator that omits or renames any of those keys raises inside "
     "the try block and the endpoint answers 500 with an error payload "
     "even though the announcement was accepted and will be spoken. "
     "Callers treat that as a failure and retry, producing duplicate "
     "speech. Make the status summary tolerant of a partial or differently "
     "shaped status object so that the outcome reported to the caller "
     "always reflects the queueing result, and reserve the error response "
     "for cases where the announcement itself did not happen."),
    ("backend/api/async_tts_handler.py",
     "Generated audio may end up as AIFF when both ffmpeg and lame are "
     "unavailable, but the cache writes every file under an mp3 name and "
     "the cache-hit path unconditionally reports audio/mpeg, so a cached "
     "AIFF payload is later served to clients as MP3 and fails to decode. "
     "Record or derive the actual format of each cached artifact and "
     "return the content type that matches the bytes on disk, including "
     "for entries already produced by the fallback path. The format "
     "information must survive a process restart, since the metadata is "
     "reloaded from disk on startup."),
    ("backend/api/direct_unlock_handler_fixed.py",
     "The daemon health check performs a synchronous blocking socket "
     "connect with a half-second timeout from inside a coroutine, so "
     "whenever the unlock daemon is down or slow the whole event loop "
     "stalls and every other request served by the process is frozen for "
     "the duration; the socket is also closed only on the success path, "
     "leaking a descriptor when the connect call itself raises. Perform "
     "the reachability probe without blocking the event loop and guarantee "
     "the socket is released on every exit path. Keep the fast-fail "
     "behaviour so an unavailable daemon is still detected quickly rather "
     "than waiting for the full WebSocket timeout."),
    ("backend/api/lazy_enhanced_vision_api.py",
     "The vision WebSocket endpoint only unregisters a client in its "
     "exception handlers, so the idle-timeout path breaks out of the "
     "receive loop and returns while the socket is still in the manager's "
     "active connection set and is never closed. The stale entry makes "
     "every later broadcast attempt a send on a dead socket, keeps the "
     "connection count wrong, and prevents monitoring from stopping when "
     "the last real client leaves. Guarantee that leaving the handler for "
     "any reason removes the connection from the manager exactly once and "
     "closes the underlying socket, without double-removing on the paths "
     "that already handle disconnection."),
    ("backend/api/notification_vision_api.py",
     "When a broadcast to a client fails, the notification manager logs "
     "the error and keeps that connection in its active list, and the "
     "WebSocket endpoint's idle-timeout branch leaves without "
     "unregistering at all, so dead sockets accumulate forever. Every "
     "subsequent notification then pays repeated failing sends, and the "
     "reported active websocket count in the status endpoint drifts away "
     "from reality. Prune connections that fail to receive a broadcast and "
     "make departure from the endpoint unregister the client on every exit "
     "path, ensuring a client removed twice is handled harmlessly."),
    ("backend/api/startup_progress_api.py",
     "Both state callbacks obtain the running event loop and schedule a "
     "broadcast, and when they are invoked from a plain worker thread the "
     "lookup raises and the update is discarded silently, so progress and "
     "readiness changes emitted off the async thread never reach connected "
     "clients and the loading page stalls with no diagnostic. The "
     "scheduled tasks are also created without keeping a reference, so "
     "they can be collected before they run. Deliver updates that "
     "originate outside the event loop instead of dropping them, keep "
     "scheduled broadcasts alive until they complete, and make a genuinely "
     "undeliverable update observable rather than invisible."),
    ("backend/api/network_recovery_api.py",
     "The diagnosis path runs ping and privileged cache-flush commands "
     "with blocking synchronous subprocess calls and no timeout from "
     "inside an async endpoint, so the entire event loop is frozen while "
     "they run and a privileged invocation that waits for a password never "
     "returns, hanging the server permanently on a request that is "
     "supposed to be a quick diagnostic. Run these probes so they cannot "
     "block the event loop and cannot exceed a bounded wall-clock budget, "
     "terminating anything that overruns. Have the diagnosis report which "
     "steps actually ran, timed out, or were skipped for lack of "
     "privileges instead of silently reporting an unknown issue."),
    ("backend/api/converged_headless.py",
     "The background boot coroutine runs hydration, the self-test wiring "
     "and the ready emission with no failure handling around the whole "
     "sequence, and the task it runs in is created without any completion "
     "handling, so an exception from hydration or the control plane kills "
     "the boot silently: the system state is left at booting forever, no "
     "readiness event is ever emitted, and the status endpoint keeps "
     "reporting a boot in progress that will never finish. Ensure a "
     "failure anywhere in the boot sequence is logged, drives the exposed "
     "system state to a terminal degraded or failed value, and still emits "
     "a readiness signal carrying that outcome so attached cockpits and "
     "pollers stop waiting. The lifespan must not raise on this path, and "
     "cancellation at shutdown must remain distinguishable from a real "
     "boot failure."),
    ("backend/api/enhanced_vision_api.py",
     "The action-suggestion callback executes every suggestion whose "
     "priority is high or urgent by calling straight into the AI core, "
     "without consulting the autonomy handler that the rest of the manager "
     "treats as the gate for autonomous behaviour, so the system performs "
     "actions on the user's machine even when autonomous mode is switched "
     "off. It also runs the batch with no error isolation, so the first "
     "failing action aborts the remainder and no client is told anything "
     "happened. Gate execution on the current autonomy state and on the "
     "suggestion's own confidence rather than priority alone, and make "
     "each suggestion succeed or fail independently with the outcome "
     "broadcast either way, including for suggestions that were withheld."),
    ("backend/api/enhanced_voice_routes.py",
     "Every request path samples CPU with a blocking hundred-millisecond "
     "interval call, twice per activation, directly on the event loop, so "
     "the load-management feature that exists to avoid overload instead "
     "serialises all concurrent requests behind repeated synchronous "
     "sleeps and makes the reported processing time mostly measurement "
     "overhead. The per-request samples are also appended to lists that "
     "are never trimmed even though only the most recent entries are ever "
     "read, so the process grows without bound. Obtain the utilisation "
     "figures without stalling the event loop and keep only a bounded "
     "window of samples, while still allowing the activation handler to "
     "compare utilisation before and after processing."),
]


def build_orders(n: int, sentinels: Tuple[str, ...]) -> List[str]:
    out: List[str] = []
    # Most design freedom FIRST. WorkOrderSensor reads the tail of
    # progress.md and the pool is FIFO, so batch order is dispatch order:
    # the work most likely to pair should reach a worker first.
    ranked = sorted(
        TASKS, key=lambda t: design_freedom_score(t[0], t[1]), reverse=True,
    )
    for rel, instruction in ranked[:n]:
        # Both refusals are structural and both run BEFORE anything is
        # written: a cage trip means the work needs an operator signature
        # this tool must never mint, and a cosmetic task means the work
        # cannot produce a trainable outcome however cleanly it runs.
        assert_produces_executable_change(rel, instruction)
        trip = is_caged(rel, sentinels)
        if trip:
            raise SystemExit(
                f"REFUSED: target {rel!r} trips the cage sentinel {trip!r}.\n"
                "This tool provisions UNAUTHORIZED-BY-DESIGN work only. "
                "In-cage work needs an operator signature: use "
                "--governance-target."
            )
        if not (REPO / rel).is_file():
            print(f"  ! skipping {rel} (not present in this checkout)")
            continue
        # ONE backticked token, and no literal "NEXT:" inside the prose.
        print(f"  [freedom {design_freedom_score(rel, instruction):+.2f}] {rel}")
        out.append(f"NEXT: {instruction} Target file: `{rel}`")
    return out


def append_orders(orders: List[str], *, apply: bool) -> None:
    text = PROGRESS.read_text(encoding="utf-8") if PROGRESS.exists() else "## Queue\n"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    block = (
        f"\n<!-- DPO farming batch staged {stamp} by scripts/"
        "provision_farming_work.py — ordinary work on non-cage targets; "
        "no authorization was minted for these. -->\n\n"
        + "\n\n".join(orders) + "\n"
    )
    if not apply:
        print("\n--- would append to progress.md (BOTTOM = the tail the sensor reads) ---")
        print(block)
        return
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROGRESS, PROGRESS.with_suffix(".md.bak")) if PROGRESS.exists() else None
    PROGRESS.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")
    print(f"  appended {len(orders)} order(s) to {PROGRESS.relative_to(REPO)}")


def clear_seen(*, apply: bool) -> None:
    if not SEEN.exists():
        print("  dedup ledger absent — nothing to clear")
        return
    if not apply:
        print(f"  would clear {SEEN.relative_to(REPO)} ({SEEN.read_text()[:80]})")
        return
    shutil.copy2(SEEN, SEEN.with_suffix(".json.bak"))
    SEEN.write_text("[]", encoding="utf-8")
    print(f"  cleared {SEEN.relative_to(REPO)} (backup .json.bak)")


def write_unsigned_roadmap(target: str, sentinels: Tuple[str, ...]) -> None:
    """Emit a roadmap goal for IN-CAGE work, UNSIGNED, for the operator.

    Deliberately stops one step short of authorization. Signing here is the
    self-authorization anti-pattern; signing is the operator's act.
    """
    trip = is_caged(target, sentinels)
    if not trip:
        print(f"  note: {target} trips no sentinel — it needs no roadmap at all.")
    doc: Dict[str, Any] = {
        "version": 1,
        "operator_id": os.environ.get("USER", "operator") + "-REVIEW-REQUIRED",
        "source": "operator_directed_agent_signed",
        "authority": "operator_directed",
        "signed": False,
        "note": (
            "DRAFT staged by scripts/provision_farming_work.py. It is "
            "UNSIGNED on purpose: strategy_signer forbids the organism "
            "signing its own goals (§41.2). Review the goal, then sign it "
            "yourself."
        ),
        "goals": [{
            "id": "farming-draft-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M"),
            "title": f"REVIEW AND EDIT before signing — draft goal for {target}",
            "description": "REPLACE THIS with the change you are authorizing.",
            "priority": "low",
            "success_criteria": "REPLACE THIS with what proves the change correct.",
            "depends_on": [],
            "target_files": [target],
            "max_duration_s": 1800,
        }],
    }
    try:
        import yaml  # type: ignore
        ROADMAP_DRAFT.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        ROADMAP_DRAFT.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"\n  wrote UNSIGNED draft -> {ROADMAP_DRAFT.relative_to(REPO)}")
    print("\n  OPERATOR ACTION (this tool will not do it for you):")
    print("    1. edit the draft — the placeholders are deliberate")
    print("    2. review that target_files is what you intend to authorize")
    print("    3. sign it with YOUR existing secret, then move it into place:")
    print("       python3 -m backend.core.ouroboros.governance.strategy_signer \\")
    print(f"         {ROADMAP_DRAFT.relative_to(REPO)} \"$JARVIS_ROADMAP_READER_HMAC_SECRET\"")
    print("    (omit the secret and the CLI mints a NEW one, invalidating every")
    print("     signature you already issued.)")


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default is a dry run)")
    ap.add_argument("--count", type=int, default=len(TASKS),
                    help="how many work orders to stage")
    ap.add_argument("--keep-seen", action="store_true",
                    help="do NOT clear the dedup ledger")
    ap.add_argument("--governance-target", metavar="PATH",
                    help="emit an UNSIGNED roadmap draft for in-cage work")
    args = ap.parse_args(argv)

    sentinels = live_sentinels()
    print(f"cage sentinels in force ({len(sentinels)}): {', '.join(sentinels)}")

    if args.governance_target:
        write_unsigned_roadmap(args.governance_target, sentinels)
        return 0

    orders = build_orders(max(1, args.count), sentinels)
    if not orders:
        print("no usable targets in this checkout")
        return 1
    print(f"\nstaging {len(orders)} work order(s), all outside every sentinel:")
    for o in orders:
        print(f"  - {o[6:90]}...")
    append_orders(orders, apply=args.apply)
    if not args.keep_seen:
        clear_seen(apply=args.apply)
    print("\n" + ("done." if args.apply else "DRY RUN — re-run with --apply"))
    print("Note: WorkOrderSensor reads only the TAIL "
          "(JARVIS_WORK_ORDER_RECENT_N, default 3). Raise it to emit more "
          "than the last 3 of these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
