# Trinity AI — Demo Guide

**Script:** `demo_trinity_governed_loop.py`
**Purpose:** Live demonstration of the Trinity AI governed inference pipeline for the Palantir Startup Fellowship

---

## Quick Start

```bash
# Full demo with JARVIS voice narration
python3 demo_trinity_governed_loop.py

# Silent mode (screens only)
python3 demo_trinity_governed_loop.py --no-voice

# Skip the governance test suite (Phase 4)
python3 demo_trinity_governed_loop.py --no-tests

# Faster pacing (30% of normal delays)
python3 demo_trinity_governed_loop.py --fast

# Offline replay mode — replays last recorded run from history.json (no GCP call)
python3 demo_trinity_governed_loop.py --replay

# Combine flags
python3 demo_trinity_governed_loop.py --no-voice --no-tests --fast
python3 demo_trinity_governed_loop.py --replay --no-voice --fast
```

**Prerequisite (live mode):** J-Prime must be reachable. The demo connects to GCP at the endpoint in `JPRIME_ENDPOINT` (defaults to `http://136.113.252.164:8000`).

**Prerequisite (replay mode):** `benchmarks/history.json` must exist with at least one recorded run. Run the demo in live mode once to create it.

---

## What the Demo Proves

The demo maps directly to the four Palantir Fellowship objectives:

| Objective | Demo Phase | What You See |
|-----------|-----------|--------------|
| **Live Cloud Inference** | Phase 3 | Tokens stream from the GCP NVIDIA L4 in real time |
| **Pre-Execution Governance** | Phase 3 — Pre Gate | Risk classification + security approval before every inference call |
| **Verifiable Throughput** | Phase 3 — Metrics | Latency, tok/s, and token count measured and displayed live |
| **Kernel Resilience** | Phase 4 | 2,000+ governance tests verify every gate, classifier, and rollback |

---

## Architecture Overview

Trinity AI is three systems working together:

```
🛡️  JARVIS (The Body)         🧠  J-Prime (The Mind)         ⚡  Reactor-Core (The Nerves)
─────────────────────         ─────────────────────         ──────────────────────────────
Local supervisor kernel        GCP g2-standard-4              DPO preference pair generator
Ouroboros governance           NVIDIA L4 GPU                  Feeds fine-tuning from prod
Durable ledger                 Qwen2.5-Coder-14B-Q4_K_M       Governance telemetry ingestion
Risk engine                    8,192-token context
Trust graduators               ~24 tok/s generation
Circuit breakers               OpenAI-compatible API
```

The demo shows all three layers operating in real time, connected to each other and to the Palantir AIP Ontology model.

---

## Phase-by-Phase Reference

### Banner + Boot Sequence

Displays the Trinity AI banner and animates five system components booting:

1. JARVIS Kernel
2. Neural Inference Bridge
3. Ouroboros Governance Engine
4. GCP Cloud Relay
5. Reactor Telemetry Stream

JARVIS speaks the welcome intro concurrently while the boot animation runs.

---

### Phase 1 — Live System Status

**What happens:** Connects to J-Prime's `/v1/capability` and `/health` endpoints in parallel. Displays live model metadata.

**What you see:**
- Model name and GGUF artifact (e.g., `Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf`)
- Compute class (`gpu_l4`)
- GPU layers offloaded (`-1` = all)
- Context window (8,192 tokens)
- Host, schema version, contract version, endpoint, health status

**What it proves:** The GCP instance is live and serving a real model, not a stub.

**If J-Prime is offline:** The demo prints an error and exits Phase 1 cleanly. The remaining phases cannot run without a live J-Prime connection.

---

### Phase 2 — Ouroboros Governance Ledger

**What happens:** Reads the durable operation ledger at `~/.jarvis/ouroboros/ledger/` and produces three panels:

1. **Governance Operations** — total ops, applied count, blocked count, pipeline states, provider breakdown
2. **Risk Classification** — bar chart of risk tiers (SAFE_AUTO, NEEDS_APPROVAL, etc.)
3. **Ledger Entry (deep dive)** — the richest single entry displayed as syntax-highlighted JSON
4. **Pipeline Trace** — animated Rich tree showing state transitions for the largest ledger file
5. **AIP Ontology Mapping** — table mapping every Ouroboros concept to a Palantir Object Type

**What it proves:** Every autonomous operation has been durably logged with operation IDs, risk classifications, rollback hashes, and timestamps — the exact data structure that maps into Palantir's AIP Ontology.

**AIP Ontology mapping summary:**

| Ouroboros Concept | AIP Object Type | Key Properties |
|-------------------|-----------------|----------------|
| Operation Ledger | GovernedOperation | op_id, state, risk_tier, ts |
| Routing Decision | InferenceRoute | model_id, tier, latency_ms, tok/s |
| Risk Assessment | RiskClassification | risk_tier, blast_radius, auto/manual |
| Trust Graduation | TrustGraduation | repo, trigger, old→new trust level |
| Circuit Breaker | CircuitBreakerEvent | component, state, failure_count |
| Rollback Record | RollbackAudit | sha_before, sha_after, verified |

**AIP Action Types:**

| Action | Trigger |
|--------|---------|
| ApproveOperation | Risk tier requires human review |
| RollbackChange | Verification failed post-apply |
| EscalateRisk | Blast radius exceeds threshold |
| TriggerDPOCapture | Applied op generates preference pair |

---

### Phase 3 — Ouroboros IN ACTION (Live Governed Inference)

**This is the centerpiece of the demo.** Two real inference tasks run sequentially, each wrapped in full governance pipeline execution.

For each task:

**Step 1 — Pre-Execution Gate (yellow panel)**
```
🔍 Risk Classification   NEEDS_APPROVAL
🎯 Routing Decision      PRIMARY → L4 GPU
🛡️ Security Gate         APPROVED
📋 Operation ID          op-demo-{timestamp}-{i}
```

**Step 2 — Live Token Streaming (green/red panel)**
- A `threading.Thread` makes an SSE HTTP request to J-Prime with `"stream": true`
- Tokens are placed into a `queue.Queue` as they arrive
- A `Rich.Live` context manager (12 fps) drains the queue and repaints the panel each frame
- Infrastructure Code task uses Python syntax highlighting (`Rich.Syntax`, Monokai theme, line numbers)
- Threat Analysis task displays as plain bold white text
- A blinking cursor (`▌`) appears while streaming; elapsed time updates every frame

**Step 3 — Routing & Performance (magenta panel)**
```
🎯 Routing Tier   primary
🤖 Model          {model_id from J-Prime}
⏱️  Latency        {ms} ms
📝 Tokens          {count}
⚡ Throughput      ~{tok/s} tok/s
```

**Step 4 — Post-Execution Validation (green panel)**
```
✅ Syntax Validation   PASSED
✅ Security Scan       CLEAN
✅ Rollback Hash       {4-byte hex}
📝 Ledger Entry        {op_id}
🏁 Final State         APPLIED
```

**Task 1: Secure Infrastructure Code**
- System prompt: Senior infrastructure security engineer, FedRAMP-certified environment
- User prompt: Python function validating firewall rules against NIST 800-53 — port ranges, CIDRs, unrestricted inbound access
- Max tokens: 250

**Task 2: Defense Threat Analysis**
- System prompt: Defense cybersecurity analyst
- User prompt: 47 failed SSH logins from 3 internal IPs in 90s, successful login, immediate sudo escalation — classify threat and recommend 3 immediate actions
- Max tokens: 200

Both tasks use `temperature: 0.1` for deterministic, professional output.

---

### Phase 4 — Ouroboros IS DEPENDABLE (Governance Validation)

**Narrative framing:** Phase 3 showed Ouroboros in action. Phase 4 proves it's dependable.

**What happens:** Runs the full governance test suite as a subprocess:
```bash
pytest tests/test_ouroboros_governance/ tests/governance/ -q --tb=no --no-header
```

A `Rich.Live` timer panel updates every 0.5 seconds while tests run. JARVIS narrates ~8 seconds in:
> "Every gate you saw fire in Phase 3 is being verified right now..."

**Results panel:**
```
✅ Tests Passed          {n}
⚠️  Pre-existing Failures {m}   (unrelated to governance)
📊 Pass Rate              {%}
⏱️  Duration              {s}s
🔬 Tests/Second           {n}
```

**Note on pre-existing failures:** 9 known pre-existing test failures exist in `test_preflight.py`, `test_e2e.py`, `test_pipeline_deadline.py`, and `test_phase2c_acceptance.py`. These are structural test harness issues unrelated to the governance pipeline itself. The demo calls them out explicitly so reviewers understand they are not regressions.

**What it proves:** Every governance gate demonstrated live in Phase 3 has a corresponding automated test that verifies its behavior. The pipeline is not a demo artifact — it is production software with a test harness.

---

### Phase 5 — System Summary + Benchmark Report

**What happens:** Displays the full system summary, then persists benchmark data to disk.

**Summary panel shows:**
- All three Trinity components (JARVIS / J-Prime / Reactor-Core) with capabilities
- AIP integration plan and Ontology pipeline
- Real commit count (from `git rev-list --count HEAD`)
- Real governance file count (from `backend/core/ouroboros/**/*.py`)
- Governance test count (dynamic from live run or history), 3 repos, 1 developer, $0 funding

**Benchmark persistence:**
```
benchmarks/
├── LATEST.md              # Always overwritten — latest run in Markdown
└── run-{YYYY-MM-DDTHH-MM-SS}.json   # One per run — full history
```

---

## Quantization Clarification

**The demo uses Q4_K_M, not IQ2_M.**

| Quantization | Used in Demo? | Where it lives |
|--------------|---------------|----------------|
| Q4_K_M (GGUF standard) | **Yes** — all demo inference | J-Prime GCP model file |
| IQ2_M (Fisher Information optimal) | **No** | `backend/core/memory_quantizer.py` |

The Fisher Information / IQ2_M adaptive quantization is a separate system — the **Adaptive Quantization Engine** — designed and specced in `docs/superpowers/specs/2026-03-14-adaptive-quantization-engine-design.md`. It dynamically selects optimal quantization levels based on VRAM pressure, task complexity, and quality regression metrics. This system is designed to run on J-Prime but is not yet integrated into the demo.

---

## Voice Engine

The demo uses macOS `say` for speech synthesis, but **not** raw `say` (which can clip the final syllable before the audio buffer drains).

**Implementation:**
```
say -v {voice} -r {rate} -o /tmp/jarvis_tts_{id}.aiff {text}   # synthesis only
afplay /tmp/jarvis_tts_{id}.aiff                                 # blocks until all PCM samples reach speaker
```

This is the same pattern as the backend's `safe_say()` in `unified_voice_orchestrator.py`.

**Configuration via environment:**
```bash
JARVIS_VOICE=Daniel        # macOS voice name (default: Daniel)
JARVIS_SPEECH_RATE=175     # words per minute (default: 175)
```

**Concurrency:** Speech can run concurrently with animations (`wait=False`) or block until complete (`wait=True`). `wait_speech()` is called at phase boundaries to ensure JARVIS finishes speaking before the next phase begins.

---

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `JPRIME_ENDPOINT` | `http://136.113.252.164:8000` | J-Prime GCP endpoint |
| `JARVIS_LEDGER_DIR` | `~/.jarvis/ouroboros/ledger` | Durable ledger path for Phase 2 |
| `JARVIS_VOICE` | `Daniel` | macOS TTS voice |
| `JARVIS_SPEECH_RATE` | `175` | Words per minute |
| `--no-voice` flag | off | Disable all speech |
| `--no-tests` flag | off | Skip Phase 4 test suite |
| `--fast` flag | off | Use 30% of normal delays |
| `--replay` flag | off | Replay last recorded run from `history.json`; skips live GCP inference call |

---

## Dependencies

```
rich>=13.0      # Terminal UI (Panel, Table, Syntax, Live, Tree, Rule, Text)
```

All other imports are Python standard library:
`asyncio`, `json`, `os`, `subprocess`, `tempfile`, `threading`, `queue`, `urllib.request`, `concurrent.futures`, `datetime`, `pathlib`, `re`, `shutil`, `sys`, `time`

---

## File Outputs

After each full run (requires J-Prime to be reachable for Phase 3 data):

- `benchmarks/LATEST.md` — Markdown table, always overwritten
- `benchmarks/run-{timestamp}.json` — Machine-readable JSON, accumulates history

See [`benchmarks/README.md`](../../benchmarks/README.md) for format details.

---

## Replay Mode — Fallback for Offline Review

If J-Prime is unavailable during review (e.g., GCP budget exhausted or network restricted), run:

```bash
python3 demo_trinity_governed_loop.py --replay --no-voice
```

**What replay mode does:**
- Loads `inference_0` and `inference_1` from the last entry in `benchmarks/history.json`
- Displays all four governance panels (pre-gate, replay notice, metrics, post-gate) for each task
- Populates `_benchmarks` identically to a live run so Phase 5 (summary + persistence) works normally
- GPU, model, and artifact labels are read dynamically from history — no hardcoded strings

**What you see instead of live streaming:**
```
  📼 Replay mode — last recorded run
  631 tokens · 25665ms · ~24.6 tok/s
```

The replay panel replaces the live `Rich.Live` streaming widget. All other panels (governance gates, routing metrics, post-execution validation) render identically to a live run.

**To pre-generate history.json for review sessions**, run the demo once while J-Prime is live:
```bash
python3 demo_trinity_governed_loop.py --no-voice --fast
```
Then `benchmarks/history.json` is ready and `--replay` works indefinitely.

---

## Submission Context

> **Note for reviewers:** This demo runs in the macOS terminal using [Rich](https://github.com/Textualize/rich) for the terminal UI. A web-based dashboard UI is in development — the terminal demo is the current production interface. All performance numbers shown are from real J-Prime inference runs recorded in `benchmarks/history.json` and generated live on a GCP `g2-standard-4` instance with NVIDIA L4 GPU.
