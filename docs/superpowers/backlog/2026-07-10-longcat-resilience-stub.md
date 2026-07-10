# Backlog Stub — LongCat as the Tier-1 Resilience Lane (fill the unfunded Claude slot)

> **Status:** STUB / DESIGN-INTENT ONLY. No code. Sequenced **after Slice 4 merges** (touches the same
> exhaustion/gradient path Slice 4 T2 just modified — `candidate_generator.py`, `race_triage.py`).
> Supersedes-and-narrows the assessment in `memory/project_longcat_provider_candidate.md`.
> **User has approved writing this stub, NOT building it.** Build requires a separate go + Phase 0 below.

## Problem (the reframe from the 3rd assessment installment)

Production's Claude (Tier 1) slot is **currently unfunded**. The provider chain is effectively:

```
DW (Tier 0)  →  [dead Claude slot]  →  J-Prime GCE (Tier 2)
```

So a Run #14-class DW exhaustion has no cheap metered fallback — it falls straight through to
immortal-retry / quarantine / VM-ignition. **LongCat fills the Tier-1 hole** at ~$0.002–0.005/op
(vs ~$0.03 Claude), and — critically — turns the failover-lifecycle awaken trigger from a
single-vendor `dw_global_outage` into a **dual-vendor** outage (DW *and* LongCat both down usually
means the fault is on our side of the wire — exactly when the self-hosted node is the right answer).
J-Prime then awakens **less often and more correctly**. This does **not** replace J-Prime's
budget-exhaustion awaken vector: LongCat is still metered spend a $0 wallet refuses, so Slice 4's
zero-budget fail-fast is unaffected.

## The transport fork (MUST be decided, not blended)

The originating request conflated two transports. They select **different provider seams** and
**cannot be combined**:

| Path | Dialect | Endpoint | Reuses seam | Trade |
|------|---------|----------|-------------|-------|
| **A — native LongCat platform** (RECOMMENDED for the Claude-slot fill) | **Anthropic Messages** | LongCat host root; SDK appends `/v1/messages` | `aegis_provider_bridge.make_async_anthropic_client()` + `ClaudeProvider` + `_serialize_attachments` (native Claude blocks) | extended-thinking + prompt-caching semantics do **not** carry; prefill/stop-boundary risk |
| **B — OpenRouter** | **OpenAI chat-completions** | `openrouter.ai/api/v1` | the **DW / `doubleword_provider.py`** OpenAI-compat seam (NOT the Claude seam) | one more hop/markup; different serializer; no Anthropic-block reuse |

**Recommendation:** Path A to literally "swap out the unfunded Claude slot," because it reuses the exact
`AsyncAnthropic` construction seam the Claude provider already flows through — the same move Aegis
already makes (base_url swap, SDK still posts `/v1/messages`). Path B is a legitimate *second*
LongCat lane (OpenAI dialect via OpenRouter) but it is a **DW-shaped** lane, not a Claude-slot fill —
map it in policy as its own brain if pursued, do not pretend it reuses the Claude seam.

## The four mandates → concrete grounded seams

### 1. Root-Cause Only (no runtime string hot-patching of Claude URLs)

Precedent already exists and is the ONLY sanctioned mechanism: **`aegis_provider_bridge.py:96`
`make_async_anthropic_client(api_key=..., **extra_kwargs)`** is the single canonical `AsyncAnthropic`
factory. It *already* overrides `base_url` + `api_key` at construction when enabled ("SDK appends
`/v1/messages`; **DO NOT prepend `/v1`** to base_url — that produces `/v1/v1/messages`"). LongCat
Path A is the identical move: resolve `base_url`/`api_key` for the LongCat brain **at this factory's
instantiation boundary** from policy — never with string surgery on a hardcoded URL, never at the
`messages.create` call site. Extend the factory's resolution (or add a sibling resolver it delegates
to); do not fork it.

### 2. Architectural Purity (fully dynamic via `brain_selection_policy.yaml`)

Policy lives at **`backend/core/ouroboros/governance/brain_selection_policy.yaml`** (schema 1.1.0,
`brains:` list; each brain has `model_name`, `endpoint`, `cost_class`, context, artifact). Map every
LongCat parameter here — model id(s) (Flash-Chat / LongCat-2.0), the host-root `endpoint`, context
window, and **per-token input/output pricing** — as new `brains:` entries with an explicit
`dialect: anthropic|openai` field and a `provider_lane` tag. Zero platform constants in the execution
loops; the urgency→provider table (STANDARD/BACKGROUND/SPECULATIVE) reads the brain id from policy,
never a hardcoded `"longcat-..."`.

### 3. DRY (reuse the existing serialization + transport)

- **Serialization:** `providers.py:899 _serialize_attachments(provider_kind=...)` already emits native
  Claude image/document blocks — Path A gets it for free; BG/SPEC routes already strip attachments
  (which doubles as the data-governance control in §Caveats).
- **Transport:** reuse `ClaudeProvider`'s `_call_with_backoff` / breaker / cascade / `messages.create`
  + `messages.stream` unchanged. **No new client, no forked payload builder.** Path B, if pursued,
  reuses `doubleword_provider.py`'s OpenAI-compat client for the same reason — still no new client.

### 4. Bulletproof (translation-layer discrepancies — ref `feedback_claude_prefill_incompat`)

The Anthropic *dialect* being accepted does **not** guarantee Anthropic *semantics*. Before any
GENERATE traffic, the implementation MUST verify and pin:

- **Prefill / assistant-turn continuation:** probe whether LongCat honors a leading `assistant` message
  as prefill the way Claude does (`feedback_claude_prefill_incompat`). If it echoes/ignores/reorders it,
  the GENERATE prompt-assembler that relies on prefill must branch per-brain — a silent mismatch here
  corrupts the candidate JSON the FSM parses.
- **Output block boundaries & stop reasons:** confirm `stop_reason`, `content` block shape, and
  tool-use block framing match what the Venom tool loop + candidate parser expect. An unexpected shape
  must **fail loud** (typed parse error → normal GENERATE-retry), never silently truncate into a
  malformed patch. Add an adversarial contract test with a deliberately off-shape LongCat response.
- **Non-carrying features:** extended-thinking and prompt-caching are Claude-only — gate them off for
  the LongCat brain in policy so the provider doesn't send params the endpoint drops or rejects.

## Caveats (carried from the assessment, must survive into the build spec)

- **Data governance:** China-based vendor; O+V ships repo source in GENERATE prompts. Gate hosted
  LongCat to **BACKGROUND/SPECULATIVE + STANDARD** routes; keep **IMMEDIATE/COMPLEX** (heaviest
  repo-source shipping) on Claude/J-Prime until a deliberate governance call is made. Reuse the
  existing BG/SPEC attachment-stripping posture as the enforcement point.
- **Gradient/quarantine machinery is DW-shaped:** decide LongCat as either (a) another model in the
  DW-style ranked walk (cheap, semantically muddy) or (b) a per-vendor health lane (cleaner, more
  work). Interacts directly with Slice 4 T2's budget-vs-outage taxonomy — a LongCat failure must be
  classified on the same `is_budget_refusal` / provider-fault axis so it neither over-triggers the
  dual-vendor awaken nor gets counted as a DW outage.
- **Official surfaces only:** `longcat.ai` + GitHub `meituan-longcat`. `longcatai.org` is an SEO
  mirror — never enter keys there.

## Phased plan (bounded)

- **Phase 0 — Endpoint-dialect verification (GATE, ~$0):** Independently confirm LongCat actually
  serves an **Anthropic-Messages-compatible** endpoint (the assessment asserts this; it has **not**
  been verified against the live API in this repo). Run the Mandate-4 prefill + output-boundary probes
  against a throwaway key. **If Path A fails the probe, fall back to Path B (OpenRouter/OpenAI dialect)
  — do not force the Anthropic seam.** No further phase proceeds until this gates green.
- **Phase 1 — policy-gated BG/SPEC resilience lane:** LongCat as fallback for routes that today have
  **none** (a real DW outage currently kills them or wakes GCE). ~$1 soak to judge generation quality.
- **Phase 2 — promote to STANDARD Tier-1 (the real payoff):** fill the Claude slot; tighten the
  failover-lifecycle awaken from single-vendor to dual-vendor. Only if Phase 1 quality clears.
- **Phase 3 (optional, later):** golden-image open-weights eval (Flash-Lite 68.5B ≈35–40GB @4-bit does
  NOT fit the L4 24GB QUALITY tier — needs A100/2×L4; only worth a bake if it decisively beats the
  current 32B on GENERATE eval). Separate decision.

## Cross-refs

- `memory/project_longcat_provider_candidate.md` (full assessment, 3 installments)
- `feedback_claude_prefill_incompat` (Mandate 4 driver)
- `aegis_provider_bridge.py` (the base_url-swap precedent — the Mandate-1 seam)
- Slice 4 T2 (`session_budget_authority.is_budget_refusal`, `race_triage.is_budget_refusal_pair`) —
  the failure-taxonomy axis a LongCat lane must classify onto.
- `project-dw-reasoning-capability-profiler` (reasoning-profile pass pattern before trusting GENERATE).
