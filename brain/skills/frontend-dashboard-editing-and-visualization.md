---
name: dashboard-editing-and-visualization
description: Use when implementing or debugging charts/dashboards in this codebase, including chart-spec → renderer flow and dashboard layout edits via patch operations.
summary: Chart spec → renderer pipeline and dashboard layout operations (move/resize/remove widgets).
triggers: [chart, dashboard, visualization, widget, move, resize, layout, Recharts]
disable-model-invocation: true

---
# Dashboard Editing & Visualization (Definitive)

## Goal
Make safe, reversible changes to dashboards and charts by:\n+- understanding the chart spec → rendering pipeline\n+- using consistent chart data shapes\n+- applying layout edits as auditable “patch” operations\n+\n+## When to use
- You’re adding/modifying a chart type or chart behavior.\n+- A chart renders incorrectly (axes/series/legend/colors/tooltips).\n+- A user asks to move/resize/reorder/remove/add a dashboard widget.\n+\n+## Chart pipeline (high level)
1. **Intent → chart spec** (skills decide dimensions/measures/type)\n+2. **Chart spec JSON** (datasets + encodings + meta)\n+3. **Frontend builder** transforms spec into renderer-friendly shape\n+4. **Renderer** (Recharts) draws interactive chart\n+\n+## Chart types and shapes
### Single-series (typical)
- Data shape: `[{ name: xValue, value: number }, ...]`\n+- Used for simple line/bar/area.\n+\n+### Multi-series (breakdown dimension)
- Data shape: `[{ name: xValue, \"Series A\": n, \"Series B\": n }, ...]`\n+- Used for grouped/stacked bars and multi-line/stacked area.\n+\n+## Debugging charts (fast checklist)
- Confirm `ChartSpec.meta` describes the intended dimensions/metric.\n+- Confirm the transformed dataset matches the chart type.\n+- Validate series keys are stable and ordered.\n+- Check axis formatting and domain/scale.\n+- Ensure color assignment is deterministic per series.\n+\n+## Dashboard editing (layout ops)
### Use this for layout, not data
- Layout edits: move/resize/reorder/remove/add widgets, update metadata.\n+- Data edits / chart-type edits belong to chart generation “edit mode”.\n+\n+### Core operations
- `move_widget` (top/bottom/left/right/before/after)\n+- `resize_widget` (grid-unit constraints)\n+- `remove_widget`\n+- `add_widget`\n+- `reorder_widgets`\n+- `update_metadata`\n+\n+### Disambiguation rules
- If multiple widgets match “the chart”, ask which one and show a numbered list.\n+- If placement is ambiguous (“over there”), ask for top/bottom/left/right or reference widget.\n+\n+### Collision handling (principles)
- Detect overlap before applying.\n+- Prefer pushing other widgets down over silent overlap.\n+- Clamp to grid bounds; warn when layout is crowded.\n+\n+## Output format (dashboard edits)
Return a single patch operation object with `op_type`, `payload`, and `reason` so edits are auditable and reversible.

