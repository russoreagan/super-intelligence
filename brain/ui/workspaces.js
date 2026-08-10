/* =====================================================================
   Workspaces — Labs / Agents / API as app-level surfaces.
   Labs is the existing app (#main), untouched; this module adds the masthead
   switcher and renders the Agents + API surfaces, bound to the live engine
   endpoints. Adapted from the Claude Design prototype (mock → real data).
   ===================================================================== */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  // 'labs' is the internal key for the live visualizer (the #main view); it's
  // surfaced to users as "MRI — Mood & Reasoning Interface". Key kept as 'labs'
  // so the #main plumbing and data-ws routing stay untouched.
  const WS_ICONS = {
    labs: '<circle cx="12" cy="12" r="9"/><path d="M7 12h2l1.5-3 2 6 1.5-3H17"/>',
    // Learning: rising trend over gridline — the wiring-drift view. Routes to the
    // MRI surface with the Learning page swapped in (ws key 'learning').
    learning: '<path d="M4 19h16M4 19V5"/><path d="m6 15 4-4 3 3 5-6"/><circle cx="18" cy="8" r="1.2"/>',
    agents: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    personas: '<path d="M12 3c3.6 0 6.5 2.4 6.5 6 0 5-3 9-6.5 9s-6.5-4-6.5-9c0-3.6 2.9-6 6.5-6z"/><circle cx="9.5" cy="10.5" r="1"/><circle cx="14.5" cy="10.5" r="1"/>',
    api: '<path d="m7 8-4 4 4 4M17 8l4 4-4 4M14 4l-4 16"/>',
  };
  const WS_NAMES = { labs: 'MRI', learning: 'Learning', agents: 'Agents', personas: 'Personas', api: 'API' };
  // The MRI dropdown glyph (scan ring + pulse), reused on every "Open in MRI" action.
  const MRI_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + WS_ICONS.labs + '</svg>';

  let workspace = 'labs';
  let _landed = false;      // first gating resolution lands on Agents (or Labs if locked)
  let isAdmin = false;      // platform super-user — sets ceilings + cross-org god view
  let orgAdmin = false;     // may manage THIS org's agents/roles/keys (within ceilings)
  let ownerEmail = '';
  let mandatesEnabled = false;
  let agentsData = null;      // { agents, roles, ceilings }
  let agentActivity = null;   // { agent_id: { count, lastTs } } from /agents/turns
  let agentUsage = null;      // { agent_id: { calls, cloud_calls, in_tok, out_tok, cloud_usd, pod_s, last_ts } }
  let agentUsageAll = null;   // [{ org_id, org_name, agent_id, … }] — superadmin all-orgs rows
  let usageRange = { key: 'today', since: null, until: null }; // dashboard date-range selector
  let usageScope = 'org';     // 'org' (this org) | 'all' (platform-superadmin fleet view)
  let _scopeChosen = false;   // true once the user picks a scope — stops the admin default re-applying
  let podStatus = null;       // /__pod_status — shared GPU pod uptime + accrued cost
  let podMeterTimer = null;   // ticking refresh while the Agents view is visible
  let agentSel = null;        // open agent_id
  let partnerKeys = null;
  let connectorsCache = null;

  // ── agent helpers (shared across rail / dashboard / list) ────────────────
  // "enabled" — the agent isn't paused. Distinct from real activity (see agentStatus).
  const isLive = (a) => !!a && a.enabled !== false;
  // An agent counts as actively running if it logged a turn within this window
  // (a proxy for "running now / in-flight" — the turn log is all the UI has).
  const ACTIVE_WINDOW_MS = 5 * 60 * 1000;
  // The status the dot communicates: paused (disabled) · active (ran in the last
  // ~5 min) · idle (enabled but quiet). Reads real last-activity from the
  // agent-turn log (agentActivity) rather than the mere enabled flag.
  function agentStatus(a) {
    if (!a || a.enabled === false) return { state: 'paused', color: 'var(--temporal)', cls: 'dot-status', label: 'paused' };
    const act = agentActivity && agentActivity[a.agent_id];
    const lastTs = act && act.lastTs ? act.lastTs : 0;
    if (lastTs && (Date.now() - lastTs) <= ACTIVE_WINDOW_MS)
      return { state: 'active', color: 'var(--ok)', cls: 'dot-status live', label: 'active' };
    return { state: 'idle', color: 'var(--ink-4)', cls: 'dot-status', label: 'idle' };
  }
  // Compact "time since" for tight rail/dashboard cells: 12s · 4m · 3h · 5d · —
  function agoShort(ms) {
    if (!isFinite(ms) || ms < 0) return '—';
    const s = Math.floor(ms / 1000);
    if (s < 10) return 'now';
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60); if (m < 60) return m + 'm';
    const h = Math.floor(m / 60); if (h < 24) return h + 'h';
    return Math.floor(h / 24) + 'd';
  }
  // Compact token counts: 840 · 12k · 1.2M.
  function fmtTokens(n) {
    n = n || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'k';
    return String(n);
  }
  // Hover detail for an agent's token cell: calls, in/out split, cloud spend.
  function usageTitle(u) {
    if (!u) return 'no model calls this session';
    const cloud = u.cloud_usd ? ` · $${u.cloud_usd.toFixed(2)} cloud spend` : '';
    return `${u.calls} model call${u.calls === 1 ? '' : 's'} · ${fmtTokens(u.in_tok)} in / ${fmtTokens(u.out_tok)} out · ${u.cloud_calls} cloud${cloud}`;
  }
  // Long-form duration for the pod uptime meter: 45s · 12m · 3h 12m · 2d 3h.
  function fmtDur(sec) {
    if (sec == null || !isFinite(sec) || sec < 0) return '—';
    sec = Math.floor(sec);
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m`;
    return `${sec}s`;
  }
  // The connectors an agent is granted (empty ⇒ inherits the org default set).
  function agentConnectors(a) {
    const p = (a && a.permissions) || {};
    const parse = (v) => (v ? String(v).split(/[\n,]/).map((s) => s.trim()).filter(Boolean) : []);
    return [...new Set([...parse(p.motor_user_connectors), ...parse(p.motor_self_connectors)])];
  }
  // ── cost = real cloud $ + pod compute-time × the pod's $/hr ───────────────
  // $/hr to value pod compute-seconds when the live pod rate is unknown (pod off /
  // historical range). Matches the typical secure-A40 rate; clearly an estimate.
  const POD_RATE_FALLBACK = 0.44;
  const RANGE_PRESETS = [
    { key: 'session', label: 'This session' },
    { key: 'today', label: 'Today' },
    { key: '7d', label: '7 days' },
    { key: '30d', label: '30 days' },
    { key: 'custom', label: 'Custom' },
  ];
  // Resolve the selected range to ISO [since, until]. 'session' → nulls (live meter).
  function rangeBounds() {
    const r = usageRange;
    if (r.key === 'session') return { since: null, until: null };
    if (r.key === 'custom') return { since: r.since || null, until: r.until || null };
    const now = Date.now();
    if (r.key === 'today') { const d = new Date(); d.setHours(0, 0, 0, 0); return { since: d.toISOString(), until: null }; }
    const days = r.key === '30d' ? 30 : 7;
    return { since: new Date(now - days * 86400000).toISOString(), until: null };
  }
  function podRate() { return (podStatus && podStatus.cost_per_hr) ? podStatus.cost_per_hr : POD_RATE_FALLBACK; }
  // An agent's cost over the loaded window: real cloud spend + pod compute time priced.
  function agentCostUsd(u) {
    if (!u) return 0;
    return (u.cloud_usd || 0) + (u.pod_s || 0) * podRate() / 3600;
  }
  function costTitle(u) {
    if (!u) return 'no model usage in this range';
    const podUsd = (u.pod_s || 0) * podRate() / 3600;
    return `$${(u.cloud_usd || 0).toFixed(4)} cloud + $${podUsd.toFixed(4)} pod (${Math.round(u.pod_s || 0)}s @ $${podRate().toFixed(2)}/hr)`;
  }
  // Switch the dashboard's date range: reload usage + pod rate, then re-render.
  async function setUsageRange(key, since, until) {
    usageRange = { key, since: since || null, until: until || null };
    await Promise.all([loadAgentUsage(), loadPodStatus()]);
    if (workspace === 'personas') {
      const pm = document.getElementById('pers-main');
      if (pm && perView === 'overview') renderPersonasView(pm);
      return;
    }
    const main = document.getElementById('ag-main');
    if (main && agView === 'agents') renderAgentsView(main);
  }
  // Re-fill the per-row cost / token / call cells in place (on the 30s tick), so a
  // ticking meter never tears down the table under the user's cursor mid-drag.
  function repaintUsageCells() {
    const ags = (agentsData && agentsData.agents) || [];
    let total = 0;
    document.querySelectorAll('#ws-agents .ag-table.roster .ag-row[data-agent]').forEach((row) => {
      const id = row.getAttribute('data-agent');
      const a = ags.find((x) => x.agent_id === id);
      if (!a) return;
      const live = isLive(a);
      const u = (agentUsage && agentUsage[id]) || null;
      total += agentCostUsd(u);
      const c = row.querySelector('[data-cost-for]');
      const t = row.querySelector('[data-tok-for]');
      const k = row.querySelector('[data-calls-for]');
      // A paused agent reads "—" across the board — never a misleading $0.00.
      if (c) { c.textContent = !live ? '—' : '$' + agentCostUsd(u).toFixed(2); c.title = costTitle(u); c.classList.toggle('zero', !live); }
      if (t) { t.textContent = !live ? '—' : fmtTokens(u ? (u.in_tok || 0) + (u.out_tok || 0) : 0); t.title = usageTitle(u); t.classList.toggle('zero', !live); }
      if (k) { k.textContent = !live ? '—' : String((u && u.calls) || 0); k.classList.toggle('zero', !live); }
    });
    const tot = document.getElementById('range-total');
    if (tot) tot.textContent = '$' + total.toFixed(2);
  }
  // Ordering rank for the roster's "sort by status" column: what's running first.
  const STATUS_RANK = { active: 0, idle: 1, paused: 2 };
  function agentLastActive(a) {
    const act = agentActivity && agentActivity[a.agent_id];
    return act && act.lastTs ? act.lastTs : 0;
  }

  // ── masthead dropdown ────────────────────────────────────────────────────
  function closeMenu() {
    $('#ws-menu').classList.remove('open');
    $('#ws-switch').classList.remove('open');
    $('#ws-switch-btn').setAttribute('aria-expanded', 'false');
  }
  function setWorkspace(ws) {
    if (ws !== 'labs' && $(`.ws-opt[data-ws="${ws}"]`)?.classList.contains('locked')) ws = 'labs';
    workspace = ws;
    $$('.ws-opt').forEach((t) => t.classList.toggle('on', t.dataset.ws === ws));
    $('#ws-cur-name').textContent = WS_NAMES[ws];
    $('#ws-cur-ico').innerHTML = WS_ICONS[ws];
    closeMenu();

    // Close the settings overlay if it's open (it belongs to Labs).
    const sp = document.getElementById('settings-page');
    if (sp && sp.classList.contains('visible') && typeof window.closeSettings === 'function') window.closeSettings();

    const main = document.getElementById('main');
    const ticker = document.getElementById('activity-ticker');
    const agents = document.getElementById('ws-agents');
    const personas = document.getElementById('ws-personas');
    const api = document.getElementById('ws-api');
    // 'learning' rides the MRI surface (same #main/#activity-ticker chrome) with
    // the Learning page swapped in over the atlas.
    const labs = ws === 'labs' || ws === 'learning';
    if (main) main.style.display = labs ? '' : 'none';
    if (ticker) ticker.style.display = labs ? '' : 'none';
    agents.classList.toggle('on', ws === 'agents');
    if (personas) personas.classList.toggle('on', ws === 'personas');
    api.classList.toggle('on', ws === 'api');
    // Search / filter / grouping are per-visit, not sticky across a workspace switch —
    // landing on a roster silently filtered by what you typed on the other surface
    // reads as "my agents are missing". The folder tree's open state DOES persist.
    if (ws === 'agents' || ws === 'personas') {
      rosterQ = ''; statusFilter = 'all';
      // The two rosters share a sort, but not every column: "role" has no meaning on
      // Personas. Fall back to the default rather than leaving a sort on a dead key.
      if (!ORG_SPECS[ws].cols.some(c => c[0] === rosterSort.k)) rosterSort = { k: 'cost', d: -1 };
    }
    if (ws === 'agents') ensureAgents();
    if (ws === 'personas') ensurePersonas();
    if (ws === 'api') ensureApi();
    // Learning shows its page; every other workspace resets the MRI surface.
    if (typeof window.showLearning === 'function') window.showLearning(ws === 'learning');
  }

  function wireSwitcher() {
    $('#ws-switch-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      const open = $('#ws-menu').classList.toggle('open');
      $('#ws-switch').classList.toggle('open', open);
      $('#ws-switch-btn').setAttribute('aria-expanded', String(open));
    });
    $('#ws-menu').addEventListener('click', (e) => {
      const t = e.target.closest('.ws-opt');
      if (t && !t.classList.contains('locked')) setWorkspace(t.dataset.ws);
    });
    document.addEventListener('click', (e) => { if (!$('#ws-switch').contains(e.target)) closeMenu(); });
  }

  // ── role gating ──────────────────────────────────────────────────────────
  async function loadGating() {
    try {
      const me = await fetch('/auth/me');
      if (me.ok) { const j = await me.json(); isAdmin = !!j.is_admin; orgAdmin = !!(j.org_admin ?? j.is_admin); ownerEmail = j.email || ''; }
    } catch (e) { isAdmin = false; orgAdmin = false; }
    // A platform super-user's own org is typically empty (it exists to monitor the
    // fleet), so default the dashboard to the cross-org "All orgs" view — otherwise
    // they land on an empty "My org" and think it's broken. They can still toggle
    // back. Range moves off "This session" since that's process-local (org-only).
    if (isAdmin && !_scopeChosen) { usageScope = 'all'; if (usageRange.key === 'session') usageRange = { key: 'today', since: null, until: null }; }
    try {
      const mr = await fetch('/agents');
      if (mr.ok) { const d = await mr.json(); mandatesEnabled = !!d.enabled; }
    } catch (e) { mandatesEnabled = false; }
    applyGating();
  }
  function applyGating() {
    // Agents: org-admin + hosted backend (manage this org's agents/roles within
    // the ceilings). API: org-admin (partner keys mint against this org; the
    // reference is informational). Plain members / companion fall back to Labs.
    // The cross-org "All orgs" view inside Agents stays platform-admin (isAdmin).
    // Personas: open to everyone (configuring your own persona is core to the app, like
    // Labs/MRI) — the cost columns inside reuse the same admin-gated usage feed as Agents.
    const show = { labs: true, learning: true, personas: true, agents: orgAdmin && mandatesEnabled, api: orgAdmin };
    $$('.ws-opt').forEach((t) => t.classList.toggle('locked', !show[t.dataset.ws]));
    // Land on Agents (the unified agents view) on the first gating resolution after boot
    // when it's available; otherwise Labs. Later re-gates only enforce the lock
    // fallback, so they don't yank a user back out of whatever they navigated to.
    if (!_landed) {
      _landed = true;
      setWorkspace(show.agents ? 'agents' : 'labs');
    } else if (!show[workspace]) {
      setWorkspace('labs');
    }
  }

  // ══════════════════════════════════════════════════════════ AGENTS ═══════
  const AGENT_PERM_FIELDS = [
    { key: 'cloud_daily_usd_budget', label: 'Daily cloud spend cap (USD)', hint: 'hard stop for the day', type: 'num' },
    { key: 'ralph_max_total_attempts', label: 'Ralph attempts', hint: 'tool dispatches per autonomous job', type: 'num' },
    { key: 'motor_max_jobs_per_window', label: 'Jobs / window', hint: 'autonomous job starts per window', type: 'num' },
    { key: 'motor_enable_shell', label: 'Shell commands', hint: 'run_command in a sandbox', type: 'bool' },
    { key: 'motor_enable_network', label: 'Network fetch', hint: 'outbound HTTP (fetch_url)', type: 'bool' },
    { key: 'motor_enable_cloud_actions', label: 'Cloud actions', hint: 'connector / agent actions', type: 'bool' },
    { key: 'motor_auto_confirm_writes', label: 'Auto-confirm writes', hint: 'skip the confirmation gate', type: 'bool' },
    { key: 'motor_user_cloud', label: 'Cloud grant (user-directed)', hint: 'off · ro · full', type: 'cloud' },
    { key: 'motor_allowed_dirs', label: 'Filesystem roots', hint: 'one path per line — within the org roots', type: 'dirs' },
  ];
  const CLOUD_RANK = { off: 0, ro: 1, full: 2 };

  async function fetchConnectors() {
    if (connectorsCache !== null) return connectorsCache;
    try {
      const r = await fetch('/connectors');
      connectorsCache = r.ok ? (await r.json()).connectors || [] : [];
    } catch (e) { connectorsCache = []; }
    return connectorsCache;
  }

  function ensureAgents() {
    if (!agentsData) { loadAgents(); return; }
    const need = [];
    if (!connectorsDetails) need.push(loadConnectorDetails());
    if (!agentActivity) need.push(loadAgentActivity());
    if (!agentUsage) need.push(loadAgentUsage());
    if (need.length) Promise.all(need).then(paintAgents); else paintAgents();
  }
  async function loadAgents() {
    const host = document.getElementById('ws-agents');
    host.innerHTML = '<div class="ws-grid"><div class="ws-main"><div class="main-pad"><div class="empty"><h3>Loading…</h3></div></div></div></div>';
    try {
      const r = await fetch('/agents');
      agentsData = r.ok ? await r.json() : { enabled: false, agents: [], roles: [], ceilings: {} };
    } catch (e) { agentsData = { enabled: false, agents: [], roles: [], ceilings: {} }; }
    await Promise.all([loadConnectorDetails(), loadAgentActivity(), loadAgentUsage()]);
    paintAgents();
  }
  // Roll up the durable agent-turn log into per-agent { count, lastTs } so the
  // Agents view + rail can show real activity (the engine-API path records
  // these; interactive personas without API traffic simply read as idle).
  async function loadAgentActivity() {
    try {
      const r = await fetch('/agents/turns?limit=200');
      const turns = r.ok ? (await r.json()).turns || [] : [];
      const m = {};
      for (const t of turns) {
        const id = t && t.agent_id; if (!id) continue;
        const ts = t.ts ? Date.parse(t.ts) : NaN;
        const e = m[id] || (m[id] = { count: 0, lastTs: 0, ts: [] });
        e.count++;
        if (!isNaN(ts)) { e.ts.push(ts); if (ts > e.lastTs) e.lastTs = ts; }
      }
      agentActivity = m;
    } catch (e) { agentActivity = {}; }
  }
  // Per-agent model usage (tokens, pod compute-seconds, cloud $) — which agent is
  // calling the model and how hard. No range → live session meter; a date range →
  // the durable ledger summed across restarts. Engine-API agent turns are
  // attributed; the owner's interactive + idle usage is excluded server-side.
  async function loadAgentUsage() {
    const { since, until } = rangeBounds();
    const qs = [];
    if (since) qs.push('since=' + encodeURIComponent(since));
    if (until) qs.push('until=' + encodeURIComponent(until));
    if (usageScope === 'all') qs.push('scope=all');
    try {
      const r = await fetch('/agents/usage' + (qs.length ? '?' + qs.join('&') : ''));
      const d = r.ok ? await r.json() : {};
      if (d.scope === 'all') { agentUsageAll = d.rows || []; agentUsage = {}; }
      else { agentUsage = d.usage || {}; agentUsageAll = null; }
    } catch (e) { agentUsage = {}; agentUsageAll = null; }
  }
  // Shared GPU pod telemetry (org-level, not per-agent): served by the gateway's
  // /__pod_status, which reverse-proxies in front of the brain. Carries the pod's
  // live uptime + cost accrued this session (running:false ⇒ cloud inference).
  async function loadPodStatus() {
    try {
      const r = await fetch('/__pod_status', { headers: { accept: 'application/json' } });
      podStatus = r.ok ? await r.json() : null;
    } catch (e) { podStatus = null; }
  }
  // which sub-view is active in Agents (the unified agents list is the landing view)
  let agView = 'agents';
  let agRoleSel = null; // persists selected role across reloads
  let agRoleMode = 'edit'; // role editor: 'edit' | 'preview'
  // Render role/instruction markdown with the settings engine's ONE renderer; fall back
  // to escaped plaintext if it isn't loaded yet (never inject raw HTML).
  function mdRender(src) {
    const ui = window.__settingsUI;
    if (ui && typeof ui.mdToHtml === 'function') { try { return ui.mdToHtml(src); } catch (e) { /* fall through */ } }
    return `<p>${esc(src || '').replace(/\n/g, '<br>')}</p>`;
  }
  // Jobs sub-view state: autonomous job outcomes (agent_jobs) surfaced for supervision.
  let jobsList = null;      // cached rows from /tasks/jobs (null = not loaded)
  let jobsFilter = '';      // state filter ('' = all)
  let jobSel = null;        // open job_id
  let jobDetail = null;     // cached full record for jobSel
  let connectorsDetails = null; // [{name, url, display_name}]
  let connectorsEnvManaged = false; // true → registry pinned via BRAIN_CMA_MCP_SERVERS
  let connectorsCloud = null;   // { available, model, actions_enabled } — the Claude cloud connector
  // ══════════════════════════════════════════════ ORGANISATION (shared) ═════
  // Both workspaces get the same pair of surfaces: a rail that NAVIGATES (search
  // over the whole tree + user folders that expand in place to their items) and a
  // roster that ANALYSES (a dense sortable table with grouping and per-group cost
  // subtotals). A flat list works at 8 agents and falls apart at 25+.
  //
  // Two of the three grouping axes come free: agent_id = "<persona>.<mandate_id>",
  // so "by persona" and "by role" are derived from data that already exists and can
  // never go stale. Only folders and pins are curated — the only new persisted state.

  let rosterQ = '';                                     // one query drives both surfaces
  let statusFilter = 'all';                             // all | active | idle | paused | pinned
  let rosterSort = { k: 'cost', d: -1 };                // default: biggest spender first
  const rosterGroup = { agents: 'none', personas: 'none' };
  const railOpen = { agents: new Set(), personas: new Set() };   // expanded folder keys
  const sessionFolders = { agents: [], personas: [] };  // created this session, not yet filled
  let personaOrg = null;    // { slug: { folder, pinned } } — /personas/organization
  const UNFILED = '__unfiled';

  // Which folders are open survives a reload — collapsing a 30-agent tree and having
  // it spring back open on every repaint would make the rail useless.
  function loadRailOpen(ws) {
    try {
      const raw = localStorage.getItem('elyceum.rail.open.' + ws);
      if (raw) railOpen[ws] = new Set(JSON.parse(raw) || []);
    } catch (e) { /* private mode / corrupt value → start collapsed */ }
  }
  function saveRailOpen(ws) {
    try { localStorage.setItem('elyceum.rail.open.' + ws, JSON.stringify([...railOpen[ws]])); }
    catch (e) { /* non-fatal — the tree just won't persist */ }
  }

  let toastTimer = null;
  function wsToast(msg) {
    let el = document.getElementById('ws-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'ws-toast'; el.className = 'ws-toast';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('on'), 1900);
  }

  const FOLD_SVG = '<svg class="fico" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.5h9A1.5 1.5 0 0 1 21 10v7.5A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/></svg>';
  const STAR_PATH = 'm12 3.5 2.6 5.6 6 .8-4.4 4.2 1.1 6-5.3-2.9-5.3 2.9 1.1-6L3.4 9.9l6-.8z';
  const STAR_FILLED_SVG = `<svg class="fico" width="13" height="13" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="${STAR_PATH}"/></svg>`;
  const SEARCH_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>';
  const CHEV_SVG = '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';
  const PLUS_SVG = '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>';
  const starSvg = (on, key) => `<svg class="star${on ? ' on' : ''}" data-pin="${esc(key)}" viewBox="0 0 24 24" fill="${on ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="${STAR_PATH}"/></svg>`;

  // Per-agent numbers over the loaded range, in the shape the roster + detail want.
  function agentMetrics(a) {
    const u = (agentUsage && agentUsage[a.agent_id]) || null;
    const lt = u && u.last_ts ? Date.parse(u.last_ts) : agentLastActive(a);
    return {
      u, cost: agentCostUsd(u), tok: u ? (u.in_tok || 0) + (u.out_tok || 0) : 0,
      calls: u ? (u.calls || 0) : 0, last: lt || 0, enabled: a.enabled !== false,
    };
  }
  // A persona is a roll-up of its agents; personaRollup() already summed them.
  function personaMetrics(p) {
    const u = { in_tok: p.in_tok, out_tok: p.out_tok, calls: p.calls, cloud_usd: p.cloud_usd, pod_s: p.pod_s, cloud_calls: p.cloud_calls };
    return {
      u, cost: personaCostUsd(p), tok: (p.in_tok || 0) + (p.out_tok || 0), calls: p.calls || 0,
      last: p.lastTs || 0, enabled: true, count: p.agents.length,
    };
  }

  // Everything the two surfaces need to differ on, in one place. The rail tree, the
  // table, sorting, grouping, search and drag-to-file are then written once.
  const ORG_SPECS = {
    agents: {
      ws: 'agents', noun: 'agent', nounPlural: 'agents',
      items: () => (agentsData && agentsData.agents) || [],
      id: (a) => a.agent_id,
      name: (a) => a.name || a.agent_id,
      folder: (a) => a.folder || '',
      pinned: (a) => !!a.pinned,
      sub: (a) => a.mandate_id,
      status: agentStatus,
      metrics: agentMetrics,
      // The agents workspace is already gated to org admins, so anyone who can see
      // it can file and pin. Personas is open to every member — see below.
      canEdit: () => true,
      haystack: (a) => [a.name, a.agent_id, personaName(a.persona), a.mandate_id, a.folder].join(' '),
      groups: [['none', 'None'], ['persona', 'Persona'], ['role', 'Role'], ['folder', 'Folder']],
      groupKey: (a, g) => g === 'persona' ? personaName(a.persona)
        : g === 'role' ? (a.mandate_id || '—')
          : g === 'status' ? agentStatus(a).label
            : (a.folder || 'Unfiled'),
      cols: [['name', 'Agent'], ['persona', 'Persona'], ['role', 'Role'], ['status', 'Status'],
        ['cost', 'Est. cost', 1], ['tok', 'Tokens', 1], ['calls', 'Calls', 1], ['last', 'Last active', 1]],
      grid: '26px 1.8fr 1fr 1fr .85fr .7fr .65fr .55fr .8fr',
      persist: (a, patch) => persistAgentOrg(a, patch),
    },
    personas: {
      ws: 'personas', noun: 'persona', nounPlural: 'personas',
      items: () => personaRollup(),
      id: (p) => p.slug,
      name: (p) => p.name,
      folder: (p) => (personaOrgEntry(p.slug).folder || ''),
      pinned: (p) => !!personaOrgEntry(p.slug).pinned,
      sub: (p) => `${p.agents.length} agent${p.agents.length === 1 ? '' : 's'}`,
      status: personaStatus,
      metrics: personaMetrics,
      // Personas is open to every member (configuring your own persona is core to
      // the app), but the folder map is ORG-SHARED state — only an org admin may
      // rearrange everyone's rail. Members see the tree, just not the handles.
      canEdit: () => orgAdmin,
      haystack: (p) => [p.name, p.slug, personaOrgEntry(p.slug).folder].join(' '),
      groups: [['none', 'None'], ['folder', 'Folder'], ['status', 'Status']],
      groupKey: (p, g) => g === 'status' ? personaStatus(p).label : (personaOrgEntry(p.slug).folder || 'Unfiled'),
      cols: [['name', 'Persona'], ['agents', 'Agents', 1], ['status', 'Status'],
        ['cost', 'Est. cost', 1], ['tok', 'Tokens', 1], ['calls', 'Calls', 1], ['last', 'Last active', 1]],
      grid: '26px 2fr .6fr .9fr .8fr .7fr .6fr .9fr',
      persist: (p, patch) => persistPersonaOrg(p, patch),
    },
  };
  function personaOrgEntry(slug) { return (personaOrg && personaOrg[slug]) || { folder: '', pinned: false }; }
  const orgSpec = (ws) => ORG_SPECS[ws];
  const orgFind = (spec, key) => spec.items().find(x => spec.id(x) === key);

  // ── optimistic writes ────────────────────────────────────────────────────
  // Filing an agent must never feel like a page load: mutate local state, repaint,
  // fire the POST, and only reload (to undo the optimism) if the server says no.
  async function persistAgentOrg(a, patch) {
    const before = { folder: a.folder, pinned: a.pinned };
    Object.assign(a, patch);
    const path = 'folder' in patch ? 'folder' : 'pinned';
    try {
      const r = await fetch(`/agents/${encodeURIComponent(a.agent_id)}/${path}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
    } catch (e) {
      Object.assign(a, before);
      paintAgents();
      window.alert('Could not save: ' + e.message);
    }
  }
  async function persistPersonaOrg(p, patch) {
    if (!personaOrg) personaOrg = {};
    const before = { ...personaOrgEntry(p.slug) };
    personaOrg[p.slug] = { ...before, ...patch };
    const path = 'folder' in patch ? 'folder' : 'pinned';
    try {
      const r = await fetch(`/personas/${encodeURIComponent(p.slug)}/${path}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
    } catch (e) {
      personaOrg[p.slug] = before;
      paintPersonas();
      window.alert('Could not save: ' + e.message);
    }
  }
  async function loadPersonaOrg() {
    try {
      const r = await fetch('/personas/organization');
      personaOrg = r.ok ? (await r.json()).organization || {} : {};
    } catch (e) { personaOrg = {}; }
  }

  // ── selection / filtering / ordering ─────────────────────────────────────
  function orgMatches(spec, x) {
    const q = rosterQ.trim().toLowerCase();
    return !q || String(spec.haystack(x) || '').toLowerCase().includes(q);
  }
  // The folder LIST is derived — `distinct folder` over the items — plus whatever the
  // user created this session. There is no folders table to keep in sync, so a folder
  // simply disappears when its last member leaves it. That is expected.
  function orgFolders(spec) {
    const seen = new Set();
    spec.items().forEach(x => { const f = spec.folder(x); if (f) seen.add(f); });
    sessionFolders[spec.ws].forEach(f => seen.add(f));
    return [...seen].sort((a, b) => a.localeCompare(b));
  }
  function orgSort(spec, list) {
    const { k, d } = rosterSort;
    const val = (x) => {
      const m = spec.metrics(x);
      switch (k) {
        case 'name': return spec.name(x).toLowerCase();
        case 'persona': return personaName(x.persona).toLowerCase();
        case 'role': return String(x.mandate_id || '').toLowerCase();
        case 'agents': return m.count || 0;
        case 'status': return STATUS_RANK[spec.status(x).state];
        case 'tok': return m.tok;
        case 'calls': return m.calls;
        case 'last': return m.last;
        default: return m.cost;
      }
    };
    return list.slice().sort((x, y) => {
      // Pinned leads every list regardless of the sort key — that is what the pin is for.
      const px = spec.pinned(x), py = spec.pinned(y);
      if (px !== py) return px ? -1 : 1;
      const vx = val(x), vy = val(y);
      return vx < vy ? -d : vx > vy ? d : 0;
    });
  }
  function rosterList(spec) {
    return orgSort(spec, spec.items().filter(x => {
      if (statusFilter === 'pinned') { if (!spec.pinned(x)) return false; }
      else if (statusFilter !== 'all' && spec.status(x).state !== statusFilter) return false;
      return orgMatches(spec, x);
    }));
  }

  // ── the rail: search field + folder tree ─────────────────────────────────
  function railSearchHtml(spec) {
    return `<label class="ws-search">${SEARCH_SVG}
      <input id="rail-q-${spec.ws}" type="text" placeholder="Find ${spec.noun === 'agent' ? 'an' : 'a'} ${spec.noun}" value="${esc(rosterQ)}" autocomplete="off" spellcheck="false">
      <span class="kbd">${/Mac|iP(hone|ad)/.test(navigator.platform || '') ? '⌘K' : 'Ctrl K'}</span></label>`;
  }
  function railTreeHtml(spec, selKey) {
    const searching = !!rosterQ.trim();
    const items = spec.items();
    const folders = orgFolders(spec);
    const nodes = [];
    const pins = orgSort(spec, items.filter(x => spec.pinned(x) && orgMatches(spec, x)));
    if (pins.length) nodes.push({ k: '__pinned', lab: 'Pinned', items: pins, icon: STAR_FILLED_SVG });
    folders.forEach(f => nodes.push({
      k: f, lab: f, icon: FOLD_SVG, drop: f,
      items: orgSort(spec, items.filter(x => spec.folder(x) === f && orgMatches(spec, x))),
    }));
    nodes.push({
      k: UNFILED, lab: 'Unfiled', icon: FOLD_SVG, drop: '', cls: ' unfiled',
      items: orgSort(spec, items.filter(x => !spec.folder(x) && orgMatches(spec, x))),
    });
    const tree = nodes.map(nd => {
      // While searching the tree becomes a result list without a mode switch: a folder
      // holding a hit opens itself, one holding none collapses.
      const open = searching ? nd.items.length > 0 : railOpen[spec.ws].has(nd.k);
      const live = nd.items.some(x => spec.status(x).state === 'active');
      const body = nd.items.map(x => railItemHtml(spec, x, selKey)).join('')
        || `<div class="empty-drop">${searching ? 'no match' : (nd.drop != null && spec.canEdit() ? `empty · drop ${spec.noun === 'agent' ? 'an' : 'a'} ${spec.noun} here` : 'empty')}</div>`;
      return `<div class="fnode${open ? '' : ' closed'}${nd.cls || ''}"${nd.drop != null ? ` data-drop="1" data-folder="${esc(nd.drop)}"` : ''}>
        <button class="fnode-head" data-toggle="${esc(nd.k)}">${CHEV_SVG}${nd.icon}
          <span class="fnode-name">${esc(nd.lab)}</span>
          ${live ? '<span class="dot-status live" style="background:var(--ok)"></span>' : ''}
          <span class="fnode-n">${nd.items.length}</span></button>
        <div class="fnode-body">${body}</div></div>`;
    }).join('');
    return `<div class="fold-lab"><span>Folders</span><span class="fold-tools"><span class="n">${folders.length}</span>
        ${spec.canEdit() ? `<button class="rail-lab-add" id="new-folder-${spec.ws}" title="New folder">${PLUS_SVG}</button>` : ''}</span></div>
      <div class="fold-tree">${tree}</div>`;
  }
  function railItemHtml(spec, x, selKey) {
    const st = spec.status(x), k = spec.id(x);
    return `<button class="fitem${selKey === k ? ' on' : ''}"${spec.canEdit() ? ' draggable="true"' : ''} data-k="${esc(k)}" title="${esc(spec.name(x))}">
      <span class="${st.cls}" style="background:${st.color}"></span>
      <span class="fi-name">${esc(spec.name(x))}</span>
      <span class="fi-sub">${esc(spec.sub(x))}</span>
      ${spec.canEdit() ? starSvg(spec.pinned(x), k) : ''}</button>`;
  }
  // Wire the rail's search, folder toggles, item clicks, pin stars and drop targets.
  // `onOpen(key)` opens that item's detail; `repaint` re-renders the whole workspace.
  function wireRailTree(root, spec, onOpen, repaint) {
    const q = root.querySelector('#rail-q-' + spec.ws);
    if (q) q.addEventListener('input', () => { rosterQ = q.value; repaint({ keepFocus: 'rail' }); });
    const add = root.querySelector('#new-folder-' + spec.ws);
    if (add) add.addEventListener('click', () => {
      const name = (window.prompt('Folder name') || '').trim();
      if (!name) return;
      if (!orgFolders(spec).includes(name)) sessionFolders[spec.ws].push(name);
      railOpen[spec.ws].add(name); saveRailOpen(spec.ws);
      wsToast('Created “' + name + '”');
      repaint();
    });
    root.querySelectorAll('.fnode-head').forEach(b => b.addEventListener('click', () => {
      const k = b.dataset.toggle;
      if (railOpen[spec.ws].has(k)) railOpen[spec.ws].delete(k); else railOpen[spec.ws].add(k);
      saveRailOpen(spec.ws);
      repaint();
    }));
    root.querySelectorAll('.fitem').forEach(b => {
      b.addEventListener('click', (e) => {
        const pin = e.target.closest('[data-pin]');
        if (pin) { togglePin(spec, pin.dataset.pin, repaint); return; }
        onOpen(b.dataset.k);
      });
      b.addEventListener('dragstart', (e) => e.dataTransfer.setData('text/plain', b.dataset.k));
    });
    if (!spec.canEdit()) return;
    root.querySelectorAll('.fnode[data-drop]').forEach(nd => {
      nd.addEventListener('dragover', (e) => { e.preventDefault(); nd.classList.add('drop'); });
      nd.addEventListener('dragleave', () => nd.classList.remove('drop'));
      nd.addEventListener('drop', (e) => {
        e.preventDefault(); nd.classList.remove('drop');
        fileInto(spec, e.dataTransfer.getData('text/plain'), nd.dataset.folder, repaint);
      });
    });
  }
  function togglePin(spec, key, repaint) {
    const x = orgFind(spec, key); if (!x) return;
    const next = !spec.pinned(x);
    spec.persist(x, { pinned: next });
    wsToast((next ? 'Pinned ' : 'Unpinned ') + spec.name(x));
    repaint();
  }
  function fileInto(spec, key, folder, repaint) {
    const x = orgFind(spec, key); if (!x) return;
    if (spec.folder(x) === (folder || '')) return;
    spec.persist(x, { folder: folder || null });
    railOpen[spec.ws].add(folder || UNFILED); saveRailOpen(spec.ws);
    wsToast(spec.name(x) + ' → ' + (folder || 'Unfiled'));
    repaint();
  }

  // ── the roster table ─────────────────────────────────────────────────────
  function rosterFiltersHtml(spec) {
    const items = spec.items();
    const pills = [['all', 'All', ''], ['active', 'Active', 'var(--ok)'], ['idle', 'Idle', 'var(--ink-4)'],
      ['paused', 'Paused', 'var(--temporal)'], ['pinned', '★ Pinned', '']];
    return `<div class="filters">
      <div class="row" style="gap:10px; flex-wrap:wrap;">
        <label class="ws-tsearch">${SEARCH_SVG}
          <input id="roster-q" type="text" placeholder="Search ${items.length} ${esc(spec.nounPlural)}" value="${esc(rosterQ)}" autocomplete="off" spellcheck="false"></label>
        <div class="row" style="gap:6px; flex-wrap:wrap;" id="status-filters">
          ${pills.map(s => `<button class="fchip${statusFilter === s[0] ? ' on' : ''}" data-s="${s[0]}">${s[2] ? `<span class="dot" style="background:${s[2]}"></span>` : ''}${s[1]}</button>`).join('')}
        </div>
      </div>
      <div class="row" style="gap:8px;"><span class="label" style="letter-spacing:0.18em;">Group by</span>
        <div class="ws-range" id="group-seg">${spec.groups.map(([g, l]) =>
      `<button data-g="${g}" class="${rosterGroup[spec.ws] === g ? 'on' : ''}">${l}</button>`).join('')}</div>
      </div></div>`;
  }
  function rosterTableHtml(spec) {
    const grid = spec.grid;
    const head = `<div class="ag-thead" style="grid-template-columns:${grid};"><span class="ag-th"></span>${spec.cols.map(([k, l, num]) =>
      `<button class="ag-th sortable${rosterSort.k === k ? ' act' : ''}${num ? ' num' : ''}" data-k="${k}">${esc(l)}<span class="arw">${rosterSort.d < 0 ? '▼' : '▲'}</span></button>`).join('')}</div>`;
    const list = rosterList(spec);
    const group = rosterGroup[spec.ws];
    let body;
    if (!list.length) {
      body = `<div class="empty" style="border:none;"><h3>Nothing matches</h3><p>No ${esc(spec.noun)} fits this search and filter.</p></div>`;
    } else if (group === 'none') {
      body = list.map(x => rosterRowHtml(spec, x)).join('');
    } else {
      const groups = new Map();
      list.forEach(x => {
        const k = spec.groupKey(x, group);
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(x);
      });
      body = [...groups.keys()]
        .sort((x, y) => x === 'Unfiled' ? 1 : y === 'Unfiled' ? -1 : String(x).localeCompare(String(y)))
        .map(k => {
          const its = groups.get(k);
          const sum = its.reduce((s, x) => s + spec.metrics(x).cost, 0);
          const act = its.filter(x => spec.status(x).state === 'active').length;
          return `<div class="ag-grow"><span class="gl"><span class="dot-status${act ? ' live' : ''}" style="background:${act ? 'var(--ok)' : 'var(--ink-4)'}"></span>${esc(k)}</span>
            <span class="gr"><span>${its.length}</span><span style="color:var(--signal-deep);">$${sum.toFixed(2)}</span></span></div>`
            + its.map(x => rosterRowHtml(spec, x)).join('');
        }).join('');
    }
    return { html: `<div class="ag-table roster">${head}${body}</div>`, list };
  }
  function rosterRowHtml(spec, x) {
    const m = spec.metrics(x), st = spec.status(x), k = spec.id(x);
    const off = !m.enabled;   // a paused agent reads "—", never a misleading $0.00
    const mid = spec.ws === 'agents'
      ? `<span class="t-cell"><span class="chip persona"><span class="dot"></span><em>${esc(personaName(x.persona))}</em></span></span>
         <span class="t-cell"><span class="chip role"><span class="dot"></span><em>${esc(x.mandate_id)}</em></span></span>`
      : `<span class="t-num">${m.count}</span>`;
    // personaSel holds the DISPLAY NAME (the settings engine keys its config pane by
    // name, not slug) — compare on the right identifier for each surface.
    const sel = spec.ws === 'agents' ? agentSel === k : personaSel === spec.name(x);
    return `<div class="ag-row${sel ? ' sel' : ''}"${spec.canEdit() ? ' draggable="true"' : ''} data-k="${esc(k)}" data-agent="${esc(k)}" style="grid-template-columns:${spec.grid};">
      ${spec.canEdit() ? `<button class="t-pin" data-pin="${esc(k)}" title="${spec.pinned(x) ? 'Unpin' : 'Pin'}">${starSvg(spec.pinned(x), k)}</button>` : '<span></span>'}
      <span class="t-name"><span class="${st.cls}" style="background:${st.color}"></span><em>${esc(spec.name(x))}</em></span>
      ${mid}
      <span class="t-status">${esc(st.label)}</span>
      <span class="t-num cost${off ? ' zero' : ''}" data-cost-for="${esc(k)}" title="${esc(costTitle(m.u))}">${off ? '—' : '$' + m.cost.toFixed(2)}</span>
      <span class="t-num${off ? ' zero' : ''}" data-tok-for="${esc(k)}" title="${esc(usageTitle(m.u))}">${off ? '—' : esc(fmtTokens(m.tok))}</span>
      <span class="t-num${off ? ' zero' : ''}" data-calls-for="${esc(k)}">${off ? '—' : m.calls}</span>
      <span class="t-when">${m.last ? esc(agoShort(Date.now() - m.last)) + ' ago' : '—'}</span></div>`;
  }
  function wireRoster(main, spec, onOpen, repaint) {
    const q = main.querySelector('#roster-q');
    if (q) q.addEventListener('input', () => { rosterQ = q.value; repaint({ keepFocus: 'roster' }); });
    main.querySelectorAll('#status-filters .fchip').forEach(b => b.addEventListener('click', () => {
      statusFilter = b.dataset.s; repaint();
    }));
    main.querySelectorAll('#group-seg button').forEach(b => b.addEventListener('click', () => {
      rosterGroup[spec.ws] = b.dataset.g; repaint();
    }));
    main.querySelectorAll('.ag-th.sortable').forEach(b => b.addEventListener('click', () => {
      const k = b.dataset.k;
      // Same column → invert. New column → text ascending, numbers descending.
      if (rosterSort.k === k) rosterSort.d *= -1;
      else rosterSort = { k, d: (k === 'name' || k === 'persona' || k === 'role' || k === 'status') ? 1 : -1 };
      repaint();
    }));
    main.querySelectorAll('.ag-table.roster .ag-row').forEach(r => {
      r.addEventListener('click', (e) => {
        const pin = e.target.closest('[data-pin]');
        if (pin) { togglePin(spec, pin.dataset.pin, repaint); return; }
        onOpen(r.dataset.k);
      });
      r.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', r.dataset.k); r.classList.add('drag'); });
      r.addEventListener('dragend', () => r.classList.remove('drag'));
    });
  }
  // ── detail-view organisation controls (folder select + pin toggle) ───────
  // The same pair on both detail surfaces, so a folder can be changed without going
  // back to the roster and dragging. Rendered inline (compact) — the agent detail
  // also gets the full Organisation panel below its identity card.
  function orgFolderOptionsHtml(spec, current) {
    const folders = orgFolders(spec);
    if (current && !folders.includes(current)) folders.push(current);
    return ['', ...folders].map(f =>
      `<option value="${esc(f)}"${(current || '') === f ? ' selected' : ''}>${f ? esc(f) : 'Unfiled'}</option>`).join('');
  }
  function orgFolderControlsHtml(spec, item) {
    if (!item || !spec.canEdit()) return '';
    const pinned = spec.pinned(item);
    return `<select class="ctrl-input org-folder-sel" title="Folder" style="min-width:120px; font-size:11px;">${orgFolderOptionsHtml(spec, spec.folder(item))}</select>
      <button class="btn org-pin-btn" title="${pinned ? 'Unpin' : 'Pin to the top of the rail'}">${pinned ? '★ Pinned' : '☆ Pin'}</button>`;
  }
  function wireOrgFolderControls(root, spec, getItem, repaint) {
    const sel = root.querySelector('.org-folder-sel');
    if (sel) sel.addEventListener('change', () => {
      const item = getItem(); if (!item) return;
      spec.persist(item, { folder: sel.value || null });
      railOpen[spec.ws].add(sel.value || UNFILED); saveRailOpen(spec.ws);
      wsToast(spec.name(item) + ' → ' + (sel.value || 'Unfiled'));
      repaint();
    });
    const pin = root.querySelector('.org-pin-btn');
    if (pin) pin.addEventListener('click', () => {
      const item = getItem(); if (!item) return;
      const next = !spec.pinned(item);
      spec.persist(item, { pinned: next });
      wsToast((next ? 'Pinned ' : 'Unpinned ') + spec.name(item));
      repaint();
    });
  }

  // Put the caret back where the user was typing after a full repaint.
  function restoreFocus(where) {
    if (!where) return;
    const el = document.querySelector(where === 'rail' ? '.workspace.on .ws-search input' : '.workspace.on .ws-tsearch input');
    if (!el || el === document.activeElement) return;
    el.focus();
    const n = el.value.length; el.setSelectionRange(n, n);
  }

  // `opts` is optional and may arrive as a Promise result (`.then(paintAgents)`), so
  // only an object carrying keepFocus counts as options.
  function paintAgents(opts) {
    const keepFocus = (opts && opts.keepFocus) || null;
    const host = document.getElementById('ws-agents');
    const spec = orgSpec('agents');
    const ags = (agentsData && agentsData.agents) || [];
    const roles = (agentsData && agentsData.roles) || [];
    const activeCount = ags.filter(a => agentStatus(a).state === 'active').length;
    host.innerHTML = `
      <div class="ws-grid" style="grid-template-columns:268px 1fr;">
        <div class="ws-rail">
          <div class="rail-head"><h2>Agents</h2><span class="n">admin</span></div>

          <div class="rail-sect">
            <button class="rail-item ag-nav ${agView==='agents'?'on':''}" data-view="agents"><span class="ri-name"><span class="dot-status ${activeCount?'live':''}" style="background:${activeCount?'var(--ok)':'var(--ink-4)'}"></span>All agents</span><span class="ri-meta">${ags.length} total · ${activeCount} active</span></button>
            <button class="rail-item ag-nav ${agView==='jobs'||agView==='jobdetail'?'on':''}" data-view="jobs"><span class="ri-name">Jobs</span><span class="ri-meta">self-directed work · outcomes</span></button>
          </div>

          <div class="rail-div"></div>

          <div class="rail-sect">
            <button class="rail-item ag-nav ${agView==='roles'?'on':''}" data-view="roles"><span class="ri-name">Roles</span><span class="ri-meta">${roles.length} reusable spec${roles.length===1?'':'s'}</span></button>
            <button class="rail-item ag-nav ${agView==='skills'?'on':''}" data-view="skills"><span class="ri-name">Skills</span><span class="ri-meta">reusable abilities · review</span></button>
            <button class="rail-item ag-nav ${agView==='limits'?'on':''}" data-view="limits"><span class="ri-name">Account limits</span><span class="ri-meta">org ceilings</span></button>
            <button class="rail-item ag-nav ${agView==='connectors'?'on':''}" data-view="connectors"><span class="ri-name">Connectors</span><span class="ri-meta">MCP servers · register</span></button>
          </div>

          <div class="rail-div"></div>
          ${railSearchHtml(spec)}
          ${railTreeHtml(spec, agView === 'detail' ? agentSel : null)}
          <div class="rail-sect" style="padding-top:0;">
            <button class="rail-add" id="ws-new-agent" title="New agent">${PLUS_SVG} New agent</button>
          </div>
        </div>
        <div class="ws-main" id="ag-main"></div>
      </div>
      <div class="modal-veil" id="ws-new-agent-modal"></div>`;
    host.querySelectorAll('.ag-nav').forEach(n => n.addEventListener('click', () => { agView = n.dataset.view; agentSel = null; paintAgents(); }));
    wireRailTree(host, spec, openAgentDetail, paintAgents);
    host.querySelector('#ws-new-agent').addEventListener('click', openNewAgent);
    const main = host.querySelector('#ag-main');
    if (agView === 'detail' && agentSel) renderAgentDetail(main);
    else if (agView === 'roles') renderRoles(main);
    else if (agView === 'skills') { if (skillsData === null) loadSkills(); renderSkills(main); }
    else if (agView === 'limits') renderAccountLimits(main);
    else if (agView === 'connectors') renderConnectors(main);
    else if (agView === 'jobdetail' && jobSel) renderJobDetail(main);
    else if (agView === 'jobs') renderJobsView(main);
    else renderAgentsView(main);
    restoreFocus(keepFocus);
  }
  function openAgentDetail(agentId) {
    agentSel = agentId; agView = 'detail'; paintAgents();
    const main = document.getElementById('ag-main'); if (main) main.scrollTop = 0;
  }

  // ── Jobs sub-view: autonomous job outcomes (supervision surface) ──────────
  const JOB_STATES = ['running', 'awaiting_approval', 'completed', 'failed', 'stopped_budget', 'deferred'];
  function jobStateColor(s) {
    if (s === 'completed') return 'var(--ok)';
    if (s === 'failed' || s === 'stopped_budget') return 'var(--danger)';
    if (s === 'awaiting_approval') return 'var(--warn)';
    if (s === 'running') return 'var(--signal-deep)';
    return 'var(--ink-4)'; // deferred / unknown
  }
  async function loadJobs() {
    try {
      const qs = '?limit=50' + (jobsFilter ? '&state=' + encodeURIComponent(jobsFilter) : '');
      const r = await fetch('/tasks/jobs' + qs);
      jobsList = r.ok ? (await r.json()).jobs || [] : [];
    } catch (e) { jobsList = []; }
  }
  function renderJobsView(main) {
    if (typeof window.clearAgentsBadge === 'function') window.clearAgentsBadge();
    if (jobsList === null) {
      main.innerHTML = '<div class="main-pad"><div class="empty"><h3>Loading jobs…</h3></div></div>';
      loadJobs().then(paintAgents);
      return;
    }
    const chips = ['', ...JOB_STATES].map(s =>
      `<button class="btn btn-sm job-chip ${jobsFilter === s ? 'on' : ''}" data-state="${s}" style="${jobsFilter === s ? 'border-color:var(--signal-deep); color:var(--ink);' : ''}">${s || 'all'}</button>`).join(' ');
    const GRID = '2.4fr 1.1fr 0.6fr 0.6fr 0.5fr 0.6fr 0.5fr';
    const rows = jobsList.map(j => {
      const goal = String(j.goal || '').slice(0, 140);
      const stories = (j.stories_total || 0) > 0 ? `${j.stories_completed || 0}/${j.stories_total}` : '—';
      const usd = j.cloud_usd != null ? '$' + Number(j.cloud_usd).toFixed(2) : '—';
      const when = j.updated_at ? agoShort(Date.now() - Date.parse(j.updated_at)) : '—';
      const needsOk = j.state === 'awaiting_approval'
        ? ` <span class="n" style="color:var(--warn);">· approval</span>` : '';
      return `<div class="ag-row job-row" data-job="${esc(j.job_id)}" style="grid-template-columns:${GRID};">
        <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${esc(j.goal || '')}">${esc(goal)}</span>
        <span class="ar-status" title="${esc(j.reason_human || '')}"><span class="dot-status" style="background:${jobStateColor(j.state)}"></span>${esc(j.state || '')}${needsOk}</span>
        <span class="n">${esc(j.source || '')}</span>
        <span class="n">${esc(stories)}</span>
        <span class="n">${j.productive_steps != null ? j.productive_steps : '—'}</span>
        <span class="n">${esc(usd)}</span>
        <span class="n">${esc(when)}</span></div>`;
    }).join('');
    main.innerHTML = `<div class="main-pad">
      <div class="row" style="justify-content:space-between; margin-bottom:14px;">
        <h2 class="serif-h" style="font-size:20px;">Jobs</h2>
        <button class="btn btn-sm" id="jobs-refresh">Refresh</button>
      </div>
      <div class="row" style="gap:6px; flex-wrap:wrap; margin-bottom:14px;">${chips}</div>
      ${jobsList.length ? `<div class="ag-table" style="grid-template-columns:none;">
        <div class="ag-table-head" style="grid-template-columns:${GRID};"><span>Goal</span><span>State</span><span>Source</span><span>Stories</span><span>Steps</span><span>Spend</span><span>When</span></div>
        ${rows}</div>`
      : '<div class="empty"><h3>No jobs yet</h3><p>Autonomous work the brain runs for you shows up here with its outcome, spend and steps.</p></div>'}
    </div>`;
    main.querySelectorAll('.job-chip').forEach(c => c.addEventListener('click', () => {
      jobsFilter = c.dataset.state; jobsList = null; paintAgents();
    }));
    main.querySelector('#jobs-refresh').addEventListener('click', () => { jobsList = null; paintAgents(); });
    main.querySelectorAll('.job-row').forEach(r => r.addEventListener('click', () => {
      jobSel = r.dataset.job; jobDetail = null; agView = 'jobdetail'; paintAgents();
    }));
  }
  function renderJobDetail(main) {
    if (jobDetail === null) {
      main.innerHTML = '<div class="main-pad"><div class="empty"><h3>Loading job…</h3></div></div>';
      fetch('/tasks/jobs/' + encodeURIComponent(jobSel))
        .then(r => r.ok ? r.json() : null)
        .then(d => { jobDetail = d || { missing: true }; paintAgents(); })
        .catch(() => { jobDetail = { missing: true }; paintAgents(); });
      return;
    }
    const j = jobDetail;
    const back = `<button class="link ag-back jobs-back" style="margin-bottom:18px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg> Jobs</button>`;
    if (j.missing) {
      main.innerHTML = `<div class="main-pad">${back}<div class="empty"><h3>Job not found</h3></div></div>`;
      main.querySelector('.jobs-back').addEventListener('click', () => { agView = 'jobs'; jobSel = null; paintAgents(); });
      return;
    }
    const steps = Array.isArray(j.steps_json) ? j.steps_json : [];
    const results = Array.isArray(j.results_json) ? j.results_json : [];
    const fmtTs = (t) => t ? new Date(t).toLocaleString() : '—';
    const timeline = steps.map((s, i) => {
      const argsPrev = (() => { try { return JSON.stringify(s.args || {}, null, 2); } catch (e) { return ''; } })();
      const out = results[i] != null ? String(results[i]) : '';
      return `<details class="job-step" style="border-bottom:1px solid var(--line-faint); padding:6px 0;">
        <summary style="cursor:pointer;"><span class="data" style="font-size:13px;">${i + 1}. ${esc(s.tool || '?')}</span>
          <span class="n" style="color:var(--ink-3); margin-left:8px;">${esc(String(s.reason || '').slice(0, 120))}</span></summary>
        ${argsPrev ? `<pre class="data" style="font-size:12px; white-space:pre-wrap; word-break:break-word; color:var(--ink-2); margin:6px 0 0 18px;">${esc(argsPrev.slice(0, 2000))}</pre>` : ''}
        ${out ? `<pre class="data" style="font-size:12px; white-space:pre-wrap; word-break:break-word; margin:6px 0 0 18px; color:var(--ink);">→ ${esc(out.slice(0, 2000))}</pre>` : ''}
      </details>`;
    }).join('');
    const links = Array.isArray(j.source_links) ? j.source_links : [];
    const files = Array.isArray(j.written_files) ? j.written_files : [];
    const storiesBar = (j.stories_total || 0) > 0
      ? `<div class="n" style="margin-top:6px;">stories ${j.stories_completed || 0}/${j.stories_total}
          <span style="display:inline-block; width:120px; height:5px; background:var(--line-faint); border-radius:3px; vertical-align:middle; margin-left:8px;">
          <span style="display:block; width:${Math.min(100, Math.round(100 * (j.stories_completed || 0) / j.stories_total))}%; height:100%; background:var(--ok); border-radius:3px;"></span></span></div>`
      : '';
    main.innerHTML = `<div class="main-pad">${back}
      <h2 class="serif-h" style="font-size:19px; margin-bottom:6px;">${esc(String(j.goal || '').slice(0, 300))}</h2>
      <div class="row" style="gap:14px; flex-wrap:wrap; margin-bottom:4px;">
        <span class="n" style="color:${jobStateColor(j.state)};">${esc(j.state || '')}</span>
        ${j.reason_code ? `<span class="n data">${esc(j.reason_code)}</span>` : ''}
        <span class="n">${esc(j.source || '')}</span>
        ${j.agent_id ? `<span class="n data">${esc(j.agent_id)}</span>` : ''}
        <span class="n">${j.cloud_usd != null ? '$' + Number(j.cloud_usd).toFixed(2) : ''}</span>
      </div>
      <div class="n" style="color:var(--ink-3); margin-bottom:10px;">created ${esc(fmtTs(j.created_at))} · updated ${esc(fmtTs(j.updated_at))}${j.completed_at ? ' · completed ' + esc(fmtTs(j.completed_at)) : ''}</div>
      ${j.reason_human ? `<p style="font-size:13px; color:var(--ink-2); margin-bottom:8px;">${esc(j.reason_human)}</p>` : ''}
      ${j.summary ? `<p style="font-size:14px; line-height:1.55; margin-bottom:8px;">${esc(j.summary)}</p>` : ''}
      ${storiesBar}
      ${steps.length ? `<h3 class="serif-h" style="font-size:15px; margin:18px 0 6px;">Steps · ${steps.length}</h3>${timeline}` : ''}
      ${links.length ? `<h3 class="serif-h" style="font-size:15px; margin:18px 0 6px;">Sources</h3>${links.map(u => `<div><a href="${esc(u)}" target="_blank" rel="noopener" class="link" style="font-size:13px; word-break:break-all;">${esc(u)}</a></div>`).join('')}` : ''}
      ${files.length ? `<h3 class="serif-h" style="font-size:15px; margin:18px 0 6px;">Written files</h3>${files.map(f => `<div class="data" style="font-size:12px;">${esc(f)}</div>`).join('')}` : ''}
    </div>`;
    main.querySelector('.jobs-back').addEventListener('click', () => { agView = 'jobs'; jobSel = null; jobDetail = null; paintAgents(); });
  }

  // Convert an ISO timestamp to a <input type="datetime-local"> value (local time).
  function toLocalInput(iso) {
    if (!iso) return '';
    const d = new Date(iso); if (isNaN(d)) return '';
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  // Switch org-scope (platform super-admin only): own org ↔ all orgs.
  async function setUsageScope(scope) {
    usageScope = scope; _scopeChosen = true;
    // "This session" is process-local — meaningless cross-org; fall back to Today.
    if (scope === 'all' && usageRange.key === 'session') usageRange = { key: 'today', since: null, until: null };
    await Promise.all([loadAgentUsage(), loadPodStatus()]);
    const main = document.getElementById('ag-main');
    if (main && agView === 'agents') renderAgentsView(main);
  }
  // ── all-orgs fleet view (platform admin) ─────────────────────────────────
  // Its own shape on purpose: a historical LEDGER, not org-shaped state. No folders,
  // no pinning, no grouping — other orgs' filing is none of this view's business, and
  // the rows come from usage alone (no agentsData), identity derived from agent_id.
  const FLEET_GRID = '26px 1.8fr .9fr 1fr .9fr .8fr .7fr .6fr';
  const FLEET_COLS = [['name', 'Agent'], ['org', 'Org'], ['persona', 'Persona'], ['role', 'Role'],
    ['cost', 'Est. cost', 1], ['tok', 'Tokens', 1], ['calls', 'Calls', 1]];
  function fleetRowHtml(row) {
    const aid = row.agent_id || '';
    const dot = aid.indexOf('.');
    const personaPart = dot >= 0 ? aid.slice(0, dot) : aid;
    const mandate = dot >= 0 ? aid.slice(dot + 1) : '';
    const orgLabel = row.org_name || (row.org_id || '').slice(0, 8) || 'org';
    return `<div class="ag-row" style="grid-template-columns:${FLEET_GRID}; cursor:default;">
      <span></span>
      <span class="t-name"><em title="${esc(aid)}">${esc(aid)}</em></span>
      <span class="t-status" title="${esc(row.org_id || '')}">${esc(orgLabel)}</span>
      <span class="t-cell"><span class="chip persona"><span class="dot"></span><em>${esc(personaName(personaPart))}</em></span></span>
      <span class="t-cell"><span class="chip role"><span class="dot"></span><em>${esc(mandate)}</em></span></span>
      <span class="t-num cost" title="${esc(costTitle(row))}">$${agentCostUsd(row).toFixed(2)}</span>
      <span class="t-num" title="${esc(usageTitle(row))}">${esc(fmtTokens((row.in_tok || 0) + (row.out_tok || 0)))}</span>
      <span class="t-num">${esc(String(row.calls || 0))}</span></div>`;
  }
  function fleetTableHtml(rows) {
    const head = `<div class="ag-thead" style="grid-template-columns:${FLEET_GRID};"><span class="ag-th"></span>${FLEET_COLS.map(([, l, num]) =>
      `<span class="ag-th${num ? ' num' : ''}">${esc(l)}</span>`).join('')}</div>`;
    const body = rows.length ? rows.map(fleetRowHtml).join('')
      : '<div class="empty" style="border:none;"><h3>No usage in this range</h3><p>No agent across any org called the model in the selected window.</p></div>';
    return `<div class="ag-table roster">${head}${body}</div>`;
  }

  // The range + scope bar, shared by both rosters. Its behaviour is unchanged from
  // the card era — usageRange / RANGE_PRESETS / setUsageRange / usageScope all stay
  // exactly as they were; only the container moved.
  function rangeBarHtml(opts) {
    const allMode = !!opts.allMode;
    const presets = allMode ? RANGE_PRESETS.filter(p => p.key !== 'session') : RANGE_PRESETS;
    const rangeLabel = (RANGE_PRESETS.find(p => p.key === usageRange.key) || {}).label || 'Range';
    const scopeToggle = opts.scope && isAdmin
      ? `<div class="ws-range" id="scope-toggle"><button class="${allMode ? '' : 'on'}" data-scope="org">My org</button><button class="${allMode ? 'on' : ''}" data-scope="all">All orgs</button></div>`
      : '';
    return `<div class="between" style="margin-top:16px; align-items:center; flex-wrap:wrap; gap:12px;">
        <div class="row" style="gap:12px; flex-wrap:wrap;">
          <div class="ws-range">${presets.map(p => `<button class="${p.key === usageRange.key ? 'on' : ''}" data-range="${p.key}">${esc(p.label)}</button>`).join('')}</div>
          ${scopeToggle}
        </div>
        <span class="data" style="font-size:10px; color:var(--ink-4);">${esc(rangeLabel)} total · <span style="color:var(--signal-deep);" id="range-total">$${opts.total.toFixed(2)}</span></span>
      </div>
      ${usageRange.key === 'custom' ? `<div class="row" style="gap:14px; margin-top:12px; flex-wrap:wrap;">
        <label class="data" style="font-size:9px; color:var(--ink-4); display:flex; align-items:center; gap:6px;">FROM <input type="datetime-local" id="range-from" class="ctrl-input" value="${esc(toLocalInput(usageRange.since))}"></label>
        <label class="data" style="font-size:9px; color:var(--ink-4); display:flex; align-items:center; gap:6px;">TO <input type="datetime-local" id="range-to" class="ctrl-input" value="${esc(toLocalInput(usageRange.until))}"></label>
      </div>` : ''}`;
  }
  function wireRangeBar(main) {
    main.querySelectorAll('.ws-range button[data-range]').forEach(b => b.addEventListener('click', () => setUsageRange(b.dataset.range)));
    main.querySelectorAll('#scope-toggle button[data-scope]').forEach(b => b.addEventListener('click', () => setUsageScope(b.dataset.scope)));
    const from = main.querySelector('#range-from'), to = main.querySelector('#range-to');
    const applyCustom = () => setUsageRange('custom',
      from && from.value ? new Date(from.value).toISOString() : null,
      to && to.value ? new Date(to.value).toISOString() : null);
    if (from) from.addEventListener('change', applyCustom);
    if (to) to.addEventListener('change', applyCustom);
  }

  // ── Agents roster — a dense table over every agent, grouped and sorted ────
  function renderAgentsView(main) {
    const spec = orgSpec('agents');
    const ags = spec.items();
    const counts = { active: 0, idle: 0, paused: 0 };
    ags.forEach(a => counts[agentStatus(a).state]++);
    const allMode = usageScope === 'all';
    const isSession = usageRange.key === 'session';
    const allRows = allMode ? (agentUsageAll || []).slice().sort((x, y) => agentCostUsd(y) - agentCostUsd(x)) : [];
    const table = allMode ? null : rosterTableHtml(spec);
    const shownList = table ? table.list : [];
    const rangeTotal = allMode
      ? allRows.reduce((s, r) => s + agentCostUsd(r), 0)
      : shownList.reduce((s, a) => s + agentMetrics(a).cost, 0);
    const orgCount = allMode ? new Set(allRows.map(r => r.org_id)).size : 1;
    main.innerHTML = `<div class="main-pad" style="max-width:none;">
      <div class="between" style="align-items:flex-start;">
        <div>
          <div class="page-eyebrow">Agents · operational${allMode ? ' · platform' : ''}</div>
          <div class="page-title">${allMode ? 'All orgs' : 'All agents'}</div>
          <p class="page-lede">${allMode
        ? 'Every org\'s agents across the platform, by cost over the selected range — cumulative through restarts. The biggest spenders float to the top.'
        : 'Every agent is a persona paired with a role, so the roster groups itself by either axis — or by the folders you keep in the rail. Sort by cost or activity to find what\'s spending, and drag a row onto a folder to file it.'}</p>
        </div>
        <div class="row" style="gap:10px; margin-top:14px; flex-shrink:0; align-items:center;">
          ${allMode ? '' : `<span class="chip"><span class="dot live" style="background:var(--ok);"></span>${counts.active} active</span>`}
          <span class="data" id="pod-meter" style="font-size:10px; color:var(--ink-4);"></span>
          ${allMode ? '' : `<button class="btn btn-primary" id="ag-new-btn">New agent</button>`}
        </div>
      </div>
      ${allMode ? '' : rosterFiltersHtml(spec)}
      ${rangeBarHtml({ allMode, scope: true, total: rangeTotal })}
      ${ags.length || allMode
        ? `<div style="margin-top:18px;">${allMode ? fleetTableHtml(allRows) : table.html}</div>
           <div class="foot-note">${allMode
          ? `${orgCount} org${orgCount === 1 ? '' : 's'} · ${allRows.length} agent${allRows.length === 1 ? '' : 's'} · $${rangeTotal.toFixed(2)} over the selected range — cumulative through restarts.`
          : `${shownList.length} of ${ags.length} shown · ${counts.active} active · ${counts.idle} idle · ${counts.paused} paused · drag a row onto a folder in the rail to file it.`}
             Est. cost = real cloud spend + the agent's share of the GPU pod, valued by its compute-seconds × the pod's $/hr (hover a cost for the split).${isSession ? ' This session = the current process uptime.' : ''}</div>`
        : `<div class="empty" style="margin-top:22px;"><h3>No agents yet</h3><p>Pair a persona with a role to create your first agent.</p></div>`}
      </div>`;
    wireRangeBar(main);
    if (!allMode) wireRoster(main, spec, openAgentDetail, paintAgents);
    const newBtn = main.querySelector('#ag-new-btn');
    if (newBtn) newBtn.addEventListener('click', openNewAgent);
    refreshPodMeter();
  }
  // Fill (and keep ticking) the shared GPU-pod uptime + accrued-cost meter in the
  // dashboard header. Self-cancelling: stops once the Agents view is no longer shown.
  async function refreshPodMeter() {
    const ws = document.getElementById('ws-agents');
    const el = document.getElementById('pod-meter');
    if (!el || !ws || !ws.classList.contains('on')) {
      if (podMeterTimer) { clearInterval(podMeterTimer); podMeterTimer = null; }
      return;
    }
    await loadPodStatus();
    const p = podStatus;
    let html = '';
    if (p && p.running && p.uptime_s != null) {
      const cost = (p.cost_accrued_usd != null)
        ? ` · <span style="color:var(--signal-deep);">$${p.cost_accrued_usd.toFixed(2)}</span> accrued`
        : '';
      html = `GPU pod · up ${esc(fmtDur(p.uptime_s))}${cost}`;
    } else if (p && ['resuming', 'pulling', 'warming'].includes(p.state)) {
      // "reloading model onto GPU" = in-place reconnect on the SAME pod (model went
      // un-resident), not a fresh boot — name it so the two read differently.
      const phase = p.detail === 'reloading model onto GPU' ? 'reconnecting' : p.state;
      html = `GPU pod · ${esc(phase)}…`;
    } else if (p) {
      html = 'cloud inference';
    }
    const live = document.getElementById('pod-meter');
    if (live) live.innerHTML = html;
    // Live-refresh only the org view (repaintUsageCells is org-shaped). The all-orgs
    // fleet view is a historical ledger snapshot — it refreshes on range/scope change.
    if (usageScope === 'org') { await loadAgentUsage(); repaintUsageCells(); }
    if (!podMeterTimer) podMeterTimer = setInterval(refreshPodMeter, 30000);
  }
  // Card → Labs. Switch to Labs and OBSERVE that agent's live lane (chemistry +
  // idle thoughts) without restarting the brain. Clicking the org's own owner
  // persona (e.g. the default The Admin) just shows the owner lane — setObservedAgent
  // resolves that by comparing the agent's persona to the active process persona.
  function openAgentInLabs(agentId, name, persona) {
    if (typeof window.setObservedAgent === 'function') window.setObservedAgent(agentId, name, persona);
    setWorkspace('labs');
  }
  // The live persona catalogue — built-ins + the org's custom personas. The settings
  // engine owns it; window.SETTINGS.personas is only the built-in seed it copies at
  // boot, so read the engine's list first or custom personas never show up here.
  function personaCatalogue() {
    const ui = window.__settingsUI;
    if (ui && typeof ui.listPersonas === 'function') {
      try { const l = ui.listPersonas(); if (l && l.length) return l; } catch (e) { /* seed below */ }
    }
    return (window.SETTINGS && window.SETTINGS.personas) || [];
  }
  function personaName(slug) {
    const p = personaCatalogue().find(x => personaSlug(x.id) === slug);
    return p ? p.name : slug;
  }
  function personaSlug(id) { return String(id || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'default'; }

  // ══════════════════════════════════════════════════════════ PERSONAS ═════
  // Mirrors the Agents workspace, but the Overview aggregates metrics BY PERSONA:
  // every agent that runs a persona rolls its cost / tokens / calls up to it. Reuses
  // the Agents data feeds (/agents, /agents/usage, /agents/turns) + helpers; no new
  // endpoint. Phase 2 = the read-only Overview + a persona rail (selecting a persona
  // opens it live in MRI); per-persona config moves in here in Phase 3.
  let perView = 'overview';   // 'overview' (the landing) — Phase 3 adds 'detail'
  let personaSel = null;

  function ensurePersonas() {
    const need = [];
    if (!agentsData) need.push(loadAgents());   // loadAgents also pulls usage + activity
    else {
      if (!agentActivity) need.push(loadAgentActivity());
      if (!agentUsage) need.push(loadAgentUsage());
    }
    if (personaOrg === null) need.push(loadPersonaOrg());
    if (need.length) Promise.all(need).then(() => paintPersonas()); else paintPersonas();
  }

  // The active process persona (whose owner-lane inner life MRI shows by default).
  function activePersonaSlug() {
    try { return personaSlug((typeof currentSettings !== 'undefined' && currentSettings && currentSettings.persona_name) || ''); }
    catch (e) { return ''; }
  }

  // All personas — from the settings catalogue plus any referenced by an agent — each
  // with its agents and summed usage. Personas with no agents still show (zero metrics).
  function personaRollup() {
    const ags = (agentsData && agentsData.agents) || [];
    const known = personaCatalogue();
    const map = {};
    const ensure = (slug, name) => map[slug] || (map[slug] = {
      slug, name: name || personaName(slug), agents: [],
      calls: 0, cloud_calls: 0, in_tok: 0, out_tok: 0, cloud_usd: 0, pod_s: 0, lastTs: 0,
    });
    known.forEach(p => ensure(personaSlug(p.id), p.name || p.id));
    for (const a of ags) {
      const e = ensure(personaSlug(a.persona), personaName(personaSlug(a.persona)));
      e.agents.push(a);
      const u = agentUsage && agentUsage[a.agent_id];
      if (u) {
        e.calls += u.calls || 0; e.cloud_calls += u.cloud_calls || 0;
        e.in_tok += u.in_tok || 0; e.out_tok += u.out_tok || 0;
        e.cloud_usd += u.cloud_usd || 0; e.pod_s += u.pod_s || 0;
        const lt = u.last_ts ? Date.parse(u.last_ts) : 0; if (lt > e.lastTs) e.lastTs = lt;
      }
      const act = agentActivity && agentActivity[a.agent_id];
      if (act && act.lastTs > e.lastTs) e.lastTs = act.lastTs;
    }
    return Object.values(map);
  }

  // Active if it's the running process persona OR any of its agents ran recently;
  // paused if it has agents and they're all disabled; otherwise idle.
  function personaStatus(p) {
    if ((p.slug && p.slug === activePersonaSlug()) || p.agents.some(a => agentStatus(a).state === 'active'))
      return { state: 'active', color: 'var(--ok)', cls: 'dot-status live', label: 'active' };
    if (p.agents.length && !p.agents.some(a => a.enabled !== false))
      return { state: 'paused', color: 'var(--temporal)', cls: 'dot-status', label: 'paused' };
    return { state: 'idle', color: 'var(--ink-4)', cls: 'dot-status', label: 'idle' };
  }
  function personaCostUsd(p) { return (p.cloud_usd || 0) + (p.pod_s || 0) * podRate() / 3600; }

  // Guard the persona-detail pane against silently dropping unsaved config edits when
  // navigating away (switching personas, Overview nav, or Back). Returns true when it's
  // safe to leave — nothing unsaved, or the user chose to discard.
  function confirmLeavePersonaDetail() {
    if (perView !== 'detail' || !personaSel) return true;
    const ui = window.__settingsUI;
    if (!ui || typeof ui.hasUnsavedPersona !== 'function' || !ui.hasUnsavedPersona()) return true;
    return confirm(`You have unsaved changes to ${personaSel}. Discard them?`);
  }

  // The rail's markup, split out so it can be repainted on its own — the catalogue can
  // change while the config pane is mounted, and rebuilding the pane under a live edit
  // is not acceptable (see repaintPersonaRail).
  function personaRailHtml() {
    const spec = orgSpec('personas');
    const rows = spec.items();
    const liveCount = rows.filter(p => personaStatus(p).state === 'active').length;
    const selKey = (perView === 'detail' && personaSel)
      ? (rows.find(p => p.name === personaSel) || {}).slug : null;
    return `
      <div class="rail-head"><h2>Personas</h2><span class="n">${rows.length}</span></div>
      <div class="rail-sect">
        <button class="rail-item pe-nav ${perView==='overview'?'on':''}" data-view="overview"><span class="ri-name"><span class="dot-status ${liveCount?'live':''}" style="background:${liveCount?'var(--ok)':'var(--ink-4)'}"></span>All personas</span><span class="ri-meta">${rows.length} total · ${liveCount} active</span></button>
      </div>
      <div class="rail-div"></div>
      ${railSearchHtml(spec)}
      ${railTreeHtml(spec, selKey)}
      <div class="rail-sect" style="padding-top:0;">
        <button class="rail-add" id="ws-new-persona" title="New persona">${PLUS_SVG} New persona</button>
      </div>`;
  }
  // Rail persona → configure it INLINE (the Agents rail→detail pattern): renders the
  // persona's full config — temperament dials, chemistry, self/voice — into the pane,
  // reusing the settings engine. "Open in MRI" is the "watch it live" path.
  function openPersonaDetail(slug) {
    const p = personaRollup().find(x => x.slug === slug);
    if (!p) return;
    if (p.name !== personaSel && !confirmLeavePersonaDetail()) return;
    personaSel = p.name; perView = 'detail'; paintPersonas();
  }
  function wirePersonaRail(rail) {
    if (!rail) return;
    rail.querySelectorAll('.pe-nav').forEach(n => n.addEventListener('click', () => {
      if (!confirmLeavePersonaDetail()) return;
      perView = n.dataset.view; personaSel = null; paintPersonas();
    }));
    // The rail can be repainted alone (repaintPersonaRail) while the config pane is
    // mounted, so its repaint hook must not tear that pane down — hence paintPersonas
    // is passed only for the actions that legitimately change the whole surface.
    wireRailTree(rail, orgSpec('personas'), openPersonaDetail, (o) => {
      if (perView === 'detail' && personaSel) { repaintPersonaRail(); restoreFocus(o && o.keepFocus); }
      else paintPersonas(o);
    });
    rail.querySelector('#ws-new-persona').addEventListener('click', openNewPersona);
  }
  // Refresh the rail alone, leaving the config pane mounted and untouched.
  function repaintPersonaRail() {
    const rail = document.getElementById('pers-rail');
    if (!rail) return;
    rail.innerHTML = personaRailHtml();
    wirePersonaRail(rail);
  }
  function paintPersonas(opts) {
    const keepFocus = (opts && opts.keepFocus) || null;
    const host = document.getElementById('ws-personas');
    if (!host) return;
    host.innerHTML = `
      <div class="ws-grid" style="grid-template-columns:268px 1fr;">
        <div class="ws-rail" id="pers-rail">${personaRailHtml()}</div>
        <div class="ws-main" id="pers-main"></div>
      </div>`;
    wirePersonaRail(host.querySelector('#pers-rail'));
    const main = host.querySelector('#pers-main');
    if (perView === 'detail' && personaSel) renderPersonaDetail(main);
    else renderPersonasView(main);
    restoreFocus(keepFocus);
  }

  // The settings engine owns the persona catalogue and can change it out from under
  // this rail — the server load fills in custom personas, and the mounted config pane
  // renames or deletes the persona in place. Follow those changes instead of going stale.
  document.addEventListener('personas-changed', (e) => {
    const d = (e && e.detail) || {};
    if (personaSel && d.from === personaSel) {
      if (d.to) personaSel = d.to;                          // renamed → follow it
      else { personaSel = null; perView = 'overview'; }      // deleted → back to the list
    }
    if (workspace !== 'personas') return;                    // repaints on next open
    // With the pane mounted, refresh ONLY the rail. A full repaint would tear down and
    // rebuild the config scaffold mid-edit — rename fires on the name field's blur,
    // before the click that caused the blur has landed.
    if (perView === 'detail' && personaSel) repaintPersonaRail();
    else paintPersonas();
  });

  // New persona = clone the selected one, then drop straight into its config with the
  // name selected for renaming (the settings engine owns the model; this owns the UI).
  // Unsaved — the pane's Save persists it, same as any other persona edit.
  async function openNewPersona() {
    const ui = window.__settingsUI;
    if (!ui || typeof ui.createPersona !== 'function') {
      window.alert('The persona engine is still loading — try again in a moment.');
      return;
    }
    if (!confirmLeavePersonaDetail()) return;
    let name;
    try { name = ui.createPersona(); }
    catch (e) { window.alert('Could not create persona: ' + e.message); return; }
    personaSel = name; perView = 'detail';
    paintPersonas();
    // mountPersona builds the scaffold #st-name lives in, so focus only after it lands.
    if (typeof ui.focusPersonaName === 'function') setTimeout(() => ui.focusPersonaName(), 0);
  }

  // Inline persona config: a save header + the scaffold container the settings engine
  // mounts into (#pers-cat-wrap). mountPersona() re-points the engine's chrome refs to
  // this header and rebuilds the scaffold here, so the dials/chem/self/save pipeline is
  // reused unchanged — just hosted in the workspace instead of the Settings overlay.
  function renderPersonaDetail(main) {
    if (!main) return;
    main.innerHTML = `
      <div style="display:flex; flex-direction:column; height:100%; min-height:0;">
        <header class="set-bar">
          <button class="set-back" id="pers-back-btn"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg> Overview</button>
          <div class="bar-head"><div id="pers-bar-title">Persona</div><div id="pers-bar-blurb"></div></div>
          <div class="bar-actions">
            ${orgFolderControlsHtml(orgSpec('personas'), personaRollup().find(p => p.name === personaSel))}
            <button class="mri-open" id="pers-open-mri" title="Watch this persona live in MRI">${MRI_SVG} Open in MRI</button>
            <div class="dirty-pill" id="pers-dirty-pill"><span class="chip"></span><span id="pers-dirty-text">0 unsaved</span></div>
            <button class="restart-banner" id="pers-restart-banner">Restart required</button>
            <button class="btn-save idle" id="pers-save-btn">Save</button>
          </div>
        </header>
        <div class="set-scroll" id="pers-scroll"><div class="cat-wrap" id="pers-cat-wrap"></div></div>
      </div>`;
    main.querySelector('#pers-back-btn').addEventListener('click', () => { if (!confirmLeavePersonaDetail()) return; perView = 'overview'; personaSel = null; paintPersonas(); });
    main.querySelector('#pers-open-mri').addEventListener('click', () => openPersonaInMri(personaSlug(personaSel)));
    // Filing + pinning from the detail header. The config pane below is mounted by the
    // settings engine and must survive, so these repaint the rail only.
    wireOrgFolderControls(main, orgSpec('personas'), () => personaRollup().find(p => p.name === personaSel), repaintPersonaRail);
    if (window.__settingsUI && window.__settingsUI.mountPersona) window.__settingsUI.mountPersona(personaSel);
  }

  function renderPersonasView(main) {
    if (!main) return;
    const spec = orgSpec('personas');
    const rows = spec.items();
    const counts = { active: 0, idle: 0, paused: 0 };
    rows.forEach(p => counts[personaStatus(p).state]++);
    const table = rosterTableHtml(spec);
    const rangeTotal = table.list.reduce((s, p) => s + personaCostUsd(p), 0);
    main.innerHTML = `<div class="main-pad" style="max-width:none;">
      <div class="between" style="align-items:flex-start;">
        <div>
          <div class="page-eyebrow">Personas · identity</div>
          <div class="page-title">All personas</div>
          <p class="page-lede">Every persona and the agents running under it — cost, tokens and calls summed across all of them. Same rail, same folders: file personas however your work is actually organised, then click one to open its configuration.</p>
        </div>
        <div class="row" style="gap:10px; margin-top:14px; flex-shrink:0; align-items:center;">
          <span class="chip"><span class="dot live" style="background:var(--ok);"></span>${counts.active} active</span>
          <span class="data" id="pers-pod-meter" style="font-size:10px; color:var(--ink-4);"></span>
          <button class="btn btn-primary" id="pers-new-btn">New persona</button>
        </div>
      </div>
      ${rosterFiltersHtml(spec)}
      ${rangeBarHtml({ allMode: false, scope: false, total: rangeTotal })}
      ${rows.length
        ? `<div style="margin-top:18px;">${table.html}</div>
           <div class="foot-note">${table.list.length} of ${rows.length} shown · ${counts.active} active · ${counts.idle} idle · ${counts.paused} paused${spec.canEdit() ? ' · drag a row onto a folder in the rail to file it' : ''}.
             Per-persona totals roll up every agent that runs this persona — its real cloud spend plus its share of the GPU pod (compute-seconds × the pod's $/hr), cumulative over the range and summed across restarts. A persona's own owner-lane idle work isn't metered here.</div>`
        : `<div class="empty" style="margin-top:22px;"><h3>No personas</h3></div>`}
      </div>`;
    wireRangeBar(main);
    wireRoster(main, spec, openPersonaDetail, paintPersonas);
    const newBtn = main.querySelector('#pers-new-btn');
    if (newBtn) newBtn.addEventListener('click', openNewPersona);
    const pm = main.querySelector('#pers-pod-meter');
    if (pm && podStatus && podStatus.running && podStatus.uptime_s != null) {
      const cost = podStatus.cost_accrued_usd != null ? ` · $${podStatus.cost_accrued_usd.toFixed(2)} accrued` : '';
      pm.innerHTML = `GPU pod · up ${esc(fmtDur(podStatus.uptime_s))}${cost}`;
    }
  }

  // Open a persona in MRI (persona focus). The ACTIVE process persona shows live now —
  // clear any agent observation so MRI paints the owner lane (its own inner life). A
  // NON-active persona can't show live thoughts without a process restart, so we don't
  // auto-switch — we surface an explicit "switch to this persona" choice (the restart).
  function openPersonaInMri(slug) {
    if (slug && slug === activePersonaSlug()) {
      if (typeof window.setObservedAgent === 'function') window.setObservedAgent(null);
      setWorkspace('labs');
      return;
    }
    // Non-active persona: switching is a process restart, so we never auto-switch —
    // requestPersonaSwitch (the persona picker's path) shows its own confirm + restarts.
    const p = personaRollup().find(x => x.slug === slug);
    const name = p ? p.name : slug;
    if (typeof window.requestPersonaSwitch === 'function') window.requestPersonaSwitch(name);
  }


  function renderAgentDetail(main) {
    const a = ((agentsData && agentsData.agents) || []).find(x => x.agent_id === agentSel);
    const ceilings = (agentsData && agentsData.ceilings) || {};
    if (!a) { main.innerHTML = '<div class="main-pad"><div class="empty"><h3>Agent not found</h3></div></div>'; return; }
    const perms = (a.permissions && typeof a.permissions === 'object') ? { ...a.permissions } : {};
    const spec = orgSpec('agents');
    const m = agentMetrics(a);
    const st = agentStatus(a);
    main.innerHTML = `<div class="main-pad" style="max-width:820px;">
      <button class="link ag-back" style="margin-bottom:18px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg> All agents</button>
      <div class="between" style="align-items:flex-start;">
        <div>
          <div class="page-eyebrow">${esc(a.agent_id)}</div>
          <div class="page-title" style="font-size:26px;">${esc(a.name || a.agent_id)}</div>
          <div class="row" style="gap:8px; margin-top:12px; flex-wrap:wrap;">
            <span class="chip persona"><span class="dot"></span>${esc(personaName(a.persona))}</span>
            <span class="chip role"><span class="dot"></span>${esc(a.mandate_id)}</span>
            <span class="chip"><span class="${st.cls}" style="background:${st.color}"></span>${esc(st.label)}</span>
            ${a.folder ? `<span class="chip">${FOLD_SVG}&nbsp;${esc(a.folder)}</span>` : ''}
          </div>
        </div>
        <div class="row" style="gap:8px; margin-top:8px; flex-shrink:0;">
          <button class="btn org-pin-btn" title="${a.pinned ? 'Unpin' : 'Pin to the top of the rail'}">${a.pinned ? '★ Pinned' : '☆ Pin'}</button>
          <button class="mri-open" id="ag-view-persona" title="Watch this agent live in MRI">${MRI_SVG} Open in MRI</button>
        </div>
      </div>
      <div class="metrics">
        <div class="metric"><div class="mv cost">${m.enabled ? '$' + m.cost.toFixed(2) : '—'}</div><div class="ml">Est. cost</div></div>
        <div class="metric"><div class="mv">${m.enabled ? esc(fmtTokens(m.tok)) : '—'}</div><div class="ml">Tokens</div></div>
        <div class="metric"><div class="mv">${m.enabled ? m.calls : '—'}</div><div class="ml">Model calls</div></div>
        <div class="metric"><div class="mv" style="font-size:15px;">${m.last ? esc(agoShort(Date.now() - m.last)) + ' ago' : '—'}</div><div class="ml">Last active</div></div>
      </div>
      <div class="note" style="margin-top:22px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg><p>Every value is <b>bounded by the account ceiling</b> set in Account Limits — an agent can be granted less, never more. Leave a field blank to inherit.</p></div>
      <div class="card" style="margin-top:18px;">
        <div class="card-head"><span class="ch-num">01</span><div><div class="ch-title">Organisation</div><div class="ch-desc">how you file it · persona and role are already derived from the id</div></div></div>
        <div class="card-body">
          <div class="org-field"><span class="fl">Display name<small>Display only — the agent id <b>${esc(a.agent_id)}</b> never changes.</small></span>
            <input class="ctrl-input" id="ag-name" style="min-width:220px;" value="${esc(a.name || '')}" placeholder="${esc(a.agent_id)}"></div>
          <div class="org-field"><span class="fl">Folder<small>Your own grouping. Saved as you pick it.</small></span>
            <select class="ctrl-input org-folder-sel">${orgFolderOptionsHtml(spec, a.folder || '')}</select></div>
          <div class="org-field"><span class="fl">Pinned<small>Floats to the top of the rail and the roster.</small></span>
            <div class="toggle${a.pinned ? ' on' : ''}" id="ag-pin-tog" role="switch" aria-checked="${!!a.pinned}"></div></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><span class="ch-num">02</span><div><div class="ch-title">Capabilities &amp; limits · motor cortex</div><div class="ch-desc">bounded by the account ceiling</div></div></div>
        <div class="card-body" id="ag-perm-body"></div>
      </div>
      <div class="card">
        <div class="card-head"><span class="ch-num">03</span><div><div class="ch-title">Connectors</div><div class="ch-desc">MCP server access per task type · all checked = inherit org default</div></div></div>
        <div class="card-body" id="ag-connectors-body"><div class="ctrl"><div class="ctrl-meta"><div class="hint" style="padding:4px 0;">Loading connectors…</div></div></div></div>
      </div>
      <div class="row" style="margin-top:16px; gap:10px;">
        <button class="btn btn-primary" id="ag-save"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Save</button>
        <button class="btn" id="ag-remove">Remove agent</button>
      </div></div>`;
    const body = main.querySelector('#ag-perm-body');
    AGENT_PERM_FIELDS.forEach(f => body.appendChild(permRow(f, perms, ceilings)));

    (async () => {
      const connectors = await fetchConnectors();
      const cbody = main.querySelector('#ag-connectors-body');
      if (!cbody) return;
      if (!connectors.length) {
        cbody.innerHTML = '<div class="ctrl"><div class="ctrl-meta"><div class="hint" style="padding:4px 0;">No MCP connectors configured on this org.</div></div></div>';
        return;
      }
      cbody.innerHTML = '';
      const parseList = v => v ? String(v).split('\n').map(s => s.trim()).filter(Boolean) : [];
      [
        { key: 'motor_user_connectors', lab: 'User-directed tasks', hint: 'connectors available when user triggers a motor action' },
        { key: 'motor_self_connectors', lab: 'Self-directed tasks',  hint: 'connectors available during autonomous / background runs' },
      ].forEach(({ key, lab, hint }) => {
        const cur = parseList(perms[key]);
        const allInherited = cur.length === 0;
        const row = document.createElement('div');
        row.className = 'ctrl';
        row.style.cssText = 'flex-direction:column; align-items:stretch; gap:10px;';
        const meta = document.createElement('div'); meta.className = 'ctrl-meta';
        meta.innerHTML = `<div class="lab">${lab}</div><div class="hint">${hint}</div>`;
        row.appendChild(meta);
        const grid = document.createElement('div'); grid.className = 'connector-grid';
        connectors.forEach(name => {
          const checked = allInherited || cur.includes(name);
          const lbl = document.createElement('label'); lbl.className = 'connector-check';
          const cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = checked; cb.dataset.connector = name;
          lbl.appendChild(cb); lbl.append(' ' + name);
          grid.appendChild(lbl);
        });
        grid.addEventListener('change', () => {
          const on = [...grid.querySelectorAll('input')].filter(c => c.checked).map(c => c.dataset.connector);
          if (on.length === connectors.length || on.length === 0) delete perms[key];
          else perms[key] = on.join('\n');
        });
        row.appendChild(grid);
        cbody.appendChild(row);
      });
    })();

    main.querySelector('.ag-back').addEventListener('click', () => { agView = 'agents'; agentSel = null; paintAgents(); });
    // Folder + pin save on the spot (optimistic), unlike name/permissions which wait
    // for Save — filing is navigation, not configuration, and must not need a commit.
    wireOrgFolderControls(main, spec, () => a, paintAgents);
    main.querySelector('#ag-pin-tog').addEventListener('click', () => {
      const next = !a.pinned;
      spec.persist(a, { pinned: next });
      wsToast((next ? 'Pinned ' : 'Unpinned ') + spec.name(a));
      paintAgents();
    });
    // Observe THIS agent's live lane in MRI (chemistry + idle thoughts), same as the
    // dashboard cards — not a blind jump to whatever persona is already selected.
    main.querySelector('#ag-view-persona').addEventListener('click', () => openAgentInLabs(a.agent_id, a.name, a.persona));
    main.querySelector('#ag-save').addEventListener('click', () => saveAgent(a.agent_id, main.querySelector('#ag-name').value.trim(), perms));
    main.querySelector('#ag-remove').addEventListener('click', () => removeAgent(a.agent_id));
  }
  function permRow(f, perms, ceilings) {
    const ceil = ceilings[f.key];
    const row = document.createElement('div'); row.className = 'ctrl';
    const ceilTxt = f.type === 'bool' ? (ceil ? 'account: allowed' : 'account: disabled') : `account: ${ceil === '' || ceil == null ? '—' : ceil}`;
    row.innerHTML = `<div class="ctrl-meta"><div class="lab">${f.label}</div><div class="hint">${f.hint} · ${ceilTxt}</div></div>`;
    const field = document.createElement('div'); field.className = 'ctrl-field'; field.style.justifyContent = 'flex-end';
    let input;
    if (f.type === 'bool') {
      const on = (f.key in perms) ? !!(+perms[f.key]) : !!(typeof ceil === 'string' ? +ceil : ceil);
      input = document.createElement('div'); input.className = 'toggle' + (on ? ' on' : '');
      const ceilOn = !!(typeof ceil === 'string' ? +ceil : ceil);
      if (!ceilOn) input.classList.add('disabled');
      input.addEventListener('click', () => { if (!ceilOn) return; const nv = !input.classList.contains('on'); input.classList.toggle('on', nv); perms[f.key] = nv ? 1 : 0; });
    } else if (f.type === 'cloud') {
      input = document.createElement('select'); input.className = 'ctrl-input';
      const ceilRank = CLOUD_RANK[String(ceil || 'off')] ?? 0;
      [['off', 'Off'], ['ro', 'Read-only'], ['full', 'Full']].forEach(([v, t]) => { const o = document.createElement('option'); o.value = v; o.textContent = t; if ((CLOUD_RANK[v] ?? 0) > ceilRank) o.disabled = true; input.appendChild(o); });
      input.value = (f.key in perms) ? perms[f.key] : (ceil || 'off');
      input.addEventListener('change', () => { perms[f.key] = input.value; });
    } else if (f.type === 'dirs') {
      input = document.createElement('textarea'); input.className = 'ctrl-input'; input.placeholder = 'inherit account roots'; input.value = perms[f.key] || '';
      input.addEventListener('input', () => { if (input.value.trim()) perms[f.key] = input.value; else delete perms[f.key]; });
      field.style.justifyContent = 'stretch'; field.style.width = '100%';
    } else {
      input = document.createElement('input'); input.type = 'number'; input.className = 'ctrl-input'; input.step = 'any';
      if (ceil !== '' && ceil != null) input.max = ceil;
      input.placeholder = `inherit (${ceil ?? '—'})`; input.value = (f.key in perms) ? perms[f.key] : '';
      input.addEventListener('input', () => { if (input.value !== '') perms[f.key] = +input.value; else delete perms[f.key]; });
    }
    field.appendChild(input); row.appendChild(field); return row;
  }
  async function saveAgent(id, name, perms) {
    try {
      const r = await fetch('/agents/' + encodeURIComponent(id) + '/permissions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ permissions: perms }) });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
      await fetch('/agents/' + encodeURIComponent(id) + '/name', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
      await loadAgents();
    } catch (e) { window.alert('Could not save agent: ' + e.message); }
  }
  async function removeAgent(id) {
    if (!window.confirm(`Remove agent "${id}"? The persona and role are unaffected — only this pairing is deleted.`)) return;
    try {
      const r = await fetch('/agents/' + encodeURIComponent(id), { method: 'DELETE' });
      if (!r.ok && r.status !== 404) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
      agentSel = null; agView = 'agents'; await loadAgents();
    } catch (e) { window.alert('Could not remove agent: ' + e.message); }
  }

  function renderRoles(main) {
    const roles = (agentsData && agentsData.roles) || [];
    main.innerHTML = `<div class="main-pad" style="max-width:760px;">
      <div class="page-eyebrow">Role · job instructions</div>
      <div class="page-title">Roles</div>
      <p class="page-lede">Reusable instructions any persona can wear. The same role drives every agent built from it.</p>
      <div class="row" style="margin-top:18px; gap:10px; flex-wrap:wrap;" id="role-chips">
        ${roles.map(r => `<button class="pick role-pick" data-id="${esc(r.id)}">${esc(r.id)}</button>`).join('')}
        <button class="btn" id="role-new"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg> New role</button>
      </div>
      <div id="role-editor"></div></div>`;
    main.querySelectorAll('.role-pick').forEach(p => p.addEventListener('click', () => { agRoleSel = p.dataset.id; main.querySelectorAll('.role-pick').forEach(x => x.classList.remove('on')); p.classList.add('on'); openRole(main, p.dataset.id); }));
    main.querySelector('#role-new').addEventListener('click', () => openNewRole(main));
    const toOpen = (agRoleSel && roles.find(r => r.id === agRoleSel)) ? agRoleSel : (roles[0] ? roles[0].id : null);
    if (toOpen) { main.querySelector(`.role-pick[data-id="${CSS.escape(toOpen)}"]`)?.classList.add('on'); openRole(main, toOpen); }
  }
  function openNewRole(main) {
    const modal = document.getElementById('ws-new-agent-modal');
    const roles = (agentsData && agentsData.roles) || [];
    const existing = id => roles.find(r => r.id === id);
    const VALID_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;
    const slugify = s => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
    modal.innerHTML = `<div class="modal">
      <div class="modal-head"><div class="serif-h" style="font-size:19px;">New role</div><button class="tool-x" id="nr-x"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>
      <p class="page-lede" style="margin-top:4px; font-size:14px;">Give this role a friendly name — the system id is generated automatically.</p>
      <div style="margin-top:20px;">
        <div class="label" style="margin-bottom:6px;">Name</div>
        <div class="input-line"><input id="nr-name" type="text" placeholder="e.g. Personal Assistant" autocomplete="off" /></div>
        <div class="label" style="margin:16px 0 6px;">System id <span style="opacity:.5; font-size:10px; text-transform:none; letter-spacing:0;">· editable</span></div>
        <div class="input-line"><input id="nr-id" type="text" placeholder="personal_assistant" autocomplete="off" /></div>
        <div id="nr-err" style="color:#c84; font-family:var(--mono); font-size:10px; margin-top:8px; min-height:14px;"></div>
      </div>
      <div class="row" style="justify-content:flex-end; margin-top:18px; gap:10px;">
        <button class="btn" id="nr-cancel">Cancel</button>
        <button class="btn btn-primary" id="nr-create" disabled>Create role</button>
      </div></div>`;
    const nameIn = modal.querySelector('#nr-name');
    const idIn = modal.querySelector('#nr-id');
    const errDiv = modal.querySelector('#nr-err');
    const createBtn = modal.querySelector('#nr-create');
    let idEdited = false;
    const validate = () => {
      const id = idIn.value.trim();
      if (!id) { errDiv.textContent = ''; errDiv.style.color = '#c84'; createBtn.disabled = true; createBtn.textContent = 'Create role'; return; }
      if (!VALID_ID.test(id)) { errDiv.textContent = 'Use lowercase letters, digits, _ or - (must start with a letter or digit)'; errDiv.style.color = '#c84'; createBtn.disabled = true; createBtn.textContent = 'Create role'; return; }
      // A colliding id is NOT an error — it opens the existing role for editing. Say so,
      // and relabel the button, so the user can't blank an existing role's instructions
      // by accident (which is exactly what an unconditional openRole(id, '') would do).
      const hit = existing(id);
      if (hit) { errDiv.textContent = `“${id}” already exists — you'll edit it, not overwrite it.`; errDiv.style.color = 'var(--ink-4)'; createBtn.textContent = 'Open role'; }
      else { errDiv.textContent = ''; errDiv.style.color = '#c84'; createBtn.textContent = 'Create role'; }
      createBtn.disabled = false;
    };
    nameIn.addEventListener('input', () => { if (!idEdited) idIn.value = slugify(nameIn.value); validate(); });
    idIn.addEventListener('input', () => { idEdited = true; validate(); });
    const close = () => { modal.classList.remove('open'); modal.innerHTML = ''; };
    const create = () => {
      const id = idIn.value.trim();
      if (!id || !VALID_ID.test(id)) return;
      close();
      // Existing → open with its real text (forceText=null reads role.role_text); new →
      // start blank. Never force '' onto an id that already carries instructions.
      const hit = existing(id);
      main.querySelectorAll('.role-pick').forEach(x => x.classList.toggle('on', x.dataset.id === id));
      openRole(main, id, hit ? null : '');
    };
    modal.querySelector('#nr-x').addEventListener('click', close);
    modal.querySelector('#nr-cancel').addEventListener('click', close);
    createBtn.addEventListener('click', create);
    nameIn.addEventListener('keydown', e => { if (e.key === 'Enter') idIn.focus(); });
    idIn.addEventListener('keydown', e => { if (e.key === 'Enter' && !createBtn.disabled) create(); });
    modal.classList.add('open');
    modal.addEventListener('click', e => { if (e.target === modal) close(); });
    nameIn.focus();
  }
  function openRole(main, id, forceText) {
    agRoleSel = id;
    const roles = (agentsData && agentsData.roles) || [];
    const role = roles.find(r => r.id === id);
    const savedText = forceText != null ? forceText : (role ? role.role_text || '' : '');
    const _check = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
    const _file  = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3v5h5"/><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/></svg>';
    const ed = main.querySelector('#role-editor');
    ed.innerHTML = `<div class="self-editor" style="margin-top:22px;">
      <div class="self-bar">
        <span class="self-file">${_file}<b>${esc(id)}</b> · <span style="color:var(--ink-4)">role</span></span>
        <span class="self-modepill" id="role-dirty"><i></i>edited</span>
        <span class="spacer"></span>
        <span class="self-meta" id="role-count"></span>
        <div class="self-seg" id="role-seg"><button type="button" data-m="edit">Edit</button><button type="button" data-m="preview">Preview</button></div>
        <button class="self-revert" id="role-save">${_check}<span>Save</span></button>
      </div>
      <textarea class="self-area" id="role-text" spellcheck="false" placeholder="Describe this role — the job, tone, and rules for it…">${esc(savedText)}</textarea>
      <div class="self-preview" id="role-preview" hidden></div>
    </div>
    ${role ? `<div style="margin-top:12px; display:flex; justify-content:flex-end;"><button class="link" id="role-delete" style="color:var(--danger); font-size:11px;">Delete role</button></div>` : ''}`;
    const area = ed.querySelector('#role-text');
    const preview = ed.querySelector('#role-preview');
    const countEl = ed.querySelector('#role-count');
    const dirtyPill = ed.querySelector('#role-dirty');
    const saveBtn = ed.querySelector('#role-save');
    const updateMeta = () => {
      const t = area.value;
      countEl.textContent = `${t.length.toLocaleString()} chars`;
      const dirty = t !== savedText;
      dirtyPill.classList.toggle('on', dirty);
      saveBtn.disabled = !dirty && !!role; // allow save for new (unsaved) roles even if empty
    };
    const applyMode = () => {
      const pre = agRoleMode === 'preview';
      area.hidden = pre; preview.hidden = !pre;
      if (pre) preview.innerHTML = mdRender(area.value);
      ed.querySelectorAll('#role-seg button').forEach(b => b.classList.toggle('on', b.dataset.m === agRoleMode));
    };
    ed.querySelectorAll('#role-seg button').forEach(b => b.addEventListener('click', () => { agRoleMode = b.dataset.m; applyMode(); }));
    area.addEventListener('input', updateMeta);
    updateMeta(); applyMode();
    saveBtn.addEventListener('click', async () => {
      const lbl = saveBtn.querySelector('span');
      lbl.textContent = 'Saving…'; saveBtn.disabled = true;
      try {
        const r = await fetch('/mandates', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, role_text: area.value }) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
        await loadAgents(); // agRoleSel re-selects this role after re-render
      } catch (e) { lbl.textContent = 'Save'; saveBtn.disabled = false; window.alert('Could not save role: ' + e.message); }
    });
    const delBtn = ed.querySelector('#role-delete');
    if (delBtn) delBtn.addEventListener('click', () => deleteRole(main, id));
  }
  // Remove a role from the library. Soft-delete server-side (episodes/tasks still
  // reference the id): it stops appearing in the Roles list and can't back new agents,
  // but any agent already built on it keeps its mandate_id until that agent is removed.
  async function deleteRole(main, id) {
    if (!window.confirm(`Delete role "${id}"? It's removed from the library, so you can't build new agents on it. Agents already using it keep it until you remove them.`)) return;
    try {
      const r = await fetch('/mandates/' + encodeURIComponent(id), { method: 'DELETE' });
      if (!r.ok && r.status !== 404) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
      if (agRoleSel === id) agRoleSel = null;
      await loadAgents(); // re-renders the Agents surface; renderRoles re-selects a role
    } catch (e) { window.alert('Could not delete role: ' + e.message); }
  }

  async function loadConnectorDetails() {
    try {
      const r = await fetch('/connectors?full=1');
      if (r.ok) { const d = await r.json(); connectorsDetails = d.details || []; connectorsCache = d.connectors || []; connectorsEnvManaged = !!d.env_managed; connectorsCloud = d.cloud || null; }
    } catch (e) { connectorsDetails = []; }
  }
  function renderConnectors(main) {
    const rows = (connectorsDetails || []);
    const envManaged = connectorsEnvManaged;
    const _plus = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>';
    const _trash = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>';
    const _info = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 16v-4M12 8h.01"/><circle cx="12" cy="12" r="10"/></svg>';
    // The cloud connector (Claude) is the conduit — the MCP connectors are reached
    // through it. Show whether it's hooked up + which model, so the list isn't just
    // a bag of MCP servers with no indication of how the agent reaches them.
    const cl = connectorsCloud || {};
    const clOn = !!cl.available && cl.actions_enabled !== false;
    const clStatus = !cl.available ? 'Not connected — no Anthropic key on this org'
      : (cl.actions_enabled === false ? 'Key present · cloud actions disabled in Account Limits'
      : 'Connected · the agent can reach external services through Claude');
    const cloudCard = `<div class="card" style="margin-top:18px; display:flex; align-items:center; gap:14px;">
        <span class="dot-status ${clOn ? 'live' : ''}" style="background:${clOn ? 'var(--ok)' : 'var(--temporal)'}; flex-shrink:0;"></span>
        <div style="flex:1; min-width:0;">
          <div class="serif-h" style="font-size:16px;">Claude <span class="data" style="font-size:9px; opacity:.55; letter-spacing:.04em;">CLOUD CONNECTOR</span></div>
          <div class="data" style="font-size:10px; margin-top:3px; color:var(--ink-3);">${esc(clStatus)}</div>
        </div>
        ${cl.available && cl.model ? `<span class="chip"><span class="dot"></span>${esc(cl.model)}</span>` : ''}
      </div>`;
    // Claude's built-in (native) tools — available on every cloud action with no
    // connector. ✎ marks the mutating (write/shell) tools, which are approval-gated.
    const nativeTools = (cl.native_tools) || [];
    const nativeBlock = nativeTools.length ? `
      <div class="rail-sect-lab" style="margin-top:18px; padding-left:2px;">Native tools · built into Claude</div>
      <p class="data" style="font-size:9px; color:var(--ink-4); margin:4px 0 8px; line-height:1.6;">Claude's own toolset — available on every cloud action without any connector. <b>✎</b> = mutating (write / shell), approval-gated.</p>
      <div style="display:flex; flex-wrap:wrap; gap:6px;">
        ${nativeTools.map(t => `<span class="chip" title="${esc(t.group || '')}${t.write ? ' · write/shell · approval-gated' : ' · read-only'}">${esc(t.name)}${t.write ? ' ✎' : ''}</span>`).join('')}
      </div>` : '';
    main.innerHTML = `<div class="main-pad" style="max-width:760px;">
      <div class="between"><div><div class="page-eyebrow">Governance · MCP</div><div class="page-title">Connectors</div>
      <p class="page-lede">External services the agent reaches <b>through Claude</b>, the cloud connector. The brain dispatches a cloud action and Claude calls the MCP servers below. Registering one generates a shared secret — copy it to both Railway and your app. Shown once.</p></div>
      ${envManaged ? '' : `<button class="btn btn-primary" id="conn-register" style="margin-top:8px;">${_plus} Register connector</button>`}</div>
      ${cloudCard}
      ${nativeBlock}
      ${envManaged ? `<div class="note" style="margin-top:18px;">${_info}<p>Connectors are pinned via <b>BRAIN_CMA_MCP_SERVERS</b> and are read-only here. Unset that environment variable to manage connectors from this page.</p></div>` : ''}
      <div class="rail-sect-lab" style="margin-top:22px; padding-left:2px;">Connectors · available through Claude${rows.length ? ` · ${rows.length}` : ''}</div>
      <div class="mint-reveal" id="conn-reveal"></div>
      <div class="ag-table" style="margin-top:24px;">
        <div class="ag-table-head" style="grid-template-columns:1fr 2fr 1fr 60px;"><span>Name</span><span>URL</span><span>Env vars</span><span></span></div>
        ${rows.length ? rows.map(c => {
          const ek = esc(c.name.toUpperCase().replace(/-/g,'_'));
          return `<div class="ag-row" style="grid-template-columns:1fr 2fr 1fr 60px; cursor:default;">
            <span><span class="serif-h" style="font-size:14px;">${esc(c.display_name||c.name)}</span><span class="data" style="font-size:9px;display:block;margin-top:2px;">${esc(c.name)}</span></span>
            <span class="data" style="font-size:11px;word-break:break-all;">${esc(c.url)}</span>
            <span class="data" style="font-size:9px;line-height:1.8;opacity:.7;">BRAIN_CMA_MCP_${ek}_TOKEN<br>${ek}_MCP_SECRET</span>
            <span>${envManaged ? '' : `<button class="link conn-remove" data-name="${esc(c.name)}" style="color:var(--ink-4);">${_trash}</button>`}</span>
          </div>`;
        }).join('') : '<div style="padding:22px;text-align:center;" class="data">No connectors registered yet.</div>'}
      </div></div>`;
    main.querySelector('#conn-register')?.addEventListener('click', () => openRegisterConnector(main));
    main.querySelectorAll('.conn-remove').forEach(b => b.addEventListener('click', () => removeConnectorUI(b.dataset.name, main)));
  }
  async function removeConnectorUI(name, main) {
    if (!window.confirm(`Remove connector "${name}"? The agent will no longer be able to call it.`)) return;
    try {
      const r = await fetch('/connectors/' + encodeURIComponent(name), { method: 'DELETE' });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
      connectorsCache = null; connectorsDetails = null;
      await loadConnectorDetails();
      renderConnectors(main);
    } catch (e) { window.alert('Could not remove connector: ' + e.message); }
  }
  function openRegisterConnector(main) {
    const modal = document.getElementById('ws-new-agent-modal');
    const VALID = /^[a-z0-9][a-z0-9_-]{0,63}$/;
    const slugify = s => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
    modal.innerHTML = `<div class="modal" style="width:520px;">
      <div class="modal-head"><div class="serif-h" style="font-size:19px;">Register connector</div><button class="tool-x" id="rc-x"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>
      <p class="page-lede" style="margin-top:4px;font-size:14px;">A shared secret is generated and shown once. Set it on both sides.</p>
      <div style="margin-top:20px;">
        <div class="label" style="margin-bottom:6px;">URL</div>
        <div class="input-line"><input id="rc-url" type="url" placeholder="https://your-app.up.railway.app/api/mcp" autocomplete="off"/></div>
        <div class="label" style="margin:16px 0 6px;">Connector name <span style="opacity:.5;font-size:10px;text-transform:none;letter-spacing:0;">· auto-generated from URL, editable</span></div>
        <div class="input-line"><input id="rc-name" type="text" placeholder="my_connector" autocomplete="off"/></div>
        <div class="label" style="margin:16px 0 6px;">Display name <span style="opacity:.5;font-size:10px;text-transform:none;letter-spacing:0;">· optional, shown in the list</span></div>
        <div class="input-line"><input id="rc-display" type="text" placeholder="My Connector" autocomplete="off"/></div>
        <div id="rc-err" style="color:#c84;font-family:var(--mono);font-size:10px;margin-top:8px;min-height:14px;"></div>
      </div>
      <div class="row" style="justify-content:flex-end;margin-top:18px;gap:10px;">
        <button class="btn" id="rc-cancel">Cancel</button>
        <button class="btn btn-primary" id="rc-create" disabled>Register</button>
      </div></div>`;
    const urlIn = modal.querySelector('#rc-url');
    const nameIn = modal.querySelector('#rc-name');
    const displayIn = modal.querySelector('#rc-display');
    const errDiv = modal.querySelector('#rc-err');
    const createBtn = modal.querySelector('#rc-create');
    let nameEdited = false;
    const validate = () => {
      const n = nameIn.value.trim();
      if (!n) { errDiv.textContent = ''; createBtn.disabled = true; return; }
      if (!VALID.test(n)) { errDiv.textContent = 'Lowercase letters, digits, _ or - only (must start with letter or digit)'; createBtn.disabled = true; }
      else { errDiv.textContent = ''; createBtn.disabled = !urlIn.value.trim(); }
    };
    const guessName = url => {
      try { const h = new URL(url).hostname; return slugify(h.split('.')[0]) || ''; } catch { return ''; }
    };
    urlIn.addEventListener('input', () => { if (!nameEdited) nameIn.value = guessName(urlIn.value); validate(); });
    nameIn.addEventListener('input', () => { nameEdited = true; validate(); });
    const close = () => { modal.classList.remove('open'); modal.innerHTML = ''; };
    const create = async () => {
      const name = nameIn.value.trim(), url = urlIn.value.trim(), display_name = displayIn.value.trim();
      if (!name || !url || !VALID.test(name)) return;
      createBtn.disabled = true; createBtn.textContent = 'Registering…';
      try {
        const r = await fetch('/connectors', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, url, display_name }) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
        const j = await r.json();
        close();
        connectorsCache = null; connectorsDetails = null;
        await loadConnectorDetails();
        renderConnectors(main);
        const reveal = main.querySelector('#conn-reveal');
        if (reveal && j.secret) {
          reveal.classList.add('on');
          reveal.innerHTML = `<div class="row" style="gap:9px;margin-bottom:10px;"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--signal-deep)" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg><span class="serif-h" style="font-size:16px;">"${esc(j.name)}" registered</span></div>
            <p style="font-size:13px;color:var(--ink-2);line-height:1.5;">Set this secret on <b>your app</b> — <b>it won't be shown again.</b> The brain already stores it securely; nothing to set on the brain side.</p>
            <div class="token-box" style="flex-direction:column;align-items:stretch;gap:10px;">
              <div class="row" style="justify-content:space-between;gap:12px;"><span class="data" style="font-size:12px;word-break:break-all;color:var(--ink);">${esc(j.secret)}</span><button class="btn btn-sm" id="conn-copy">Copy</button></div>
              <div style="font-family:var(--mono);font-size:10px;color:var(--ink-3);line-height:2;border-top:1px solid var(--line-faint);padding-top:10px;">
                Your app &nbsp;→&nbsp; <b style="color:var(--ink);">${esc(j.app_env_var)}</b>
              </div>
            </div>`;
          reveal.querySelector('#conn-copy').addEventListener('click', () => navigator.clipboard?.writeText(j.secret));
          reveal.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      } catch (e) { createBtn.disabled = false; createBtn.textContent = 'Register'; errDiv.textContent = e.message; }
    };
    modal.querySelector('#rc-x').addEventListener('click', close);
    modal.querySelector('#rc-cancel').addEventListener('click', close);
    createBtn.addEventListener('click', create);
    urlIn.addEventListener('keydown', e => { if (e.key === 'Enter') nameIn.focus(); });
    nameIn.addEventListener('keydown', e => { if (e.key === 'Enter' && !createBtn.disabled) create(); });
    modal.classList.add('open');
    modal.addEventListener('click', e => { if (e.target === modal) close(); });
    urlIn.focus();
  }

  const LIMIT_FIELDS = [
    { key: 'cloud_daily_usd_budget', label: 'Org daily cloud budget (USD)', hint: 'combined cap / day', type: 'num' },
    { key: 'ralph_max_total_attempts', label: 'Ralph max attempts', hint: 'hard cap on tool dispatches / job', type: 'num' },
    { key: 'motor_max_jobs_per_window', label: 'Jobs / window', hint: 'autonomous job starts per window', type: 'num' },
    { key: 'motor_enable_shell', label: 'Shell commands', hint: 'org-wide capability', type: 'bool' },
    { key: 'motor_enable_network', label: 'Network fetch', hint: 'org-wide capability', type: 'bool' },
    { key: 'motor_enable_cloud_actions', label: 'Cloud actions', hint: 'org-wide capability', type: 'bool' },
    { key: 'motor_allowed_dirs', label: 'Allowed directories', hint: 'absolute paths — the outer bound', type: 'dirs' },
  ];
  function renderAccountLimits(main) {
    const ceilings = (agentsData && agentsData.ceilings) || {};
    const patch = {};
    // The org admin sets their own org's ceilings (motor reach + spend) — these
    // bound every agent in the org, which the per-agent editors narrow within.
    // Filesystem grants saved here are jailed to the tenant root server-side.
    // Read-only for plain members (who can't reach this workspace anyway).
    const readOnly = !orgAdmin;
    main.innerHTML = `<div class="main-pad" style="max-width:720px;">
      <div class="page-eyebrow">Governance · org-level</div>
      <div class="page-title">Account Limits</div>
      <p class="page-lede">The ceilings every agent is bounded by — the maximum motor reach and operational spend for the whole organization; per-agent editors can grant any value up to these, never beyond.${readOnly ? ' <b>Set by the platform</b> — view only.' : ' Set the ceilings here.'}</p>
      <div class="card" style="margin-top:24px;">
        <div class="card-head"><span class="ch-num">A</span><div><div class="ch-title">Account ceilings</div><div class="ch-desc">the outer bound on what any agent may touch</div></div></div>
        <div class="card-body" id="limit-body"></div>
      </div>
      ${readOnly
        ? `<div class="note" style="margin-top:16px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg><p>Account ceilings are set by the platform. Narrow each agent within them in its permission editor.</p></div>`
        : `<div class="row" style="margin-top:16px;"><button class="btn btn-primary" id="limit-save"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Save limits</button></div>`}
      </div>`;
    const body = main.querySelector('#limit-body');
    LIMIT_FIELDS.forEach(f => {
      const cur = ceilings[f.key];
      const row = document.createElement('div'); row.className = 'ctrl';
      row.innerHTML = `<div class="ctrl-meta"><div class="lab">${f.label}</div><div class="hint">${f.hint}</div></div>`;
      const field = document.createElement('div'); field.className = 'ctrl-field'; field.style.justifyContent = 'flex-end';
      let input;
      if (f.type === 'bool') {
        const on = !!(typeof cur === 'string' ? +cur : cur);
        input = document.createElement('div'); input.className = 'toggle' + (on ? ' on' : '') + (readOnly ? ' disabled' : '');
        if (!readOnly) input.addEventListener('click', () => { const nv = !input.classList.contains('on'); input.classList.toggle('on', nv); patch[f.key] = nv ? 1 : 0; });
      } else if (f.type === 'dirs') {
        input = document.createElement('textarea'); input.className = 'ctrl-input'; input.value = cur || ''; input.disabled = readOnly;
        if (!readOnly) input.addEventListener('input', () => { patch[f.key] = input.value; });
        field.style.width = '100%';
      } else {
        input = document.createElement('input'); input.type = 'number'; input.className = 'ctrl-input'; input.step = 'any'; input.value = (cur ?? ''); input.disabled = readOnly;
        if (!readOnly) input.addEventListener('input', () => { patch[f.key] = input.value === '' ? 0 : +input.value; });
      }
      field.appendChild(input); row.appendChild(field); body.appendChild(row);
    });
    if (readOnly) return;
    main.querySelector('#limit-save').addEventListener('click', async () => {
      if (!Object.keys(patch).length) return;
      try {
        const r = await fetch('/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        await loadAgents();
        window.alert('Account limits saved.');
      } catch (e) { window.alert('Could not save limits: ' + e.message); }
    });
  }

  async function openNewAgent() {
    // The live catalogue, not window.SETTINGS.personas (the built-in seed) — otherwise
    // the org's custom personas never appear in the picker. Same rule as personaName/rollup.
    const personas = personaCatalogue();
    const roles = (agentsData && agentsData.roles) || [];
    if (!roles.length) { window.alert('Create a role first (Roles).'); return; }
    const modal = document.getElementById('ws-new-agent-modal');
    // Pull the org's enabled skills for the picker: global ones apply automatically,
    // specific-scope ones can be attached to this agent.
    let enabledSkills = [];
    try { const r = await fetch('/skills'); if (r.ok) enabledSkills = ((await r.json()).skills || []).filter(s => s.status === 'enabled'); } catch (e) { enabledSkills = []; }
    const globalSkills = enabledSkills.filter(s => s.all_agents);
    const specificSkills = enabledSkills.filter(s => !s.all_agents);
    let pSel = personas[0] ? personas[0].id : '';
    let rSel = roles[0] ? roles[0].id : '';
    const skSel = new Set();
    const skillsBlock = () => {
      const globalNote = globalSkills.length ? `<div class="data" style="font-size:10.5px; opacity:.7; margin-bottom:8px;">${globalSkills.length} skill${globalSkills.length > 1 ? 's' : ''} apply to all agents automatically.</div>` : '';
      const picker = specificSkills.length
        ? `<div class="pick-row" id="na-skills">${specificSkills.map(s => `<button class="pick ${skSel.has(s.id) ? 'on' : ''}" data-id="${esc(s.id)}">${esc(s.display_name || s.id)}</button>`).join('')}</div>`
        : `<div class="data" style="font-size:10.5px; opacity:.6;">No agent-specific skills yet. Mark a skill “Specific agents” in the Skills tab to assign it here.</div>`;
      return `<div class="label" style="margin:18px 0 9px;">Skills</div>${globalNote}${picker}`;
    };
    const draw = () => {
      modal.innerHTML = `<div class="modal">
        <div class="modal-head"><div class="serif-h" style="font-size:19px;">New agent</div><button class="tool-x" id="na-x"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>
        <p class="page-lede" style="margin-top:4px; font-size:14px;">Pair a persona with a role, and choose which skills it carries. The id is derived automatically.</p>
        <div style="margin-top:18px;"><div class="label" style="margin-bottom:9px;">Persona</div><div class="pick-row" id="na-personas">${personas.map(p => `<button class="pick ${p.id===pSel?'on':''}" data-id="${esc(p.id)}">${esc(p.name)}</button>`).join('')}</div>
        <div class="label" style="margin:18px 0 9px;">Role</div><div class="pick-row" id="na-roles">${roles.map(r => `<button class="pick ${r.id===rSel?'on':''}" data-id="${esc(r.id)}">${esc(r.id)}</button>`).join('')}</div>
        ${skillsBlock()}
        <div class="id-preview" style="margin-top:18px;"><span class="label">Derived id</span><span class="data" style="color:var(--signal); font-size:13px;">${esc(personaSlug(pSel))}.${esc(rSel)}</span></div></div>
        <div class="row" style="justify-content:flex-end; margin-top:22px; gap:10px;"><button class="btn" id="na-cancel">Cancel</button><button class="btn btn-primary" id="na-create">Create agent</button></div></div>`;
      modal.querySelectorAll('#na-personas .pick').forEach(b => b.addEventListener('click', () => { pSel = b.dataset.id; draw(); }));
      modal.querySelectorAll('#na-roles .pick').forEach(b => b.addEventListener('click', () => { rSel = b.dataset.id; draw(); }));
      modal.querySelectorAll('#na-skills .pick').forEach(b => b.addEventListener('click', () => { const id = b.dataset.id; if (skSel.has(id)) skSel.delete(id); else skSel.add(id); b.classList.toggle('on'); }));
      modal.querySelector('#na-x').addEventListener('click', close);
      modal.querySelector('#na-cancel').addEventListener('click', close);
      modal.querySelector('#na-create').addEventListener('click', create);
    };
    const close = () => { modal.classList.remove('open'); modal.innerHTML = ''; };
    const create = async () => {
      try {
        const r = await fetch('/agents', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ persona: pSel, mandate_id: rSel, skills: Array.from(skSel) }) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
        close(); agentSel = `${personaSlug(pSel)}.${rSel}`; agView = 'detail'; await loadAgents();
      } catch (e) { window.alert('Could not create agent: ' + e.message); }
    };
    draw(); modal.classList.add('open');
    modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  }

  // ══════════════════════════════════════════════════════════ API ═════════
  let apiView = 'docs';
  let skillsData = null;       // { enabled, is_admin, skills:[], flagged:[] }
  function ensureApi() { renderApi(); if (apiView === 'partner') loadPartnerKeys(); }
  function renderApi() {
    const host = document.getElementById('ws-api');
    host.innerHTML = `<div class="ws-grid" style="grid-template-columns:256px 1fr;">
      <div class="ws-rail">
        <div class="rail-head"><h2>API</h2><span class="n">integration</span></div>
        <div class="rail-sect">
          <button class="rail-item api-nav ${apiView==='docs'?'on':''}" data-view="docs"><span class="ri-name">Documentation</span><span class="ri-meta">guide &amp; endpoints</span></button>
          <button class="rail-item api-nav ${apiView==='partner'?'on':''}" data-view="partner"><span class="ri-name">Partner Keys</span><span class="ri-meta">customer-facing tokens</span></button>
        </div>
      </div>
      <div class="ws-main" id="api-main"></div></div>
      <div class="modal-veil" id="ws-api-modal"></div>`;
    host.querySelectorAll('.api-nav').forEach(n => n.addEventListener('click', () => { apiView = n.dataset.view; ensureApi(); }));
    const main = host.querySelector('#api-main');
    if (apiView === 'partner') renderPartnerKeys(main);
    else renderDocs(main);
  }
  // The Documentation payload is built server-side (brain/api/docs.py, served at
  // /api_docs): the hand-written guide rendered to HTML, each page carrying the
  // live endpoint cards for the routes it documents. Shape: { base_url,
  // pages:[{id,title,slug,html,endpoints:[{method,path,anchor,description,scope,
  // tag,body,curl,gateway}]}], anchors:{slug:pageId}, index:[...] }.
  let docsData = null;
  async function loadDocs() {
    const empty = { base_url: '', pages: [], anchors: {}, index: [] };
    try { const r = await fetch('/api_docs'); docsData = r.ok ? await r.json() : empty; }
    catch (e) { docsData = empty; }
  }
  // Pretty-print a JS object as syntax-highlighted JSON for the example blocks.
  const hlJson = (v, ind = 0) => {
    const pad = '  '.repeat(ind), pad1 = '  '.repeat(ind + 1);
    if (Array.isArray(v)) return v.length ? `[\n${v.map(x => pad1 + hlJson(x, ind + 1)).join(',\n')}\n${pad}]` : '[]';
    if (v && typeof v === 'object') {
      const ks = Object.keys(v);
      return ks.length ? `{\n${ks.map(k => `${pad1}<span class="k">"${esc(k)}"</span>: ${hlJson(v[k], ind + 1)}`).join(',\n')}\n${pad}}` : '{}';
    }
    if (typeof v === 'string') return `<span class="s">"${esc(v)}"</span>`;
    return `<span class="p">${esc(String(v))}</span>`;
  };
  // reference.py's compact method tokens are the CSS class names for the chips.
  const METHOD_CLASS = { GET: 'get', POST: 'post', PUT: 'put', DELETE: 'del', WS: 'ws' };
  const INDEX_PAGE = 'index';   // sentinel for the generated "All endpoints" page
  let docsPage = 0;

  // One endpoint card: the generated facts (method, path, description, scope)
  // plus the copy-ready snippet. `anchor` is the id the guide's own cross-links
  // and the index page jump to.
  function docCard(e) {
    const cls = METHOD_CLASS[e.method] || 'get';
    const chips = (e.tag ? `<span class="chip">${esc(e.tag)}</span>` : '')
      + (e.scope === 'owner' ? '<span class="chip role">owner</span>' : '')
      + (e.gateway ? '<span class="chip">gateway</span>' : '');
    return `<div class="ep-card" id="${esc(e.anchor)}">
      <div class="row" style="gap:12px;"><span class="method ${cls}">${esc(e.method)}</span><span class="data" style="font-size:14px; color:var(--ink);">${esc(e.path)}</span>${chips}</div>
      ${e.description ? `<p class="ep-desc">${esc(e.description)}</p>` : ''}
      ${e.curl ? `<div class="label ep-lab">${e.method === 'WS' ? 'Connect' : 'Try it'}<button class="ep-copy" data-copy="${esc(e.curl)}">Copy</button></div>
      <div class="code">${esc(e.curl)}</div>` : ''}
      ${e.body ? `<div class="label ep-lab">Request body</div><div class="code">${hlJson(e.body)}</div>` : ''}
    </div>`;
  }

  function renderDocs(main) {
    if (docsData === null) {
      main.innerHTML = '<div class="main-pad"><div class="empty"><h3>Loading documentation…</h3></div></div>';
      loadDocs().then(() => renderDocs(main));
      return;
    }
    const pages = docsData.pages || [];
    const index = docsData.index || [];
    if (!pages.length) {
      main.innerHTML = '<div class="main-pad"><div class="empty"><h3>Documentation unavailable</h3><p>The guide could not be rendered — check the brain logs.</p></div></div>';
      return;
    }
    const railHtml = pages.map((p, i) =>
      `<button class="rail-item doc-item ${docsPage === i ? 'on' : ''}" data-i="${i}"><span class="ri-name">${esc(p.title)}</span>${p.endpoints.length ? `<span class="ri-meta" style="margin-left:auto;">${p.endpoints.length}</span>` : ''}</button>`
    ).join('')
      + '<div class="rail-div"></div>'
      + `<button class="rail-item doc-item ${docsPage === INDEX_PAGE ? 'on' : ''}" data-i="${INDEX_PAGE}"><span class="ri-name">All endpoints</span><span class="ri-meta" style="margin-left:auto;">${index.length}</span></button>`;

    main.innerHTML = `<div style="display:grid; grid-template-columns:320px 1fr; grid-template-rows:minmax(0,1fr); height:100%;">
      <div class="ws-rail" style="border-right:1px solid var(--line-soft); overflow-y:auto;">
        <div class="rail-head"><h2>Documentation</h2><span class="n">v1 · ${index.length} endpoints</span></div>
        <div class="rail-sect" id="doc-list" style="padding-top:0;">${railHtml}</div>
      </div>
      <div class="ws-main" id="doc-scroll"><div class="main-pad" style="max-width:760px;" id="doc-detail"></div></div></div>`;

    const detail = main.querySelector('#doc-detail');
    const scroller = main.querySelector('#doc-scroll');

    const paint = () => {
      if (docsPage === INDEX_PAGE) {
        detail.innerHTML = `<h2 class="doc-h1">All endpoints</h2>
          <p class="page-lede">Generated from the live route table, so this list is always what the server actually serves. ${esc(docsData.base_url || '')} is your base URL.</p>
          <div class="param-table ep-index"><div class="pt-head"><span>Method</span><span>Path</span><span>Documented in</span></div>
          ${index.map(e => `<div class="pt-row"><span><span class="method ${METHOD_CLASS[e.method] || 'get'}">${esc(e.method)}</span></span><span class="data" style="font-size:11px;">${esc(e.path)}</span><span><a href="#${esc(e.anchor)}" class="doc-xref">${esc((pages[e.page] || {}).title || '')}</a>${e.scope === 'owner' ? ' <span class="chip role">owner</span>' : ''}</span></div>`).join('')}
          </div>`;
      } else {
        const p = pages[docsPage] || pages[0];
        // The page's own `## ` line was consumed as its title by the splitter, so
        // render it here — and carry the page slug as the id, so a cross-link to
        // the section (`#10-streaming-websocket`) has something to scroll to.
        // p.html is server-rendered by brain/api/markdown.py, which escapes all
        // source text and allowlists URL schemes (see its security contract) —
        // the ONE place this file injects HTML it did not escape itself. Every
        // other value below still goes through esc().
        detail.innerHTML = `<h1 class="doc-h1" id="${esc(p.slug)}">${esc(p.title)}</h1>`
          + `<div class="doc-body">${p.html}</div>`
          + (p.endpoints.length ? `<div class="label ep-sec-lab">Endpoints</div>${p.endpoints.map(docCard).join('')}` : '');
      }
      detail.querySelectorAll('.ep-copy').forEach(b => b.addEventListener('click', () => {
        // Hand the clipboard the UNESCAPED text — dataset already decoded it.
        navigator.clipboard?.writeText(b.dataset.copy);
        const was = b.textContent; b.textContent = 'Copied'; setTimeout(() => { b.textContent = was; }, 1200);
      }));
      main.querySelectorAll('.doc-item').forEach(x => x.classList.toggle('on', x.dataset.i === String(docsPage)));
    };

    const goTo = (page, slug) => {
      if (docsPage !== page) { docsPage = page; paint(); }
      const el = slug ? detail.querySelector('[id="' + slug.replace(/["\\]/g, '') + '"]') : null;
      // Position the scroller directly rather than via scrollIntoView: inside a
      // nested scroll container its smooth behaviour is unreliable, and a missed
      // jump strands the reader mid-page on a section they didn't ask for.
      // No slug, or a slug this page doesn't contain → top of the page.
      if (el) {
        scroller.scrollTop += el.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 8;
      } else {
        scroller.scrollTop = 0;
      }
    };

    // The guide cross-links between its own sections (`[§10](#10-streaming-websocket)`).
    // Now that it is paginated those targets usually live on another page, so a
    // plain hash jump would land nowhere. Resolve through the server's anchors
    // map; an unknown slug is left alone rather than swallowed.
    detail.addEventListener('click', (ev) => {
      const a = ev.target.closest && ev.target.closest('a[href^="#"]');
      if (!a) return;
      let slug = a.getAttribute('href').slice(1);
      try { slug = decodeURIComponent(slug); } catch (e) { /* keep raw */ }
      const target = docsData.anchors[slug];
      if (target === undefined) return;
      ev.preventDefault();
      goTo(target, slug);
    });

    main.querySelectorAll('.doc-item').forEach(b => b.addEventListener('click', () => {
      const v = b.dataset.i;
      goTo(v === INDEX_PAGE ? INDEX_PAGE : +v, null);
    }));
    paint();
  }

  // ─────────────────────────────────────────────────────────── Skills ──
  const SKILL_STATUS_COLOR = { enabled: 'var(--ok)', flagged: 'var(--warn)', rejected: 'var(--danger)', pending: 'var(--ink-4)' };
  const skStatColor = (s) => SKILL_STATUS_COLOR[s] || 'var(--ink-4)';
  async function loadSkills() {
    try { const r = await fetch('/skills'); skillsData = r.ok ? await r.json() : { enabled: false, is_admin: false, skills: [], flagged: [] }; }
    catch (e) { skillsData = { enabled: false, is_admin: false, skills: [], flagged: [] }; }
    if (workspace === 'agents' && agView === 'skills') renderSkills(document.getElementById('ag-main'));
  }
  function renderFlaggedCard(s) {
    const notes = s.screen_notes || {};
    const judge = notes.judge || {};
    const stat = notes.static || {};
    const reasons = (judge.reasons || []).map(r => esc(r)).join(', ');
    const findings = (stat.findings || []).map(r => esc(r)).join(', ');
    const why = [judge.verdict ? `Judge: <b>${esc(judge.verdict)}</b>` : '', reasons ? `reasons: ${reasons}` : '', findings ? `static: ${findings}` : ''].filter(Boolean).join(' · ');
    return `<div class="es-card" style="padding:16px; margin-bottom:12px;">
      <div class="between"><div><span class="serif-h" style="font-size:15px;">${esc(s.display_name || s.id)}</span>
        <span class="data" style="font-size:10px; display:block; margin-top:2px;">${esc(s.id)}${s.submitted_by ? (' · ' + esc(s.submitted_by)) : ''}</span></div>
        <span class="ar-status"><span class="dot-status" style="background:var(--warn)"></span>flagged</span></div>
      ${s.description ? `<p class="page-lede" style="margin:8px 0 0;">${esc(s.description)}</p>` : ''}
      ${why ? `<div class="note" style="margin-top:10px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4M12 17h.01"/></svg><p>${why}</p></div>` : ''}
      <div class="code" style="margin-top:12px; max-height:220px; overflow:auto; white-space:pre-wrap;">${esc(s.body || '')}</div>
      <div class="row" style="gap:8px; margin-top:12px;">
        <button class="btn btn-primary sk-approve" data-id="${esc(s.id)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg> Approve</button>
        <button class="btn sk-reject" data-id="${esc(s.id)}">Reject</button>
      </div></div>`;
  }
  function renderSkills(main) {
    if (!main) return;
    const d = skillsData;
    const canAdd = d && d.enabled && d.is_admin;
    const addBtn = canAdd ? `<button class="btn btn-primary" id="sk-new" style="margin-top:8px; flex:0 0 auto;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg> Add skill</button>` : '';
    const head = `<div class="between"><div>
        <div class="page-eyebrow">Agents · skills</div><div class="page-title">Skills</div>
        <p class="page-lede" style="max-width:600px;">Reusable abilities the agent draws on, selected per turn or pinned per session. They come from three places now. Add your own here. Your apps can register them over the engine API. And the brain proposes its own as it learns what works for you. Everything is screened before it goes live. Anything the screener can't auto-clear waits in the review queue below.</p>
      </div>${addBtn}</div>`;
    if (!d) { main.innerHTML = `<div class="main-pad" style="max-width:820px;">${head}<p class="page-lede" style="opacity:.6;">Loading…</p></div>`; return; }
    if (!d.enabled) {
      main.innerHTML = `<div class="main-pad" style="max-width:820px;">${head}<div class="note" style="margin-top:18px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 16v-4M12 8h.01"/><circle cx="12" cy="12" r="10"/></svg><p>Skills require the Supabase storage backend, which isn't enabled in this deployment.</p></div></div>`;
      return;
    }
    const flagged = d.flagged || [];
    const skills = d.skills || [];
    let queue = '';
    if (d.is_admin && flagged.length) {
      queue = `<div class="label" style="margin:26px 0 10px;">Review queue · ${flagged.length}</div>${flagged.map(renderFlaggedCard).join('')}`;
    }
    const GRID = '1.5fr .7fr 1fr .9fr 28px';
    const appliesLabel = s => s.all_agents ? 'All agents' : `${(s.agents || []).length} agent${(s.agents || []).length === 1 ? '' : 's'}`;
    const nameCell = s => d.is_admin
      ? `<button class="sk-open" data-id="${esc(s.id)}" style="background:none; border:none; padding:0; cursor:pointer; text-align:left; display:block;"><span class="serif-h" style="font-size:14px;">${esc(s.display_name || s.id)}</span><span class="data" style="font-size:9px; display:block; margin-top:2px;">${esc(s.id)}</span></button>`
      : `<span class="serif-h" style="font-size:14px;">${esc(s.display_name || s.id)}</span><span class="data" style="font-size:9px; display:block; margin-top:2px;">${esc(s.id)}</span>`;
    const skRow = s => `<div class="ag-row" style="grid-template-columns:${GRID}; cursor:default;">
      <span>${nameCell(s)}</span>
      <span class="ar-status"><span class="dot-status" style="background:${skStatColor(s.status)}"></span>${esc(s.status || 'pending')}</span>
      <span>${d.is_admin ? `<button class="link sk-scope" data-id="${esc(s.id)}">${esc(appliesLabel(s))}</button>` : `<span class="data" style="font-size:11px;">${esc(appliesLabel(s))}</span>`}</span>
      <span class="data" style="font-size:11px;">${esc(s.submitted_by || '—')}</span>
      <span class="ar-chev">${d.is_admin ? `<button class="link sk-del" data-id="${esc(s.id)}" title="Remove skill">✕</button>` : ''}</span></div>`;
    // Brain-authored skills carry the `self-` id prefix (node_authoring) and submitted_by "brain".
    const isAuthored = s => String(s.id || '').startsWith('self-') || s.submitted_by === 'brain';
    const authored = skills.filter(isAuthored);
    const library = skills.filter(s => !isAuthored(s));
    const tableHead = `<div class="ag-table-head" style="grid-template-columns:${GRID};"><span>Skill</span><span>Status</span><span>Applies to</span><span>Submitted by</span><span></span></div>`;
    const section = (label, list, note) => list.length
      ? `<div class="label" style="margin:28px 0 10px;">${label} · ${list.length}</div>${note || ''}<div class="ag-table" style="grid-template-columns:none;">${tableHead}${list.map(skRow).join('')}</div>`
      : '';
    const authoredNote = authored.length
      ? `<p class="page-lede" style="max-width:600px; margin:0 0 12px; font-size:12px; opacity:.75;">The brain proposed these itself as it learned what works for you. Each was screened like any other skill before going live.</p>`
      : '';
    main.innerHTML = `<div class="main-pad" style="max-width:820px;">${head}${queue}
      ${section('Self-authored', authored, authoredNote)}
      ${section('Library', library, '')}
      ${skills.length === 0 ? '<div style="padding:22px; text-align:center; margin-top:18px;" class="data">No skills registered yet.</div>' : ''}
      ${!d.is_admin ? '<p class="data" style="margin-top:14px; font-size:11px; opacity:.6;">Reviewing and approving skills is an org-admin action.</p>' : ''}</div>`;
    main.querySelectorAll('.sk-approve').forEach(b => b.addEventListener('click', () => skillApprove(b.dataset.id)));
    main.querySelectorAll('.sk-reject').forEach(b => b.addEventListener('click', () => skillReject(b.dataset.id)));
    main.querySelectorAll('.sk-del').forEach(b => b.addEventListener('click', () => skillDelete(b.dataset.id)));
    main.querySelectorAll('.sk-scope').forEach(b => b.addEventListener('click', () => { const sk = skills.find(x => x.id === b.dataset.id); if (sk) openSkillScope(sk); }));
    main.querySelectorAll('.sk-open').forEach(b => b.addEventListener('click', () => openSkillEditor(b.dataset.id)));
    const newBtn = main.querySelector('#sk-new'); if (newBtn) newBtn.addEventListener('click', openNewSkill);
  }
  async function openSkillEditor(id) {
    const modal = document.getElementById('ws-new-agent-modal');
    if (!modal) return;
    let sk = null;
    try { const r = await fetch('/skills/' + encodeURIComponent(id)); if (r.ok) sk = (await r.json()).skill; } catch (e) { sk = null; }
    if (!sk) { window.alert('Could not load skill “' + id + '”.'); return; }
    const applies = sk.all_agents ? 'all agents' : `${(sk.agents || []).length} agent${(sk.agents || []).length === 1 ? '' : 's'}`;
    modal.innerHTML = `<div class="modal" style="max-width:640px; max-height:90vh; overflow:auto;">
      <div class="modal-head"><div class="serif-h" style="font-size:19px;">Edit skill</div><button class="tool-x" id="se-x"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>
      <div class="data" style="font-size:10px; margin-top:2px; opacity:.75;">${esc(sk.id)} · <span style="color:${skStatColor(sk.status)};">${esc(sk.status || 'pending')}</span> · ${esc(applies)}</div>
      <div style="margin-top:16px;">
        <div class="label" style="margin-bottom:6px;">Name</div>
        <div class="input-line"><input id="se-name" type="text" value="${esc(sk.display_name || '')}" autocomplete="off" /></div>
        <div class="label" style="margin:16px 0 6px;">Description <span style="opacity:.5; font-size:10px; text-transform:none; letter-spacing:0;">· used to match it to a turn</span></div>
        <div class="input-line"><input id="se-desc" type="text" value="${esc(sk.description || '')}" autocomplete="off" /></div>
        <div class="label" style="margin:16px 0 6px;">Skill body</div>
        <textarea class="self-area" id="se-body" spellcheck="false" style="min-height:52vh; width:100%; box-sizing:border-box;">${esc(sk.body || sk.approved_body || '')}</textarea>
        <div class="label" style="margin:16px 0 6px;">Keywords <span style="opacity:.5; font-size:10px; text-transform:none; letter-spacing:0;">· comma-separated</span></div>
        <div class="input-line"><input id="se-kw" type="text" value="${esc((sk.keywords || []).join(', '))}" autocomplete="off" /></div>
        <div id="se-err" style="color:#c84; font-family:var(--mono); font-size:10px; margin-top:8px; min-height:14px;"></div>
      </div>
      <div class="row" style="justify-content:space-between; align-items:center; margin-top:16px; gap:10px;">
        <span class="data" id="se-count" style="font-size:10px; opacity:.6;"></span>
        <div class="row" style="gap:10px;"><button class="btn" id="se-cancel">Cancel</button><button class="btn btn-primary" id="se-save">Save</button></div>
      </div></div>`;
    const nameIn = modal.querySelector('#se-name');
    const descIn = modal.querySelector('#se-desc');
    const bodyIn = modal.querySelector('#se-body');
    const kwIn = modal.querySelector('#se-kw');
    const errDiv = modal.querySelector('#se-err');
    const saveBtn = modal.querySelector('#se-save');
    const countEl = modal.querySelector('#se-count');
    const updateCount = () => { countEl.textContent = `${bodyIn.value.length.toLocaleString()} chars`; };
    bodyIn.addEventListener('input', updateCount);
    updateCount();
    const close = () => { modal.classList.remove('open'); modal.innerHTML = ''; };
    const save = async () => {
      const body = bodyIn.value;
      if (!body.trim()) { errDiv.textContent = 'Body cannot be empty.'; return; }
      saveBtn.disabled = true; saveBtn.textContent = 'Saving…';
      try {
        const keywords = kwIn.value.split(',').map(s => s.trim()).filter(Boolean);
        const r = await fetch('/skills', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: sk.id, display_name: nameIn.value.trim() || null, description: descIn.value.trim(), body, keywords, tier: sk.tier }) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
        close(); await loadSkills();
      } catch (e) { errDiv.textContent = e.message; saveBtn.disabled = false; saveBtn.textContent = 'Save'; }
    };
    modal.querySelector('#se-x').addEventListener('click', close);
    modal.querySelector('#se-cancel').addEventListener('click', close);
    saveBtn.addEventListener('click', save);
    modal.classList.add('open');
    modal.addEventListener('click', e => { if (e.target === modal) close(); });
    nameIn.focus();
  }
  async function openSkillScope(skill) {
    const modal = document.getElementById('ws-new-agent-modal');
    if (!modal) return;
    let agents = [];
    try { const r = await fetch('/agents'); if (r.ok) agents = (await r.json()).agents || []; } catch (e) { agents = []; }
    const aid = a => `${a.persona}.${a.mandate_id}`;
    let allAgents = !!skill.all_agents;
    const sel = new Set(skill.agents || []);
    const draw = () => {
      modal.innerHTML = `<div class="modal" style="max-width:520px;">
        <div class="modal-head"><div class="serif-h" style="font-size:19px;">Applies to</div><button class="tool-x" id="ss-x"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>
        <p class="page-lede" style="margin-top:4px; font-size:14px;">Which agents can use “${esc(skill.display_name || skill.id)}”.</p>
        <div class="pick-row" style="margin-top:16px;">
          <button class="pick ${allAgents ? 'on' : ''}" id="ss-all">All agents</button>
          <button class="pick ${!allAgents ? 'on' : ''}" id="ss-specific">Specific agents</button>
        </div>
        ${!allAgents ? `<div style="margin-top:14px;">${agents.length ? `<div class="pick-row" id="ss-agents">${agents.map(a => `<button class="pick ${sel.has(aid(a)) ? 'on' : ''}" data-id="${esc(aid(a))}">${esc(a.name || aid(a))}</button>`).join('')}</div>` : '<div class="data" style="font-size:10.5px; opacity:.6;">No agents yet — create one first (Agents).</div>'}</div>` : ''}
        <div class="row" style="justify-content:flex-end; margin-top:20px; gap:10px;"><button class="btn" id="ss-cancel">Cancel</button><button class="btn btn-primary" id="ss-save">Save</button></div></div>`;
      modal.querySelector('#ss-all').addEventListener('click', () => { allAgents = true; draw(); });
      modal.querySelector('#ss-specific').addEventListener('click', () => { allAgents = false; draw(); });
      modal.querySelectorAll('#ss-agents .pick').forEach(b => b.addEventListener('click', () => { const id = b.dataset.id; if (sel.has(id)) sel.delete(id); else sel.add(id); b.classList.toggle('on'); }));
      modal.querySelector('#ss-x').addEventListener('click', close);
      modal.querySelector('#ss-cancel').addEventListener('click', close);
      modal.querySelector('#ss-save').addEventListener('click', save);
    };
    const close = () => { modal.classList.remove('open'); modal.innerHTML = ''; };
    const save = async () => {
      try {
        const r = await fetch('/skills/' + encodeURIComponent(skill.id) + '/agents', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ all_agents: allAgents, agents: allAgents ? [] : Array.from(sel) }) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
        close(); await loadSkills();
      } catch (e) { window.alert('Could not save: ' + e.message); }
    };
    draw(); modal.classList.add('open');
    modal.addEventListener('click', e => { if (e.target === modal) close(); });
  }
  function openNewSkill() {
    const modal = document.getElementById('ws-new-agent-modal');
    if (!modal) return;
    const VALID_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;
    const slugify = s => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
    modal.innerHTML = `<div class="modal" style="max-width:640px; max-height:90vh; overflow:auto;">
      <div class="modal-head"><div class="serif-h" style="font-size:19px;">New skill</div><button class="tool-x" id="ns-x"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>
      <p class="page-lede" style="margin-top:4px; font-size:14px;">A reusable instruction your agent applies when it's relevant. Authored here it goes live immediately — skills submitted over the engine API are screened first.</p>
      <div style="margin-top:18px;">
        <div class="label" style="margin-bottom:6px;">Name</div>
        <div class="input-line"><input id="ns-name" type="text" placeholder="e.g. Portfolio Reader" autocomplete="off" /></div>
        <div class="label" style="margin:16px 0 6px;">System id <span style="opacity:.5; font-size:10px; text-transform:none; letter-spacing:0;">· editable</span></div>
        <div class="input-line"><input id="ns-id" type="text" placeholder="portfolio_reader" autocomplete="off" /></div>
        <div class="label" style="margin:16px 0 6px;">Description <span style="opacity:.5; font-size:10px; text-transform:none; letter-spacing:0;">· what it's for — used to match it to a turn</span></div>
        <div class="input-line"><input id="ns-desc" type="text" placeholder="How to read the portfolio tables and summarize holdings" autocomplete="off" /></div>
        <div class="label" style="margin:16px 0 6px;">Skill body</div>
        <textarea class="self-area" id="ns-body" spellcheck="false" style="min-height:44vh; width:100%; box-sizing:border-box;" placeholder="The instructions the agent should follow when this skill applies…"></textarea>
        <div class="label" style="margin:16px 0 6px;">Keywords <span style="opacity:.5; font-size:10px; text-transform:none; letter-spacing:0;">· optional, comma-separated</span></div>
        <div class="input-line"><input id="ns-kw" type="text" placeholder="portfolio, holdings, positions" autocomplete="off" /></div>
        <div id="ns-err" style="color:#c84; font-family:var(--mono); font-size:10px; margin-top:8px; min-height:14px;"></div>
      </div>
      <div class="row" style="justify-content:flex-end; margin-top:18px; gap:10px;">
        <button class="btn" id="ns-cancel">Cancel</button>
        <button class="btn btn-primary" id="ns-create" disabled>Create skill</button>
      </div></div>`;
    const nameIn = modal.querySelector('#ns-name');
    const idIn = modal.querySelector('#ns-id');
    const descIn = modal.querySelector('#ns-desc');
    const bodyIn = modal.querySelector('#ns-body');
    const kwIn = modal.querySelector('#ns-kw');
    const errDiv = modal.querySelector('#ns-err');
    const createBtn = modal.querySelector('#ns-create');
    let idEdited = false;
    const validate = () => {
      const id = idIn.value.trim();
      const hasBody = bodyIn.value.trim().length > 0;
      if (id && !VALID_ID.test(id)) { errDiv.textContent = 'Use lowercase letters, digits, _ or - (must start with a letter or digit)'; createBtn.disabled = true; return; }
      errDiv.textContent = '';
      createBtn.disabled = !(id && VALID_ID.test(id) && hasBody);
    };
    nameIn.addEventListener('input', () => { if (!idEdited) idIn.value = slugify(nameIn.value); validate(); });
    idIn.addEventListener('input', () => { idEdited = true; validate(); });
    bodyIn.addEventListener('input', validate);
    const close = () => { modal.classList.remove('open'); modal.innerHTML = ''; };
    const create = async () => {
      const id = idIn.value.trim();
      if (!id || !VALID_ID.test(id) || !bodyIn.value.trim()) return;
      createBtn.disabled = true; createBtn.textContent = 'Creating…';
      try {
        const keywords = kwIn.value.split(',').map(s => s.trim()).filter(Boolean);
        const r = await fetch('/skills', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, display_name: nameIn.value.trim() || null, description: descIn.value.trim(), body: bodyIn.value, keywords }) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
        close(); await loadSkills();
      } catch (e) { errDiv.textContent = e.message; createBtn.disabled = false; createBtn.textContent = 'Create skill'; }
    };
    modal.querySelector('#ns-x').addEventListener('click', close);
    modal.querySelector('#ns-cancel').addEventListener('click', close);
    createBtn.addEventListener('click', create);
    nameIn.addEventListener('keydown', e => { if (e.key === 'Enter') idIn.focus(); });
    modal.classList.add('open');
    modal.addEventListener('click', e => { if (e.target === modal) close(); });
    nameIn.focus();
  }
  async function skillApprove(id) {
    try {
      const r = await fetch('/skills/' + encodeURIComponent(id) + '/approve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
      await loadSkills();
    } catch (e) { window.alert('Could not approve skill: ' + e.message); }
  }
  async function skillReject(id) {
    const reason = (window.prompt('Reason for rejecting "' + id + '" (optional):', '') || '').trim();
    try {
      const r = await fetch('/skills/' + encodeURIComponent(id) + '/reject', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason }) });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
      await loadSkills();
    } catch (e) { window.alert('Could not reject skill: ' + e.message); }
  }
  async function skillDelete(id) {
    if (!window.confirm('Remove skill "' + id + '"? It will stop being injected into turns.')) return;
    try {
      const r = await fetch('/skills/' + encodeURIComponent(id), { method: 'DELETE' });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
      await loadSkills();
    } catch (e) { window.alert('Could not remove skill: ' + e.message); }
  }

  async function loadPartnerKeys() {
    try { const r = await fetch('/partner_keys'); partnerKeys = r.ok ? (await r.json()).keys || [] : []; }
    catch (e) { partnerKeys = []; }
    if (apiView === 'partner') renderPartnerKeys(document.getElementById('api-main'));
  }
  function renderPartnerKeys(main) {
    if (!main) return;
    const keys = partnerKeys || [];
    main.innerHTML = `<div class="main-pad" style="max-width:760px;">
      <div class="between"><div><div class="page-eyebrow">API · partner keys</div><div class="page-title">Partner Keys</div>
      <p class="page-lede">Customer-facing tokens that authorize requests to the engine API. A key's secret is shown <em>once</em> at mint time and never again.</p></div>
      <button class="btn btn-primary" id="pk-mint" style="margin-top:8px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg> Mint partner key</button></div>
      <div class="mint-reveal" id="pk-reveal"></div>
      <div class="ag-table" style="margin-top:24px; grid-template-columns:none;">
        <div class="ag-table-head" style="grid-template-columns:1.4fr 1fr 0.8fr 28px;"><span>Partner</span><span>Key id</span><span>Status</span><span></span></div>
        ${keys.map(k => `<div class="ag-row" style="grid-template-columns:1.4fr 1fr 0.8fr 28px; cursor:default;"><span><span class="serif-h" style="font-size:14.5px;">${esc(k.partner_id)}</span><span class="data" style="font-size:9px; display:block; margin-top:2px;">${esc(k.label||'')}</span></span><span class="data" style="font-size:11px;">${esc(k.id)}</span><span class="ar-status"><span class="dot-status" style="background:${k.active?'var(--ok)':'var(--ink-4)'}"></span>${k.active?'active':'revoked'}</span><span class="ar-chev">${k.active?`<button class="link pk-revoke" data-id="${esc(k.id)}">revoke</button>`:''}</span></div>`).join('') || '<div style="padding:22px; text-align:center;" class="data">No partner keys yet.</div>'}
      </div></div>`;
    main.querySelector('#pk-mint').addEventListener('click', mintKey);
    main.querySelectorAll('.pk-revoke').forEach(b => b.addEventListener('click', () => revokeKey(b.dataset.id)));
  }
  async function mintKey() {
    const partner_id = (window.prompt('Partner id (the customer this key belongs to):', '') || '').trim();
    if (!partner_id) return;
    const label = (window.prompt('Label (optional):', '') || '').trim();
    try {
      const r = await fetch('/partner_keys', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ partner_id, label }) });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
      const j = await r.json();
      await loadPartnerKeys();
      const reveal = document.getElementById('pk-reveal');
      if (reveal && j.token) {
        reveal.classList.add('on');
        reveal.innerHTML = `<div class="row" style="gap:9px; margin-bottom:10px;"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--signal-deep)" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg><span class="serif-h" style="font-size:16px;">${esc(j.partner_id)} minted</span></div>
          <p style="font-size:13px; color:var(--ink-2); line-height:1.5;">Copy it now — <b>this is the only time the full token is shown.</b></p>
          <div class="token-box"><span class="data" style="font-size:13px; color:var(--ink); word-break:break-all;">${esc(j.token)}</span><button class="btn btn-sm" id="pk-copy">Copy</button></div>`;
        reveal.querySelector('#pk-copy').addEventListener('click', () => navigator.clipboard && navigator.clipboard.writeText(j.token));
      }
    } catch (e) { window.alert('Could not mint key: ' + e.message); }
  }
  async function revokeKey(id) {
    if (!window.confirm('Revoke this key? Requests using it will be rejected.')) return;
    try { await fetch('/partner_keys/' + encodeURIComponent(id), { method: 'DELETE' }); await loadPartnerKeys(); }
    catch (e) { window.alert('Could not revoke: ' + e.message); }
  }

  // ── boot ─────────────────────────────────────────────────────────────────
  function boot() {
    if (!document.getElementById('ws-switch')) return;
    window.setWorkspace = setWorkspace;       // let other code drive it
    window.getWorkspace = () => workspace;    // so closeSettings can restore context
    // Deep-link into the Jobs supervision view (toasts / approval bubbles land here).
    window.openAgentJobs = (jobId) => {
      setWorkspace('agents');
      if (jobId) { jobSel = jobId; jobDetail = null; agView = 'jobdetail'; }
      else { agView = 'jobs'; jobsList = null; }
      if (agentsData) paintAgents(); // no data yet → ensureAgents (from setWorkspace) paints
    };
    // Live-refresh an open jobs view when a task_outcome event lands.
    window.refreshAgentJobs = () => {
      if (workspace !== 'agents' || (agView !== 'jobs' && agView !== 'jobdetail')) return;
      jobsList = null; jobDetail = null; paintAgents();
    };
    loadRailOpen('agents'); loadRailOpen('personas');
    wireSwitcher();
    wireOrgKeys();
    loadGating();
  }
  // ⌘K / Ctrl-K focuses the rail search of whichever organised workspace is showing;
  // Esc backs out of a detail view to its roster. Both are no-ops elsewhere, and both
  // stand down while the user is typing into some other field.
  function wireOrgKeys() {
    document.addEventListener('keydown', (e) => {
      const onOrgWs = workspace === 'agents' || workspace === 'personas';
      if (!onOrgWs) return;
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key && e.key.toLowerCase() === 'k') {
        const input = document.querySelector('.workspace.on .ws-search input');
        if (!input) return;
        e.preventDefault();
        input.focus(); input.select();
        return;
      }
      if (e.key !== 'Escape') return;
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      if (workspace === 'agents' && agView === 'detail') { agView = 'agents'; agentSel = null; paintAgents(); }
      else if (workspace === 'personas' && perView === 'detail') {
        if (!confirmLeavePersonaDetail()) return;
        perView = 'overview'; personaSel = null; paintPersonas();
      }
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
