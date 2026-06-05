# Rocco status JSON schema (v1)

The Rocco-side agent writes this document to `~/.cache/rocco-status.json`
every ~5s. The Swift menubar app (`AIPulseCore/Sources/RoccoStatus.swift`)
decodes it verbatim.

```jsonc
{
  "schema_version": 1,
  "host": "rocco.cs.wm.edu",
  "ts": 1737759600,                  // unix seconds the snapshot was written
  "agent_uptime_s": 12345,           // how long rocco-agent has been alive
  "gpus": [
    {
      "idx": 0,
      "name": "NVIDIA A100 80GB PCIe",
      "util_pct": 42,                // 0-100
      "mem_used_mib": 36210,
      "mem_total_mib": 81920,
      "temp_c": 63,
      "power_w": 210
    }
  ],
  "vllm": {
    "running": true,
    "model": "moonshotai/Kimi-Dev-72B",   // null when running=false
    "port": 8000,
    "pid": 1234,                          // null when running=false
    "uptime_s": 7200
  },
  "services": [{"port": 8000, "proc": "vllm", "pid": 1234}],
  "tier": 4,                              // 1=worst → 5=best
  "tier_reason": "4 GPUs free",
  "inference_recent": {                   // null if vLLM not exporting /metrics
    "requests_last_5m": 17,
    "avg_latency_ms": 842.3
  },
  "errors": []
}
```

### Tier mapping (Swift `TierPalette`)

| tier | color           | meaning |
| ---: | :--             | :-- |
|    1 | systemRed       | unusable (no GPUs / hardware fault) |
|    2 | systemOrange    | degraded (vLLM offline) |
|    3 | systemYellow    | partial (high contention) |
|    4 | systemGreen     | healthy |
|    5 | systemMint      | excellent (low load + everything up) |
| else | systemGray      | malformed / unknown |

### Freshness mapping (Swift `IconState`)

| age of snapshot vs now | state         |
| ---                    | ---           |
| ≤ 60s                  | `.fresh`      |
| 60s … 600s             | `.stale`      |
| > 600s                 | `.veryStale`  |

`(snapshot=nil, lastError!=nil)` → `.unreachable`; both nil → `.unknown`.
