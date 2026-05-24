---
name: dndtale
description: A comprehensive DnD campaign and adventure creation skill for game masters and creative content creators. Helps design complete campaigns, adventures, NPCs, encounters, maps, and storylines tailored for tabletop play. Use this when designing D&D content, creating campaign worlds, developing adventure hooks, designing encounters, or building narrative structures for player tables.
disable-model-invocation: true
---

# Dndtale - DnD Campaign & Adventure Creator

Dndtale is a specialized skill designed to assist Dungeon Masters and creative content creators in building complete, engaging Dungeons & Dragons campaigns and adventures.

---

## Quick Start

### For New Campaigns

1. **Use TodoWrite** immediately to create a planning checklist
2. **Follow the workflow:** [workflows/campaign-creation-workflow.md](workflows/campaign-creation-workflow.md) (create if missing)
3. **Use AskUserQuestion** to gather requirements if not provided
4. **Use templates:** All templates in [templates/](templates/) (create if missing)
5. **Quality check:** Use [checklists/campaign-quality-checklist.md](checklists/campaign-quality-checklist.md) when done

### For Updating Existing Campaigns

1. **Read existing content** before making changes
2. **Follow the iteration workflow:** [workflows/iteration-workflow.md](workflows/iteration-workflow.md)
3. **Check consistency:** Use [checklists/consistency-checklist.md](checklists/consistency-checklist.md)
4. **Use Edit tool** for targeted changes to existing files

---

## What Dndtale Does

This skill helps you create:

- **Complete Campaigns** - Multi-session story arcs with interconnected plots, factions, and long-term consequences
- **One-Shot Adventures** - Single-session adventures with clear objectives and satisfying conclusions
- **NPCs** - Memorable characters with personalities, motivations, secrets, and stat blocks
- **Locations** - Detailed settings with atmosphere, history, and interactive elements
- **Encounters** - Balanced challenges with multiple solutions and meaningful consequences
- **Story Frameworks** - Narrative structures that preserve player agency while ensuring coherent plots
- **Image Prompts** - Detailed prompts for AI image generation tools

---

## Core Principles

### Player Agency First
- Always provide multiple solutions to problems
- Design consequences that matter
- Avoid railroading (forced single paths)
- Let player choices shape the story

### Usability at the Table
- Write clear, scannable DM notes
- Provide concise read-aloud text
- Include quick reference tables
- Anticipate common DM needs

### Completeness and Consistency
- Cross-reference between documents
- Maintain timeline and logic
- Keep names and facts consistent
- Check dependencies when changing content

---

## File Organization

Every campaign should follow this structure:

```
campaigns/[campaign-name]/
├── campaign-overview.md
├── changelog/
├── README.md
├── chapter-01.md
├── chapter-02.md
├── chapters-summary.md
├── npcs.md
├── locations.md
├── factions.md
├── timeline.md
└── art/
```

---

## Workflow Overview

### Creating a New Campaign

**Phase 1: Gather Requirements**
1. Use TodoWrite to create planning checklist
2. Use AskUserQuestion if briefing incomplete
3. Collect: story idea, length, level, setting, tone

**Phase 2: Campaign Framework**
1. Choose campaign type (Linear, Sandbox, Event-Based, Setting-Based)
2. Create campaign-overview.md
3. Plan chapter breakdown
4. Create chapters-summary.md
5. Identify major NPCs and locations

**Phase 3: Detailed Development**
1. Write each chapter
2. Detail NPCs (use npcs.md template)
3. Detail locations (use locations.md template)
4. Create factions if needed

**Phase 4: Player-Facing Content**
1. Write README.md (spoiler-free)
2. Ensure NO SPOILERS in the briefing

**Phase 5: Polish & QA**
1. Create image prompts for key scenes
2. Run campaign-quality checklist
3. Read entire campaign for flow and consistency

### Updating an Existing Campaign

1. **Read** all affected files first
2. **Plan** changes and identify dependencies
3. **Edit** existing files with targeted changes
4. **Update** cross-references
5. **Check** consistency with consistency checklist

---

## Important Guidelines

### Always Do This

- **Use TodoWrite for Complex Tasks** - Create planning checklist, track progress, one task in_progress at a time
- **Ask Questions When Needed** - Clarify tone, content boundaries, player preferences
- **Read Before Editing** - Always read existing files before using Edit
- **Preserve Player Agency** - Multiple solutions, meaningful consequences, avoid forced single paths
- **Follow Templates** - Maintain consistent formatting and required sections

### Never Do This

- **Don't Railroad Players** - Never force a single solution or invalidate player choices
- **Don't Skip Quality Checks** - Use checklists before completion, verify cross-references
- **Don't Break Existing Content** - When editing, maintain story logic and update all references

---

## Formatting Standards

**Read-Aloud Text:**
```markdown
> Text the DM reads to players
> Detailed, evocative, multi-sensory
> Present tense, no secrets
```

**DM Notes:** Regular text with mechanical details, secrets, contingencies

**Stat Blocks:** Reference Monster Manual when possible, or provide custom stats

**Cross-References:** Use markdown links: `[Chapter 2](chapter-02.md)` or `[NPCs](npcs.md#npc-name)`

---

## Session Zero Considerations

When content might be disturbing or NSFW:
- Include content warnings in README.md
- Suggest Session Zero discussion topics
- Recommend safety tools (X-Card, Lines & Veils)
- Clearly mark mature content

---

## Success Criteria

A campaign is ready when:

- [ ] All chapters are complete and detailed
- [ ] NPCs have personality, motivations, and stats
- [ ] Locations are described with atmosphere and features
- [ ] Multiple solutions exist for every problem
- [ ] Cross-references are accurate
- [ ] Briefing is complete and spoiler-free
- [ ] Quality checklist passes
- [ ] DM can run Session 1 with current materials
