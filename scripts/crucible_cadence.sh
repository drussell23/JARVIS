#!/bin/bash
# =============================================================================
# Sovereign Cognitive Crucible — the Immortal Cadence loop (2026-06-20).
#
# Container entrypoint for a GRADUATION node (selected via docker-compose.
# crucible.yml). Distinct from the perpetual-battle-test entrypoint: this runs
# ONE live-fire graduation soak at a time so only a single battle-test ever
# competes for budget/CPU (no contention), records its TTFT/AST evidence to the
# bind-mounted .jarvis ledger, asks the autonomous graduation engine to propose
# a [SOVEREIGN GRADUATION] PR for any flag whose 3 clean soaks cleared the math
# veto, then aggressively syncs .jarvis to GCS so a Spot preemption resumes
# exactly where it left off (amnesia-proofing).
#
# Loop per iteration:
#   1. live_fire_graduation_soak.py run   → next pickable flag, ONE soak
#   2. evaluate_graduations + execute_graduations → propose PR if eligible
#   3. state-vault sync .jarvis → GCS (after EVERY soak, per spec)
#   4. sleep JARVIS_CRUCIBLE_CADENCE_SLEEP_S (default 30s)
#
# Idempotent + fail-soft: any step's failure is logged and the loop continues.
# The soak harness/engine are gated by the master flags set in the overlay.
# =============================================================================
set -uo pipefail
cd /app || { echo "[crucible] FATAL: /app missing"; exit 1; }

# Source .env (DW key, GH token, GCS target) — never echoed.
if [[ -f /app/.env ]]; then set -a; source /app/.env; set +a; fi

SLEEP_S="${JARVIS_CRUCIBLE_CADENCE_SLEEP_S:-30}"
COST_CAP="${OUROBOROS_BATTLE_COST_CAP:-1.00}"
WALL_CAP="${OUROBOROS_BATTLE_MAX_WALL_SECONDS:-2400}"
TIMEOUT="${JARVIS_CRUCIBLE_SOAK_TIMEOUT_S:-2700}"

echo "── 🧬 Sovereign Crucible Cadence ──────────────────────────────"
echo "  cost_cap=\$${COST_CAP}  wall_cap=${WALL_CAP}s  sleep=${SLEEP_S}s"
echo "  graduation_engine=${JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED:-unset}"
echo "  soak_harness=${JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED:-unset}"
echo "  pr_gate=${JARVIS_CRUCIBLE_GRADUATION_PR_ENABLED:-unset}"
echo "  gh_token=${GH_TOKEN:+present (masked)}"
echo "  gcs_backup=${JARVIS_BACKUP_TARGET:-<none>} (backend=${JARVIS_BACKUP_BACKEND:-unset})"
echo "───────────────────────────────────────────────────────────────"

# git uses the JIT token for branch push (orange_pr_reviewer + gh CLI read GH_TOKEN).
if [[ -n "${GH_TOKEN:-}" ]]; then
  git config --global credential.helper store 2>/dev/null || true
  printf 'https://x-access-token:%s@github.com\n' "$GH_TOKEN" > ~/.git-credentials 2>/dev/null || true
  chmod 600 ~/.git-credentials 2>/dev/null || true
  git config --global url."https://x-access-token:${GH_TOKEN}@github.com/".insteadOf "https://github.com/" 2>/dev/null || true
fi

_sync_state() {
  # After EVERY soak: mirror .jarvis → GCS (fail-soft; preemption-resume input).
  # Direct gsutil (one-shot) — same argv the Evidence Vault's `gcs` backend
  # builds (build_backup_commands), but inline so the loop never blocks on a
  # long-lived daemon. The Vault daemon/systemd path remains for non-cadence use.
  if [[ -n "${JARVIS_BACKUP_TARGET:-}" ]]; then
    gsutil -m rsync -r -d /app/.jarvis "${JARVIS_BACKUP_TARGET}" 2>/dev/null \
      && echo "[crucible] state synced → ${JARVIS_BACKUP_TARGET}" \
      || echo "[crucible] state sync failed (non-fatal)"
  fi
}

_propose() {
  # Ask the engine to evaluate eligible flags + propose source-of-truth PRs.
  python3 - <<'PYEOF' 2>&1 | sed 's/^/[crucible] /' || true
try:
    from backend.core.ouroboros.governance.autonomous_graduation_engine import (
        autonomous_graduation_engine_enabled, evaluate_graduations,
        execute_graduations,
    )
    if autonomous_graduation_engine_enabled():
        rep = evaluate_graduations()
        res = execute_graduations(rep)
        print("graduation pass: recorded=%s advised=%s"
              % (getattr(res, "recorded_overrides", ()),
                 getattr(res, "advisories_emitted", ())))
    else:
        print("graduation engine disabled — skipping propose")
except Exception as exc:  # noqa: BLE001
    print("propose pass error (non-fatal): %s" % exc)
PYEOF
}

ITER=0
while true; do
  ITER=$((ITER + 1))
  echo "🧬 [crucible] iteration ${ITER} $(date -u +%FT%TZ) — running one soak…"
  python3 scripts/live_fire_graduation_soak.py run \
    --cost-cap "${COST_CAP}" \
    --max-wall-seconds "${WALL_CAP}" \
    --timeout "${TIMEOUT}" \
    --recorded-by crucible_cadence 2>&1 | sed 's/^/[soak] /' || \
    echo "[crucible] soak run returned non-zero (non-fatal)"
  _propose
  _sync_state
  echo "🧬 [crucible] iteration ${ITER} done — sleeping ${SLEEP_S}s"
  sleep "${SLEEP_S}"
done
