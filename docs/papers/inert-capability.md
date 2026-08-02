# Inert Capability: Measuring Whether an Autonomous Agent's Own Features Are Reachable

**Derek J. Russell**
Independent · `github.com/drussell23/JARVIS-AI-Agent`

*Draft — 2026-08-02. All measurements taken on the date shown against the commit named in §6.*

---

## Abstract

Self-modifying coding agents are evaluated on whether their changes raise a score.
We report a failure mode that scores cannot see: an agent ships a feature that is
syntactically present, type-correct, test-covered, and **never executed**. We call
this an *inert capability*. Over a twelve-month, 10,052-commit, 3.64M-line
autonomous development project we observed inert capabilities arriving faster than
they were noticed, including five in a single day — one of them inert *inside the
patch written to fix inertness*. Three tests asserted that a handler's source
contained a feature flag's name; they passed for the entire period the feature was
dead.

We contribute (i) a four-class taxonomy of inertness distinguished by *where* the
circuit opens, (ii) three static instruments that measure the first three classes on
a live tree, pinned by 123 tests, and (iii) measurements from a real self-modifying
system. We also report the instruments' own failures — two detector versions that
were wrong before one was right, a retracted finding, and a fourth-class detector
that was never test-pinned and has since been lost — because an instrument for
measuring dead code is worth little if it cannot survive its own criterion.

We claim no generality. Every number here is N=1, self-audited. The contribution we
believe transfers is the taxonomy and the instruments; the obvious next experiment is
to run them against agent-authored code we did not write.

---

## 1. The failure that benchmarks cannot see

A self-improving agent that rewrites its own code is judged by whether the rewrite
raises a benchmark score [DGM; SICA]. That framing assumes the change *runs*. The
assumption is load-bearing and, in a large autonomous codebase, frequently false.

A concrete instance from our tree. A verb `/narrate verbose` was implemented to
raise narration density. It parsed its argument, set the environment variable
`JARVIS_NARRATIVE_THINKING_VERBOSE`, and reported success. Repository-wide, **no
code read that variable.** The verb worked in the sense that every line of it
executed correctly and the operator was told it had worked. It had no effect.

What makes this a *measurement* problem rather than a bug is how it survived. Three
tests covered the feature. All three asserted that the handler's **source text**
contained the flag's name — for example
`test_narrate_verbose_enables_thinking_surfacing`, which established that verbose
mode worked by confirming that the handler mentioned a flag nothing read. The tests
never called the handler and never observed a consumer. They passed continuously
throughout the period the feature did not work, and they would have kept passing
forever.

An agent optimizing a benchmark has no gradient toward finding this. An agent
optimizing *test pass rate* is actively pushed away from finding it: source-assertion
tests are cheap to write, cheap to satisfy, and indistinguishable from behavioural
tests in any aggregate.

## 2. Definition and taxonomy

**Inert capability.** Code that is present, parseable, and covered by passing tests,
but for which no execution path exists from any entry point of the shipped system.

Inertness is not one condition. It is at least four, distinguished by *where the
circuit opens*. The distinction matters because each class requires a different
instrument, and because an instrument for one class returns CLEAN on the others —
which is exactly how a subsystem can pass every audit and still be dead.

| # | Class | The open edge | Instrument |
|---|-------|---------------|------------|
| 1 | **Import-dark** | Nothing imports the module | import-graph reachability |
| 2 | **Entry-unreachable** | Imported, but no path from any entry point | entry-closure reachability |
| 3 | **Handoff-dropped** | Value passed across a call boundary; callee never reads it | handoff consumption |
| 4 | **Producerless sink** | Consumer wired correctly; nothing ever produces into it | producer/consumer pairing |

Class 4 is the one we found last and the one that most resembles working software.
The module `anticipation_surface.py` passed all three earlier instruments — it is
imported, its hooks are consumed, it is entry-reachable — while its ingest methods
`record_banner` and `record_prefetch` had no call sites outside docstrings. The
surface could only ever render an empty ring. **Three green instruments and a dead
feature.**

A fifth condition is *not* inertness and must be excluded, or the taxonomy generates
false alarms. A subsystem whose writers are unwired **and** whose readers are gated
default-false is not broken; it is coherently ungraduated. We initially misread 13
such modules as an open circuit because we checked only the producer side. The rule
that resolves it: *check the reader's flag default before calling a circuit open.*

## 3. Instruments

Each instrument is static (AST over the tracked tree), fail-soft, and pinned by
tests in the same repository it audits.

### 3.1 Import reachability — `progress_board`

Walks the source tree, extracts declared feature flags, and classifies each by
whether the module declaring it is imported anywhere: `live`, `entry`, `off`,
`dark`, `dynamic`.

Three defects in the walk had to be fixed before its numbers meant anything, and we
report them because two inflated results in the flattering direction:

1. Nested checkouts (git worktrees) were enumerated as source, adding 7,382 files of
   stale duplicates.
2. **Overlapping scan roots.** The root set contained `.` *and* `backend` *and*
   `scripts`; 3,810 of 14,830 files were parsed twice and every import in them
   counted twice, so importer counts under those roots were approximately double.
   A comment at that site claimed deduplication happened. It was false — one
   collection was deduplicated and the adjacent one was not.
3. `rglob` cannot prune, so excluded trees were enumerated and then discarded.

The fix uses `os.walk(topdown=True)` with in-place `dirnames[:]` pruning and a
`seen` set. Worktrees are excluded **structurally**, not by name: a directory
containing `.git` (file *or* directory, via `exists() or is_symlink()`) is a checkout
in its own right, which catches submodules and vendored clones for free.
`followlinks=False` is deliberate — a symlink to an ancestor is an infinite walk.

Result: 18,637 → 7,221 files, 303.5s → 70.0s (4.3×). Roughly 163 vanished flag rows
were flags that existed *only* in stale worktree copies.

### 3.2 Entry-point closure — `surface_reachability`

Computes the transitive closure from derived entry points (`pyproject` scripts,
`__main__` guards, shell-invoked modules) and reports modules outside it.

Its central doctrine is that **asymmetry is evidence, never a conclusion.** A module
reachable from one surface and not another is a question, not a defect: our system is
two processes (a daemon that renders and a client that mirrors), so daemon-only
reachability is usually correct. Reporting asymmetry as failure produced noise;
reporting it as a prompt produced findings.

This instrument states its own blind spot in its docstring: *"Reachability sees
imports; it cannot see that a KeyBindings object was handed across."* That sentence
is what motivated §3.3.

### 3.3 Handoff consumption — `capability_handoff`

Measures whether a value passed across a call boundary is **read**. Design decisions,
each of which we got wrong first:

- **Sinks are discovered by shape, not by name or annotation** — any function with
  ≥ *N* keyword-only parameters (default 8). A name heuristic (`build_*`) misses
  renamed builders; an annotation heuristic misses everything, because the real hooks
  in our tree (`completer`, `history`, `auto_suggest`, `turn_spinner`) are all
  annotated `Any`.
- **Forwarding is not consumption**, resolved transitively with a cycle guard. A
  pass-through wrapper is not a defect; a drop *behind* one still is.
- **`waived("reason")` returns `None`** — runtime-identical to omitting the argument,
  statically a decision recorded at the site where it was made. This mirrors a rule
  we adopted elsewhere: *silence and declined are different facts.*
- **`del param` is a declared drop.** Python already has the sentence; no decorator
  or registry is needed.
- **Divergence, not "unfilled", is the signal.** Reporting every unfilled optional
  produced 102 rows of which 4 mattered. Reporting only *surfaces that disagree*
  produced 4 rows of which 4 mattered.
- **Never `**splat` a mount.** A splatted call reads as OPAQUE, and the audit goes
  blind precisely where composition is densest.

### 3.4 The instrument we did not pin

The class-4 detector was written, found 64 producerless ingest methods, and was left
in a scratchpad directory with a note that it *"should be promoted into
`tests/battle_test/` or it will rot."*

It was not promoted. **It is gone.** We report this as a result rather than omitting
it: an instrument that measures dead code and is not itself pinned by a test is
subject to its own criterion, and ours failed the test within four days.

## 4. Measurements

Live readings, 2026-08-02, on the tree described in §6.

**System under audit.** 7,401 tracked Python files; 3,644,449 lines (backend
2,295,367 / tests 1,073,739 / other 275,343); 58,379 test functions; 10,052 commits
from 2025-08-13 to 2026-08-01.

**Import reachability.** 7,230 files scanned in 60.8s → 4,378 flag rows:
3,776 `live`, 290 `entry`, 182 `off`, **126 `dark`**, 4 `dynamic`; 126 actionable
across 58 modules.

A finding that only appears once the classes are separated: **all 126 dark flags are
tuning knobs; none is a master switch.** No feature in this system is dark *and*
enabled behind its own front door. The dark surface is 58 modules' worth of
parameters, not 58 dead features — a materially less alarming reading than the raw
count, and one the raw count cannot express.

**Entry-point closure.** 149 modules scanned, 146 resolved, 151 entry-reachable from
11 roots; 79 asymmetric; **1 orphan**.

**Handoff consumption.** 8 sinks, 103 hooks, 3 surfaces, 121 fills, 0 unparseable;
**0 dropped, 0 divergent**, 9 declared waivers, 8 unset. This is the post-repair
state; the ratchet asserts `dropped() == []` over the real tree, together with a
guard that the primary sink is still among those discovered — otherwise a refactor
could empty the audit and turn the ratchet green by measuring nothing.

**Source-assertion rate.** 3,404 test files, 58,188 test functions:
53,274 behavioural, 171 structural pins, **930 source-only**, 3,813 with no
assertion. Against the 54,375 tests that assert at all, the source-assertion
rate is **1.71%** — 930 tests that cannot fail for a behavioural reason. Of
those, **54 assert an environment-variable-shaped literal into source text**,
which is the `/narrate verbose` class exactly: a test naming a contract it never
exercises.

The rate is small, and that is worth stating plainly rather than spinning: 98.3%
of asserting tests in this repository can observe an effect. The number matters
because of *where* the 930 sit, not their share — they are concentrated in the
structural-guard tests that a self-modifying system writes about itself, which is
precisely the population that must not be self-confirming.

**Test pinning.** 70 / 15 / 38 / 24 tests respectively (147 total).
Class 4: zero.

### 4.1 What the instruments found

- **`search_rows` dropped.** Accepted by the layout builder, forwarded by the REPL
  entry point, resolved by a real provider — and read by nothing. Transcript search
  was dark on the shipping client. A truncated comment at the site
  (`"The search bar sits DIRECTLY above the prompt"`) records an edit that lost the
  code and kept the comment.
- **`on_mux` dead on the builder.** Zero loads. This one falsified the author's own
  plan: a recommendation to "pass `on_mux` in the demo so it demonstrates the real
  mechanic" would have silently done nothing.
- **Directional coverage gap.** The daemon filled 7 of 18 cockpit hooks; the client
  filled nearly all. The *direction* was the finding — the daemon is the **source**
  of the state it was not displaying. Repaired to 12 of 18; the remaining 6 are
  honest gaps, deliberately left unwaived so the audit keeps reporting them.
- **A silent field rename.** `Panic.traceback_text` versus the renderer's expected
  `"traceback"`. A `__dict__` passthrough renders the fatal-error overlay with an
  empty traceback: the alarm fires and drops the only useful part.
- **A mounted strip with no key to open it** — a search bar nothing could open,
  which would have shipped as a *closed* gap.

### 4.2 Findings against the instruments themselves

- **Two wrong detectors before one right one.** Excluding same-module callers flagged
  legitimate self-driven hooks; counting only `ast.Call` nodes flagged
  reference-passing (`on_telemetry=ui.on_telemetry`). The fix was to reuse
  `capability_handoff`'s existing read-versus-forward distinction rather than invent
  a third notion of use.
- **Three wrong versions of the source-assertion detector.** Its first reading was
  3.20%; the published 1.71% is what survived three corrections, each found by
  *reading flagged tests* rather than by any checker. (i) Binding every
  context-manager target as source text made `with PtySession(...) as s` and
  `with console.capture() as cap` look like source reads, so PTY and render tests
  reported as source-only. (ii) A bare `.read()` matches a pipe, socket, or PTY
  drain — runtime output, which is the *opposite* of a source assertion.
  (iii) `state_file.read_text()` reads a JSON artefact the test itself wrote; that
  observes an effect. The discriminator that resolved (iii) is that a genuine
  source assertion addresses a *module*: both confirmed true positives reach their
  target through `inspect.getsource` or a path built from `__file__` with a `.py`
  literal. Every correction moved the number **down**, so the instrument's error
  was systematically in the alarming direction.
- **A retracted finding.** We reported that 11 dark rows were instrument error, on
  the basis that a registry module named them. It was false: the list was
  `_SUBSTRATE_EXCLUSIONS` — the modules the registry **refuses** to mount. The rule
  we extracted: *being named by a registry is not being mounted by one.* Net false
  positives from the class after retraction: zero.
- **Prose false positives, four times.** Structural tests matched text inside
  docstrings, because good docstrings name the functions they describe. Generalized
  fix: strip docstrings via AST and compare *statement positions*, not string offsets.
- **A severity-1 finding produced by the retraction.** An adaptive-threshold consumer
  is enabled by default and reads a YAML file. Exactly one function writes that file,
  and its only two callers are an unreferenced function and an HTTP route mounted
  nowhere. The human-in-the-loop path we had described as "closed end to end" is not
  closed. Benign in effect — the consumer falls back to baseline — but the adaptive
  path is decorative.

## 5. Related work

**Self-modifying agents.** The Darwin Gödel Machine, SICA, the Huxley-Gödel Machine
and MOSS all have an agent edit its own implementation. All but MOSS anchor the
feedback loop to a benchmark score. MOSS moves to a production substrate and gates
promotion behind plan review, code review, runtime verification on ephemeral trial
workers, and user consent; it explicitly notes that a production substrate has *"no
clean benchmark score to anchor evolution against."* None of these measure whether a
shipped change is reachable — a change that is inert *is* score-neutral, so a
score-anchored loop cannot distinguish inert from useless.

**Evidence-gated lifecycles.** Proof-or-Stop treats agent output as a *claim* rather
than as state, admitting lifecycle transitions only on evidence bound to content
hashes of the current source, with tiered evidence quality and human escalation. It
is the closest published relative of the gating discipline our instruments feed. It
does not address self-modification, and its evidence concerns whether work was
*done*, not whether the resulting code is *reachable*. The two are complementary:
inertness is a property an evidence gate could require proof against.

**Epistemic integrity.** A growing line of work audits whether an agent's *claims
about the world* are grounded — claim-level auditability for research agents,
integrity benchmarks reporting fabrication rates. Our concern is the reflexive case:
whether the system's claims about **its own capabilities** are grounded. We are not
aware of prior work measuring this.

**The named gap.** A 2026 survey of 1,250 RSI papers identifies *governance-grade
measurement of self-improvement* as the field's most underpopulated niche, and names
self-confirming loops as a principal failure mode. A source-assertion test that
"proves" a dead feature works is a minimal, fully mechanical instance of a
self-confirming loop, and it is measurable.

## 6. Reproduction

Commit: `e30ddb7de5`, branch `feat/sovereign-cross-lane-pin`.
Instruments: `battle_test/progress_board.py` (first commit 2026-07-28, `c6df9fa9a4`),
`battle_test/surface_reachability.py` (2026-07-28, `b83429800c`),
`ui/capability_handoff.py` (2026-07-29, `ebe86da65b`).
Tests: `tests/battle_test/test_{progress_board,surface_reachability,capability_handoff}.py`.

## 7. Limitations

**N=1, and self-audited.** Every measurement is from one codebase, by instruments
written by the same author, in the same repository. Nothing here establishes that the
rates generalize. The taxonomy and the instruments are what we believe transfer; the
numbers are an existence proof, not a distribution.

**Static analysis under-approximates.** Dynamic dispatch, registry lookup by string,
and `eval` are treated as OPAQUE — never pass, never fail. A framework that wires
itself by reflection is invisible to all four instruments.

**No causal claim.** We do not show that agent-authored code is *more* inert than
human-authored code. We have no human-authored control. It is plausible that
inertness is an old problem that autonomy merely accelerates; we cannot separate
those.

**Class 4 is unmeasured at present**, per §3.4.

**Repair is not attributed.** Several findings were fixed in the same sessions that
found them, so we cannot cleanly separate detection benefit from ordinary review.

## 8. What would settle it

The instruments should be run against agent-authored repositories the authors did not
write — the output of open self-improving agents is the natural corpus, since it is
public, dated, and generated under exactly the incentive we claim is blind to this
failure. Two measurements would be decisive:

1. **Inert-capability rate** for agent-authored versus human-authored changes in the
   same repository, which requires only commit authorship and the class-1–3
   instruments.
2. **Source-assertion test rate** — the fraction of tests that assert on source text
   rather than behaviour. This is mechanically detectable, it is the mechanism that
   hid every instance we found, and to our knowledge nobody else reports it. We now
   report ours (§4): **1.71%**, with 54 tests asserting a flag name into source. The
   instrument is `battle_test/source_assertion_audit.py`, pinned by 24 tests, and it
   runs against any Python tree in about 25 seconds — so this number is cheap for
   others to produce, which is the property that would make it a comparable.

We can no longer say the second number is unknown for our own tree, only that it is
unknown for everyone else's. That is the experiment we are asking for: if 1.71% turns
out to be high, this paper is a bug report about one codebase; if it is ordinary or
low, then the interesting quantity is the *concentration* we observed — that
source-only tests cluster in exactly the structural guards a self-modifying system
writes about itself — and that is a benchmark.

---

### Notes for revision

- **Not yet cited properly.** Fill in arXiv IDs: DGM (2505.22954), SICA (2504.15228),
  Huxley-Gödel (2510.21614), MOSS (2605.22794), Proof-or-Stop (2607.14890), RSI
  survey (2607.07663), claim-level auditability (2602.13855).
- **Decide the framing.** "Inert capability" is the coined term; alternatives
  considered and rejected: *dead feature* (collides with dead-code elimination),
  *unwired capability* (implies a fix rather than a measurement).
- **Before posting:** an arXiv preprint is public disclosure. If any patent position
  is wanted on the governance kernel or routing economics, file a provisional first.
- **Venue:** NeurIPS 2026 *Managing Agents that Manage Agents* lists "honest analyses
  of how self-improving agents drift or game their own reward" as an accepted topic
  and asks, verbatim, *"who evaluates and oversees an agent that builds or improves
  another agent, or itself?"* — verify the deadline.
