#!/usr/bin/env bash
# DW SSE stall isolation — tells us whether the stall is on DW's side or O+V's client.
#
# What it does:
#   - Hits DW's /v1/chat/completions with stream=true, agent-scale prompt
#   - Pipes the raw response through awk with per-line timestamps
#   - Lets you SEE every byte/line as it arrives in real time
#
# What to look for:
#
#   1. If you see lines arriving every <5s throughout (including `:` comment lines):
#      → DW emits keepalives. Your parser may be misclassifying them.
#      → CONCLUSION: O+V client-side issue (fixable by you alone).
#
#   2. If you see steady `data: {...}` lines, then NOTHING for 30+ seconds,
#      then more data: lines:
#      → DW does NOT emit keepalives. The wire genuinely goes silent.
#      → CONCLUSION: DW server-side gap. Keepalive absence is the root cause.
#      → Your 30s threshold trips because there's literally no data to read.
#
#   3. If you see steady `data: {...}` lines throughout with no 30s gaps at all:
#      → Either the issue is production-pipeline-context-specific (concurrent
#        load, multi-turn tool-loop framing) or has been fixed since Apr 14.
#      → CONCLUSION: §25.2 hypothesis #1 (resolved) or #2-#4 (context-dependent).
#
# Prerequisites:
#   - $DOUBLEWORD_API_KEY env var set
#   - $DOUBLEWORD_BASE_URL env var set (or override DW_URL below)
#
# Cost: 1 request, agent-scale prompt. ~$0.005-0.02 depending on output length.

set -euo pipefail

DW_URL="${DOUBLEWORD_BASE_URL:-https://api.doubleword.ai}/v1/chat/completions"
MODEL="${DW_MODEL:-Qwen/Qwen3.5-397B-A17B-FP8}"

if [[ -z "${DOUBLEWORD_API_KEY:-}" ]]; then
  echo "ERROR: \$DOUBLEWORD_API_KEY not set" >&2
  exit 1
fi

# Agent-scale reasoning prompt — ~1500 tokens input, asks for deep reasoning
# that will force a long "thinking" pause before token emission begins.
PROMPT=$(cat <<'EOF'
You are a senior systems architect. Think step by step through this problem
in detail before responding. Do NOT skip your reasoning — reason carefully.

Problem: Design a deterministic recursion-bounding mechanism for a self-
modifying AI system. The mechanism must guarantee that a sequence of
self-applied code mutations cannot escape a fixed safety envelope, even
under adversarial inputs. Your design must include:

1. A formal definition of the safety envelope and what "escape" means.
2. A bounded-iteration property with a concrete termination proof.
3. An adversarial input class against which the mechanism must hold.
4. A non-trivial example of an attempted escape and how the mechanism
   detects + rejects it.
5. The trade-offs vs. a learned classifier approach.

Take your time. Reason carefully. The answer should be ~800 tokens.
EOF
)

REQUEST_BODY=$(python3 -c "
import json, sys
body = {
    'model': '${MODEL}',
    'stream': True,
    'temperature': 0.2,
    'max_tokens': 1500,
    'messages': [
        {'role': 'user', 'content': '''${PROMPT}'''},
    ],
}
print(json.dumps(body))
")

echo "=========================================="
echo "DW SSE Stall Isolation Test"
echo "Model:   ${MODEL}"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="
echo "Watching every line as it arrives. Each line is timestamped with"
echo "milliseconds since the request started."
echo ""
echo "Look for: gaps of 30+ seconds between lines."
echo "Look for: ':' (comment-line) keepalive frames during pauses."
echo ""
echo "Press Ctrl-C if it stalls longer than ~60s and you've seen enough."
echo "=========================================="
echo ""

# Curl with -N (no buffering) and pipe through awk for timestamping.
# %.3f format gives milliseconds since the script started.
START_NS=$(date +%s%N)

curl -N -s \
  -H "Authorization: Bearer ${DOUBLEWORD_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -X POST "${DW_URL}" \
  -d "${REQUEST_BODY}" \
  | awk -v start_ns="${START_NS}" '
    {
      # Get current time in ns, compute elapsed seconds since start
      cmd = "date +%s%N"
      cmd | getline now_ns
      close(cmd)
      elapsed_ms = (now_ns - start_ns) / 1000000
      printf "[%8.3fs] %s\n", elapsed_ms / 1000, $0
      fflush()
    }
  '

echo ""
echo "=========================================="
echo "Test complete: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="
