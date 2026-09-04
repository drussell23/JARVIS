#!/usr/bin/env bash
# Read the baseline development test's result off its own session log.
#
# The metric is NOT "did it pass". It is WHERE IT STOPPED, because the
# stopping point is what the trained model has to beat. A run that dies at
# the governance cage and a run that dies at VERIFY need opposite fixes,
# and only the terminal reason separates them.
set -u
WT=${1:-/mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis-devtest}
D=$(ls -dt "$WT"/.ouroboros/sessions/bt-* 2>/dev/null | head -1)
[ -n "$D" ] || { echo "no session under $WT" >&2; exit 2; }
L="$D/debug.log"
echo "=== BASELINE DEVELOPMENT TEST — $(basename "$D") ==="
[ -f "$D/summary.json" ] && python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
for k in ("session_outcome","stop_reason","duration_s","cost_total"):
    print(f"  {k}: {d.get(k)}")' "$D/summary.json"

echo
echo "--- did the sanctioned goal reach the pipeline? ---"
echo "  work orders emitted : $(grep -aoE 'emitted [0-9]+ work order' "$L" | tail -1)"
echo "  provenance verified : $(grep -ac 'origin_class=roadmap' "$L")"
echo "  declared-symbol hit : $(grep -ac 'METHOD_DECLARED\|declared_symbol' "$L")"

echo
echo "--- how far did it get? (phase reach) ---"
for p in CLASSIFY ROUTE PLAN GENERATE VALIDATE GATE APPLY VERIFY COMPLETE; do
  printf '  %-9s %s\n' "$p" "$(grep -ac "dispatching $p" "$L")"
done

echo
echo "--- terminal reasons (the honest stopping point) ---"
grep -aoE "terminal_reason_code=[a-z_0-9]+" "$L" | sed 's/.*=//' | sort | uniq -c | sort -rn | head -10

echo
echo "--- governance: which gate, how often ---"
grep -aoE "(self_modification_unsanctioned_source|touches_kernel|touches_supervisor|touches_security|target_out_of_scope|delegated_provenance_self_modification|APPROVAL_REQUIRED|NOTIFY_APPLY|SAFE_AUTO)" "$L" \
  | sort | uniq -c | sort -rn | head -10

echo
echo "--- did a byte actually land? ---"
echo "  APPLY ok      : $(grep -acE 'APPLY.*(success|applied)' "$L")"
echo "  VERIFY pass   : $(grep -acE 'verify.*(pass|ok)' "$L")"
echo "  commits       : $(grep -acE 'AutoCommit|auto_commit|committed' "$L")"
echo "  promotion     : $(grep -acE 'promote_commits|WorkspacePromoter|promotion' "$L")"

echo
echo "--- ground truth: is the docstring actually fixed? ---"
if awk '/def _should_use_lean_prompt/,/^def [a-z_]/' \
     "$WT/backend/core/ouroboros/governance/providers.py" \
     | grep -q "should_skip_venom_for_route"; then
  echo "  YES — the goal's success_criteria is met in the worktree"
else
  echo "  NO  — docstring unchanged"
fi
echo
echo "--- commits on the devtest branch ---"
git -C "$WT" log --oneline main..HEAD 2>/dev/null | head -10 || echo "  (none)"
