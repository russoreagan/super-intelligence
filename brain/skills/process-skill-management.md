---
name: skill-management
description: Create, review, install, and maintain skills and MCP integrations safely (structure, progressive disclosure, security scanning, and documentation-to-skill workflows).
summary: Create, review, and maintain skills and MCP integrations with security scanning and provenance tracking.
triggers: [skill, MCP, integration, create skill, install skill, docs to skill]
disable-model-invocation: true

---
# Skill Management (Unified)

## Intent
Use when you need to:\n+- create a new skill\n+- update/refactor an existing skill\n+- install skills from a repo\n+- convert docs into a reusable skill\n+- build an MCP server / add an integration\n+
## Canonical principles
- **Keep SKILL.md lean**; push bulk content into `references/`.\n+- **Progressive disclosure**: metadata → body → references/scripts.\n+- **Security first**: treat downloaded skills as untrusted until reviewed.\n+- **Provenance**: track source repo/path/license.\n+
## Skill anatomy (standard)
```
skill-name/
  SKILL.md
  SOURCES.md
  references/   (optional)
  scripts/      (optional)
  assets/       (optional)
```

## Creating/updating a skill
1. **Define triggers**: the `description` should be “Use when …” (when-to-use only).\n+2. **Write the workflow**: steps, decision points, and output formats.\n+3. **Split heavy docs**: move long material into `references/` and link it.\n+4. **Add deterministic scripts** for repeated operations.\n+5. **Verify** using “pressure scenarios” (TDD-for-skills): run baseline → add skill → confirm behavior.\n+
## Installing skills from Git repos (safe workflow)
1. Enumerate available skills (folders containing `SKILL.md`).\n+2. For each candidate, **download all files** (SKILL + scripts + references + assets).\n+3. **Security scan**:\n+   - suspicious shell exec / destructive ops\n+   - credential access (`~/.ssh`, `~/.aws`, env)\n+   - unexpected network calls\n+   - supply-chain installs\n+4. Only install approved skills.\n+
## Docs → skill conversion (new integrations)
When integrating a new API/platform:\n+1. Identify the official docs entry point.\n+2. Choose scraping mode (quick vs comprehensive).\n+3. Generate skill skeleton + categorized references.\n+4. Add a human-readable “how to use” workflow in SKILL.md.\n+5. Add security guidance + rate limit/pagination patterns.\n+
### REQUIRED: Context7 documentation check
Any time you add a new integration that depends on a library/framework:\n+- Resolve the library in Context7.\n+- Query up-to-date docs/examples.\n+- Prefer documented APIs over guesswork.\n+
## MCP server integration (design checklist)
- Tool naming consistency and discoverability\n+- Pagination and filtering\n+- Clear, actionable errors\n+- Structured outputs where possible\n+- Security hints (readOnly/destructive/idempotent)\n+- Evaluation questions for realistic usage\n+
