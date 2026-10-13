# O+V Foreground Session ("talk to the organism") — architecture

Status (2026-10-13): §4.5 (live pipeline blocks, op selection, `?` legend,
status counts, learning toolbar + lock screen) is BUILT (1d25bd8c26,
519a963d89, a6e0cca7e2, c59a41228c, 860d4b8ed2). The session mode itself
(§3, §4.1–4.4, §5, §6) is in progress. This file was recovered from the
2026-10-09 session transcript after its scratchpad copy was lost; it lives in
the repo now.

§7 defaults taken (each an env knob, flip on request): a session's change
shows its diff and waits for Enter even when the gates would auto-apply;
sessions survive a cockpit detach but not a daemon restart; no cap on
background ops while a session is active.

## 1. What it is

Claude Code's interaction model, O+V's governance. The operator types; ONE Venom
loop works in the foreground, its tool calls and diffs render as live blocks,
it answers, and the next message continues the SAME context. Every byte that
changes the repo still goes through VALIDATE → Iron Gate → APPLY → VERIFY.
It is not a second agent: it is a session wrapped around the loop that already
exists.

## 2. Principle: two loops would be two organisms

No fork of Venom, no second provider path, no second gate.

| Concern            | Reused, not rebuilt                                              |
|--------------------|------------------------------------------------------------------|
| Agent loop         | `ToolLoopCoordinator.run()` (governance/tool_executor.py)        |
| Generation         | the same CandidateGenerator lane (local J-Prime today)            |
| Mutation           | a normal governed op via GLS.submit (full 11-phase pipeline)     |
| Op ↔ session link  | `/goal` scoped injection + cryptographic sanction (existing)     |
| Tool result reuse  | OpContextCache (built in the queue BEFORE this)                  |
| Rendering          | typed blocks (ui/block_protocol) + BlockStore + canvas live region|
| Output routing     | attach_session session/lane ContextVars + cockpit_fsm modes      |
| Background summary | ambient_deck severity classifier + lane_rings                    |

## 3. Components

### 3.1 SessionContext (persistent context manager), `governance/foreground_session.py`

The ONLY new state. Holds, per session id:
- `turns`: user message, assistant answer, and a digest of that turn's tool
  results (path, symbol, hash) rather than the raw output, which stays in OpContextCache.
- `focus`: files and symbols the conversation is about (drives cache priority).
- `owned_ops`: op ids this session started (for routing and status).
- `budget`: token budget, from the served window
  (`local_inference_director.prompt_window_for`), never a constant.

Per user message it:
1. Builds the turn prompt: the session preamble, then a rolling summary of
   older turns, then the last N turns verbatim, then cached tool results
   (OpContextCache, focus first), then the new message. The whole prompt is
   fitted with the structure-aware `fit_prompt_to_window`, with the tool
   section and the current message protected.
2. Calls `ToolLoopCoordinator.run(..., is_read_only=True)`. The conversation
   itself never mutates: read, search, web and MCP are allowed; edit, write
   and bash mutations are refused by the existing read-only gate (rule 0d).
3. Streams the loop's tool calls as blocks addressed to this session.
4. If the answer is a change (`2b.1-diff` / `2b.1` candidate), it does not
   apply it. It turns the change into a governed op (section 3.2).
5. Rolls the turn into `turns`. When the transcript nears its budget, older
   turns are summarized DETERMINISTICALLY: the digest of tool results plus
   the answer's first paragraph. No extra model call, nothing invented.

Persistence: in memory per daemon session, plus an append-only JSONL under
`.jarvis/sessions/<id>.jsonl`, so `/session resume` survives a cockpit
detach. It does not survive a daemon restart by default. Bounded by count
and age (env).

### 3.2 Mutation path: the session never writes

A proposed change becomes an IntentEnvelope:
- `source="foreground_session"` and `session_id`;
- the candidate attached as a pre-generated candidate, so the pipeline can
  skip re-generation, then VALIDATE / GATE / APPLY / VERIFY / commit;
- sanctioned the way `/goal` is (operator-originated, signed).

The risk tier is computed exactly as for any op. The operator being present
does NOT lower it. An APPROVAL_REQUIRED change prompts in the session
transcript (the existing gate prompt, addressed). A failed VALIDATE or VERIFY
is returned to the session as the next turn's context ("the change failed:
<digest>"), so the conversation can repair it. The repair is still a new
governed op.

### 3.3 Session lifecycle verbs (auto-discovered REPL verbs)

- `/session` (start or attach), `/session end`, `/session list`,
  `/session resume <id>`.
- `Esc` cancels the in-flight turn (existing cancel token).
- `Esc Esc` rewinds to the previous turn (existing rewind controller).
- Plain text inside a session is a turn. Plain text outside one keeps the
  current /chat routing. Nothing that exists today changes behaviour.

## 4. TUI multiplexer: one sink, one routing predicate

Existing fact: every transcript line lands in ONE place
(`BipartiteLayout._append` / `set_live`, through the block store). So the
multiplexer is not a second renderer. It is a predicate at that sink, deciding
for each block or line where it lands. No tearing is possible: nothing writes
to the terminal except the canvas, and prompt_toolkit diffs the canvas.

### 4.1 Classification, by data and never by text

Every frame already carries `session` (addressed) and `lane`. Blocks carry
`op_id`, and session-owned op ids are known. So:

| Frame                                         | In a session                      |
|-----------------------------------------------|-----------------------------------|
| addressed to this session                     | FOREGROUND: transcript            |
| block whose op_id ∈ session.owned_ops         | FOREGROUND: transcript            |
| gate prompt for an owned op                   | FOREGROUND: transcript + input    |
| ambient, severity FATAL                       | FOREGROUND interrupt line (rare)  |
| ambient, severity WARN                        | status badge (count + newest)     |
| ambient INFO / SOCIAL, other ops' blocks      | SIDELINED: buffered, not drawn    |

### 4.2 Sidelined output is buffered, not lost

Sidelined frames go to the lane rings (`lane_rings`, already bounded with
tombstones) and to a background digest. One live row (the canvas live region,
key "background") shows: `⚙ 3 ops running · 1 landed · 1 warning · /bg`.
`/bg` (or `^X` lanes) shows the background, and leaving the session replays
the buffered summary as collapsed lines: one per op, closure lines only.
Bounded by the existing ring sizes.

### 4.3 Mode as state

`cockpit_fsm` gains `session:<id>` beside `flow` / `select` / `focus:<lane>`.
The predicate reads the FSM mode, so switching modes re-routes instantly and
deterministically. No timing, no heuristics.

### 4.4 Ordering and resize

The foreground is ordered by the block `seq` the daemon assigns (already
in-order, idempotent, out-of-order safe). Resize is prompt_toolkit's, as
today. The live region holds the session's in-flight blocks and the
background row as separate keys, so neither overwrites the other.

### 4.5 Real-time transparency: every op's pipeline as a live block (prerequisite)

The operator must be able to watch background work happen in real time,
session or not. Today a phase transition is shown only for the ONE op the
status bar tracks; `▸ phase` transcript lines were removed as noise, so other
ops' progress is invisible.

With the block protocol, each in-flight op owns one LIVE block (kind
`pipeline`) whose lines re-render in place as phases advance, and which
freezes into its closure line (plus its diff) when the op ends:

    ● TestFailure · tests/test_model_status_api.py          1m 12s
      ✓ explore  ✓ generate  ✓ validate  ✓ gate  ● apply  ○ verify  ○ commit

Learning gets the same: one live block per training cycle, fed by
training_handoff's progress
(`● Learning · cycle 4 · step 120/400 · loss 0.41`), frozen as
"adapter tune #N published" or "rejected: <reason>". Promotion
(`main ← sha`) updates the same op's block.

- Flow mode: these blocks ARE the real-time view, and they add no transcript lines.
- Session mode: they are what the sidelined area holds. `/bg` shows them, and
  the `⚙ N running · M landed` row is computed from them.
- The phase sequence is read from the pipeline's own phase enum, not retyped.

Built right after the deploy, BEFORE the session mode: it makes today's
cockpit transparent on its own.

#### 4.5.1 Agent tree inside each op block

Each op block shows the op, the agents in it, and the gate verdicts:

    ● DocStaleness · governance/foo.py                          0m 41s
      ✓ explore  ● generate  ○ validate  ○ gate  ○ apply  ○ verify  ○ commit
      ├ ● Venom (J-Prime 30B) · round 2 · Search("risk_tier")
      └ ● REVIEW subagent · reading 2 files

When the op ends, the block freezes as: agent summary · gate verdicts · diff ·
commit/landing. Data comes from phase heartbeats, tool_inflight, agent_roster,
the inference gateway (who holds the GPU), and DECISION/SemanticGuardian
telemetry. There is no second source of truth.

#### 4.5.2 Cockpit layout

Top to bottom:

1. **Crest.** Collapses after boot.
2. **TRANSCRIPT.** Frozen blocks, scrollable.
3. **LIVE area.** In-flight op trees and the learning block. It sits in the
   canvas live region and re-renders in place.
4. **Activity pulse.**
5. **Hints row.** Model · voice · keys.
6. **Input.**
7. **Status bar.** `N ops · M agents · GPU: <op> · landed · /h · main ← sha`.

Density rules:

- **One expanded op.** The op holding the GPU, or the one the operator
  focused, shows its full tree. Every other op collapses to one line
  (`○ op · phase · state`).
- **Height follows the terminal.** The live area takes a share of the
  terminal height (env-tunable). Overflow becomes `+N more · /agents`.
- **Order is fixed.** Live blocks are ordered by `seq`. An op leaves the live
  area only by freezing into the transcript, keeping its rhythm.

Drill-down reuses what exists:

- `^X`/`^L` select an op, then Enter focuses it (cockpit_fsm `focus:`).
- `/agents` opens the full roster.
- `/expand <ref>` shows a full tool result or diff.
- `?` shows the legend (op = job, agent = worker, gate = check; ● ○ ✓ ✗).

In session mode the live area collapses to one background summary row; a FATAL
event still breaks through.

## 5. Concurrency and resources

- One foreground turn at a time per session. A message typed while a turn
  runs is queued (the existing operator_input_queue) and shown as pending.
- The foreground turn uses the same generation lane as background ops.
  Priority: foreground turns enter admission as IMMEDIATE (the operator is
  waiting), and background ops yield at their next admission point; nothing
  is preempted mid-generation. The VRAM mutex and admission gate are reused,
  not bypassed.
- Background autonomy keeps running (the organism drives); it is only
  sidelined on screen. An operator who wants quiet uses the existing
  autonomy pause (viewport lock), not something new.

## 6. Invariants (each becomes a test)

1. No file changes from a session except through a governed op (an AST pin:
   foreground_session imports no change_engine / edit path, and the loop is
   called with `is_read_only=True`).
2. Risk tier and gates are identical for session-originated and
   sensor-originated ops.
3. Every frame is routed exactly once (foreground XOR sidelined), and nothing
   sidelined is dropped (ring accounting).
4. Leaving a session restores flow mode, with the background summary
   replayed once.
5. A FATAL ambient event always reaches the operator, in session or not.
6. Context never exceeds the served window (the structure-aware fit).
7. Every phase transition, promotion and learning event reaches the cockpit
   as live state (a pipeline block), in flow mode and in session mode.

## 7. Open decisions for the operator

- Should a session's change auto-apply when the gates allow it (SAFE_AUTO),
  or always show the diff and wait for Enter? (Claude Code asks by default.)
- Session persistence across daemon restarts: off by default; on?
- A soft cap on concurrent background ops while a session is active (to
  keep foreground latency low), or full background throughput?
