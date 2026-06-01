# DoubleWord Qwen3.5 reasoning-model integration spec

**Status:** authoritative — replaces the retracted `vendor_doubleword_empty_stream_repro.md`,
which incorrectly blamed DoubleWord for "empty token streams." That report was
**wrong**: DW serves correctly; the empty `content` was a client-side gap in how
O+V handled reasoning-model output. This document records the bare-metal API
reality, verified by direct out-of-band probes (2026-06-01).

## Root cause (corrected)

`Qwen/Qwen3.5-35B-A3B-FP8` and `Qwen/Qwen3.5-397B-A17B-FP8` are **reasoning
models**. They emit chain-of-thought into a `reasoning` field, and the final
answer into `content` only **after** reasoning completes. Two client-side
problems made this look like a DW outage:

1. **Token-budget starvation.** With a small `max_tokens`, the model spends the
   entire budget thinking and hits `finish_reason=length` before emitting any
   `content`.
2. **Reasoning-blind parsing.** Our streaming parser read only `delta.content`
   and our batch parser fell back to the **wrong field name**
   (`reasoning_content` — the real field is `reasoning`). During the reasoning
   phase `content` is empty, so the stream was misclassified as
   `done_before_content` → `live_transport` degraded.

## Verified response layout (non-streaming, `max_tokens=64`)

```json
{
  "choices": [{
    "index": 0,
    "finish_reason": "length",
    "message": {
      "role": "assistant",
      "content": null,
      "reasoning": "Thinking Process:\n1. Analyze the Request...",
      "reasoning_details": [{"format": "unknown", "index": 0, "text": "..."}],
      "refusal": null
    }
  }],
  "usage": {
    "prompt_tokens": 15,
    "completion_tokens": 64,
    "completion_tokens_details": {"reasoning_tokens": 62}
  }
}
```

- The answer text lives in `message.reasoning` (NOT `reasoning_content`) until
  the model exits the reasoning phase; the final answer then lands in
  `message.content`.
- `usage.completion_tokens_details.reasoning_tokens` reports the reasoning burn.

## Control knobs — probe matrix (Qwen3.5-35B, "Reply with exactly: OK")

| Request param | finish_reason | content | reasoning_tokens | Verdict |
|---|---|---|---|---|
| *(baseline)* `max_tokens=64` | `length` | `''` | 62 | reasoning eats the budget |
| `max_tokens=2000` | `stop` | `'OK'` | 131 | **content appears once reasoning fits** |
| `chat_template_kwargs={"enable_thinking": false}` | `length` | `''` | 62 | **IGNORED by DW** (the old code used this — it never worked) |
| `reasoning_effort="none"` | `stop` | `'OK'` | 0 | **WORKS** — straight to content, zero reasoning burn |

**Decisive:** `reasoning_effort="none"` is the OpenAI-standard knob DW honors;
`chat_template_kwargs.enable_thinking=false` is silently dropped.

## O+V integration rules (Slice 54)

1. **Send `reasoning_effort`** on every generation request (env
   `JARVIS_DW_REASONING_EFFORT`, default `none`). `none` makes Qwen3.5 behave as
   a normal completion model — fast, cheap, content-only. Raise to `low`/
   `medium` per route/complexity when chain-of-thought is wanted for quality.
   (The ineffective `enable_thinking:false` is retained only as a harmless
   belt-and-braces when effort is `none`.)
2. **Parse reasoning natively** for the reasoning-enabled case: treat
   `delta.reasoning` deltas as affirmative liveness (so the watchdog /
   surface-health never trip `done_before_content` during a long think), and
   read the answer from `content`, falling back to `reasoning` /
   `reasoning_details[].text` (the correct fields).
3. **Budget headroom** when reasoning is enabled: `max_tokens` must cover
   reasoning + the answer payload (the existing complexity-tiered
   `_compute_dynamic_max_tokens` already scales this).
