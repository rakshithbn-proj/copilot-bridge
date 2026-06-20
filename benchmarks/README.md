# Benchmarks

Measures response latency and concurrent request handling for the Copilot Bridge.

## What it measures

| Benchmark | What it tests |
|---|---|
| **Health (HTTP only)** | Pure TCP + JSON round-trip, no LLM — isolates HTTP overhead |
| **Sequential ask()** | End-to-end LLM latency: one request at a time |
| **Streaming TTFT** | Time To First Token over SSE — what users perceive as "typing lag" |
| **Concurrent ask()** | Throughput and tail latency under parallel load |

## Prerequisites

1. VS Code open with the `copilot-bridge` extension active  
2. Python client installed:
   ```bash
   pip install copilot-bridge
   ```
   or from source:
   ```bash
   cd copilot-bridge-dist && pip install .
   ```

## Run

```bash
# Full benchmark (all 4 suites)
python benchmarks/benchmark.py

# HTTP-only (no VS Code required)
python benchmarks/benchmark.py --no-llm

# Higher load, save results
python benchmarks/benchmark.py --llm-rounds 10 --concurrency 5 --output benchmarks/results.json

# Print a Markdown table for pasting into docs
python benchmarks/benchmark.py --markdown
```

## Options

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bridge host |
| `--port` | `5150` | Bridge port |
| `--warmup N` | `2` | Warmup rounds (excluded from stats) |
| `--health-rounds N` | `30` | Rounds for `/health` benchmark |
| `--llm-rounds N` | `5` | Rounds for LLM benchmarks |
| `--concurrency N` | `3` | Parallel workers for load test |
| `--no-llm` | — | Skip all LLM benchmarks |
| `--output FILE` | — | Save results as JSON |
| `--markdown` | — | Print Markdown table |
| `--prompt TEXT` | `"Reply with exactly one word: pong"` | LLM prompt used in benchmarks |

## Results

_Measured 2026-06-20 · bridge v5.2.0 · localhost · prompt: "Reply with exactly one word: pong"_

| Benchmark | Rounds | Mean | p50 | p95 | p99 | Errors |
|-----------|-------:|-----:|----:|----:|----:|-------:|
| Health (HTTP only) | 30 | 7 ms | 2 ms | 17 ms | 18 ms | 0% |
| Echo (ext host, no LLM) | 30 | 8 ms | 2 ms | 19 ms | 24 ms | 0% |
| Sequential ask() | 20 | 1949 ms | 1885 ms | 2138 ms | 2651 ms | 0% |
| Streaming TTFT | 20 | 1958 ms | 1869 ms | 2829 ms | 2831 ms | 0% |
| Concurrent (5 workers) | 100 | 2018 ms | 1950 ms | 2732 ms | 3054 ms | 0% |

**Throughput (5 concurrent workers):** 2.45 req/s over 100 requests · 0 errors

**Latency breakdown (proven by isolation):**

| Layer | Overhead |
|---|---|
| HTTP stack (`/health`) | ~7 ms mean / ~2 ms p50 |
| + Auth + extension host (`/echo` − `/health`) | **+0.7 ms** |
| + Copilot LLM (`ask()` − `/echo`) | **+1941 ms** |

The bridge itself accounts for **< 1% of total latency**. The LLM is the bottleneck.

Notable observations:
- **TTFT ≈ full round-trip** (1958 ms vs 1949 ms mean) — for short prompts, the model completes almost immediately after the first token
- **Concurrent p99 (3.1 s) vs sequential p50 (1.9 s)** — VS Code Copilot serialises calls internally; concurrent requests queue rather than run in parallel
- **0 errors across 100 concurrent requests** — the bridge is stable under load

> Re-run with `python benchmarks/benchmark.py --markdown` to regenerate these numbers.

## Understanding the results

- **TTFT < Mean ask()** — expected; streaming starts before the full response is ready
- **Concurrent p99 >> Sequential p99** — queuing delay under load; VS Code Copilot serialises calls internally
- **Health mean ~3–5 ms** — baseline TCP + JSON deserialisation overhead for localhost

## Metrics glossary

| Metric | Meaning |
|---|---|
| **mean** | Average latency across all successful rounds |
| **p50** | Median — half of requests are faster than this |
| **p95** | 95th percentile — 95% of requests complete within this time |
| **p99** | 99th percentile — your worst-case SLA target |
| **TTFT** | Time To First Token — latency until the stream starts writing |
| **req/s** | Successful requests per second (wall-clock) |
