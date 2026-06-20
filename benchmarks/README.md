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

## Sample output

```
Connecting to Copilot Bridge at 127.0.0.1:5150 …
Connected  v5.1.5  port 5150

[1/4] Health endpoint  (30 rounds, 2 warmup) …
      mean 3.2 ms  p95 5.1 ms  errors 0
[2/4] Sequential ask() (5 rounds, 2 warmup) …
      mean 1843.0 ms  p95 2201.0 ms  errors 0
[3/4] Streaming TTFT   (5 rounds, 2 warmup) …
      mean 912.0 ms  p95 1050.0 ms  errors 0
[4/4] Concurrent       (3 workers × 15 requests) …
      mean 2104.0 ms  p95 3012.0 ms  1.38 req/s  errors 0

====================================================================
  Copilot Bridge — Benchmark Results
  2026-06-20T10:00:00Z  •  bridge v5.1.5
  host: 127.0.0.1:5150
====================================================================

┌─ Health endpoint (HTTP only, no LLM) ─────────────────────────────┐
│  Rounds : 30          Errors: 0 (0.0%)
│  Mean   :      3.2 ms    p95:      5.1 ms
│  Median :      3.0 ms    p99:      6.3 ms
│  Min    :      2.1 ms    Max:      8.4 ms
└──────────────────────────────────────────────────────────────────┘
...
```

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
