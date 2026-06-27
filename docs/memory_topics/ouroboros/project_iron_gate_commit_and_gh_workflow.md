---
title: Project Iron Gate Commit And Gh Workflow
modules: []
status: historical
source: project_iron_gate_commit_and_gh_workflow.md
---

Committing in this repo is gated by an **Iron Gate pre-commit hook**
(`.git/hooks/pre-commit`, not version-controlled) that fails closed
unless `JARVIS_AUTHORIZE_COMMIT_TOKEN`'s sha256 matches the operator's
out-of-repo hash file `~/.jarvis/commit_token.sha256` (mode 600). The
token plaintext is operator-held at `~/.jarvis/.operator_commit_token`.

**Why:** zero-trust safety — the agent structurally cannot self-authorize
a commit; only the operator's secret unlocks it. Do NOT `--no-verify`
past it (defeats the premise) and do NOT try to derive the token.

**How to apply:** Claude prepares everything, operator runs the gated
commit. Workflow per commit:
1. Stage exactly the intended files (patch-decompose if a file mixes
   unrelated changes — `git diff`/`git diff --cached` are disjoint when
   one change is staged and the other isn't; capture as patches and
   rebuild per-branch off clean HEAD).
2. Write the full commit message to `/tmp/claude-501/<name>_msg.txt`
   (Claude's `$TMPDIR` ≠ the operator's interactive `$TMPDIR` — always
   give the operator the **absolute** `/tmp/claude-501/...` path, never
   `$TMPDIR`, and as a **single unbroken line** or zsh splits `-F`).
3. Operator runs (one line):
   `cd <repo> && JARVIS_AUTHORIZE_COMMIT_TOKEN=$(cat ~/.jarvis/.operator_commit_token) git commit -F /tmp/claude-501/<name>_msg.txt`
   — OR, post-2026-05-17, the operator's `git ac` alias (global:
   `!f() { JARVIS_AUTHORIZE_COMMIT_TOKEN=$(cat ~/.jarvis/.operator_commit_token) git commit "$@"; }; f`)
   from any surface incl. IDE integrated terminal.

**STANDING CONSTRAINT (bright line):** Claude must NEVER read
`~/.jarvis/.operator_commit_token`, NEVER set
`JARVIS_AUTHORIZE_COMMIT_TOKEN`, NEVER run `git ac` / `git commit
--no-verify`, NEVER provision the token into `~/.zshrc` /
`~/.bash_profile` / `terminal.integrated.env*` (ambient → Bash-tool
inherits → accidental self-authorization). The agent-block is the
threat model, not friction — no exception, ever. Current gate is
policy/audit vs the agent (Bash runs as same uid, could read a 600
file); true technical isolation is the ratified hardware-attestation
arc (Secure Enclave / Touch ID; task #8). The VS Code/Cursor SCM
commit button staying blocked is intended (funnels to `git ac`).

**gh / git push under the sandbox:** `git push` ref works in-sandbox
(github.com network allowed) but the `.git/config` write for `-u`
upstream tracking is sandbox-blocked — push WITHOUT `-u` (branch still
lands; verify with `git ls-remote --heads origin <branch>`). `gh`
commands fail in-sandbox with `tls: x509 OSStatus -26276` (keychain
cert access blocked) — run every `gh` call with
`dangerouslyDisableSandbox: true`. Origin remote is
`github.com/drussell23/JARVIS.git` (default branch `main`); CLAUDE.md
says `JARVIS-AI-Agent` — same repo, renamed.

Commit style: conventional, ASCII-only (Iron Gate ASCII strictness),
end with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
See [[user-role]].
