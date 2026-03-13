#!/usr/bin/env python3
"""
Benchmark: IPC latency and ML inference timing for Trinity AI.
Measures actual localhost HTTP roundtrip and inference response times.
"""

import asyncio
import time
import statistics
import json
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def benchmark_localhost_http_roundtrip(host: str = "127.0.0.1", port: int = 8000, iterations: int = 100):
    """Measure raw HTTP roundtrip latency to localhost (IPC benchmark)."""
    import aiohttp

    results = []
    url = f"http://{host}:{port}/health"

    connector = aiohttp.TCPConnector(limit_per_host=10)
    timeout = aiohttp.ClientTimeout(total=5.0)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # Warmup - establish connection pool
        for _ in range(5):
            try:
                async with session.get(url) as resp:
                    await resp.read()
            except Exception:
                print(f"[WARN] Could not reach {url} — is J-Prime running?")
                return None

        # Benchmark
        for i in range(iterations):
            start = time.perf_counter_ns()
            try:
                async with session.get(url) as resp:
                    await resp.read()
                elapsed_ns = time.perf_counter_ns() - start
                results.append(elapsed_ns / 1_000_000)  # Convert to ms
            except Exception as e:
                print(f"  Request {i} failed: {e}")

    return results


async def benchmark_tcp_connect_latency(host: str = "127.0.0.1", port: int = 8000, iterations: int = 100):
    """Measure raw TCP connection latency (no HTTP overhead)."""
    results = []

    for i in range(iterations):
        start = time.perf_counter_ns()
        try:
            reader, writer = await asyncio.open_connection(host, port)
            elapsed_ns = time.perf_counter_ns() - start
            results.append(elapsed_ns / 1_000_000)  # ms
            writer.close()
            await writer.wait_closed()
        except Exception:
            if i == 0:
                print(f"[WARN] Could not TCP connect to {host}:{port}")
                return None

    return results


async def benchmark_unix_pipe_latency(iterations: int = 1000):
    """Measure Unix pipe IPC latency as a baseline comparison."""
    results = []

    for _ in range(iterations):
        r_fd, w_fd = os.pipe()
        start = time.perf_counter_ns()
        os.write(w_fd, b"ping")
        os.read(r_fd, 4)
        elapsed_ns = time.perf_counter_ns() - start
        results.append(elapsed_ns / 1_000_000)  # ms
        os.close(r_fd)
        os.close(w_fd)

    return results


async def benchmark_asyncio_event_latency(iterations: int = 1000):
    """Measure asyncio event signaling latency (in-process IPC)."""
    results = []

    for _ in range(iterations):
        event = asyncio.Event()
        start = time.perf_counter_ns()

        async def setter():
            event.set()

        asyncio.get_event_loop().call_soon(event.set)
        await event.wait()
        elapsed_ns = time.perf_counter_ns() - start
        results.append(elapsed_ns / 1_000_000)  # ms

    return results


async def benchmark_inference_latency(host: str = "127.0.0.1", port: int = 8000, iterations: int = 10):
    """Measure actual ML inference latency against J-Prime."""
    import aiohttp

    results = []
    url = f"http://{host}:{port}/v1/generate"

    connector = aiohttp.TCPConnector(limit_per_host=5)
    timeout = aiohttp.ClientTimeout(total=60.0)

    payload = {
        "prompt": "Say hello in one word.",
        "max_tokens": 10,
        "temperature": 0.1,
    }

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # Warmup
        try:
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
                if resp.status != 200:
                    print(f"[WARN] Inference endpoint returned {resp.status}: {data}")
                    return None
        except Exception as e:
            print(f"[WARN] Could not reach inference endpoint {url}: {e}")
            return None

        # Benchmark
        for i in range(iterations):
            start = time.perf_counter_ns()
            try:
                async with session.post(url, json=payload) as resp:
                    data = await resp.json()
                elapsed_ns = time.perf_counter_ns() - start
                elapsed_ms = elapsed_ns / 1_000_000

                server_time = data.get("x_inference_time_seconds")
                server_ms = server_time * 1000 if server_time else None

                results.append({
                    "total_ms": elapsed_ms,
                    "server_inference_ms": server_ms,
                    "network_overhead_ms": (elapsed_ms - server_ms) if server_ms else None,
                })
                print(f"  Inference {i+1}/{iterations}: {elapsed_ms:.1f}ms total"
                      f"{f', {server_ms:.1f}ms server' if server_ms else ''}")
            except Exception as e:
                print(f"  Inference {i+1} failed: {e}")

    return results


def print_stats(name: str, values_ms: list[float]):
    """Print statistical summary."""
    if not values_ms:
        print(f"\n{'='*60}")
        print(f"  {name}: NO DATA (service not reachable)")
        print(f"{'='*60}")
        return

    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    print(f"  Samples:    {len(values_ms)}")
    print(f"  Min:        {min(values_ms):.4f} ms")
    print(f"  Max:        {max(values_ms):.4f} ms")
    print(f"  Mean:       {statistics.mean(values_ms):.4f} ms")
    print(f"  Median:     {statistics.median(values_ms):.4f} ms")
    print(f"  Stdev:      {statistics.stdev(values_ms):.4f} ms" if len(values_ms) > 1 else "")
    print(f"  P50:        {sorted(values_ms)[len(values_ms)//2]:.4f} ms")
    print(f"  P95:        {sorted(values_ms)[int(len(values_ms)*0.95)]:.4f} ms")
    print(f"  P99:        {sorted(values_ms)[int(len(values_ms)*0.99)]:.4f} ms")

    # Verdict
    mean = statistics.mean(values_ms)
    if mean < 1.0:
        verdict = "SUB-MS (< 1ms)"
    elif mean < 5.0:
        verdict = "LOW-LATENCY (1-5ms)"
    elif mean < 50.0:
        verdict = "MODERATE (5-50ms)"
    elif mean < 200.0:
        verdict = "ACCEPTABLE (50-200ms)"
    else:
        verdict = "HIGH (> 200ms)"
    print(f"  VERDICT:    {verdict}")


async def main():
    print("=" * 60)
    print("  TRINITY AI — IPC & INFERENCE BENCHMARK")
    print("=" * 60)
    print(f"  Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Platform: {sys.platform}")
    print()

    all_results = {}

    # --- IPC Benchmarks ---
    print("\n[1/5] Benchmarking Unix pipe IPC (baseline)...")
    pipe_results = await benchmark_unix_pipe_latency(1000)
    if pipe_results:
        print_stats("Unix Pipe IPC (baseline — fastest possible)", pipe_results)
        all_results["unix_pipe"] = pipe_results

    print("\n[2/5] Benchmarking asyncio event signaling...")
    event_results = await benchmark_asyncio_event_latency(1000)
    if event_results:
        print_stats("Asyncio Event Signal (in-process IPC)", event_results)
        all_results["asyncio_event"] = event_results

    print("\n[3/5] Benchmarking raw TCP connect to localhost:8000...")
    tcp_results = await benchmark_tcp_connect_latency("127.0.0.1", 8000, 100)
    if tcp_results:
        print_stats("Raw TCP Connect (localhost:8000)", tcp_results)
        all_results["tcp_connect"] = tcp_results

    print("\n[4/5] Benchmarking HTTP /health roundtrip to localhost:8000...")
    http_results = await benchmark_localhost_http_roundtrip("127.0.0.1", 8000, 100)
    if http_results:
        print_stats("HTTP /health Roundtrip (localhost:8000)", http_results)
        all_results["http_health"] = http_results

    # --- Inference Benchmark ---
    print("\n[5/5] Benchmarking ML inference (10 requests)...")
    inference_results = await benchmark_inference_latency("127.0.0.1", 8000, 10)
    if inference_results:
        total_times = [r["total_ms"] for r in inference_results]
        server_times = [r["server_inference_ms"] for r in inference_results if r["server_inference_ms"]]
        overhead_times = [r["network_overhead_ms"] for r in inference_results if r["network_overhead_ms"]]

        print_stats("ML Inference — Total (network + compute)", total_times)
        if server_times:
            print_stats("ML Inference — Server-Side Only", server_times)
        if overhead_times:
            print_stats("ML Inference — Network Overhead", overhead_times)
        all_results["inference"] = inference_results

    # --- Also try GCP remote inference ---
    print("\n[BONUS] Benchmarking HTTP /health to GCP VM (136.113.252.164:8000)...")
    gcp_http = await benchmark_localhost_http_roundtrip("136.113.252.164", 8000, 20)
    if gcp_http:
        print_stats("HTTP /health Roundtrip (GCP VM)", gcp_http)
        all_results["gcp_health"] = gcp_http

    print("\n[BONUS] Benchmarking ML inference to GCP VM...")
    gcp_inference = await benchmark_inference_latency("136.113.252.164", 8000, 10)
    if gcp_inference:
        total_times = [r["total_ms"] for r in gcp_inference]
        server_times = [r["server_inference_ms"] for r in gcp_inference if r["server_inference_ms"]]
        print_stats("GCP ML Inference — Total", total_times)
        if server_times:
            print_stats("GCP ML Inference — Server-Side Only", server_times)
        all_results["gcp_inference"] = gcp_inference

    # --- Summary ---
    print("\n" + "=" * 60)
    print("  SUMMARY FOR SLIDE DECK CLAIMS")
    print("=" * 60)

    if pipe_results:
        print(f"\n  Unix Pipe IPC:     {statistics.mean(pipe_results):.4f}ms mean")
    if event_results:
        print(f"  Asyncio Event:     {statistics.mean(event_results):.4f}ms mean")
    if tcp_results:
        print(f"  TCP Connect:       {statistics.mean(tcp_results):.4f}ms mean")
    if http_results:
        print(f"  HTTP Roundtrip:    {statistics.mean(http_results):.4f}ms mean")
    if inference_results:
        t = [r["total_ms"] for r in inference_results]
        print(f"  Local Inference:   {statistics.mean(t):.1f}ms mean")
    if gcp_inference:
        t = [r["total_ms"] for r in gcp_inference]
        print(f"  GCP Inference:     {statistics.mean(t):.1f}ms mean")

    print()
    print("  CLAIM VERDICTS:")

    if pipe_results and event_results:
        in_process_mean = statistics.mean(event_results)
        if in_process_mean < 1.0:
            print(f"  'sub-ms IPC': TRUE for in-process (asyncio) at {in_process_mean:.4f}ms")
        else:
            print(f"  'sub-ms IPC': FALSE for in-process at {in_process_mean:.4f}ms")

    if http_results:
        http_mean = statistics.mean(http_results)
        if http_mean < 1.0:
            print(f"  'sub-ms IPC': TRUE for HTTP at {http_mean:.4f}ms")
        else:
            print(f"  'sub-ms IPC': FALSE for HTTP at {http_mean:.4f}ms — suggest 'low-latency IPC'")

    if inference_results:
        server_times = [r["server_inference_ms"] for r in inference_results if r["server_inference_ms"]]
        if server_times:
            s_mean = statistics.mean(server_times)
            if s_mean <= 200:
                print(f"  '100-200ms ML Inference': TRUE — server inference at {s_mean:.1f}ms")
            else:
                print(f"  '100-200ms ML Inference': NEEDS ADJUSTMENT — server inference at {s_mean:.1f}ms")

    # Save results
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "benchmark_results.json")
    serializable = {}
    for k, v in all_results.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            serializable[k] = v
        elif isinstance(v, list):
            serializable[k] = {
                "min_ms": min(v),
                "max_ms": max(v),
                "mean_ms": statistics.mean(v),
                "median_ms": statistics.median(v),
                "p95_ms": sorted(v)[int(len(v)*0.95)],
                "p99_ms": sorted(v)[int(len(v)*0.99)],
                "samples": len(v),
            }

    with open(output_path, "w") as f:
        json.dump(serializable, f, indent=2)
    print(f"\n  Results saved to: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
