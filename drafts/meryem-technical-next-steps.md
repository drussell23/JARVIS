Hi Meryem,

As promised, here's the technical next steps summary from our conversation on Monday.

---

**What I've built since we spoke (48 hours)**

1. **397B Benchmark Complete** -- I ran 4 iterative benchmarks against `Qwen/Qwen3.5-397B-A17B-FP8` via your batch API, calibrating token budgets for reasoning models.

   Final results (batch `d36e8837`):
   - Both tasks: `finish_reason: stop` (complete output)
   - Infra task (NIST 800-53 code gen): 4,323 output tokens, $0.00174
   - Threat task (SOC analysis): 2,640 output tokens, $0.00106
   - **4x cheaper** than J-Prime ($0.0028 vs $0.011 total)
   - **$8.40/mo vs $216/mo** at 100 ops/day
   - Batch completed in 55 seconds (well within 1h SLA)

   Key finding: The 397B uses **4-5x the token budget on chain-of-thought reasoning** before producing output. A 4,300-token code output required a 20,000-token budget. This is a feature, not a bug -- the reasoning quality is excellent. I've updated my provider default to `max_tokens=10000`.

2. **Complexity-Based Routing** -- Tier 0 (Doubleword) now only fires for `heavy_code` or `complex` tasks, or cross-repo operations. Simple tasks skip straight to J-Prime. No batch API latency on trivial ops.

3. **Async Non-Blocking Pipeline** -- Split the `DoublewordProvider` into `submit_batch()` (<2s fast path) and `poll_and_retrieve()` (background task). The governance pipeline submits a batch, records it in the audit ledger, and falls through to J-Prime immediately. When Doubleword results arrive, they're cached for future use. Tier 0 adds zero perceived latency to the hot path.

4. **DPO Scoring Pipeline** -- Built `DPOScorer` for Reactor-Core: scores N candidates against a reference in a single coalesced JSONL batch (one poll, one retrieval). The 397B's chain-of-thought rationale is preserved alongside numeric scores for richer training signal. 24h SLA -- designed for nightly training epochs.

5. **Audit Trail** -- Every Doubleword batch now records `PENDING_TIER0` and `TIER0_COMPLETE` entries in the governance ledger with full traceability: `operation_id -> batch_id -> output_file_id`.

6. **Vision Model Benchmark (all 5 models)** -- I benchmarked every vision and OCR model in your catalog against a live screenshot from my JARVIS Vision Smoke Test (bouncing ball with on-screen counters — known ground truth values).

   Results:

   | Model | Screen Description | Coordinate Extraction | Counter Accuracy | Status |
   |-------|-------------------|-----------------------|-----------------|--------|
   | VL-235B (22B active) | **5.7s avg** (4.4s warm) | **5.0s avg** (2.3s warm) | Perfect | Best overall |
   | VL-30B (3B active) | 7.5s avg | 4.2s avg | Perfect | Good but slower |
   | DeepSeek-OCR-2 | 403 Forbidden | 403 Forbidden | — | API question below |
   | olmOCR-2-7B | 403 Forbidden | 403 Forbidden | — | API question below |
   | LightOnOCR-1B (bbox) | 403 Forbidden | 403 Forbidden | — | API question below |

   Key findings:
   - **VL-235B is the winner** — perfect counter reads (`Horizontal Bounces: 101, Vertical Bounces: 118, Total Bounces: 219, Speed: 331 px/s`), returns pixel coordinates for on-screen elements, and is surprisingly **faster than VL-30B** on warm calls (likely better infrastructure allocation for the more popular model).
   - **VL-30B** also reads perfectly but is consistently slower — 7.5s vs 5.7s for descriptions. Not a good speed tier candidate currently.
   - Both VL models are already wired as Tier 0 in my Lean Vision Loop (provider cascade: Doubleword VL-235B -> Claude Vision -> J-Prime GCP).
   - I built a reusable `benchmark_vision.py` that tests all models with configurable iterations and screenshots.

   This means Doubleword now powers **three use cases** in Trinity: governance (397B batch), DPO scoring (397B batch), and real-time vision (VL-235B direct). Three different models, three different patterns, all through the same API.

---

**Technical Next Steps (Proposed Timeline)**

**Week 1 (this week)**
- Run the DPO scorer end-to-end with real candidate pairs from Ouroboros governance ops
- Validate that preference pairs produce measurably better training signal for J-Prime fine-tuning
- Test circuit breaker behavior (3 consecutive failures -> 5 min cooldown)

**Week 2-3**
- Implement `BatchAccumulator` with 30-second coalescing window (collect multiple Tier 0 requests before flushing as one batch)
- Add deadline-aware fallback: if batch hasn't completed by `deadline - 60s`, automatically fall through to J-Prime
- Wire Doubleword stats into the TUI dashboard (batch count, cost, latency)

**Week 4+**
- If Doubleword adds webhook/callback on batch completion, replace polling with event-driven retrieval
- If data deletion API is available, add post-retrieval cleanup for defense compliance workloads
- Begin DPO training runs with 397B-scored preference pairs

---

**Questions (some from our call, some new from benchmarking)**

1. **Token budget guidance** -- My benchmarks confirmed the 397B needs 4-5x expected output. Is there a way to hint the model to allocate more budget to output vs reasoning? Or is the right approach simply setting `max_tokens` high and letting it self-regulate?

2. **Batch file size limits** -- I'm planning to coalesce 20-50 DPO scoring requests per batch. Is there a hard ceiling on requests per JSONL file?

3. **Webhook on completion** -- Is this on the roadmap? It would simplify my async pattern significantly (replace polling with a POST to my webhook endpoint).

4. **OCR model API endpoint** -- I benchmarked all 5 vision/OCR models via `/v1/chat/completions` with base64 image input. The two VL models worked perfectly, but the three OCR models (`DeepSeek-OCR-2`, `olmOCR-2-7B`, `LightOnOCR-1B-bbox-soup`) all returned `403 Forbidden`. Do these OCR models use a different API contract? For example, a document upload endpoint instead of chat completions with `image_url`? The `LightOnOCR-1B-bbox-soup` model is particularly interesting for my use case — if it returns bounding box coordinates for text regions, that would directly solve my UI element coordinate accuracy problem (currently my biggest vision gap).

5. **VL-30B slower than VL-235B?** -- The 30B model was consistently slower than the 235B on warm calls (7.5s vs 5.7s avg for descriptions). Is this expected? If the 235B has more allocated infrastructure because it's higher-traffic, the 30B might not be a useful "faster, lighter" tier for me.

---

**What this means for the partnership**

Doubleword now powers three independent subsystems in Trinity:

1. **Ouroboros governance** (397B batch) -- The 397B gives me 12x the reasoning capacity my L4 can provide, at the same per-token cost. Complexity-based routing ensures only the hardest tasks hit the batch API.

2. **DPO training pipeline** (397B batch) -- The 397B acts as the judge that scores candidate responses, generating preference pairs that make J-Prime smarter over time. The 397B's chain-of-thought rationale is preserved as training signal.

3. **Real-time vision** (VL-235B direct) -- The VL-235B serves as the fast eye in my dual-model vision loop at 4.4s warm latency, reading on-screen text with 100% accuracy. Completely different use pattern from batch -- real-time, image-in/text-out.

Three models, three use patterns, all through the same Doubleword API. I don't think many of your customers are using your platform for batch governance, DPO training, AND real-time vision simultaneously. If the OCR models become accessible, that's a fourth pattern (document/UI parsing). This could be a very compelling reference case for what a single API can power.

Happy to share benchmarks, architecture docs, the Jupyter notebook with charts, or a live demo anytime.

Looking forward to continuing to build together.

Best,
Derek J. Russell
