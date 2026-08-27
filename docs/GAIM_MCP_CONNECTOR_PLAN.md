# gaim MCP Connector — Elyceum-side plan

**Date:** 2026-08-02
**Status:** Planned
**Companion plan:** `AI GM v2/docs/plans/ELYCEUM_MCP_CONNECTOR_GAIM_PLAN.md` (gaim side)
**Decision record:** `AI GM v2/docs/architecture/ELYCEUM_GM_ENGINE.md`

## Goal

Let the brain act as a tabletop-RPG party member (later possibly GM) for the gaim app,
using gaim's native functionality through its new MCP server. Phase 1 is config-only;
Phase 2 adds code only if the tool path proves too slow.

## Phase 1 — connector + persona (no code changes)

1. **Register the connector** on the Railway deployment. Either env-pinned:
   ```json
   BRAIN_CMA_MCP_SERVERS={"servers":[{
     "name": "gaim",
     "url": "https://<gaim-host>/api/mcp/game",
     "description": "Tabletop RPG game: read game state and characters, request player dice rolls, look up rules/skills.",
     "identity": true,
     "access_token": "<shared secret = gaim's ELYCEUM_GAME_MCP_SECRET>"
   }]}
   ```
   or via `POST /connectors` on the UI server (Supabase-backed; returns the secret once;
   rows are always `identity: true`). **`identity: true` is required** — env-pinned
   connectors default it to false; without it the `mcpu_` end-user token is never minted
   and gaim can't resolve the game session.
2. **Persona + agent.** `PUT /v1/personas/<slug>` for the party-member character
   (disposition/personality/speaking + chemistry baseline); a mandate holding the
   role text ("you are a party member in a live tabletop session; …"); agent
   `<slug>.<mandate_id>`. Scope the connector per-agent:
   `agents.permissions.motor_user_connectors = ["gaim"]` so other agents never see it.
3. **Session contract with gaim** (the only coupling): gaim creates sessions with
   `end_user_id = "<gameSessionId>:<agentRole>[:<playerId>]"`. The CMA executor mints the
   `mcpu_` token from exactly this value; gaim's `lib/mcp/identity.ts` resolves it.
4. **Transport + flags for tool turns:** use HTTP `POST /v1/sessions/{id}/turns` or the
   SSE variant (`inline_tools=True` by default → tool result folds into the same reply).
   Do NOT use the WS transport for tool turns (it defers tools to background jobs) and
   do NOT set `answer_only: true` on turns that may need tools (it suppresses
   `requires_action` entirely).

### Known constraints to design around (verified in code, 2026-08-02)

- Inline path: **1 tool per turn** (`BRAIN_MOTOR_INLINE_STEP_CAP`), 30 s wall clock
  (`BRAIN_MOTOR_INTERACTIVE_TIMEOUT_S`) — a slow cloud call dies at 30 s with
  `[tool_error]`.
- `_classify_action()` naming rules: gaim tools are named to hit `_READ_PREFIXES`
  (`get_game_state`, `get_character`, `get_skill_details`); `request_player_roll` and
  `roll_*` pass under `autonomy_approve_external_only=1` (default).
- Game-state writes: `is_write` is the Haiku planner's judgment. If the confirmation
  handshake fires on game actions, the lever is `motor_auto_confirm_writes` (org setting
  AND per-agent permission) — enable it **for the game agent only**, not org-wide.
- `temporal._looks_like_tool_request()`'s lexicon won't match game phrasing
  ("roll for initiative", "what's my HP") — tool triggering relies on the LLM
  `requires_action` flag / Approach stage. If under-triggering shows up in Phase 1
  testing, extending `_TOOL_REQUEST_PATTERNS` with game verbs is a two-line change.

## Phase 1 exit measurement

With the connector live, time 10–20 turns of each kind against Railway:
- plain conversational turns (this is also the ELYCEUM_GM_ENGINE Phase B latency number);
- tool turns ("check the party's situation" → `get_game_state`).

Expected from code reading: tool turns ~4–12 s warm (Haiku planner + a full
managed-agent session that reasons, calls the MCP tool, reports back), worse cold.
Party-member tolerance is ~3–5 s. If tool turns miss the bar → Phase 2.

## Phase 2 (contingent) — fast in-turn GameTools family

Copy the **trading-tools precedent**, the one genuinely fast tool path in the codebase:
`motor_cortex.set_trading_tools()` registers a tool family (`TOOL_NAMES` + `dispatch()`)
directly on the motor cortex; `_dispatch_once()` awaits it in-process — one Haiku planner
hop + one Python call (~1 s), no subprocess, no nested agent loop. The planner's system
prompt is rewritten to show each tool by name and signature.

- New `brain/clusters/game_tools.py`: `GameTools` class holding a persistent MCP client
  (or plain HTTP client) to gaim's MCP server, exposing the same 4 tools; mints/refreshes
  the `mcpu_` token itself (reuse `mint_end_user_token` from `cma_executor.py`).
- Wire via `motor_cortex.set_game_tools()` mirroring `set_trading_tools()` (registration
  site: `session_setup.py`, gated on a setting/env so it's off unless configured).
- Keep names `get_`-prefixed; describe tools to the planner as game-state utilities.
- The CMA connector from Phase 1 stays registered for anything outside the fast set.

## Verification

- Phase 1: `connectors_summary` shows `gaim`; a Railway turn invokes `get_game_state`
  and the reply contains real game state; per-agent scoping confirmed (an agent without
  `motor_user_connectors: ["gaim"]` cannot reach it); latency numbers recorded in the
  decision doc.
- Phase 2 (if built): unit tests alongside the trading-tools tests; tool-turn latency
  re-measured ≤ ~2 s warm; `pytest` green.
