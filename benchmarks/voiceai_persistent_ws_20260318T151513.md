# Voice.ai Persistent WebSocket Benchmark

Date: 2026-03-18 15:15:13
Rounds: 10 | Warmup: 1

```

===========================================================================
  PERSISTENT WEBSOCKET BENCHMARK RESULTS
  Connection handshake: 618.5ms (one-time)
===========================================================================

  Sentence    TTFB Mean   TTFB Med   TTFB P95   TTFB Min  Std Dev      Total
---------------------------------------------------------------------------
  short         268.7ms    268.7ms    268.7ms    268.7ms    0.0ms    611.0ms
---------------------------------------------------------------------------

  Comparison to macOS Daniel (from previous benchmark):
  macOS Daniel TTFB:  short=2451ms  medium=6656ms  long=3953ms
  Voice.ai persistent WS (short):  268.7ms (9.1x faster)

  200ms threshold for conversational AI: 
    Under 200ms: 0/1 (0%)
    Under 400ms: 1/1 (100%)
```
