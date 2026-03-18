# Voice.ai TTS Benchmark Results

Date: 2026-03-18 15:01:07
Rounds: 5 | Warmup: 1

```

==========================================================================================
  BENCHMARK COMPARISON -- Voice.ai vs macOS Daniel (JARVIS baseline)
==========================================================================================
  Provider        Protocol     Sentence  TTFB Mean   TTFB P95  Std Dev      Total  Success
------------------------------------------------------------------------------------------
  macOS_Daniel    local        short      2450.5ms   3568.2ms  899.6ms   5087.9ms  100.0%
  macOS_Daniel    local        medium     6655.8ms  14863.4ms 5965.0ms  14938.8ms  100.0%
  macOS_Daniel    local        long       3953.2ms   8481.8ms 3283.9ms  24045.6ms  100.0%
  Voice.ai        http_stream  short       584.2ms    714.3ms  139.6ms    962.6ms  100.0%
  Voice.ai        http_stream  medium      363.6ms    404.0ms   29.8ms   1925.8ms  100.0%
  Voice.ai        http_stream  long        533.8ms    946.8ms  300.0ms   6147.2ms  100.0%
  Voice.ai        http_sync    short       800.7ms    955.4ms  115.8ms    800.7ms  100.0%
  Voice.ai        http_sync    medium     2115.2ms   2331.6ms  158.8ms   2115.2ms  100.0%
  Voice.ai        http_sync    long       5890.1ms   5962.7ms   65.1ms   5890.1ms  100.0%
  Voice.ai        websocket    short       767.5ms    806.7ms   46.4ms   1240.3ms  100.0%
  Voice.ai        websocket    medium      797.0ms    885.7ms   66.0ms   2524.2ms  100.0%
  Voice.ai        websocket    long        886.1ms   1049.3ms  144.6ms   6524.3ms  100.0%
------------------------------------------------------------------------------------------

  Voice.ai WS TTFB vs macOS Daniel TTFB (medium): -5858.8ms
  VERDICT: Voice.ai WebSocket is within 200ms conversational threshold
```
