# Bypass Pattern Trend Report
**Generated:** 2026-06-06
**Analysis Period:** Recent Langfuse traces

## Executive Summary
Langfuse queries returned sparse bypass-related scoring data. The raw traces captured show two primary input types (`brain-job` and `dmn-thought`), but no explicit "bypass_count" or bypass-pattern scores were populated in the returned datasets.

## Data Quality Notes
- **Traces Retrieved:** 2 unique trace IDs with metadata
- **Score Filtering Attempts:** 5 queries with varying `score_name` filters yielded empty results
- **Implication:** Either bypass-count scoring is not yet instrumented in the current Langfuse schema, or the score name label differs from expected naming conventions

## Input Type Inventory
| Input Type | Count | Observed Patterns |
|-----------|-------|-------------------|
| brain-job | 1 | Goal-driven, high latency (64.6s) |
| dmn-thought | 1 | Thought-introspective, null latency |

## Baseline vs. Emerging Trends
- **No statistically significant trend shift detected** — dataset too sparse to establish frequency distributions
- **Recommendation:** Verify Langfuse instrumentation captures bypass-count scores with explicit naming (e.g., `bypass_count`, `attempted_bypasses`, `security_bypass_flag`)

## Next Steps
1. Audit Langfuse trace schema to confirm bypass-scoring instrumentation is enabled
2. Expand query window or increase `limit` parameter to capture larger sample size
3. Re-run analysis once score labels are confirmed

---
*Report flagged for low data coverage — findings inconclusive pending schema validation*