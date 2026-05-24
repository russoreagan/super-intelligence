---
name: prompting-and-eval
description: Use when designing, optimizing, or debugging prompts/agent instructions and you need reliable structured outputs, context-efficient prompting, and an evaluation loop to prevent regressions.
summary: LLM prompt design, structured outputs, context engineering, and evaluation loops for regressions.
triggers: [prompt, LLM, context, few-shot, evaluation, structured output, agent instructions]
disable-model-invocation: true

---
# Prompting & Evaluation (Definitive)

## Goal
One workflow for production-quality LLM prompting:\n+- prompt patterns (few-shot, templates, structured outputs)\n+- context engineering (what to include vs retrieve)\n+- evaluation (baselines, regression tests, judge frameworks)\n+\n+## Prompting workflow
### 1) Start simple, then add constraints
- Level 1: direct instruction\n+- Level 2: constraints + output format\n+- Level 3: examples (few-shot)\n+- Level 4: verification step\n+\n+### 2) Prefer structured outputs
- Define a schema (JSON shape / typed model).\n+- Add “what to do on failure” (retry, error section, partial output).\n+\n+### 3) Few-shot and templates
- Use 2–5 representative examples.\n+- Include edge-case examples.\n+- Use templated variables and keep stable instructions out of per-call prompts.\n+\n+### 4) Context engineering
- Treat context as finite.\n+- Prefer progressive disclosure: load only what you need.\n+- Put critical instructions early; keep noise out of the middle.\n+\n+## Evaluation loop (non-negotiable for changes)
### 1) Establish baselines
- Create a small test set of representative prompts/tasks.\n+- Record expected outputs or scoring rubric.\n+\n+### 2) Evaluate three ways
- Automated metrics (task-specific)\n+- Human review for nuanced quality\n+- LLM-as-judge (pointwise/pairwise)\n+\n+### 3) Regression prevention
- Run eval suite before/after changes.\n+- Track: accuracy/quality, cost (tokens), latency.\n+- If quality drops, revert or iterate.\n+\n+## Output format (recommended)
```\n+## Prompt\n+<final prompt or template>\n+\n+## Output schema\n+<json/pydantic shape>\n+\n+## Evaluation plan\n+- dataset: ...\n+- metrics/judge: ...\n+- pass criteria: ...\n+```\n+
