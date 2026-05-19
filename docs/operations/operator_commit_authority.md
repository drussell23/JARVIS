# Operator Commit Authority (OCA) — Operator Runbook

**Status:** OCA arc (Slices 1→4) + `git_index_guard` + `persistent_master` +
`harness_sovereignty_pin` — **CLOSED** (all merged to `main`).
One page. If something blocks a Cursor commit, this page is the fix.

---

## TL;DR daily ritual

Before a Cursor Source-Control commit session, establish operator presence
**once** (any one of these — all compose the same substrate, no shell token):

| Surface | Command | Use when |
|---|---|---|
| Daemon (one-click) | start `commit_authority_daemon` (master ON) → IDE/helper sends `{"verb":"refresh","repo_root":"<repo>","branch":"<b>"}` | IDE has no shell env |
| REPL | `/commit grant --branch <branch>` | inside Serpent REPL |
| CLI | `python3 -m backend.core.ouroboros.governance.commit_authority_cli grant --channel ide --branch <branch> --minutes 480` | terminal |
| Whole-repo arming | `… commit_authority_cli enable` | one marker valid on **every** branch this session |

`enable` mints a **whole-repo** presence entry (matches any branch); a
branch `grant` mints a branch entry. Multi-entry presence means these
**coexist** — a branch grant never clobbers your whole-repo arming, and
no other repo/agent clobbers you. Default presence TTL 900 s
(`JARVIS_COMMIT_PRESENCE_TTL_S`); the rituals above use 8 h.

## After PRs land

```
git checkout main && git fetch origin && git merge --ff-only origin/main
```

If you work on a named branch, fast-forward it to `main` so it runs the
current OCA/sovereignty/presence code (the enforcement only protects code
that *is* the new code).

## Keep Cursor background Agents STOPPED on the operator main checkout

A Cursor background Agent autonomously `git commit`s WIP / duplicate work
onto whatever branch is checked out (observed multiple times — verbose
LLM-prose messages + `[integrity-verified:` trailer). The structural
defenses (sovereignty + presence + multi-entry + daemon) are the durable
system, but **operator policy is still required**: do not run background
Agents on this repo root. The daemon gives one-click grant refresh so you
never need an Agent for the commit ritual.

## What the refusal messages mean

| Message | Meaning | Fix |
|---|---|---|
| `denied_sovereignty — autonomous commit into non-owned tree` | Channel resolved to **autonomous** (no valid operator-presence marker for this repo+branch) and `ledger_sovereignty` is ON for an unmarked tree (the operator main). This is the gate working — it's how a rogue Agent commit is refused. | Run the daily ritual (presence + grant) for the branch you're committing on. |
| `denied_no_grant` | Presence is valid (channel resolved **ide**) but there is no matching unexpired signed grant for this repo/branch/channel. | `/commit grant --branch <branch>` (or daemon `refresh`). |
| `IRON GATE — Commit blocked: <verdict> …` | The pre-commit dispatcher's operator-facing wrapper around any non-authorized verdict. The `<verdict>` token is one of the above — read it and apply the matching fix. | Per the verdict token. |
| `Missing operator cryptographic authorization` | Legacy path: OCA master is OFF and no shell token. | Graduate OCA (`commit_authority_cli enable`) — do not reintroduce the shell-token hack. |

`denied_no_grant` is the *good* failure (presence proven, just needs a
grant — one command). `denied_sovereignty` means presence is absent —
re-run the ritual.

## Recover from Agent branch contamination

If a branch diverged because a background Agent autonomously committed
work already on `main` (verbose-LLM message + `[integrity-verified:`
trailer, byte-identical to `main`):

1. **Confirm zero unique content** (do **not** blind-force):
   ```
   git fetch origin
   git diff --stat origin/main <branch>          # only main-newer commits should differ
   git diff --quiet origin/main <rogue-sha> -- <files>   # exit 0 == byte-identical
   ```
2. Only if every rogue file is byte-identical to `origin/main` and the
   branch has **no unique content**:
   ```
   git checkout <branch> && git reset --hard origin/main
   ```
   This is verified-equivalent cleanup, not blind force. If anything is
   unique, **merge** `main` instead and review the unique commit.
3. Re-establish presence (ritual) — `reset --hard` doesn't touch
   `~/.jarvis/`, but confirm with the verification script below.

Never `reset --hard` without step 1 proving equivalence.

## Verify

```
python3 scripts/verify_oca.py
```

Read-only smoke (no grants issued, no commits, uses throwaway tmp repos):
hook authorizes with the active ritual + no shell token; a presence-less
forged-`ide` context → `denied_sovereignty`; daemon `status` → authorized.
All three PASS = the system is healthy and your ritual is active.

## Key env flags (FlagRegistry-seeded)

| Flag | Default | Meaning |
|---|---|---|
| `JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED` | (env) | OCA master (also via signed persistent-enable record — GUI-reachable) |
| `JARVIS_LEDGER_SOVEREIGNTY_ENABLED` | false (env) OR signed `persistent_master` record | Sovereignty gate; ON ⇒ unowned-tree autonomous commits refused |
| `JARVIS_COMMIT_PRESENCE_TTL_S` | 900 | Operator-presence marker lifetime |
| `JARVIS_COMMIT_PRESENCE_MAX_ENTRIES` | 64 | Multi-entry presence store bound (drop-oldest) |
| `JARVIS_COMMIT_AUTHORITY_DAEMON_ENABLED` | false | Unix-socket daemon master |
| `JARVIS_COMMIT_AUTHORITY_SOCK` | `~/.jarvis/commit_authority/daemon.sock` | Daemon socket (0o600 in 0o700 dir) |
| `JARVIS_COMMIT_AUTHORITY_DAEMON_TIMEOUT_S` | 5.0 | Daemon per-connection timeout |
| `JARVIS_COMMIT_AUTHORITY_ARCHIVE_ENABLED` | false | OCA decision ring + JSONL ledger |
| `JARVIS_GIT_INDEX_GUARD_ENABLED` | false | Missing-`.git/index` advisory rebuild guard |

## Observability

- REPL: `/commit status | recent`
- HTTP: `GET /observability/commit-authority[?limit=N]`
- SSE: `commit_authority_decision_recorded`, `git_index_anomaly`
- Archive ring refs: `c-N` (`/commit recent`)
