# Bring-up — the Engine on Windows / WSL2

The Body stays on the Mac (CoreAudio, AppKit, Quartz capture, the cockpit).
This runbook stands up the **Engine** on the Windows box and gets `ov doctor`
to a clean read. Nothing here needs the Body running.

Target: AMD 9950X3D · RTX 5090 (32 GB) · 64 GB DDR5 · Windows + WSL2.

---

## 0. Why WSL2 and not native Windows

The engine imports `fcntl` in 11 files, Unix sockets in 7, and `SIGHUP` in 9.
Python's asyncio has **no** `start_unix_server` on Windows, and `flock`
semantics have no clean Win32 equivalent — so native Windows is a two-seam
port, not a configuration. WSL2 runs the tree unmodified, and vLLM is
Linux-only regardless.

The port is tractable later (consolidate the 7 files that bypass the
canonical lock primitive, then one transport seam for the cockpit). It is not
needed to start.

---

## 1. WSL2

```powershell
wsl --install -d Ubuntu-24.04
wsl --update
```

`%UserProfile%\.wslconfig` — **WSL2 defaults to 50% of RAM (32 GB)**, which
silently halves your offload budget:

```ini
[wsl2]
memory=52GB
processors=16
swap=16GB
networkingMode=mirrored
```

`networkingMode=mirrored` (Win11 22H2+) makes WSL2 share the host's
interfaces. Without it the guest gets a NAT address that **changes on every
restart**, and the alternative — `netsh interface portproxy` — breaks on
every reboot. This matters for §29's link, not for local work.

Restart: `wsl --shutdown`, then reopen.

---

## 2. GPU

Install the NVIDIA driver **on Windows only**. Never install a Linux driver
inside WSL — it will shadow the projected stub and break CUDA.

```bash
nvidia-smi                    # should list the 5090
ls -l /usr/lib/wsl/lib/nvidia-smi
```

You do **not** need the CUDA toolkit. `nvcc` and headers are for *compiling*
CUDA; inference runtimes ship their own. `compute_topology` reads the driver
directly via `nvidia-smi` and requires no PyTorch at all.

If you do install PyTorch, it must be a **cu128** build — the 5090 is
Blackwell (`sm_120`) and older wheels will not see the device. They fail
softly: the probe falls through to `nvidia-smi` and still works.

---

## 3. Repository

Clone onto the **ext4** filesystem, never `/mnt/c`. The Oracle walks 6,593
Python files and 9p will make that miserable.

```bash
cd ~ && git clone <remote> JARVIS-AI-Agent && cd JARVIS-AI-Agent
```

Drop the `.nosync` suffix — that is an iCloud workaround with no meaning
here. `.gitattributes` normalises line endings to LF, so the working tree is
byte-identical to the Mac's.

```bash
pyenv install 3.11.10 && pyenv local 3.11.10     # matches .python-version
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .                                  # provides the `ov` script
hash -r                                           # purge any stale shim
```

---

## 4. The two things that do not travel

**Secrets.** `.env` is gitignored (`.gitignore:21`). The daemon environment
carries ~150 variables — `DOUBLEWORD_API_KEY`, `ANTHROPIC_API_KEY`,
`JARVIS_DB_PASSWORD`, `JARVIS_AEGIS_BOOTSTRAP_PSK`, GCP config, Discord
webhooks. `.env.example` is a template, not the values. Copy `.env` across by
hand, out of band. **Nothing boots the same way without it.**

**The organism's memory.** `.jarvis/` is gitignored (`.gitignore:123`) except
four deliberately-tracked files. It holds the semantic index cache, posture
history, bandit router state, mTLS certs, acoustic profile, user preferences
and every postmortem. So `LastSessionSummary`, `PostmortemRecall`,
`SemanticIndex` and `UserPreferenceMemory` all start empty — **the synthetic
soul begins with amnesia.** Correct for a gitignore, and a real consequence:
copy `.jarvis/` deliberately if you want continuity.

---

## 5. Verify — count collections, not passes

```bash
# On the Mac, first:
python3 -m pytest tests/ -q --collect-only 2>/dev/null | tail -1

# Then here. The numbers must differ ONLY by the macOS-only files.
python3 -m pytest tests/ -q --collect-only 2>/dev/null | tail -1
```

Eight test files import Quartz/AppKit/CoreAudio. On Linux they do not fail —
they **fail to collect**, and a suite that cannot collect reports no
failures. A green run with a quietly smaller denominator is the trap; compare
the *collected* count, then add explicit `pytest.importorskip` guards so the
skip is declared rather than inferred from an error.

Then the governance spine:

```bash
python3 -m pytest tests/governance/test_compute_topology.py \
                  tests/governance/test_local_model_admission.py \
                  tests/governance/test_link_protocol.py -q
```

---

## 6. The ten-minute hardware check

```bash
export JARVIS_COMPUTE_TOPOLOGY_ENABLED=1
ov doctor
```

**Edge 9 works with the daemon down** — that is the point of it on a machine
that has never run the organism. Expect:

```
9 compute   OK   discrete gpu_32gib · 28.x GiB usable · src=torch_cuda
```

`unified` or `unknown` here means the probe did not find the device; check
`nvidia-smi` and the cu128 build. This single line closes the three things
that could not be verified from the Mac: whether WSL2's `nvidia-smi` parses,
whether `mem_get_info` behaves on `sm_120`, and what the real usable figure
is.

---

## 7. The local lane

Policy entries already carry `min_vram_gb` and `weight_gb` for all six
brains. **Calibrate `weight_gb` against the artifacts you actually pull** —
the shipped values are reasonable Q4/Q6 estimates, not measurements of your
files.

Resident in VRAM beats offloaded: a 32B coder at Q6_K (~27 GiB) for GENERATE,
plus a ~13 GiB small MoE for the BACKGROUND/SPECULATIVE lanes. Your DDR5 runs
~90–100 GB/s against the 5090's ~1.8 TB/s, so anything spilled is ~18× slower
per byte.

Raise `JARVIS_BG_POOL_SIZE` past 3 — that cap was chosen for an 8-core
laptop, and this box has 16 cores.

---

## 8. Then, and only then

Run a soak with `--max-wall-seconds 2400 --headless` and confirm ops
complete. That is what restarts §26's trust-gate evidence, which has been at
zero since 2026-08-11 because every paid lane was dry. A local Tier 3 cannot
return `402`.

---

## Appendix — env quick reference

| Variable | Purpose |
|---|---|
| `JARVIS_COMPUTE_TOPOLOGY_ENABLED` | master switch for the measured accelerator probe |
| `JARVIS_COMPUTE_TOPOLOGY_SMI_DIRS` | override driver-CLI search dirs (`os.pathsep` separated) |
| `JARVIS_COMPUTE_TOPOLOGY_PREWARM_BUDGET_S` | boot probe ceiling (default 15s) |
| `JARVIS_LOCAL_MODEL_UNKNOWN_CEILING_GB` | largest blind load on an unmeasured host (default 8) |
| `JARVIS_VRAM_RESERVATION_SETTLE_S` | when a soft claim is assumed resident (default 120) |
| `JARVIS_VRAM_RECONCILE_AFTER_S` | when a claim becomes eligible for phantom detection (default 20) |
| `JARVIS_LINK_BRIDGE_ENABLED` | §29 Body/Engine link (default false) |
| `JARVIS_BG_POOL_SIZE` | background worker count (default 3) |
