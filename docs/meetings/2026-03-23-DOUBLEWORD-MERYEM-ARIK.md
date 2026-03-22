# Meeting Prep: Meryem Arik (Doubleword)

**Date:** Sunday, March 23, 2026
**Time:** 9:00 AM PST / 4:00 PM GMT
**Duration:** 1 hour
**Format:** Video call
**Who:** Meryem Arik — Co-Founder & CEO, Doubleword (London, UK)
**LinkedIn:** "Inference for Background Agents | Co-founder/CEO at Doubleword"

---

## Background & Relationship Timeline

| Date | Event |
|------|-------|
| Mar 12 | Meryem cold-emailed Derek after seeing the JARVIS repo — offered 20M token trial |
| Mar 12 | Derek replied expressing interest in benchmarking Doubleword for Reactor-Core |
| Mar 12 | Derek followed up with technical clarification on CPU vs GPU routing |
| Mar 13 | Meryem confirmed Doubleword is well-suited for Trinity's escalation routing |
| Mar 17 | Derek submitted Palantir Startup Fellowship application (Doubleword featured as Tier 0) |
| Mar 17 | Derek sent detailed email with benchmark results, 6 technical questions, and linked the integration doc on GitHub |
| Mar 17 | LinkedIn: Meryem said "Let's hop on a call later this week" |
| Mar 19 | LinkedIn: Agreed on Friday (rescheduled to Sunday Mar 23) |
| Mar 22 | LinkedIn: Confirmed meeting — "chat then! have a good weekend :)" |

**Key dynamic:** She reached out to *you*. You are not being sold to — this is a mutual exploration. You've already done the benchmark, written the integration doc, and have a concrete use case. That puts you in a strong position.

---

## Meeting Objectives

1. Get answers to your 6 technical questions from the Mar 17 email
2. Establish a direct technical relationship (not just email threads)
3. Negotiate extended trial credits beyond the 20M token starter
4. Confirm the 397B model is available and priced the same as the 35B
5. Explore partnership angle — Palantir fellowship, reference customer potential

---

## What to Have Open Before the Call

- [ ] GitHub integration doc (rendered): `https://github.com/drussell23/JARVIS/blob/main/docs/integrations/DOUBLEWORD_INTEGRATION.md`
- [ ] Your email thread with Meryem (for reference if needed)
- [ ] Doubleword app: `https://app.doubleword.ai`
- [ ] This document (for quick reference during the call)
- [ ] A blank notes doc for capturing her answers

---

## Suggested Flow (60 min)

| Time | Topic | Notes |
|------|-------|-------|
| 0-5 min | Intros & context setting | Let her give a quick Doubleword overview. Then: "I've already benchmarked your API and wrote a full integration guide — happy to walk you through what I found." |
| 5-15 min | Screen-share the GitHub integration doc | Walk through: 3-tier routing diagram, 29x cost chart, token budget finding. This is more impressive than any slide deck. |
| 15-30 min | Your 6 technical questions | The meat of the call. Let her talk. Take notes. |
| 30-40 min | Partnership & credits discussion | Extended trial, startup pricing, Palantir angle |
| 40-50 min | Her roadmap & alignment with Trinity | Real-time endpoints? Self-hosted Control Layer? New models? |
| 50-60 min | Next steps & action items | Agree on follow-ups |

---

## Your Numbers (Know These Cold)

### Benchmark Results (Batch ID: `ca6b7b1f-da63-4c44-ac8e-e9e8b796eae4`)

| Metric | Value |
|--------|-------|
| Cost savings | **29x cheaper** (Doubleword $0.000376 vs J-Prime $0.010988) |
| Percentage savings | **96.6%** |
| Latency tradeoff | **7.8x slower** wall time (4.3 min vs 33s) |
| Latency impact on UX | **Zero** — batch is async, doesn't block the hot path |
| Break-even (6hr/day spot) | ~1,150 ops/day |
| Break-even (always-on) | ~4,600 ops/day |
| Current operating range | Well below break-even — Doubleword wins unambiguously |

### Per-Task Breakdown

| Task | J-Prime | Doubleword | Savings |
|------|---------|------------|---------|
| Secure Infrastructure Code | $0.009210 | $0.000288 | 32x cheaper |
| Defense Threat Analysis | $0.001778 | $0.000088 | 20x cheaper |

### Token Budget Finding

- Both tasks hit `finish_reason: length` — all output tokens consumed by reasoning layer
- `Qwen3.5-35B-A3B-FP8` has a separate `reasoning_content` field (chain-of-thought)
- Zero useful `content` output in the benchmark — but this is a calibration issue, not a product issue
- Fix: raise `max_tokens` — rule of thumb: `expected_output + 2x reasoning_overhead`
- Even with corrected budgets (~2.5x higher cost), still **11x cheaper** than J-Prime

---

## Trinity Architecture (What She Needs to Know)

```
Tier 0 — Doubleword Batch API                              [ASYNC]
  Model: Qwen3.5-397B-A17B-FP8 (397B total, ~17B active)
  Cost:  $0.10/1M input, $0.40/1M output
  SLA:   1-hour or 24-hour batch window
  Use:   Architecture reviews, cross-repo analysis, DPO scoring

Tier 1 — J-Prime (NVIDIA L4, GCP g2-standard-4)            [REALTIME]
  Model: Qwen2.5-Coder-14B-Q4_K_M (~24 tok/s)
  Cost:  ~$0.009/request (VM spot pricing)
  Use:   Standard governance ops, streaming inference

Tier 2 — Claude API                                         [REALTIME]
  Model: claude-sonnet-4-6
  Cost:  $3/1M input, $15/1M output
  Use:   Emergency fallback only
```

Routing decided by: `complexity_score` + `task_category` + `deadline_ms`

---

## MoE Economics (In Case It Comes Up)

- 397B total params, but only ~17B active per forward pass (Mixture of Experts)
- You get 397B-quality reasoning at ~17B compute cost
- Same token pricing across the entire Doubleword catalog — bigger models aren't more expensive per token
- This makes the 397B the obvious choice: same cost, dramatically better quality
- For DPO scoring: 397B judge produces higher-quality preference pairs at identical per-token cost

---

## Your Two Use Cases for Doubleword

### 1. Tier 0 Governance Operations
- Ultra-complex tasks routed when `complexity > 0.85` or `task_category in (CROSS_REPO_PLANNING, MULTI_FILE_ANALYSIS)`
- Architecture reviews, multi-repo code analysis, security audits
- Submitted as async batch, results retrieved when ready, governance pipeline continues in parallel

### 2. Reactor-Core DPO Scoring Pipeline
- 397B model acts as judge, scoring N candidate responses
- Preference pairs feed back into J-Prime fine-tuning
- **Batch coalescing**: all candidates per scoring cycle go into one JSONL, not one batch per candidate
- This is latency-insensitive by design — 1-hour or 24-hour window is fine

---

## Your 6 Technical Questions

These were sent in your Mar 17 email. Get definitive answers:

### 1. Reasoning Token Budgets
> Is there guidance on minimum `max_tokens` when using 35B/397B chain-of-thought models? My benchmark showed both tasks exhausting their budget entirely within the reasoning layer before producing output.

**Why it matters:** Without correct budgets, you pay for thinking but get zero usable output.

### 2. 397B Pricing
> Is `Qwen3.5-397B-A17B-FP8` priced at the same $0.10/$0.40 per 1M tokens as the 35B?

**Why it matters:** If pricing scales with model size, the cost projections in your integration doc change.

### 3. Control Layer Stability
> Is the Control Layer stable enough for single-tenant production deployment? Is the audit log schema exportable for Palantir AIP mapping?

**Why it matters:** The Control Layer → AIP audit trail mapping is a core argument in the Palantir fellowship pitch.

### 4. Autobatcher Batch ID Exposure
> Does the autobatcher surface the underlying batch ID for external tracking in my governance ledger?

**Why it matters:** Every operation in Ouroboros needs a traceable ID chain — `operation_id → batch_id → output_file_id`.

### 5. Data Retention
> How long are files retained after a batch completes? Is there an explicit deletion API for FedRAMP-adjacent workloads?

**Why it matters:** Defense use cases require data lifecycle control. Input data with threat intelligence or network topology cannot persist in uncontrolled storage.

### 6. Batch File Size Limits
> Is there a maximum number of requests per JSONL batch file?

**Why it matters:** Reactor-Core batch coalescing needs to know if there's a ceiling on how many DPO candidates can be scored in a single batch.

---

## Questions She Might Ask You

Prepare short, confident answers for these:

### About Trinity
| Question | Your Answer |
|----------|-------------|
| "What is Trinity exactly?" | "A governed, multi-tier AI inference system for defense and critical infrastructure. Three tiers: JARVIS (local supervisor with 50+ agents), J-Prime (self-hosted GPU inference on GCP), and Reactor-Core (continuous improvement via DPO fine-tuning)." |
| "How far along are you?" | "Solo founder, 5,400+ commits across 3 repos in 7 months, ~3M lines of code, deployed 24/7 on GCP with an NVIDIA L4. 2,132 governance tests at 99.3% pass rate." |
| "What's your scale right now?" | "Pre-revenue, pre-seed, bootstrapped. Current operations are well below the break-even threshold — single-digit to low double-digit ops per day during development." |
| "Who are your target customers?" | "Defense contractors and highly regulated enterprises needing SOC 2 / FedRAMP-compliant autonomous infrastructure." |

### About the Integration
| Question | Your Answer |
|----------|-------------|
| "How would you use Doubleword specifically?" | "Two paths: (1) Tier 0 for ultra-complex governance ops that exceed my L4's 14B model ceiling, and (2) 397B judge model for DPO preference scoring in my training pipeline." |
| "What model do you want to use?" | "The 397B — `Qwen3.5-397B-A17B-FP8`. Same token cost as the 35B, dramatically more reasoning capacity. It's the obvious choice for judge workloads." |
| "How many requests would you send?" | "Initially low — 10-50 ops/day during development. At production scale, the DPO pipeline could generate 100-500 scoring requests per training epoch, coalesced into a handful of batch jobs." |
| "Have you used the API already?" | "Yes — ran a live benchmark (batch ca6b7b1f) on March 18 using the 35B model. Cost, latency, and token volume all documented with charts." |

### About Palantir
| Question | Your Answer |
|----------|-------------|
| "What's the Palantir connection?" | "I applied to the Palantir Startup Fellowship on March 17. Trinity's architecture is the centerpiece — I'm positioning Doubleword as Tier 0 in the compute stack. The argument is: governed inference needs a batch-first provider to scale beyond single-GPU ceilings while maintaining audit trails through the Control Layer." |
| "How does Doubleword fit in the fellowship pitch?" | "Three ways: (1) scaling beyond the single-GPU ceiling, (2) providing high-quality DPO scoring via the 397B model, and (3) using the open-source Control Layer as an audit gateway that maps to Palantir AIP requirements." |

### About Your Background
| Question | Your Answer |
|----------|-------------|
| "What's your background?" | "2x NASA software engineer, letter of recommendation from Sam Altman. Building Trinity full-time as a solo founder." |

---

## What You Want From This Call

Be clear about your asks:

1. **Extended trial credits** — 20M tokens will burn through quickly once DPO scoring starts. Ask for 100M+ or a startup credit program.
2. **397B model access confirmation** — Is it available now for batch? Any waitlist?
3. **Startup/fellowship pricing** — Is there a program for pre-seed companies?
4. **Direct technical contact** — Slack channel, Discord, or a point person for integration questions?
5. **Permission to feature Doubleword** — Confirm she's comfortable being named in Palantir fellowship materials and the public GitHub integration doc.

---

## Their Roadmap (Things to Ask About)

- **Real-time/streaming endpoints** — Will Doubleword ever offer synchronous inference? Would change the Tier 0 routing calculus.
- **Self-hosted Control Layer** — For air-gapped defense deployments, can you run the Rust gateway on-prem?
- **Model catalog expansion** — Any code-specific models beyond Qwen coming? Larger models?
- **SOC 2 / compliance certifications** — On the roadmap? Timeline?
- **Webhook/callback on batch completion** — Instead of polling, can Doubleword POST to a webhook when a batch finishes? Would simplify the async governance pattern.

---

## Links to Review Tonight

| Resource | Why |
|----------|-----|
| [Your integration doc on GitHub](https://github.com/drussell23/JARVIS/blob/main/docs/integrations/DOUBLEWORD_INTEGRATION.md) | The main artifact you'll screen-share — skim it so the flow is fresh |
| [Doubleword app](https://app.doubleword.ai) | Familiarize with the dashboard in case she references it |
| [Doubleword API docs](https://api.doubleword.ai) | Review the batch API protocol (4-stage: upload → create → poll → retrieve) |
| [Your Palantir slide deck](/Users/djrussell23/Downloads/Trinity%20AI-3.pptx) | Don't present it, but know the content in case Palantir comes up |
| [Your Mar 17 email to Meryem](#) | Re-read your own email so you remember exactly what you sent |

---

## Do NOT

- Present the Palantir slide deck — it's built for a fellowship committee, not a CEO partnership call
- Oversell Trinity's current scale — you're a solo founder, she knows this, your strength is engineering depth
- Spend the whole hour talking — let her drive portions of the conversation
- Treat this as a sales call where you're being sold to — she came to *you*
- Forget to take notes on her answers to your 6 questions

---

## After the Call

- [ ] Send a follow-up email summarizing key takeaways and agreed next steps
- [ ] Update the integration doc with any new technical details from the call
- [ ] If credits are extended, run a benchmark with the 397B model and correct token budgets
- [ ] Update the Palantir fellowship materials if anything changes
- [ ] Save meeting notes to `docs/meetings/` for future reference
