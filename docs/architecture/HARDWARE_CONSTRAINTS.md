# Hardware Constraints — the envelope O+V actually runs inside

> Measured on this host 2026-09-08. Every number below was read from the
> machine, not assumed. Re-measure before trusting it on different hardware;
> the commands are given so this document can be regenerated rather than
> maintained by hand.

This exists because a whole diagnostic arc was spent reading a **hardware**
failure as a **model-quality** failure. Thirteen of thirty ops in one session
terminated `generation_failed`, which looks like "the local model writes bad
code". It was not. The model never ran: four ops entered GENERATE together, the
card ran out of room, and every stream returned `tokens=0 tps=0.0`.

Design decisions for O+V must start here, because on this host the binding
constraint is VRAM, not intelligence.

---

## 1. The machine

| Component | Measured | Command |
|---|---|---|
| GPU | NVIDIA GeForce RTX 5090 | `nvidia-smi --query-gpu=name --format=csv` |
| VRAM | **32607 MiB (31.8 GiB)** | `nvidia-smi --query-gpu=memory.total --format=csv` |
| Driver | 610.62 | `nvidia-smi --query-gpu=driver_version --format=csv` |
| CPU (WSL guest) | 24 cores | `nproc` |
| RAM (WSL guest) | 47 GiB | `free -g` |
| Serving runtime | Ollama @ `127.0.0.1:11434` | `curl -s localhost:11434/api/tags` |

## 2. The model

`qwen3-coder-ov:30b` — the pinned local primary (`JARVIS_LOCAL_MODEL_NAME`).

| Property | Value | Source |
|---|---|---|
| Parameters | 30.5B (MoE, 128 experts, 8 active) | `/api/show` |
| Quantization | Q4_K_M | `/api/show` |
| Weights on disk/VRAM | **18,583,466,060 B = 17.3 GiB** | `/api/tags` |
| Layers (`block_count`) | 48 | `/api/show` |
| KV heads (`head_count_kv`) | 4 (GQA, 32 query heads) | `/api/show` |
| key/value length | 128 / 128 | `/api/show` |
| Max context | 262144 | `/api/show` |

## 3. The arithmetic that governs everything

KV cache is the only thing that grows with concurrency — weights load **once**
and are shared by every in-flight request. So the question is never "how many
models fit", it is "how many KV caches fit beside one model".

```
KV bytes per token = (key_length + value_length) × head_count_kv × bytes × layers
                   = (128 + 128) × 4 × 2 (fp16) × 48
                   = 98,304 B  =  96 KiB / token
```

| num_ctx | KV per stream | Streams that fit in 14.5 GiB headroom |
|---|---|---|
| 8,192   | 0.75 GiB | ~19 |
| 16,384  | 1.5 GiB  | ~9 |
| **32,768** | **3.0 GiB** | **~4 (at 92% card occupancy — the danger zone)** |
| 65,536  | 6.0 GiB  | ~2 |
| 131,072 | 12.0 GiB | 1 |

Headroom = 31.8 GiB card − 17.3 GiB weights = **14.5 GiB**.

**The measured failure**: 4 concurrent streams at num_ctx=32768 →
12 GiB KV + 17.3 GiB weights = **29.3 GiB of 31.8 GiB (92%)**, before Ollama's
compute buffers and MoE routing overhead. The card had nothing left.

## 4. Why it fails SILENTLY (the part that cost the most time)

WSL2 has **CUDA system-memory fallback ON**. When an allocation would exceed
VRAM, the driver does not fail — it spills into host RAM over PCIe. The result
is not an out-of-memory error you can catch; it is a generation that runs three
orders of magnitude slower than the inter-token watchdog allows, so the stream
is killed and reported as:

```
[StreamRender] provider= tokens=0 first_token_ms=-1 total_ms=5452 tps=0.0
→ no_candidates_returned → generation_failed
```

**An empty stream on this host means VRAM pressure until proven otherwise.**
It does not mean the model produced bad output — it produced none.

Turning the fallback OFF (NVIDIA Control Panel → CUDA Sysmem Fallback Policy →
"Prefer No Sysmem Fallback") converts a silent 1000× slowdown into a loud
allocation failure. That is strictly better for an autonomous system, which
cannot notice "unusually slow" but can absolutely handle an error. This is a
**host-side operator action**, not something O+V can set for itself.

## 5. The 24 GiB phantom — conditional, and worth knowing

`_awakened_vram_bytes()` (the Context-Hardware Negotiator's VRAM input) returns
the **correct 31.8 GiB** in a normally-booted process. It returns **24 GiB** —
the `nvidia-l4` provisioning-spec default its own docstring warns about ("a
local 32 GiB card was sized as 24 GiB… a confident wrong number that silently
halved the derived context window") — when called from a process that has not
loaded `.env`.

Verified 2026-09-08:

```
# bare process                → vram=24.0GiB   (spec fallback)
# after backend.core.env_bootstrap.load_env_once()
#                             → vram=31.8GiB   (measured)
```

So this is not a live defect in the running organism, but it IS a trap for
diagnostics, one-off probes and tests: **a script that measures capacity
without loading `.env` will silently size against a card 25% smaller than the
one present.** Load the environment first, or you are measuring a different
machine than the one O+V runs on.

The same ordering bit the envelope itself — see §7.

## 6. Optimisation levers, in order of measured value

Stated honestly: **these are configuration levers, not low-level coding
problems.** llama.cpp/Ollama already ship hand-tuned CUDA kernels for this
architecture; hand-written C/C++ or assembly would not beat them and is not
where the headroom is on this host. The headroom is in KV cache and
concurrency.

1. **KV cache quantization** — the single biggest lever.
   `OLLAMA_KV_CACHE_TYPE=q8_0` halves KV (96 → 48 KiB/token), turning a 32k
   context from 3.0 GiB into 1.5 GiB and roughly doubling safe concurrency.
   `q4_0` quarters it. Quality impact on KV is far smaller than on weights.
2. **num_ctx discipline** — KV scales linearly with context. A goal that needs
   8k does not need 32k, and the Context-Hardware Negotiator already derives
   this; feeding it a correct VRAM number (§5) is worth more than raising it.
3. **Concurrency clamping** — implemented in
   `governance/autonomy/local_lane_capacity.py`; derives the lane's
   concurrency from `vram − weights` and fails safe to 1.
4. **`OLLAMA_NUM_PARALLEL`** — Ollama's own request parallelism. Should agree
   with (3); two components disagreeing about concurrency is how the card gets
   oversubscribed by a factor nobody chose.
5. **Model choice per phase** — a 30B for GENERATE and something smaller for
   cheap classification is a legitimate trade, but only after (1)–(4), since it
   costs a second resident model (VRAM) unless the small one is CPU-served.

## 7. Rules this implies for building O+V

* **One local GPU is a single-lane resource.** Any component that wants
  concurrency must ask `local_lane_capacity`, not assume a cloud-shaped
  default. `primary_concurrency=4` and `JARVIS_BG_POOL_SIZE=6` are correct for
  a hosted fleet and wrong here.
* **Queue, do not refuse.** With one lane, work must wait
  (`IntakePriorityQueue` back-pressure), not fail.
* **Treat an empty stream as a resource signal**, and record it as such — the
  taxonomy `no_candidates_returned → generation_failed` currently hides a
  hardware cause behind a quality-shaped name.
* **Measure before attributing to the model.** The model's actual code quality
  on this host is, as of this document, still UNMEASURED. It has been observed
  generating successfully — `Generated 1 candidates in 26.7s, 18596+3177
  tokens, 119.2 tok/s` — so the capability is there; what has not been measured
  is the quality of what it produces under a clean capacity budget.
* **Hydrate `.env` before deriving anything from the hardware.** The envelope
  originally hydrated BEFORE `.env` loaded, so `JARVIS_LOCAL_PRIME_ENABLED`
  read as unset, the lane resolved as CLOUD, and the pool came up at
  `pool_size=6` on one card. The clamp existed, had passing tests, and was
  completely inert. Any component that derives from the environment must run
  after `load_env_once()`.
* **`StreamRender tokens=0` is NOT proof of a failed generation.** In a
  headless run the renderer logs `non-TTY stdout — Live skipped, log-only
  stream` and reports zero tokens for streams that generated normally. Confirm
  against `[PrimeProvider] Generated N candidates` and
  `no_candidates_returned` counts before concluding anything from it.

## 8. Regenerating this document

```bash
nvidia-smi --query-gpu=name,memory.total,memory.free,driver_version --format=csv,noheader
nproc && free -g | head -2
curl -s localhost:11434/api/tags | python3 -m json.tool | grep -E '"name"|"size"'
curl -s localhost:11434/api/show -d '{"name":"qwen3-coder-ov:30b"}' \
  | python3 -m json.tool | grep -E 'block_count|head_count_kv|key_length|value_length|quantization'
```
