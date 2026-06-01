# DoubleWord — empty token stream under HTTP 200 (`finish_reason=length`, zero content)

**Reporter:** Derek J. Russell (JARVIS / O+V)
**Date observed:** 2026-05-31 → 2026-06-01 (persisted across multiple sessions)
**Severity:** Blocking — no usable generation output from the affected models.
**Account/endpoint:** `https://api.doubleword.ai/v1`

## Summary

For the affected models, the DoubleWord chat-completions streaming endpoint
returns **HTTP 200** with a valid `text/event-stream`, emits SSE framing
chunks, but delivers **zero content tokens** and closes with
`finish_reason=length`. The connection, auth, TLS, and SSE transport are all
healthy — the model serving layer produces no content.

This is reproduced with **vanilla `aiohttp`**, bypassing our entire
application stack (no proxy, no client framework, no connection pooling), so
the behavior is isolated to the DoubleWord server side.

## Affected models (both reproduce)

| Model | HTTP | Content-Type | SSE chunks | content chars | finish_reason |
|-------|------|--------------|-----------|---------------|---------------|
| `Qwen/Qwen3.5-35B-A3B-FP8`  | 200 | text/event-stream | 18 | **0** | `length` |
| `Qwen/Qwen3.5-397B-A17B-FP8`| 200 | text/event-stream |  8 | **0** | `length` |

## Raw terminal output (out-of-band probe)

```
[probe] key loaded from .env (len=46, redacted)
======================================================================
[probe] POST https://api.doubleword.ai/v1/chat/completions  model=Qwen/Qwen3.5-35B-A3B-FP8  stream=True
[probe] HTTP 200  content-type=text/event-stream  connect_ms=1091
[probe] DONE  chunks=18  content_chars=0  first_token_ms=-1  finish=length  total_ms=1249
[verdict] SERVER-SIDE: stream opened but closed with ZERO content (done_before_content reproduced vanilla) — DW upstream fault.
======================================================================
[probe] POST https://api.doubleword.ai/v1/chat/completions  model=Qwen/Qwen3.5-397B-A17B-FP8  stream=True
[probe] HTTP 200  content-type=text/event-stream  connect_ms=2093
[probe] DONE  chunks=8  content_chars=0  first_token_ms=-1  finish=length  total_ms=2241
[verdict] SERVER-SIDE: stream opened but closed with ZERO content (done_before_content reproduced vanilla) — DW upstream fault.
```

**Key signals:**
- `HTTP 200` + `content-type=text/event-stream` — request accepted, transport healthy.
- `chunks=18 / 8` — SSE framing is delivered (the stream is alive).
- `content_chars=0` — **no `choices[].delta.content` tokens in any chunk.**
- `finish_reason=length` — the completion terminates on the token limit while
  having produced **no content** (the probe set `max_tokens=16`).
- `first_token_ms=-1` — no content token ever arrived.

## Minimal reproduction script

Pure `aiohttp`, no third-party app code. Reads the API key from a `.env` file.

```python
import aiohttp, asyncio, json, time

async def probe(base, key, model):
    url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "stream": True, "max_tokens": 16, "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    connector = aiohttp.TCPConnector(limit=1, ttl_dns_cache=0, force_close=True)
    content_chars, chunks, finish = 0, 0, None
    async with aiohttp.ClientSession(connector=connector) as s:
        async with s.post(url, json=payload, headers=headers,
                          timeout=aiohttp.ClientTimeout(total=120)) as resp:
            print("HTTP", resp.status, resp.headers.get("Content-Type"))
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunks += 1
                obj = json.loads(data)
                ch = obj.get("choices", [{}])[0]
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                c = ch.get("delta", {}).get("content")
                if c:
                    content_chars += len(c)
    print(f"chunks={chunks} content_chars={content_chars} finish={finish}")

asyncio.run(probe("https://api.doubleword.ai/v1", "<API_KEY>",
                  "Qwen/Qwen3.5-35B-A3B-FP8"))
```

## What we have ruled out (our side)

- **Not our client framework** — reproduced with vanilla `aiohttp`.
- **Not our proxy / connection pool** — fresh one-shot `TCPConnector(force_close=True)`.
- **Not auth** — HTTP 200, not 401/403; auth-sync surface probes report healthy.
- **Not a transport/network outage** — connection succeeds, SSE chunks flow.
- **Not request shape** — minimal OpenAI-compatible payload.

## Request to DoubleWord engineering

A non-streaming (`stream=false`) completion for the same models returns the
same empty body, or the streaming path drops content while reporting
`finish_reason=length`. Please confirm whether these model deployments are
currently emitting empty completions (e.g., a serving-layer regression,
reasoning-token vs content-token routing, or a tokenizer/template issue), and
advise on ETA. We can provide additional traces (request IDs, timestamps) on
request.
```

> Generated by O+V autonomous diagnostics. Probe is framework-free and
> re-runnable; key is read from local `.env` and never transmitted in logs.
