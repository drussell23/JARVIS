# Sovereign Cloud Orchestrator — production deployment

IaC path to the first real `state=applied` generation run on a high-compute
Linux host. Removes the three empirically-proven local-M1 bottlenecks
(nested event-loop starvation, pytest SIGKILL @30s, AST pool pinned to 1).

## Components
- **`docker-compose.prod.yml`** — reuses `docker/Dockerfile.soak` (deps + code),
  `restart: always`, `.env` (runtime secrets) + `.jarvis/` bind-mount
  (funded keys + evidence chain survive container death). Entrypoint is
  `scripts/launch_linux_prod.sh` (computes `$(nproc)` inside the container →
  AST pool = cores-1, BG pool = cores/2). **No CPU cap** — wants every core.
- **`deploy/ouroboros_linux_prod.env`** — the tuned horizons (pytest 30→180s,
  AST 1→nproc, $10 cap, 60-min wall, all session features on).
- **`deploy/provision_docker_host.sh`** — idempotent Docker Engine + compose
  install for a raw Ubuntu/RHEL host; enables dockerd on boot.

## Prerequisite
Place the funded `.env` (DOUBLEWORD_API_KEY / ANTHROPIC_API_KEY) in the repo
root on the host — out-of-band (scp), **never committed**. DW is funded; if
Claude credits are exhausted, set `JARVIS_PROVIDER_CLAUDE_DISABLED=true` in
`.env` for pure DW autarky (now safe — the per-provider quota isolation fix
keeps Claude's death from poisoning the DW lane).

## The single-line bootstrap (raw Linux host → first generation run)
From a host that already has `.env` staged in `/opt/jarvis/.env`:

```bash
sudo bash -c 'curl -fsSL https://raw.githubusercontent.com/drussell23/JARVIS/main/deploy/provision_docker_host.sh | bash && git clone https://github.com/drussell23/JARVIS.git /opt/jarvis && cp /opt/jarvis/.env.staged /opt/jarvis/.env 2>/dev/null; cd /opt/jarvis && docker compose -f docker-compose.prod.yml up -d --build && docker compose -f docker-compose.prod.yml logs -f jarvis-prod'
```

Or, broken into the three honest steps (recommended — easier to verify each):

```bash
# 1. provision docker on the raw host (idempotent)
curl -fsSL https://raw.githubusercontent.com/drussell23/JARVIS/main/deploy/provision_docker_host.sh | sudo bash
# 2. clone + stage funded secrets (scp your .env into the repo root first)
git clone https://github.com/drussell23/JARVIS.git /opt/jarvis && cd /opt/jarvis && scp you@vault:/secure/.env .
# 3. build + boot the engine + watch it hunt
docker compose -f docker-compose.prod.yml up -d --build && docker compose -f docker-compose.prod.yml logs -f jarvis-prod
```

## What to watch for (the first `state=applied`)
```bash
docker compose -f docker-compose.prod.yml logs -f jarvis-prod \
  | grep -E "QUOTA ISOLATION|AUTARKY|emitting 2b|state=applied|APPLY|on-loop"
```
Success markers: `preflight REFUSED: 0`, `emitting 2b.1-diff`, an op reaching
`APPLY`, and `state=applied` — with **no** `on-loop call exceeded threshold`
storms (the host now has the cores the M1 lacked).

## Validation status (honest)
- `docker compose -f docker-compose.prod.yml config` → **resolves clean** (validated locally).
- Provisioner + compose are structurally sound + idempotent.
- **Not yet run on a real Linux host** — that run IS the first `state=applied`
  proving ground. The software (budget fix + quota isolation + fleet evaluator)
  is validated; this bridge gives it the compute it needs.
