# JARVIS Authorization Plugin — screen unlock via SecurityAgent

> ## ⏸️ STATUS: ON HOLD — deliberately not wired in
>
> **Decision, 2026-08-07: JARVIS will not lock or unlock the screen.** The
> infrastructure is built, installed and proven; the final step — placing our
> mechanism into the live unlock chain — is **not being taken**, and this is an
> accepted decision rather than an unfinished task.
>
> The reason is stated in full under [Why it stopped](#why-it-stopped). In short:
> the only right on macOS 26 that can host an unlock mechanism is
> `system.login.screensaver.unlock`, Apple labels it **"Do not modify"**, it has
> `tries: 1` and no password mechanism behind us, and the one remaining unknown
> could only be resolved by putting a real lock screen into a state nobody has
> observed on this OS. The value did not justify that.
>
> **Nothing here affects your ability to unlock your Mac.** The authorization
> database is untouched. See [Current machine state](#current-machine-state).

---

## Table of contents

- [What this is](#what-this-is)
- [The defect that started it](#the-defect-that-started-it)
- [The second defect: a guard inside what it guarded](#the-second-defect-a-guard-inside-what-it-guarded)
- [What was built](#what-was-built)
- [What was proven, and what was not](#what-was-proven-and-what-was-not)
- [Why it stopped](#why-it-stopped)
- [Current machine state](#current-machine-state)
- [How to remove it](#how-to-remove-it)
- [How to resume, if that day comes](#how-to-resume-if-that-day-comes)
- [File map](#file-map)
- [Durable lessons](#durable-lessons)

---

## What this is

A macOS Authorization Plugin — a bundle loaded by SecurityAgent into
`authorizationhosthelper` — that can grant a screen unlock when JARVIS has
independently verified the operator's voice.

Four components, each authorising its peers by **code-signing requirement
computed from the actual signed binary on this machine**. No requirement string
is written down anywhere; a literal would be a claim about a signing identity
nobody has seen.

```
jarvis-unlock-grant      helper. Deposits a short-lived grant.
        │ XPC (deposit service, peer verified by requirement)
        ▼
jarvis-unlock-broker     LaunchDaemon. Holds grants, TTL-bounded.
        │ XPC (consume service, peer verified by requirement)
        ▼
JARVISUnlock.bundle      mechanism. Runs inside SecurityAgent's host process.
        │
        ▼
system.login.screensaver.unlock          ← the step NOT taken
```

The mechanism **never denies**. It either grants or yields, and a yield is
`kAuthorizationResultAllow` — "I am satisfied, continue" — never `Deny`, which
would fail the whole right and lock the operator out of their own machine. There
is deliberately no code path that can emit `Deny`.

---

## The defect that started it

On 2026-08-06/07 this machine authenticated into a **black screen with a live
cursor**, repeatedly, after Touch ID accepted.

`install.sh` **authored** the authorization rule it wrote, from a template
literal in its own body:

```xml
class      = evaluate-mechanisms
mechanisms = ( JARVISUnlock:grant,privileged, builtin:authenticate,privileged )
```

and wrote that over `system.login.screensaver`, whose stock definition is:

```xml
class = rule
rule  = use-login-window-ui
```

**`use-login-window-ui` is a delegation, not an authenticator.** It hands the
right to **loginwindow**, which owns the lock screen: the wallpaper, the clock,
your avatar, the password field, and the session resume that re-attaches
WindowServer after a successful authentication. An `evaluate-mechanisms` chain
only **authenticates**. Nothing in it paints a panel and nothing in it resumes a
session — so the machine authenticated into nothing, and WindowServer kept
drawing the cursor on its own plane.

### The evidence had been spent on a different configuration

The risk was known. `probe_screensaver_rule.sh` existed specifically to measure
it — and it measured the wrong thing. It swapped the right to
`authenticate-session-owner-or-admin`, which is **`class: user`**: a
SecurityAgent-evaluated rule with **no `mechanisms` array at all**. It confirmed
Touch ID and the password survived, and closed by advising that the plugin was
therefore viable *"against evaluate-mechanisms"*.

Two different configurations. The one that was measured worked. The one that was
installed black-screened the machine.

> **A measurement of one shape cannot be spent on another** — not by a comment,
> not by an argument about equivalence, and not by a flag.

### Nothing noticed

Every check in `verify.sh` was **static** — hashes and plist keys. All of them
stayed green throughout, because the chain they found *was* coherent. It simply
had nothing to draw with.

---

## The second defect: a guard inside what it guarded

Independently, `authorizationhosthelper` **segfaulted 27 times across two days**
while every static check reported the installation healthy. Twelve reports were
parseable; **all twelve** landed in our delivery path (8 × `JARVISDeliver`,
4 × `os_log_type_enabled` called from inside it).

The mechanism armed an uncancellable `dispatch_after` and kept its
"already delivered" `atomic_flag` **inside the `calloc`'d `JARVISMechanism`**.
SecurityAgent calls `MechanismDestroy` — freeing that allocation — the instant
the chain advances past us, and every escaping callback can still fire
afterwards. So reading the flag to ask *"did I lose the race?"* **was** the
use-after-free.

> **A guard placed inside the thing it guards is not a guard.**

Fixed by **ownership, not defensiveness**: `JARVISDelivery` is an ARC object every
racer co-owns strongly, so its memory cannot go away while any callback can still
run — by construction. Blocks capture `self` **strongly on purpose**: a weak
capture would trade the crash for a hang, with `SetResult` never called and the
chain stalled forever — the same black screen reached through `nil` instead of
through a segfault.

A dead host is strictly worse than a mismatch. A mismatch fails **closed** to a
password prompt. A host that dies before `SetResult` means *nothing in the chain
ever answers* — including the `builtin:authenticate` that the install had
carefully kept behind us, which is never reached.

---

## What was built

Five layers. The first four stop the installer; the fifth stops the *machine*
from staying broken.

### 1 · Gate 7a — ask Apple's schema, about the stock definition

Reads `/System/Library/Security/authorization.plist` and refuses any right whose
**stock** class is not `evaluate-mechanisms`.

Consulted against the **stock** definition, never the live one: after a
conversion the live rule *is* a mechanism chain, so a live-rule check would
cheerfully ratify its own damage on every reinstall.

### 2 · Gate 7b — derive, never author

The rule written is a **byte copy of the incumbent** with `mechanisms.0`
inserted. `tries`, `shared`, `allow-root`, `version` and anything a future macOS
adds now survive; the template literal silently dropped all of them and invented
values of its own. Idempotent, and it verifies the result differs from the
incumbent by exactly one entry in exactly one position.

### 3 · Gate 7c — a shape may only be written if that shape was measured here

`probe_*` records a **shape identity** (`class=…;mechanisms=a|b|c`) computed from
the rule the authorization engine was *actually left holding*, not from what was
requested. `install.sh` matches on it as a whole whitespace-delimited field, so a
shape that merely *starts with* another cannot borrow its evidence.

**There is no override flag**, on purpose. A flag is the shortest path back to
spending one configuration's evidence on another.

Shapes naming our mechanism must additionally clear **`failopen=proven`** —
see [Why it stopped](#why-it-stopped).

### 4 · The login-rights guard

Lives at `jarvis_authdb_write`, the single seam every mutation passes through —
not at four call sites with four chances to be missed by the fifth.

> Refuse to write a rule **naming our mechanism** into a right whose stock chain
> runs a **`loginwindow:`** mechanism.

Both halves are load-bearing:

- **"naming our mechanism"** — restore and strip still pass. A safety check that
  blocks the recovery path strands someone at a lock screen.
- **`loginwindow:` prefix, not the substring `login`** — our own target's sole
  mechanism is `CryptoTokenKit:login`. A guard that bans its own target gets
  removed by the first person it inconveniences.

Derived, not listed, so a login right shipped by a future macOS is protected on
day one:

| right | stock chain runs `loginwindow:` | |
|---|---|---|
| `system.login.console` | yes | **protected** |
| `system.login.filevault` | yes | **protected** |
| `system.login.fus` | yes | **protected** |
| `system.login.screensaver.unlock` | no — `CryptoTokenKit:login` | allowed |
| `system.restart`, `system.disk.unlock` | no | allowed |

A right absent from the schema is refused outright: unknown provenance is not a
yes.

### 5 · The sentinel — the machine cannot stay in an unsanctioned state

The four gates stop the *installer*. Four roads to a black screen stay open after
a **correct** install:

1. the mechanism starts crashing its host
2. an OS update rewrites the right *(what "Do not modify" is about)*
3. the bundle goes away while the chain still names it
4. someone edits the rule by hand *(how this machine got here)*

All four end the same way: the machine sits broken until a human notices.

The sentinel inverts that. **The default state is safe and JARVIS has to keep
earning its place.** A LaunchDaemon watching `SecurityAgentPlugins`,
`DiagnosticReports` and `/var/db/auth.db` checks five things — bundle present, no
crash reports *naming us*, live shape equals the sanctioned one, chain still
contains everything the backup had, right still a mechanism host — and pulls our
mechanism out if any fail. No prompt, no human.

**No resident daemon, and therefore no `KeepAlive`.** launchd's `WatchPaths` is
kqueue-backed, so the script is *invoked* on a change rather than polling for
one. Idle cost is not near-zero — it is **zero**, because between events there is
no process. That answers *"what if the sentinel itself crashes"* at the root
rather than with restarts: a thing that is not running cannot crash.

Its only write is a **revert**, through the same function `uninstall.sh` uses. A
sentinel that could put the mechanism back would be a second installer with no
gates in front of it, and a test asserts it cannot.

Urgency is graded: a vanished bundle or a crashing host repairs **immediately,
even mid-authentication**, because an active lock screen is exactly when a stuck
user needs it. Shape drift defers while SecurityAgent is running — but only for a
bounded number of passes, because a guard that waits for the guarded system to go
idle is a deadlock.

---

## What was proven, and what was not

All measurements on **macOS 26.6.1 (25G76)**, 2026-08-07.

| # | Claim | Result |
|---|---|---|
| 1 | The build is coherent; every requirement derives from a real signature | ✅ `make verify` green |
| 2 | The XPC channel works and signatures are accepted by a **live peer** | ✅ `self-test PASSED` — grant `EFFBFA33-ABDA-465C-8D27-2C7B39A0B01F` |
| 3 | The sentinel arms, runs, and leaves no resident process | ✅ `runs=1 · exit=0 · state=not running`, 2 × watch active |
| 4 | Every component agrees about every peer | ✅ `verify.sh` → *coherent, and deliberately not wired in* |
| 5 | **The use-after-free class is closed** | ✅ **25 lifecycles, 25 yields, 0 host crashes** |
| 6 | A yield still reaches a password prompt on `tries: 1` | ❌ **NEVER MEASURED** |

### On claim 5

`probe_mechanism_lifecycle.sh` created a right of our own invention
(`com.jarvis.probe.lifecycle`, in our own reverse-DNS namespace, consulted by
nothing), put our mechanism in it **alone**, and invoked it 25 times with
`security authorize` — then removed it and proved it was gone.

The ratio is the result, not the absence of crashes:

```
authorize returned ok : 25     yields observed : 25     host crashes : 0
```

**25 invocations, exactly 25 yields.** The use-after-free was a *race* — one
racer freeing what the other still read. Twenty-five consecutive full
`Create → Invoke → SetResult → Destroy` cycles delivering exactly one result
each, with the host alive at the end of every one, is the ownership rewrite doing
precisely what it was designed to do, inside the process that died 27 times.

`system.restart` was considered as a venue and **rejected**: invoking it runs
`RestartAuthorization:restart`, and whether that mechanism merely *checks* or
actually initiates a restart is not something to learn by being wrong about it.

### On claim 6 — the one that stopped everything

Never measured, and **not measurable without risking the lock screen.**

---

## Why it stopped

`system.login.screensaver` (`class: rule` → `use-login-window-ui`) delegates to
loginwindow, which internally evaluates **`system.login.screensaver.unlock`** —
and *that* is a real mechanism chain, already shipping `CryptoTokenKit:login`,
which is how smartcard unlock works. It is the supported host, one level below
the delegator the original design was destroying.

The architecture was right. The insertion point was one level off.

But that right has three properties that together ended it:

| | |
|---|---|
| **`tries: 1`** | One attempt. Nothing retries on our behalf. |
| **no `builtin:authenticate` in the chain** | The old design put the stock authenticator directly behind us, so *"we yield and it prompts"* was a property of the chain. **Here it is not.** Whatever password path exists belongs to loginwindow, **outside** the chain. |
| **Apple's comment: `"Do not modify."`** | Unsupported, and an OS update may rewrite it without notice. |

Whether a yield reaches a prompt is therefore **not derivable from the
configuration**. It is a runtime property of loginwindow, and the only instrument
that can answer it is `probe_screensaver_rule.sh` — which must write our
mechanism into the live unlock chain and hold it there while the operator locks
their real screen.

That probe is well-defended: dead man's switch armed *before* the write,
Option-key panic bypass, the sentinel watching `/var/db/auth.db`, and a verdict
read from crash reports and the unified log rather than from the operator. The
worst realistic case is a hard power cycle — `system.login.console` is untouched,
so a reboot always reaches a normal login window.

**It was still a lock screen in a state nobody has observed on macOS 26, for a
convenience feature.** That trade was declined.

### What survives the decision

The grant-broker architecture is proven and does **not** require the lock screen.
The lifecycle probe demonstrated the whole chain working through a right *we
invented*. Authorising JARVIS's own privileged actions through a
`com.jarvis.*` right that nothing in macOS consults carries none of the risk
above, cannot black-screen anything, and no OS update can break it.

---

## Current machine state

As of 2026-08-07, after `install.sh --skip-authdb`:

```
✓ /Library/Security/SecurityAgentPlugins/JARVISUnlock.bundle
✓ /usr/local/libexec/jarvis-unlock-broker        (running)
✓ /usr/local/libexec/jarvis-unlock-grant
✓ /Library/LaunchDaemons/com.jarvis.unlockbroker.plist
✓ /Library/LaunchDaemons/com.jarvis.unlocksentinel.plist
✓ /usr/local/libexec/jarvis-authplugin/          (recovery scripts)

system.login.screensaver.unlock:  UNTOUCHED
    class=evaluate-mechanisms  mechanisms=[CryptoTokenKit:login]
    modified == created   ← never written
```

**SecurityAgent is not consulting anything of ours.** Files on disk and a daemon
running; the unlock path is exactly as macOS shipped it.

Recovery scripts install to `/usr/local/libexec/jarvis-authplugin/` rather than
running from the repository: a machine that will not unlock is a bad time to
discover the checkout lives in a cloud-synced directory that has not mounted.

---

## How to remove it

```bash
sudo /usr/local/libexec/jarvis-authplugin/uninstall.sh
```

Order matters and is enforced: the authorization rule is restored **first** —
that is what unwedges a machine — then the sentinel is disarmed, then the broker,
then the bundle. Removing the bundle first would leave a rule pointing at a
mechanism that no longer exists, which is strictly worse than either the
installed or the stock configuration.

Safe to run twice, half-way through a failed install, or when nothing is
installed at all. Runs from a Recovery Terminal or a bare SSH session, and
depends on no Python, no virtualenv, and no repository.

---

## How to resume, if that day comes

The ladder, in order. Each rung is cheap; only the last has teeth.

```bash
cd backend/voice_unlock/authplugin

sudo ./install/install.sh --dry-run          # mutates nothing
sudo ./install/install.sh --skip-authdb      # everything except the rule
sudo ./install/verify.sh                     # coherence + crash history
sudo ./install/probe_mechanism_lifecycle.sh  # the UAF class, zero stakes
sudo ./install/probe_screensaver_rule.sh     # ← the only rung with risk
sudo ./install/install.sh                    # refuses without failopen=proven
```

**Before the last two:**

1. `sudo systemsetup -setremotelogin on` — and **verify SSH from another device
   first**. An untested escape hatch is not an escape hatch. *(It was OFF when
   this was written.)*
2. Save all work. The realistic cost of a bad run is unsaved documents, not a
   lost machine.
3. Daylight, no time pressure, second machine already connected.

Consider re-pointing `JARVIS_AUTH_RIGHT` at a `com.jarvis.*` right instead and
taking none of this risk.

---

## File map

```
Makefile                      build + `verify` (runs the self-tests; install.sh
                              calls this as step 1, so the gates cannot rot)
src/
  JARVISUnlockMechanism.m     the mechanism. Ownership-based race arbiter.
  JARVISUnlockBroker.m        LaunchDaemon holding TTL-bounded grants
  jarvis_unlock_grant.m       depositing helper
  JARVISGrantProtocol.[hm]    shared XPC protocol + verdict vocabulary
  JARVISUnlockConfig.[hm]     Info.plist config incl. the panic key
install/
  common.sh                   ALL shared logic: rule shape, composition,
                              classification, the login guard, revert,
                              dead man's switch, crash + log evidence
  install.sh                  ordered install; gates 7a/7b/7c live here
  uninstall.sh                recovery. Written before the plugin, on purpose.
  verify.sh                   static coherence + rule shape + crash history
  sentinel.sh                 launchd-invoked health check (layer 5)
  probe_mechanism_lifecycle.sh   synthetic right; proves the UAF fix
  probe_screensaver_rule.sh      the real right; the rung not taken
  test_rule_shape.sh          106 self-tests, run by `make verify`
```

---

## Durable lessons

Each of these cost a session.

**A guard placed inside the thing it guards is not a guard.** The `atomic_flag`
lived in the allocation whose lifetime it was policing, so reading it *was* the
use-after-free.

**A measurement of one shape cannot be spent on another.** `class: user` was
probed; `evaluate-mechanisms` was installed.

**Never author a rule — amend one.** A template literal cannot know what it is
destroying, because it never read it.

**Ask the stock schema, not the live state.** After a conversion the live state
*is* the damage, so a live check ratifies it.

**Match the prefix, not the substring.** `CryptoTokenKit:login` contains
`login`. (`"lock" in "unlock my screen"` is also `True`.)

**A safety check must never block the recovery path.** Scope guards to what adds
risk, never to what removes it.

**A check that reports the safe state as a fault, and names the dangerous action
as its remedy, is worse than no check.** `verify.sh` said *"partially installed,
1 component absent"* with everything green — the "absent" thing was the rule
being deliberately unwired — and prescribed `install.sh`, the one irreversible
step. All 99 tests passed while that was live. **It took running it.**

**Static checks cannot see a runtime death.** 27 crashes, every check green.

**A watchdog that shares a resource with the system it guards is not a
watchdog** — including a state-ledger. The correct answer to "it got severed
mid-operation" is a larger *static* margin, never an activity-gated waiver.

**The strongest guarantee is not a better gate; it is a system that cannot stay
in a bad state.** Gates stop the installer. Only the sentinel stops the machine.
