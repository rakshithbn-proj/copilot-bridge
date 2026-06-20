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

_Measured 2026-06-20 · bridge v5.1.5 · localhost · prompt: "Reply with exactly one word: pong"_

| Benchmark | Rounds | Mean | p50 | p95 | p99 | Errors |
|-----------|-------:|-----:|----:|----:|----:|-------:|
| Health (HTTP only) | 30 | 7 ms | 2 ms | 17 ms | 17 ms | 0% |
| Sequential ask() | 5 | 3230 ms | 2122 ms | 6568 ms | 7420 ms | 0% |
| Streaming TTFT | 5 | 2064 ms | 2073 ms | 2159 ms | 2174 ms | 0% |
| Concurrent (3 workers) | 15 | 2298 ms | 2154 ms | 3325 ms | 3849 ms | 0% |

**Throughput (3 concurrent workers):** 1.13 req/s over 15 requests · 0 errors

Notable observations:
- **HTTP overhead is ~2–7 ms** — the bridge itself adds minimal latency; the bottleneck is the LLM
- **TTFT (2.1 s median) is faster than full round-trip (2.1–7.6 s)** — streaming gets the first token before the response completes
- **Concurrent p99 (3.8 s) > Sequential p99 (7.4 s at 5-round sample)** — VS Code Copilot serialises calls internally; concurrent requests queue rather than running truly in parallel

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
