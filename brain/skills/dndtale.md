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
