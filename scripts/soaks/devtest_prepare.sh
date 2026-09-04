#!/usr/bin/env bash
# Build the isolated worktree the baseline development test runs in.
#
# A bare `git worktree add` is NOT enough. Three artifacts the chain needs
# are GITIGNORED and live only on this box, and each one missing fails
# SILENTLY -- the run boots, does nothing, and looks like a negative result:
#
#   .env                          HMAC secret + every chain flag
#   .jarvis/roadmap.yaml          the signature authorizing the goal
#   .superpowers/sdd/progress.md  the work DECLARATION
#
# Declaration and authorization are different artifacts. progress.md says
# WHAT work exists; roadmap.yaml says it is PERMITTED. A goal in only one
# of them does nothing, silently -- that mistake has cost a soak before.
set -eu
MAIN=/mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis
WT=/mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis-devtest
BRANCH=devtest/baseline-untrained

cd "$MAIN"

if [ -e "$WT" ]; then
  echo "REFUSING: $WT already exists. Remove it first:" >&2
  echo "  git worktree remove --force $WT && git branch -D $BRANCH" >&2
  exit 2
fi

git worktree add -b "$BRANCH" "$WT" main >/dev/null
echo "worktree: $WT  branch: $BRANCH"

# --- carry the gitignored config across -----------------------------------
cp "$MAIN/.env" "$WT/.env"
mkdir -p "$WT/.jarvis" "$WT/.superpowers/sdd"
cp "$MAIN/.jarvis/roadmap.yaml" "$WT/.jarvis/roadmap.yaml"

# The DECLARATION: exactly one item, so the sanctioned goal is the only
# work in the queue. The farming batch stays in the MAIN tree untouched --
# a dev run that fixed those 41 targets would burn them as corpus material.
cat > "$WT/.superpowers/sdd/progress.md" <<'DECL'
# Progress — operator work queue (baseline development test)

One item only. The sanctioned goal from .jarvis/roadmap.yaml, declared here
so WorkOrderSensor can emit it. Authorization lives in roadmap.yaml; this
file only declares that the work exists.

NEXT: Correct the stale skip-tools docstring on _should_use_lean_prompt in `backend/core/ouroboros/governance/providers.py` so it names should_skip_venom_for_route() instead of quoting the pre-extraction inline route-tuple test. Comment-only: change no executable line.
DECL

# A fresh ledger, or the single item is deduped away before it is ever seen.
rm -f "$WT/.jarvis/work_order_seen.json"

echo
echo "=== preflight ==="
for f in .env .jarvis/roadmap.yaml .superpowers/sdd/progress.md; do
  printf '  %-32s %s\n' "$f" "$([ -e "$WT/$f" ] && echo present || echo MISSING)"
done

# Is the goal actually still undone in this worktree?
if grep -q "should_skip_venom_for_route" \
   <(awk '/def _should_use_lean_prompt/,/^def [a-z_]/' \
     "$WT/backend/core/ouroboros/governance/providers.py"); then
  echo "  goal state                       ALREADY DONE -- test would measure nothing"
else
  echo "  goal state                       still undone (good: measurable)"
fi
echo
echo "next:  cd $WT && bash scripts/soaks/devtest_baseline.sh"
