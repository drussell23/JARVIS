# Slice 138/139 — The Sovereign Infrastructure & Migration Matrix

Reboot-surviving, self-backing-up deployment for the T5 unattended soak. One
command arms the crypto gate and launches the organism as systemd services — and
one command migrates the whole organism from your workstation to a cloud host.

## Two environments (deliberate) — full host is canonical for the 12-month soak

| Environment | Deps | Oracle | Use |
|---|---|---|---|
| **Full host** (`migrate_to_host.sh` → `provision_host.sh` installs `requirements.txt`) | complete graph (incl. `networkx`/`tree_sitter`/`scipy`/`sklearn`) | **runs** (codebase graph live) | **Canonical 12-month production soak** |
| **Lean Docker** (`docker/requirements-soak.txt`) | minimal governance-loop set only | **DEGRADED by design** (context-free) | Cost/governance + Discord-observability soak; quick local runs |
| **Oracle-capable Docker** (`docker/requirements-soak-oracle.txt`, `launch_docker_soak.sh --oracle`) | lean + Oracle graph stack (`networkx`/`scipy`/`scikit-learn`/`tree_sitter`/`aiofiles`, arm64 wheels) | **runs** | **macOS soak with the Oracle live** — detached + decoupled, no separate Linux host |

**On macOS (no separate Linux host):** `./scripts/launch_docker_soak.sh --oracle`
builds the Oracle-capable image (the lean set + the Oracle graph deps via `-r`, NOT
the full torch/transformers stack) and runs it **detached under dockerd** — surviving
this session + reboot (with Docker Desktop auto-start), decoupled from any agent
event loop, with the Oracle booting (preflight passes). This is the canonical-
equivalent soak for a Mac without provisioning a cloud VM.

**Do not bloat `docker/requirements-soak.txt`** to make the lean image run the
Oracle — that muddies the architecture. The lean image is *meant* to run
context-free. **Slice 149-P4 guardrail:** `oracle_ipc` now **preflights its hard
deps before spawning** (single source: `_ORACLE_REQUIRED_DEPS`, backed by
`oracle.py`'s explicit `networkx is required` raise). In a lean environment the
Oracle **DEGRADES once** with a clear diagnostic — no 3× respawn storm — and the
engine continues context-free; a transient crash on a full host still uses the
bounded respawn. The canonical 12-month soak runs on a **full host**.

## Autonomous cloud migration (Slice 139) — workstation → industrial host

From the **main checkout** (where `.jarvis/` holds the real keys + signed roadmap):

```bash
# Package, ship, provision, and ignite on a fresh Linux host — one command:
./scripts/migrate_to_host.sh ops@gcp-jprime /opt/jarvis --launch
```

It runs three composable pieces (each usable alone):
- **`scripts/pack_sovereign_release.sh`** → a lean `.tar.gz`: excludes `.venv`,
  `.git`, caches, and the 600M+ of regenerable `.jarvis` state; **preserves** the
  Ed25519 pubkey + salt + meta + **signed roadmap** + the tamper-evident evidence
  chain + episodic memory (allowlist) so the organism wakes up authorized and
  remembering; **`.env` is NEVER in the artifact**.
- **`deploy/provision_host.sh`** (IaC bootstrap) → installs Python 3.11+, the
  build toolchain (numpy/fastembed), git/rsync, makes the venv, `pip install -r
  requirements.txt`, and import-sanity-checks the deps. apt + dnf.
- **The handshake** → `scp`s the artifact, ships `.env` **out-of-band** over the
  same authenticated SSH (never in the tarball), extracts, provisions, and (with
  `--launch`) runs `arm_and_launch.sh`. Because the signed roadmap travels in the
  artifact, the host ignition is **non-interactive** (no passphrase prompt;
  `arm_and_launch.sh` skips re-signing when a signed roadmap is present).

Omit `--launch` to stage + provision only, then verify `.env` and ignite manually.

---

## One-command ignition (Linux + systemd)

```bash

## One-command ignition (Linux + systemd)

```bash
./scripts/arm_and_launch.sh
```

It will: provision the Ed25519 operator key (if needed, passphrase-prompted —
never stored) → sign `.jarvis/roadmap.draft.yaml` → `.jarvis/roadmap.signed.yaml`
→ render + install two **user** systemd units → `enable --now` both →
`enable-linger` so they survive logout **and** reboot.

### Prerequisites (you provide)
- `.env` at repo root with funded `DOUBLEWORD_API_KEY` / `ANTHROPIC_API_KEY`
  (loaded by `launch_shadow_soak.sh`'s Slice-125 credential bootstrap; never
  parsed by systemd).
- `.jarvis/roadmap.draft.yaml` — your authority-free draft of the SAFE authorized
  scopes. The **un-signable floor still holds absolutely**: Order-2/M10, recursion
  breach, governance touches, and `APPROVAL_REQUIRED`/`BLOCKED` always escalate to
  a live operator regardless of signature.
- (Optional but recommended) a backup target so `.jarvis/` survives host-death.

## The two services
| Unit | What |
|---|---|
| `jarvis-agent.service` | The soak: `launch_shadow_soak.sh --production-soak --layer4-autonomous --headless`. `Restart=always`, 10s→5min exponential backoff (systemd ≥254), logs → `.jarvis/t5_soak.out`. Prefix-cache stays OFF (inert until its seam lands). |
| `jarvis-state-vault.service` | The evidence vault: `state_persistence_daemon` continuously mirrors `.jarvis/` (episodic memory + tamper-evident evidence chain + signed roadmap) to your remote target. Gated, fail-soft. |

### State-vault backends (`JARVIS_BACKUP_BACKEND` + `JARVIS_BACKUP_TARGET`)
- `rsync` → `user@host:/vault` (needs ssh-agent / key)
- `s3` → `s3://bucket/jarvis` (needs `AWS_*` in `.env`)
- `git` → a private remote (a self-contained repo inside `.jarvis/`)

## Monitor / stop
```bash
systemctl --user status jarvis-agent.service
tail -f .jarvis/t5_soak.out
systemctl --user stop jarvis-agent.service jarvis-state-vault.service
```

## Non-systemd hosts
- **macOS** dev hosts have no systemd — run the soak on a dedicated **Linux**
  server. (A `launchd` plist is the macOS equivalent; not shipped here.)
- Quick/ephemeral fallback (dies on reboot — not for a real 12-month run):
  ```bash
  nohup env JARVIS_SOVEREIGN_KEYS_ENABLED=1 JARVIS_EPISODIC_CORE_ENABLED=1 \
    JARVIS_SEMANTIC_CACHE_ENABLED=1 JARVIS_CAI_ROUTER_ENABLED=1 JARVIS_KAREN_VOICE_ENABLED=0 \
    ./scripts/launch_shadow_soak.sh --production-soak --layer4-autonomous --headless \
    --cost-cap 500 --idle-timeout 0 --max-wall-seconds 0 > .jarvis/t5_soak.out 2>&1 &
  ```

> Run on a **dedicated host**, never nested under another agent/event loop
> (Slices 123 & 127: nesting causes control-plane starvation — environmental,
> not a code defect).
