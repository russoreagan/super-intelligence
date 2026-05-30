# Eval / Langfuse Audit — Handoff Notes

Working notes for continuing the evaluator audit + fixes. Branch:
`claude/langfuse-eval-audit` (PR #5). Safe to delete once the work lands.

## Resume

```bash
git fetch origin
git checkout claude/langfuse-eval-audit
git pull
uv sync --all-groups

# Live audit (needs LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY in env):
uv run python -m eval.langfuse_audit --since-days 7

# Offline audit of the committed export (no keys needed):
uv run python -m eval.langfuse_audit --from-file 1780086675271-lf-events-export-*.jsonl
```

> Note: the Claude Code **web** sandbox does not inherit a local machine's env, so
> the live audit only runs where `LANGFUSE_*` is configured (a local CLI session, or
> the web environment's configured env vars).

## Two evaluator execution paths

| Path | What | v4.6.1 status |
|---|---|---|
| 8 native UI evaluators (voice/grounding/self_model + others) | run server-side in Langfuse, model is UI-configured | independent of SDK |
| in-process judges (emotion / quality+pipeline+novelty / learning), gated by `BRAIN_EVAL_*` | `brain/observability/timeline.py` via `create_score` | OK |
| `eval/langfuse_batch_eval.py` (CLI) | `api.trace.list` + `create_score` | OK |
| `eval/langfuse_judge.py` (CLI) | was `fetch_traces`/`score` (removed) → **ported to v4** | fixed |

Score `source` distinguishes them in the live API: `API` = local SDK/CLI,
`EVAL` (+`config_id`) = native UI evaluator, `ANNOTATION` = human. The flattened
**export** does NOT carry `source` — use the live audit for the authoritative split.

## Done & pushed (PR #5)

- `eval/langfuse_audit.py` — live (`--since-days`) + offline (`--from-file`) auditor;
  flags CONSTANT / STUCK-AT-0.5 / LOW-MEAN / LOW-VARIANCE / OUT-OF-RANGE / MULTI-SCORED.
- v4 SDK port of `langfuse_judge.py`; `0.5`→`null`/`SKIP` not-applicable handling;
  `_score` no longer fabricates `0.5` on error/parse-failure.
- NE + AEA added to every neuromodulator guide (emotion / self_model / pipeline / novelty).
- Comparative quality judge **blinded** (randomized A/B, `_unblind_quality`, unit-tested)
  + **information-matched baseline** (`BRAIN_EVAL_BASELINE_FAIR`, default on);
  `report.py` aggregation made None-safe.
- New evaluators: memory **faithfulness** (`scorer.py`), **safety/boundary**
  (`langfuse_batch_eval.py` + UI guide `[9]`), cross-session **identity**
  (`eval/identity_judge.py`), **skill-selection** efficacy (`eval/skill_judge.py`).
- `voice.speakability` prompt **recalibrated** (CLI + JSON + UI guide).
- 951 tests pass; no `brain/` behavior change.

## Findings from the export (3,752 records, 256 scored brain-turns)

- 🔴 `voice.speakability` — broken: mean 0.30, 100/256 at exactly 0.0 on clearly
  speakable conversational text (graded against a written-prose standard). Prompt now
  rewritten → **needs a rerun** to regenerate scores.
- 🟡 `self_model.calibration` 77% / `self_model.coherence` 71% / `grounding.*` ~47%
  stuck at exactly 0.5 — the not-applicable sentinel. Native UI evaluators are
  numeric-only, so fix via the updated UI prompts and/or a target filter.
- ⚪ Produced ZERO scores: `critic.*`, `thought.*`, and (not even in the export)
  `emotion.* / judge.* / pipeline.* / novelty.*` — in-process judges + DMN-thought
  scoring weren't submitting in that window.
- Several names show double-scoring (local CLI + native UI writing the same name).

## Pending (do where keys exist)

1. Run live `langfuse_audit --since-days N` → confirm per-score `source`; prune the
   local-vs-native double-scoring.
2. Update native UI evaluators from `python -m eval.langfuse_batch_eval --print-setup`
   (speakability, self_model.*, grounding.directness, and the new safety.boundary `[9]`).
3. Re-run `langfuse_batch_eval` to regenerate clean scores under the fixed prompts.
4. Turn on the in-process judges (`BRAIN_EVAL_*`) so critic/emotion/quality/faithfulness
   actually populate; verify they submit.
5. Separate from eval: the `scattered`/GABA caveat for the merged paper (the 10%
   prevalence reflects a since-fixed GABA miscalibration).

## Still open for discussion

- Compute-matched (not just info-matched) baseline — give the baseline a comparable
  multi-call budget, not just the same memory.
- Whether to deprecate `langfuse_judge.py` in favor of `langfuse_batch_eval.py`
  (overlapping voice/self_model/grounding coverage).
