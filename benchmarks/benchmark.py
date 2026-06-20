#!/usr/bin/env python
"""
Copilot Bridge — Benchmark Suite
=================================
Measures response latency and concurrent request handling.

Benchmarks
----------
1. Health endpoint        — pure HTTP overhead, no LLM
2. Single-round ask()     — end-to-end LLM latency (sequential)
3. Streaming TTFT         — time to first token over SSE
4. Concurrent ask()       — throughput and tail latency under load

Usage
-----
    # Requires bridge running: VS Code with copilot-bridge-extension active
    python benchmarks/benchmark.py

    # Adjust load
    python benchmarks/benchmark.py --llm-rounds 10 --concurrency 5 --output results.json

    # Skip LLM tests (HTTP-only, safe without VS Code open)
    python benchmarks/benchmark.py --no-llm

Options
-------
    --host HOST           Bridge host           [default: 127.0.0.1]
    --port PORT           Bridge port           [default: 5150]
    --warmup N            Warmup rounds before measuring [default: 2]
    --health-rounds N     Health endpoint rounds [default: 30]
    --llm-rounds N        LLM ask() rounds       [default: 5]
    --concurrency N       Concurrent workers     [default: 3]
    --no-llm              Skip all LLM benchmarks
    --output FILE         Save JSON results to FILE
    --prompt TEXT         Prompt used for LLM benchmarks
                          [default: "Reply with exactly one word: pong"]
"""

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Path setup — works from repo root or as installed package
# ---------------------------------------------------------------------------
_script_dir = os.path.dirname(os.path.abspath(__file__))
_dist_dir = os.path.join(os.path.dirname(_script_dir), "copilot-bridge-dist")
if os.path.isdir(_dist_dir):
    sys.path.insert(0, _dist_dir)

try:
    from copilot_bridge import CopilotBridge
except ImportError:
    print("ERROR: copilot_bridge not found. Run: pip install copilot-bridge")
    print("       or run from the repo root so copilot-bridge-dist/ is discoverable.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def percentile(data: List[float], p: float) -> float:
    """Return the p-th percentile of data (0–100)."""
    if not data:
        return float("nan")
    sorted_data = sorted(data)
    idx = (p / 100) * (len(sorted_data) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (idx - lo)


def summarise(samples: List[float]) -> Dict[str, float]:
    """Return a dict of stats for a list of latency samples (in seconds)."""
    if not samples:
        return {"n": 0}
    ms = [s * 1000 for s in samples]  # convert to ms for readability
    return {
        "n": len(ms),
        "mean_ms": round(statistics.mean(ms), 1),
        "median_ms": round(statistics.median(ms), 1),
        "p95_ms": round(percentile(ms, 95), 1),
        "p99_ms": round(percentile(ms, 99), 1),
        "min_ms": round(min(ms), 1),
        "max_ms": round(max(ms), 1),
        "stdev_ms": round(statistics.stdev(ms) if len(ms) > 1 else 0.0, 1),
    }


# ---------------------------------------------------------------------------
# Individual benchmark functions
# ---------------------------------------------------------------------------

def bench_health(client: CopilotBridge, rounds: int, warmup: int) -> Dict:
    """Benchmark the /health endpoint — pure HTTP, no LLM involved."""
    url = f"{client.base_url}/health"
    headers = client._auth_headers()

    # Warmup
    for _ in range(warmup):
        requests.get(url, headers=headers, timeout=5)

    samples = []
    errors = 0
    for _ in range(rounds):
        t0 = time.perf_counter()
        try:
            r = requests.get(url, headers=headers, timeout=5)
            r.raise_for_status()
        except Exception:
            errors += 1
            continue
        samples.append(time.perf_counter() - t0)

    result = summarise(samples)
    result["errors"] = errors
    result["error_rate_pct"] = round(errors / rounds * 100, 1)
    return result


def bench_sequential_ask(client: CopilotBridge, prompt: str,
                          rounds: int, warmup: int) -> Dict:
    """Benchmark sequential ask() calls — one at a time, measures LLM round-trip."""
    # Warmup
    for _ in range(warmup):
        try:
            client.ask(prompt)
        except Exception:
            pass

    samples = []
    errors = 0
    for _ in range(rounds):
        t0 = time.perf_counter()
        try:
            reply = client.ask(prompt)
            if not reply:
                raise ValueError("Empty response")
        except Exception:
            errors += 1
            continue
        samples.append(time.perf_counter() - t0)

    result = summarise(samples)
    result["errors"] = errors
    result["error_rate_pct"] = round(errors / rounds * 100, 1)
    return result


def bench_streaming_ttft(client: CopilotBridge, prompt: str,
                          rounds: int, warmup: int) -> Dict:
    """
    Benchmark SSE streaming — measures Time To First Token (TTFT).

    TTFT is the time from sending the request until the first non-empty chunk
    arrives. This is the metric users perceive as 'time before it starts typing'.
    """
    url = f"{client.base_url}/chat/stream"
    headers = {**client._auth_headers(), "Content-Type": "application/json"}
    body = {"messages": [{"role": "user", "content": prompt}]}

    def _ttft() -> float:
        t0 = time.perf_counter()
        with requests.post(url, json=body, headers=headers,
                           stream=True, timeout=client.timeout) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data = json.loads(line[6:])
                if data.get("chunk"):
                    return time.perf_counter() - t0
        return float("nan")  # no chunk received

    # Warmup
    for _ in range(warmup):
        try:
            _ttft()
        except Exception:
            pass

    samples = []
    errors = 0
    for _ in range(rounds):
        try:
            t = _ttft()
            if t != t:  # nan
                errors += 1
            else:
                samples.append(t)
        except Exception:
            errors += 1

    result = summarise(samples)
    result["errors"] = errors
    result["error_rate_pct"] = round(errors / rounds * 100, 1)
    return result


def bench_concurrent(client: CopilotBridge, prompt: str,
                      concurrency: int, total_requests: int) -> Dict:
    """
    Benchmark concurrent ask() calls using a thread pool.

    Reports:
    - Latency stats across all concurrent requests
    - Throughput (requests per second, wall-clock)
    - Error rate
    """
    def _single_ask(_: int):
        # Each worker uses a fresh CopilotBridge instance to avoid shared state
        c = CopilotBridge(host=client._host, port=client._port,
                          auto_discover=False, api_key=client._api_key)
        t0 = time.perf_counter()
        try:
            reply = c.ask(prompt)
            ok = bool(reply)
        except Exception:
            ok = False
        return time.perf_counter() - t0, ok

    samples = []
    errors = 0
    wall_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_single_ask, i) for i in range(total_requests)]
        for future in as_completed(futures):
            elapsed, ok = future.result()
            if ok:
                samples.append(elapsed)
            else:
                errors += 1

    wall_elapsed = time.perf_counter() - wall_start

    result = summarise(samples)
    result["concurrency"] = concurrency
    result["total_requests"] = total_requests
    result["errors"] = errors
    result["error_rate_pct"] = round(errors / total_requests * 100, 1)
    result["wall_time_s"] = round(wall_elapsed, 2)
    result["throughput_rps"] = round(
        (total_requests - errors) / wall_elapsed, 2
    ) if wall_elapsed > 0 else 0.0
    return result


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def _bar(value: float, max_value: float, width: int = 20) -> str:
    """Render a simple ASCII bar chart segment."""
    if max_value == 0:
        return " " * width
    filled = int(round(value / max_value * width))
    return "█" * filled + "░" * (width - filled)


def print_report(results: Dict) -> None:
    meta = results["meta"]
    print()
    print("=" * 68)
    print("  Copilot Bridge — Benchmark Results")
    print(f"  {meta['timestamp']}  •  bridge v{meta['bridge_version']}")
    print(f"  host: {meta['host']}")
    print("=" * 68)

    # ── Health (HTTP only) ──────────────────────────────────────────
    h = results.get("health")
    if h:
        print()
        print("┌─ Health endpoint (HTTP only, no LLM) " + "─" * 29 + "┐")
        print(f"│  Rounds : {h['n']:<10}  Errors: {h['errors']} ({h['error_rate_pct']}%)")
        print(f"│  Mean   : {h['mean_ms']:>8.1f} ms    p95: {h['p95_ms']:>8.1f} ms")
        print(f"│  Median : {h['median_ms']:>8.1f} ms    p99: {h['p99_ms']:>8.1f} ms")
        print(f"│  Min    : {h['min_ms']:>8.1f} ms    Max: {h['max_ms']:>8.1f} ms")
        print("└" + "─" * 66 + "┘")

    # ── Sequential LLM ask() ───────────────────────────────────────
    s = results.get("sequential_ask")
    if s:
        print()
        print("┌─ Sequential ask() — LLM round-trip " + "─" * 30 + "┐")
        print(f"│  Rounds : {s['n']:<10}  Errors: {s['errors']} ({s['error_rate_pct']}%)")
        print(f"│  Mean   : {s['mean_ms']:>8.1f} ms    p95: {s['p95_ms']:>8.1f} ms")
        print(f"│  Median : {s['median_ms']:>8.1f} ms    p99: {s['p99_ms']:>8.1f} ms")
        print(f"│  Min    : {s['min_ms']:>8.1f} ms    Max: {s['max_ms']:>8.1f} ms")
        print("└" + "─" * 66 + "┘")

    # ── Streaming TTFT ─────────────────────────────────────────────
    t = results.get("streaming_ttft")
    if t:
        print()
        print("┌─ Streaming TTFT (time to first token) " + "─" * 27 + "┐")
        print(f"│  Rounds : {t['n']:<10}  Errors: {t['errors']} ({t['error_rate_pct']}%)")
        print(f"│  Mean   : {t['mean_ms']:>8.1f} ms    p95: {t['p95_ms']:>8.1f} ms")
        print(f"│  Median : {t['median_ms']:>8.1f} ms    p99: {t['p99_ms']:>8.1f} ms")
        print(f"│  Min    : {t['min_ms']:>8.1f} ms    Max: {t['max_ms']:>8.1f} ms")
        print("└" + "─" * 66 + "┘")

    # ── Concurrent ────────────────────────────────────────────────
    c = results.get("concurrent")
    if c:
        print()
        print(f"┌─ Concurrent ask() — {c['concurrency']} workers, {c['total_requests']} requests " + "─" * 18 + "┐")
        print(f"│  Succeeded : {c['n']:<7}   Errors: {c['errors']} ({c['error_rate_pct']}%)")
        print(f"│  Mean      : {c['mean_ms']:>8.1f} ms    p95: {c['p95_ms']:>8.1f} ms")
        print(f"│  Median    : {c['median_ms']:>8.1f} ms    p99: {c['p99_ms']:>8.1f} ms")
        print(f"│  Throughput: {c['throughput_rps']:>6.2f} req/s   Wall: {c['wall_time_s']:.1f}s")
        print("└" + "─" * 66 + "┘")

    print()


def print_markdown_table(results: Dict) -> None:
    """Print a copy-pasteable Markdown table for the README."""
    rows = []
    for key, label in [
        ("health", "Health (HTTP only)"),
        ("sequential_ask", "Sequential ask()"),
        ("streaming_ttft", "Streaming TTFT"),
        ("concurrent", f"Concurrent ({results.get('concurrent', {}).get('concurrency', '?')} workers)"),
    ]:
        d = results.get(key)
        if not d or not d.get("n"):
            continue
        rows.append((
            label,
            str(d.get("n", "—")),
            f"{d['mean_ms']:.0f} ms",
            f"{d['median_ms']:.0f} ms",
            f"{d['p95_ms']:.0f} ms",
            f"{d['p99_ms']:.0f} ms",
            f"{d.get('error_rate_pct', 0):.0f}%",
        ))

    print()
    print("### Benchmark Results")
    print()
    print(f"_Generated {results['meta']['timestamp']} · bridge v{results['meta']['bridge_version']}_")
    print()
    print("| Benchmark | Rounds | Mean | p50 | p95 | p99 | Errors |")
    print("|-----------|-------:|-----:|----:|----:|----:|-------:|")
    for r in rows:
        print(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} |")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Copilot Bridge benchmark suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5150)
    p.add_argument("--warmup", type=int, default=2,
                   help="Warmup rounds before measuring (default: 2)")
    p.add_argument("--health-rounds", type=int, default=30,
                   help="Rounds for /health benchmark (default: 30)")
    p.add_argument("--llm-rounds", type=int, default=5,
                   help="Rounds for LLM benchmarks (default: 5)")
    p.add_argument("--concurrency", type=int, default=3,
                   help="Concurrent workers for load test (default: 3)")
    p.add_argument("--no-llm", action="store_true",
                   help="Skip LLM benchmarks (HTTP-only mode)")
    p.add_argument("--output", metavar="FILE",
                   help="Save results as JSON to FILE")
    p.add_argument("--markdown", action="store_true",
                   help="Print Markdown table for README")
    p.add_argument("--prompt", default="Reply with exactly one word: pong",
                   help="Prompt used for LLM benchmarks")
    return p.parse_args()


def main():
    args = parse_args()

    print(f"Connecting to Copilot Bridge at {args.host}:{args.port} …")
    client = CopilotBridge(host=args.host, port=args.port, auto_discover=True)

    if not client.is_available():
        print("ERROR: Bridge not reachable.")
        print("  1. Open VS Code")
        print("  2. Ensure the copilot-bridge extension is installed and active")
        print(f"  3. Check that port {args.port} is not blocked")
        sys.exit(1)

    health = client.get_health()
    bridge_version = health.get("version", "unknown")
    print(f"Connected  v{bridge_version}  port {health.get('port', args.port)}")
    print()

    results: Dict = {
        "meta": {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bridge_version": bridge_version,
            "host": f"{client._host}:{client._port}",
            "prompt": args.prompt,
            "warmup": args.warmup,
        }
    }

    # ── 1. Health benchmark ────────────────────────────────────────
    print(f"[1/4] Health endpoint  ({args.health_rounds} rounds, {args.warmup} warmup) …")
    results["health"] = bench_health(client, args.health_rounds, args.warmup)
    h = results["health"]
    print(f"      mean {h['mean_ms']:.1f} ms  p95 {h['p95_ms']:.1f} ms  errors {h['errors']}")

    if args.no_llm:
        print()
        print("Skipping LLM benchmarks (--no-llm)")
    else:
        # ── 2. Sequential ask() ────────────────────────────────────
        print(f"[2/4] Sequential ask() ({args.llm_rounds} rounds, {args.warmup} warmup) …")
        results["sequential_ask"] = bench_sequential_ask(
            client, args.prompt, args.llm_rounds, args.warmup
        )
        s = results["sequential_ask"]
        print(f"      mean {s['mean_ms']:.1f} ms  p95 {s['p95_ms']:.1f} ms  errors {s['errors']}")

        # ── 3. Streaming TTFT ──────────────────────────────────────
        print(f"[3/4] Streaming TTFT   ({args.llm_rounds} rounds, {args.warmup} warmup) …")
        results["streaming_ttft"] = bench_streaming_ttft(
            client, args.prompt, args.llm_rounds, args.warmup
        )
        t = results["streaming_ttft"]
        print(f"      mean {t['mean_ms']:.1f} ms  p95 {t['p95_ms']:.1f} ms  errors {t['errors']}")

        # ── 4. Concurrent load ─────────────────────────────────────
        total = args.concurrency * max(args.llm_rounds, 2)
        print(f"[4/4] Concurrent       ({args.concurrency} workers × {total} requests) …")
        results["concurrent"] = bench_concurrent(
            client, args.prompt, args.concurrency, total
        )
        c = results["concurrent"]
        print(f"      mean {c['mean_ms']:.1f} ms  p95 {c['p95_ms']:.1f} ms  "
              f"{c['throughput_rps']:.2f} req/s  errors {c['errors']}")

    # ── Report ─────────────────────────────────────────────────────
    print_report(results)

    if args.markdown:
        print_markdown_table(results)

    if args.output:
        output_path = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"Results saved to: {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
