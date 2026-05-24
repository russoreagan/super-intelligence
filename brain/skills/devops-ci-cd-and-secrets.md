---
name: ci-cd-and-secrets
description: Use when designing or fixing CI/CD pipelines (GitHub Actions/GitLab) with safe deployments, approval gates, and secure secrets management.
summary: CI/CD pipelines, deployment strategies (rolling/canary/blue-green), approval gates, and secrets management.
triggers: [CI, CD, pipeline, deploy, GitHub Actions, GitLab, secrets, release]
disable-model-invocation: true

---
# CI/CD & Secrets (Definitive)

## Goal
Ship changes quickly **without** sacrificing safety by using:\n+- multi-stage pipelines (build → test → deploy)\n+- approval gates and progressive delivery\n+- secure secrets management\n+- fast feedback on CI failures\n+
## Pipeline blueprint
### Standard stages
1. Source/checkout\n+2. Build/package\n+3. Test (unit/integration)\n+4. Security scans (SAST/deps/container)\n+5. Deploy to staging\n+6. Smoke/E2E tests\n+7. Approval gate\n+8. Deploy to prod (rolling/canary/blue-green)\n+9. Verify + rollback plan\n+
## Deployment strategies (when to use)
- **Rolling**: default, low risk.\n+- **Blue/green**: high-risk releases, fast rollback.\n+- **Canary**: gradual exposure + monitoring.\n+- **Feature flags**: deploy without releasing; fast rollback.\n+
## Secrets management (non-negotiable)
### Principles
- Never commit secrets.\n+- Separate per environment.\n+- Least privilege for CI identities.\n+- Rotate regularly and after incidents.\n+- Mask secrets in logs.\n+
### Options
- Native CI secrets (GitHub/GitLab) for simpler setups.\n+- Vault / cloud secret managers for centralization + rotation.\n+- Add secret scanning (and treat findings as incidents).\n+
## Fixing failing CI
1. Identify failing check/job.\n+2. Pull logs and isolate the first real error.\n+3. Reproduce locally if possible.\n+4. Fix smallest issue; rerun targeted checks.\n+5. Confirm green before merging.\n+
