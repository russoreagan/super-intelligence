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
    agents: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    personas: '<path d="M12 3c3.6 0 6.5 2.4 6.5 6 0 5-3 9-6.5 9s-6.5-4-6.5-9c0-3.6 2.9-6 6.5-6z"/><circle cx="9.5" cy="10.5" r="1"/><circle cx="14.5" cy="10.5" r="1"/>',
    api: '<path d="m7 8-4 4 4 4M17 8l4 4-4 4M14 4l-4 16"/>',
  };
  const WS_NAMES = { labs: 'MRI', agents: 'Agents', personas: 'Personas', api: 'API' };
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
  // Re-fill the per-card cost / token / call cells in place (on the 30s tick).
  function repaintUsageCells() {
    const ags = (agentsData && agentsData.agents) || [];
    document.querySelectorAll('#ws-agents .dash-card').forEach((card) => {
      const id = card.getAttribute('data-agent');
      const a = ags.find((x) => x.agent_id === id);
      const live = isLive(a);
      const u = (agentUsage && agentUsage[id]) || null;
      const c = card.querySelector('[data-cost-for]');
      const t = card.querySelector('[data-tok-for]');
      const k = card.querySelector('[data-calls-for]');
      if (c) { c.textContent = u ? '$' + agentCostUsd(u).toFixed(2) : (live ? '$0.00' : '—'); c.title = costTitle(u); }
      if (t) { t.textContent = u ? fmtTokens((u.in_tok || 0) + (u.out_tok || 0)) : (live ? '0' : '—'); t.title = usageTitle(u); }
      if (k) { k.textContent = u ? String(u.calls) : (live ? '0' : '—'); }
    });
    const tot = document.getElementById('range-total');
    if (tot) tot.textContent = '$' + dashboardShown().reduce((s, a) => s + agentCostUsd(agentUsage && agentUsage[a.agent_id]), 0).toFixed(2);
  }
  // The unified Agents view lists EVERY agent — its status dot says whether each is
  // active / idle / paused. Ordered by last active (most recent first), so whatever ran
  // most recently floats to the top; agents that have never run sink to the bottom
  // (ordered by name there for a stable layout). Cost breaks a last-active tie.
  const STATUS_RANK = { active: 0, idle: 1, paused: 2 };
  function agentLastActive(a) {
    const act = agentActivity && agentActivity[a.agent_id];
    return act && act.lastTs ? act.lastTs : 0;
  }
  function dashboardShown() {
    const ags = (agentsData && agentsData.agents) || [];
    return ags.slice().sort((x, y) => {
      const lx = agentLastActive(x), ly = agentLastActive(y);
      if (lx !== ly) return ly - lx;  // most recently active first; never-run (0) last
      if (!lx) return (x.name || x.agent_id).localeCompare(y.name || y.agent_id);
      return agentCostUsd(agentUsage && agentUsage[y.agent_id]) - agentCostUsd(agentUsage && agentUsage[x.agent_id]);
    });
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
    const labs = ws === 'labs';
    if (main) main.style.display = labs ? '' : 'none';
    if (ticker) ticker.style.display = labs ? '' : 'none';
    agents.classList.toggle('on', ws === 'agents');
    if (personas) personas.classList.toggle('on', ws === 'personas');
    api.classList.toggle('on', ws === 'api');
    if (ws === 'agents') ensureAgents();
    if (ws === 'personas') ensurePersonas();
    if (ws === 'api') ensureApi();
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
    const show = { labs: true, personas: true, agents: orgAdmin && mandatesEnabled, api: orgAdmin };
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
  let connectorsDetails = null; // [{name, url, display_name}]
  let connectorsEnvManaged = false; // true → registry pinned via BRAIN_CMA_MCP_SERVERS
  let connectorsCloud = null;   // { available, model, actions_enabled } — the Claude cloud connector
  function paintAgents() {
    const host = document.getElementById('ws-agents');
    const ags = (agentsData && agentsData.agents) || [];
    const roles = (agentsData && agentsData.roles) || [];
    const activeCount = ags.filter(a => agentStatus(a).state === 'active').length;
    host.innerHTML = `
      <div class="ws-grid" style="grid-template-columns:268px 1fr;">
        <div class="ws-rail">
          <div class="rail-head"><h2>Agents</h2><span class="n">admin</span></div>

          <div class="rail-sect">
            <button class="rail-item ag-nav ${agView==='agents'?'on':''}" data-view="agents"><span class="ri-name"><span class="dot-status ${activeCount?'live':''}" style="background:${activeCount?'var(--ok)':'var(--ink-4)'}"></span>Agents</span><span class="ri-meta">${ags.length} total · ${activeCount} active</span></button>
          </div>

          <div class="rail-div"></div>

          <div class="rail-sect">
            <button class="rail-item ag-nav ${agView==='roles'?'on':''}" data-view="roles"><span class="ri-name">Roles</span><span class="ri-meta">${roles.length} reusable spec${roles.length===1?'':'s'}</span></button>
            <button class="rail-item ag-nav ${agView==='limits'?'on':''}" data-view="limits"><span class="ri-name">Account limits</span><span class="ri-meta">org ceilings</span></button>
            <button class="rail-item ag-nav ${agView==='connectors'?'on':''}" data-view="connectors"><span class="ri-name">Connectors</span><span class="ri-meta">MCP servers · register</span></button>
          </div>

          <div class="rail-div"></div>

          <div class="rail-sect-lab" style="padding-left:22px; display:flex; justify-content:space-between; padding-right:18px;"><span>Agents</span><span class="n">${ags.length}</span></div>
          <div class="rail-sect">${ags.map(a => railAgent(a)).join('') || '<div class="ri-meta" style="padding:6px 14px; opacity:.6;">No agents yet</div>'}</div>
          <button class="rail-add" id="ws-new-agent"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg> New agent</button>
        </div>
        <div class="ws-main" id="ag-main"></div>
      </div>
      <div class="modal-veil" id="ws-new-agent-modal"></div>`;
    host.querySelectorAll('.ag-nav').forEach(n => n.addEventListener('click', () => { agView = n.dataset.view; agentSel = null; paintAgents(); }));
    host.querySelectorAll('.rail-agent').forEach(n => n.addEventListener('click', () => { agentSel = n.dataset.agent; agView = 'detail'; paintAgents(); }));
    host.querySelector('#ws-new-agent').addEventListener('click', openNewAgent);
    const main = host.querySelector('#ag-main');
    if (agView === 'detail' && agentSel) renderAgentDetail(main);
    else if (agView === 'roles') renderRoles(main);
    else if (agView === 'limits') renderAccountLimits(main);
    else if (agView === 'connectors') renderConnectors(main);
    else renderAgentsView(main);
  }
  function railAgent(a) {
    const st = agentStatus(a);
    const act = agentActivity && agentActivity[a.agent_id];
    // "persona · <last-activity>" when it has run, else the status word.
    const detail = st.state === 'paused' ? 'paused'
      : (act && act.lastTs ? agoShort(Date.now() - act.lastTs) : st.state);
    const meta = `${personaName(a.persona)} · ${detail}`;
    return `<button class="rail-item rail-agent" data-agent="${esc(a.agent_id)}"><span class="ri-name"><span class="${st.cls}" style="background:${st.color}"></span>${esc(a.name || a.agent_id)}</span><span class="ri-meta">${esc(meta)}</span></button>`;
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
  // A card in the all-orgs fleet view: built from a usage row (no agentsData), with
  // an org chip and identity derived from agent_id. Not clickable (other orgs' Labs).
  function dashCardAll(row) {
    const aid = row.agent_id || '';
    const dot = aid.indexOf('.');
    const personaPart = dot >= 0 ? aid.slice(0, dot) : aid;
    const mandate = dot >= 0 ? aid.slice(dot + 1) : '';
    const cost = agentCostUsd(row);
    const lt = row.last_ts ? Date.parse(row.last_ts) : 0;
    const lastActive = lt ? agoShort(Date.now() - lt) + ' ago' : '—';
    const orgLabel = row.org_name || (row.org_id || '').slice(0, 8) || 'org';
    return `<div class="dash-card" style="cursor:default;">
      <div class="dc-head">
        <div class="dc-identity">
          <span class="chip" title="${esc(row.org_id || '')}">${esc(orgLabel)}</span>
          <span class="dc-name" style="font-size:15px;">${esc(personaName(personaPart))}</span>
          <span class="chip role"><span class="dot"></span>${esc(mandate)}</span>
        </div>
        <span class="data" style="font-size:9px; color:var(--ink-4);">${esc(aid)}</span>
      </div>
      <div class="dc-metrics">
        <div class="dc-metric dm-cost"><div class="dm-val" title="${esc(costTitle(row))}">$${cost.toFixed(2)}</div><div class="dm-lab">Est. cost</div></div>
        <div class="dc-metric"><div class="dm-val" title="${esc(usageTitle(row))}">${esc(fmtTokens((row.in_tok || 0) + (row.out_tok || 0)))}</div><div class="dm-lab">Tokens</div></div>
        <div class="dc-metric"><div class="dm-val">${esc(String(row.calls))}</div><div class="dm-lab">Model calls</div></div>
        <div class="dc-metric"><div class="dm-val">${esc(lastActive)}</div><div class="dm-lab">Last active</div></div>
      </div>
    </div>`;
  }

  // ── Agents — every agent (status dot = live/idle/paused) + a date-range cost monitor ──
  function renderAgentsView(main) {
    const ags = (agentsData && agentsData.agents) || [];
    const counts = { active: 0, idle: 0, paused: 0 };
    ags.forEach(a => counts[agentStatus(a).state]++);
    const allMode = usageScope === 'all';
    const presets = allMode ? RANGE_PRESETS.filter(p => p.key !== 'session') : RANGE_PRESETS;
    const isSession = usageRange.key === 'session';
    const rangeLabel = (RANGE_PRESETS.find(p => p.key === usageRange.key) || {}).label || 'Range';
    // org scope → a card per agent in this org; all scope → rows from every org.
    const shown = allMode ? [] : dashboardShown();
    const allRows = allMode ? (agentUsageAll || []).slice().sort((x, y) => agentCostUsd(y) - agentCostUsd(x)) : [];
    const rangeTotal = allMode
      ? allRows.reduce((s, r) => s + agentCostUsd(r), 0)
      : shown.reduce((s, a) => s + agentCostUsd(agentUsage && agentUsage[a.agent_id]), 0);
    const orgCount = allMode ? new Set(allRows.map(r => r.org_id)).size : 1;
    const scopeToggle = isAdmin
      ? `<div class="ws-range" id="scope-toggle"><button class="${allMode ? '' : 'on'}" data-scope="org">My org</button><button class="${allMode ? 'on' : ''}" data-scope="all">All orgs</button></div>`
      : '';
    main.innerHTML = `<div class="main-pad" style="max-width:none;">
      <div class="between" style="align-items:flex-start;">
        <div>
          <div class="page-eyebrow">Agents · operational${allMode ? ' · platform' : ''}</div>
          <div class="page-title">${allMode ? 'All orgs' : 'Agents'}</div>
          <p class="page-lede">${allMode
            ? 'Every org\'s agents across the platform, by cost over the selected range — cumulative through restarts. The biggest spenders float to the top.'
            : 'Every agent and its model usage — the status dot shows whether it\'s active (ran in the last few minutes), idle, or paused. Pick a range to total cost + tokens across every time an agent ran, cumulative through restarts. Click a card to open that agent live in MRI.'}</p>
        </div>
        <div class="row" style="gap:10px; margin-top:14px; flex-shrink:0; align-items:center;">
          ${allMode ? '' : `<span class="chip"><span class="dot live" style="background:var(--ok);"></span>${counts.active} active</span>`}
          <span class="data" id="pod-meter" style="font-size:10px; color:var(--ink-4);"></span>
        </div>
      </div>
      <div class="between" style="margin-top:20px; flex-wrap:wrap; gap:12px;">
        <div class="row" style="gap:12px; flex-wrap:wrap;">
          <div class="ws-range">${presets.map(p => `<button class="${p.key === usageRange.key ? 'on' : ''}" data-range="${p.key}">${esc(p.label)}</button>`).join('')}</div>
          ${scopeToggle}
        </div>
        <span class="data" style="font-size:10px; color:var(--ink-4);">${esc(rangeLabel)} total · <span style="color:var(--signal-deep);" id="range-total">$${rangeTotal.toFixed(2)}</span></span>
      </div>
      ${usageRange.key === 'custom' ? `<div class="row" style="gap:14px; margin-top:12px; flex-wrap:wrap;">
        <label class="data" style="font-size:9px; color:var(--ink-4); display:flex; align-items:center; gap:6px;">FROM <input type="datetime-local" id="range-from" class="ctrl-input" value="${esc(toLocalInput(usageRange.since))}"></label>
        <label class="data" style="font-size:9px; color:var(--ink-4); display:flex; align-items:center; gap:6px;">TO <input type="datetime-local" id="range-to" class="ctrl-input" value="${esc(toLocalInput(usageRange.until))}"></label>
      </div>` : ''}
      ${(allMode ? allRows.length : shown.length)
        ? `<div class="dash-grid" style="margin-top:22px;">${allMode ? allRows.map(dashCardAll).join('') : shown.map(a => dashCard(a)).join('')}</div>
           <div class="data" style="font-size:8.5px; color:var(--ink-4); margin-top:12px; line-height:1.6;">Est. cost — real cloud spend + the agent's share of the GPU pod, valued by its compute-seconds × the pod's $/hr (hover a cost for the split). Totals are cumulative over the selected range, summed across every restart.${isSession ? ' This session = the current process uptime.' : ''}</div>`
        : `<div class="empty" style="margin-top:22px;"><h3>${allMode ? 'No usage in this range' : 'No agents yet'}</h3><p>${allMode
            ? `No agent across any org called the model in the selected window.`
            : 'Pair a persona with a role to create your first agent.'}</p></div>`}
      <div style="margin-top:28px; padding-top:20px; border-top:1px solid var(--line-faint); display:flex; align-items:center; justify-content:space-between;">
        <span class="data" style="font-size:9px; color:var(--ink-4);">${allMode
          ? `${orgCount} org${orgCount === 1 ? '' : 's'} · ${allRows.length} agent${allRows.length === 1 ? '' : 's'}`
          : `${counts.active} active · ${counts.idle} idle · ${counts.paused} paused`}</span>
      </div></div>`;
    main.querySelectorAll('.ws-range button[data-range]').forEach(b => b.addEventListener('click', () => setUsageRange(b.dataset.range)));
    main.querySelectorAll('#scope-toggle button[data-scope]').forEach(b => b.addEventListener('click', () => setUsageScope(b.dataset.scope)));
    const from = main.querySelector('#range-from'), to = main.querySelector('#range-to');
    const applyCustom = () => setUsageRange('custom',
      from && from.value ? new Date(from.value).toISOString() : null,
      to && to.value ? new Date(to.value).toISOString() : null);
    if (from) from.addEventListener('change', applyCustom);
    if (to) to.addEventListener('change', applyCustom);
    if (!allMode) main.querySelectorAll('.dash-card').forEach(c => c.addEventListener('click', () => openAgentInLabs(c.dataset.agent, c.dataset.name, c.dataset.persona)));
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
      html = `GPU pod · ${esc(p.state)}…`;
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
  function dashCard(a) {
    const st = agentStatus(a);
    const enabled = a.enabled !== false; // cost cells read $0.00 when enabled, — when paused
    const u = (agentUsage && agentUsage[a.agent_id]) || null;
    const lt = u && u.last_ts ? Date.parse(u.last_ts)
      : (agentActivity && agentActivity[a.agent_id] ? agentActivity[a.agent_id].lastTs : 0);
    const lastActive = lt ? agoShort(Date.now() - lt) + ' ago' : '—';
    const costLabel = u ? '$' + agentCostUsd(u).toFixed(2) : (enabled ? '$0.00' : '—');
    const tokLabel = u ? fmtTokens((u.in_tok || 0) + (u.out_tok || 0)) : (enabled ? '0' : '—');
    const callsLabel = u ? String(u.calls) : (enabled ? '0' : '—');
    return `<button class="dash-card" data-status="${st.state}" data-persona="${esc(a.persona)}" data-agent="${esc(a.agent_id)}" data-name="${esc(a.name || a.agent_id)}">
      <div class="dc-head">
        <div class="dc-identity">
          <span class="${st.cls}" style="background:${st.color};" title="${esc(st.label)}"></span>
          <span class="dc-name">${esc(a.name || a.agent_id)}</span>
          <span class="chip persona"><span class="dot"></span>${esc(personaName(a.persona))}</span>
          <span class="chip role"><span class="dot"></span>${esc(a.mandate_id)}</span>
          <span class="data" style="font-size:8px; letter-spacing:0.14em; text-transform:uppercase; color:var(--ink-4);">${esc(st.label)}</span>
        </div>
        <span class="dc-launch-hint">${MRI_SVG} Open in MRI</span>
      </div>
      <div class="dc-metrics">
        <div class="dc-metric dm-cost"><div class="dm-val" data-cost-for="${esc(a.agent_id)}" title="${esc(costTitle(u))}">${esc(costLabel)}</div><div class="dm-lab">Est. cost</div></div>
        <div class="dc-metric"><div class="dm-val" data-tok-for="${esc(a.agent_id)}" title="${esc(usageTitle(u))}">${esc(tokLabel)}</div><div class="dm-lab">Tokens</div></div>
        <div class="dc-metric"><div class="dm-val" data-calls-for="${esc(a.agent_id)}">${esc(callsLabel)}</div><div class="dm-lab">Model calls</div></div>
        <div class="dc-metric"><div class="dm-val">${esc(lastActive)}</div><div class="dm-lab">Last active</div></div>
      </div>
    </button>`;
  }
  // Card → Labs. Switch to Labs and OBSERVE that agent's live lane (chemistry +
  // idle thoughts) without restarting the brain. Clicking the org's own owner
  // persona (e.g. the default The Admin) just shows the owner lane — setObservedAgent
  // resolves that by comparing the agent's persona to the active process persona.
  function openAgentInLabs(agentId, name, persona) {
    if (typeof window.setObservedAgent === 'function') window.setObservedAgent(agentId, name, persona);
    setWorkspace('labs');
  }
  function personaName(slug) {
    const p = (window.SETTINGS && window.SETTINGS.personas || []).find(x => personaSlug(x.id) === slug);
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
    if (need.length) Promise.all(need).then(paintPersonas); else paintPersonas();
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
    const known = (window.SETTINGS && window.SETTINGS.personas) || [];
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

  function paintPersonas() {
    const host = document.getElementById('ws-personas');
    if (!host) return;
    const rows = personaRollup();
    const activeSlug = activePersonaSlug();
    const liveCount = rows.filter(p => personaStatus(p).state === 'active').length;
    host.innerHTML = `
      <div class="ws-grid" style="grid-template-columns:268px 1fr;">
        <div class="ws-rail">
          <div class="rail-head"><h2>Personas</h2><span class="n">${rows.length}</span></div>
          <div class="rail-sect">
            <button class="rail-item pe-nav ${perView==='overview'?'on':''}" data-view="overview"><span class="ri-name"><span class="dot-status ${liveCount?'live':''}" style="background:${liveCount?'var(--ok)':'var(--ink-4)'}"></span>Overview</span><span class="ri-meta">${rows.length} total · ${liveCount} active</span></button>
          </div>
          <div class="rail-div"></div>
          <div class="rail-sect-lab" style="padding-left:22px; display:flex; justify-content:space-between; padding-right:18px;"><span>Personas</span><span class="n">${rows.length}</span></div>
          <div class="rail-sect">${rows.map(p => railPersona(p, activeSlug)).join('') || '<div class="ri-meta" style="padding:6px 14px; opacity:.6;">No personas</div>'}</div>
        </div>
        <div class="ws-main" id="pers-main"></div>
      </div>`;
    host.querySelectorAll('.pe-nav').forEach(n => n.addEventListener('click', () => { perView = n.dataset.view; personaSel = null; paintPersonas(); }));
    // Rail persona → configure it INLINE (the Agents rail→detail pattern): renders the
    // persona's full config — temperament dials, chemistry, self/voice — into the pane,
    // reusing the settings engine. The Overview cards are the "watch it live" path.
    host.querySelectorAll('.rail-persona').forEach(n => n.addEventListener('click', () => {
      personaSel = n.dataset.name; perView = 'detail'; paintPersonas();
    }));
    const main = host.querySelector('#pers-main');
    if (perView === 'detail' && personaSel) renderPersonaDetail(main);
    else renderPersonasView(main);
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
            <button class="mri-open" id="pers-open-mri" title="Watch this persona live in MRI">${MRI_SVG} Open in MRI</button>
            <div class="dirty-pill" id="pers-dirty-pill"><span class="chip"></span><span id="pers-dirty-text">0 unsaved</span></div>
            <button class="restart-banner" id="pers-restart-banner">Restart required</button>
            <button class="btn-save idle" id="pers-save-btn">Save</button>
          </div>
        </header>
        <div class="set-scroll" id="pers-scroll"><div class="cat-wrap" id="pers-cat-wrap"></div></div>
      </div>`;
    main.querySelector('#pers-back-btn').addEventListener('click', () => { perView = 'overview'; personaSel = null; paintPersonas(); });
    main.querySelector('#pers-open-mri').addEventListener('click', () => openPersonaInMri(personaSlug(personaSel)));
    if (window.__settingsUI && window.__settingsUI.mountPersona) window.__settingsUI.mountPersona(personaSel);
  }

  function railPersona(p, activeSlug) {
    const st = personaStatus(p);
    const detail = p.slug === activeSlug ? 'running now' : `${p.agents.length} agent${p.agents.length === 1 ? '' : 's'}`;
    const on = (perView === 'detail' && personaSel === p.name) ? ' on' : '';
    return `<button class="rail-item rail-persona${on}" data-persona="${esc(p.slug)}" data-name="${esc(p.name)}"><span class="ri-name"><span class="${st.cls}" style="background:${st.color}"></span>${esc(p.name)}</span><span class="ri-meta">${esc(detail)}</span></button>`;
  }

  function renderPersonasView(main) {
    if (!main) return;
    const rows = personaRollup().sort((x, y) => {
      const rx = STATUS_RANK[personaStatus(x).state], ry = STATUS_RANK[personaStatus(y).state];
      if (rx !== ry) return rx - ry;
      return personaCostUsd(y) - personaCostUsd(x);
    });
    const counts = { active: 0, idle: 0, paused: 0 };
    rows.forEach(p => counts[personaStatus(p).state]++);
    const rangeLabel = (RANGE_PRESETS.find(p => p.key === usageRange.key) || {}).label || 'Range';
    const rangeTotal = rows.reduce((s, p) => s + personaCostUsd(p), 0);
    main.innerHTML = `<div class="main-pad" style="max-width:none;">
      <div class="between" style="align-items:flex-start;">
        <div>
          <div class="page-eyebrow">Personas · operational</div>
          <div class="page-title">Personas</div>
          <p class="page-lede">Every persona and its aggregated usage — cost, tokens and model calls summed across all of its agents. The status dot shows whether the persona is active (it's the running process, or one of its agents just ran), idle, or paused. Pick a range to total cost over time. Click a persona to open it live in MRI.</p>
        </div>
        <div class="row" style="gap:10px; margin-top:14px; flex-shrink:0; align-items:center;">
          <span class="chip"><span class="dot live" style="background:var(--ok);"></span>${counts.active} active</span>
          <span class="data" id="pers-pod-meter" style="font-size:10px; color:var(--ink-4);"></span>
        </div>
      </div>
      <div class="between" style="margin-top:20px; flex-wrap:wrap; gap:12px;">
        <div class="row" style="gap:12px; flex-wrap:wrap;">
          <div class="ws-range">${RANGE_PRESETS.map(p => `<button class="${p.key === usageRange.key ? 'on' : ''}" data-range="${p.key}">${esc(p.label)}</button>`).join('')}</div>
        </div>
        <span class="data" style="font-size:10px; color:var(--ink-4);">${esc(rangeLabel)} total · <span style="color:var(--signal-deep);">$${rangeTotal.toFixed(2)}</span></span>
      </div>
      ${usageRange.key === 'custom' ? `<div class="row" style="gap:14px; margin-top:12px; flex-wrap:wrap;">
        <label class="data" style="font-size:9px; color:var(--ink-4); display:flex; align-items:center; gap:6px;">FROM <input type="datetime-local" id="prange-from" class="ctrl-input" value="${esc(toLocalInput(usageRange.since))}"></label>
        <label class="data" style="font-size:9px; color:var(--ink-4); display:flex; align-items:center; gap:6px;">TO <input type="datetime-local" id="prange-to" class="ctrl-input" value="${esc(toLocalInput(usageRange.until))}"></label>
      </div>` : ''}
      ${rows.length
        ? `<div class="dash-grid" style="margin-top:22px;">${rows.map(personaCard).join('')}</div>
           <div class="data" style="font-size:8.5px; color:var(--ink-4); margin-top:12px; line-height:1.6;">Per-persona totals roll up every agent that runs this persona — its real cloud spend plus its share of the GPU pod (compute-seconds × the pod's $/hr). Cumulative over the selected range, summed across restarts. A persona's own owner-lane idle work isn't metered here.</div>`
        : `<div class="empty" style="margin-top:22px;"><h3>No personas</h3></div>`}
      <div style="margin-top:28px; padding-top:20px; border-top:1px solid var(--line-faint); display:flex; align-items:center; justify-content:space-between;">
        <span class="data" style="font-size:9px; color:var(--ink-4);">${counts.active} active · ${counts.idle} idle · ${counts.paused} paused</span>
      </div></div>`;
    main.querySelectorAll('.ws-range button[data-range]').forEach(b => b.addEventListener('click', () => setUsageRange(b.dataset.range)));
    const from = main.querySelector('#prange-from'), to = main.querySelector('#prange-to');
    const applyCustom = () => setUsageRange('custom',
      from && from.value ? new Date(from.value).toISOString() : null,
      to && to.value ? new Date(to.value).toISOString() : null);
    if (from) from.addEventListener('change', applyCustom);
    if (to) to.addEventListener('change', applyCustom);
    main.querySelectorAll('.dash-card').forEach(c => c.addEventListener('click', () => openPersonaInMri(c.dataset.persona)));
    const pm = main.querySelector('#pers-pod-meter');
    if (pm && podStatus && podStatus.running && podStatus.uptime_s != null) {
      const cost = podStatus.cost_accrued_usd != null ? ` · $${podStatus.cost_accrued_usd.toFixed(2)} accrued` : '';
      pm.innerHTML = `GPU pod · up ${esc(fmtDur(podStatus.uptime_s))}${cost}`;
    }
  }

  function personaCard(p) {
    const st = personaStatus(p);
    const u = { in_tok: p.in_tok, out_tok: p.out_tok, calls: p.calls, cloud_usd: p.cloud_usd, pod_s: p.pod_s, cloud_calls: p.cloud_calls };
    const lastActive = p.lastTs ? agoShort(Date.now() - p.lastTs) + ' ago' : '—';
    return `<button class="dash-card" data-status="${st.state}" data-persona="${esc(p.slug)}">
      <div class="dc-head">
        <div class="dc-identity">
          <span class="${st.cls}" style="background:${st.color};" title="${esc(st.label)}"></span>
          <span class="dc-name">${esc(p.name)}</span>
          <span class="chip role"><span class="dot"></span>${p.agents.length} agent${p.agents.length === 1 ? '' : 's'}</span>
          <span class="data" style="font-size:8px; letter-spacing:0.14em; text-transform:uppercase; color:var(--ink-4);">${esc(st.label)}</span>
        </div>
        <span class="dc-launch-hint">${MRI_SVG} Open in MRI</span>
      </div>
      <div class="dc-metrics">
        <div class="dc-metric dm-cost"><div class="dm-val" title="${esc(costTitle(u))}">$${personaCostUsd(p).toFixed(2)}</div><div class="dm-lab">Est. cost</div></div>
        <div class="dc-metric"><div class="dm-val" title="${esc(usageTitle(u))}">${esc(fmtTokens((p.in_tok || 0) + (p.out_tok || 0)))}</div><div class="dm-lab">Tokens</div></div>
        <div class="dc-metric"><div class="dm-val">${esc(String(p.calls || 0))}</div><div class="dm-lab">Model calls</div></div>
        <div class="dc-metric"><div class="dm-val">${esc(lastActive)}</div><div class="dm-lab">Last active</div></div>
      </div>
    </button>`;
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
    main.innerHTML = `<div class="main-pad" style="max-width:760px;">
      <button class="link ag-back" style="margin-bottom:18px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg> Agents</button>
      <div class="between" style="align-items:flex-start;">
        <div>
          <div class="page-eyebrow">Agent · permission editor</div>
          <div class="page-title" style="font-size:26px;">${esc(a.name || a.agent_id)}</div>
          <div class="row" style="gap:8px; margin-top:12px;">
            <span class="chip persona"><span class="dot"></span>${esc(personaName(a.persona))}</span>
            <span class="chip role"><span class="dot"></span>${esc(a.mandate_id)}</span>
            <span class="data" style="font-size:10px;">${esc(a.agent_id)}</span>
          </div>
        </div>
        <button class="mri-open" id="ag-view-persona" style="margin-top:8px;" title="Watch this agent live in MRI">${MRI_SVG} Open in MRI</button>
      </div>
      <div class="note" style="margin-top:22px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg><p>Every value is <b>bounded by the account ceiling</b> set in Account Limits — an agent can be granted less, never more. Leave a field blank to inherit.</p></div>
      <div class="card" style="margin-top:18px;">
        <div class="card-head"><span class="ch-num">01</span><div><div class="ch-title">Identity</div><div class="ch-desc">display name shown to operators</div></div></div>
        <div class="card-body">
          <div class="ctrl"><div class="ctrl-meta"><div class="lab">Display name</div><div class="hint">optional · defaults to the id</div></div><div class="ctrl-field"><input class="ctrl-input" id="ag-name" value="${esc(a.name || '')}" placeholder="${esc(a.agent_id)}"></div></div>
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
    main.querySelector('#ag-view-persona').addEventListener('click', () => {
      setWorkspace('labs');
      // best-effort: open the persona in the settings studio
      if (window.__settingsUI && window.__settingsUI.open) { try { document.getElementById('settings-btn')?.click(); } catch (e) {} }
    });
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
      if (!id) { errDiv.textContent = ''; createBtn.disabled = true; return; }
      if (!VALID_ID.test(id)) { errDiv.textContent = 'Use lowercase letters, digits, _ or - (must start with a letter or digit)'; createBtn.disabled = true; }
      else { errDiv.textContent = ''; createBtn.disabled = false; }
    };
    nameIn.addEventListener('input', () => { if (!idEdited) idIn.value = slugify(nameIn.value); validate(); });
    idIn.addEventListener('input', () => { idEdited = true; validate(); });
    const close = () => { modal.classList.remove('open'); modal.innerHTML = ''; };
    const create = () => { const id = idIn.value.trim(); if (!id || !VALID_ID.test(id)) return; close(); openRole(main, id, ''); };
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
        <button class="self-revert" id="role-save">${_check}<span>Save</span></button>
      </div>
      <textarea class="self-area" id="role-text" spellcheck="false" placeholder="Describe this role — the job, tone, and rules for it…">${esc(savedText)}</textarea>
    </div>`;
    const area = ed.querySelector('#role-text');
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
    area.addEventListener('input', updateMeta);
    updateMeta();
    saveBtn.addEventListener('click', async () => {
      const lbl = saveBtn.querySelector('span');
      lbl.textContent = 'Saving…'; saveBtn.disabled = true;
      try {
        const r = await fetch('/mandates', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, role_text: area.value }) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
        await loadAgents(); // agRoleSel re-selects this role after re-render
      } catch (e) { lbl.textContent = 'Save'; saveBtn.disabled = false; window.alert('Could not save role: ' + e.message); }
    });
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
    const personas = (window.SETTINGS && window.SETTINGS.personas) || [];
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
  let apiView = 'reference';
  let skillsData = null;       // { enabled, is_admin, skills:[], flagged:[] }
  function ensureApi() { renderApi(); if (apiView === 'partner') loadPartnerKeys(); if (apiView === 'skills') loadSkills(); }
  function renderApi() {
    const host = document.getElementById('ws-api');
    host.innerHTML = `<div class="ws-grid" style="grid-template-columns:256px 1fr;">
      <div class="ws-rail">
        <div class="rail-head"><h2>API</h2><span class="n">integration</span></div>
        <div class="rail-sect">
          <button class="rail-item api-nav ${apiView==='reference'?'on':''}" data-view="reference"><span class="ri-name">API Reference</span><span class="ri-meta">engine endpoints</span></button>
          <button class="rail-item api-nav ${apiView==='skills'?'on':''}" data-view="skills"><span class="ri-name">Skills</span><span class="ri-meta">app-provided · review</span></button>
          <button class="rail-item api-nav ${apiView==='partner'?'on':''}" data-view="partner"><span class="ri-name">Partner Keys</span><span class="ri-meta">customer-facing tokens</span></button>
        </div>
      </div>
      <div class="ws-main" id="api-main"></div></div>
      <div class="modal-veil" id="ws-api-modal"></div>`;
    host.querySelectorAll('.api-nav').forEach(n => n.addEventListener('click', () => { apiView = n.dataset.view; ensureApi(); }));
    const main = host.querySelector('#api-main');
    if (apiView === 'partner') renderPartnerKeys(main);
    else if (apiView === 'skills') renderSkills(main);
    else renderReference(main);
  }
  const ENDPOINTS = [
    // ── Sessions & turns ──
    { grp: 'Sessions', m: 'post', p: '/v1/sessions', t: 'Start a session for an end-user on an agent. Optionally pin app-provided skills into every turn of the session.',
      body: { agent_id: 'the_visionary.research_lead', end_user_id: 'u_8821', skills: ['house_policy_v2'] } },
    { grp: 'Sessions', m: 'post', p: '/v1/sessions/{id}/turns', t: 'Run one turn. Returns clean display text, the structured affect block + mood, and a confirmation block when a cloud write is pending. Send message OR audio_input for voice-in.',
      body: { message: 'What changed in the market today?' } },
    { grp: 'Sessions', m: 'post', p: '/v1/sessions/{id}/turns/stream', t: 'Stream the turn over SSE — an open event, inner-thought + mood-delta events, a final done event, then optional audio_chunk frames when audio.enabled.', tag: 'SSE',
      body: { message: 'Summarise the thread', audio: { enabled: true } } },
    { grp: 'Sessions', m: 'ws', p: '/v1/sessions/{id}/stream', t: 'Realtime duplex WebSocket: stream PCM16 audio in (live STT), receive inner-life events and TTS chunks back. Auth is checked on the upgrade request; unauthorised connections close 1008.', tag: 'WS' },
    { grp: 'Sessions', m: 'post', p: '/v1/sessions/{id}/consolidate', t: 'Run the session-end Hebbian/sleep consolidation now and persist learning, without tearing the brain down. Idempotent and single-flight.',
      body: { reason: 'debate_end' } },
    { grp: 'Sessions', m: 'post', p: '/v1/sessions/{id}/confirm', t: 'Approve or discard the cloud-write action a turn parked for sign-off.',
      body: { approve: true } },
    { grp: 'Sessions', m: 'get', p: '/v1/sessions/{id}/approvals', t: 'List sensitive tool-actions the brain skipped and is waiting on a yes/no for. Owner keys also see the autonomous (unattended) lane.' },
    { grp: 'Sessions', m: 'post', p: '/v1/sessions/{id}/approvals/{approval_id}/resolve', t: 'Approve (run it) or skip one pending tool-action on behalf of the end-user.',
      body: { approve: true } },

    // ── Utility ──
    { grp: 'Utility', m: 'post', p: '/v1/extract', t: 'Sessionless structured extraction — one cheap model call returns JSON matching your schema. No persona, memory, motor, or DMN. Metered and bounded by the daily USD ceiling (402 when over budget).',
      body: { input: 'Apple beat earnings expectations…', schema: { type: 'object', properties: { ticker: { type: 'string' }, sentiment: { type: 'string' } } }, instructions: 'Pull the tradeable signal.' } },

    // ── Audio ──
    { grp: 'Audio', m: 'post', p: '/v1/tts', t: 'Text-to-speech with the affect→voice mapping: pass affect to drive mood-aware prosody. Stateless; 503 when no provider key is configured.',
      body: { text: 'Markets are calm today.', affect: { valence: 0.4, arousal: 0.2 }, voice_id: '…' } },
    { grp: 'Audio', m: 'post', p: '/v1/stt', t: 'Speech-to-text (base64 audio in) — the commodity transcription path, with optional diarisation. Stateless.',
      body: { audio: '<base64>', mimetype: 'audio/wav', diarize: false } },

    // ── Mandates (the role library) ──
    { grp: 'Mandates', m: 'get', p: '/v1/mandates', t: 'List the org role library. Pass ?include_inactive=true to include deactivated roles.' },
    { grp: 'Mandates', m: 'put', p: '/v1/mandates/{id}', t: 'Create or update a role (mandate). conduct_rules / reward_weights are stored for partner sync.',
      body: { role_text: 'You are a meticulous research lead…', conduct_rules: null, reward_weights: null } },
    { grp: 'Mandates', m: 'del', p: '/v1/mandates/{id}', t: 'Deactivate a role.' },
    { grp: 'Mandates', m: 'get', p: '/v1/personas/{persona}/mandates', t: 'List the roles assigned to a persona, in order.' },
    { grp: 'Mandates', m: 'put', p: '/v1/personas/{persona}/mandates/{id}', t: 'Assign a role to a persona (idempotent). Optional sort_order.',
      body: { sort_order: 0 } },
    { grp: 'Mandates', m: 'del', p: '/v1/personas/{persona}/mandates/{id}', t: 'Unassign a role from a persona.' },

    // ── Agents (the persona×role pairing) ──
    { grp: 'Agents', m: 'get', p: '/v1/agents', t: 'List the org\'s agents and the account permission ceilings.' },
    { grp: 'Agents', m: 'get', p: '/v1/agents/{id}', t: 'Fetch one agent (persona×role) — its name, permissions, and model tier.' },
    { grp: 'Agents', m: 'put', p: '/v1/agents/{id}', t: 'Create-or-update an agent: set its display name, per-agent permission narrowing, and model tier.',
      body: { name: 'Research Lead', tier: 'full', permissions: { cloud_writes: false } } },
    { grp: 'Agents', m: 'del', p: '/v1/agents/{id}', t: 'Delete an agent (unassign the persona×role pairing).' },

    // ── App-provided skills ──
    { grp: 'Skills', m: 'get', p: '/v1/skills', t: 'List app-provided skills. Filters: ?status= and ?include_inactive=. A non-owner partner sees only its own submissions.' },
    { grp: 'Skills', m: 'get', p: '/v1/skills/{id}', t: 'Fetch one skill.' },
    { grp: 'Skills', m: 'put', p: '/v1/skills/{id}', t: 'Submit or update an app-provided skill. Runs the admission screener: obviously-safe → enabled, anything in question → flagged for review.',
      body: { body: 'When the user asks about returns, cite the 30-day policy…', description: 'House return-policy answers', keywords: ['returns', 'policy'], tier: 2 } },
    { grp: 'Skills', m: 'del', p: '/v1/skills/{id}', t: 'Remove a skill.' },

    // ── Admin (owner credential) ──
    { grp: 'Admin', scope: 'owner', m: 'get', p: '/v1/admin/skills/flagged', t: 'List skills the screener flagged, awaiting superadmin review.' },
    { grp: 'Admin', scope: 'owner', m: 'post', p: '/v1/admin/skills/{id}/approve', t: 'Approve a flagged skill — it goes live.' },
    { grp: 'Admin', scope: 'owner', m: 'post', p: '/v1/admin/skills/{id}/reject', t: 'Reject a flagged skill.',
      body: { reason: 'duplicates built-in behaviour' } },

    // ── Keys & end-user lifecycle (owner credential) ──
    { grp: 'Keys', scope: 'owner', m: 'get', p: '/v1/partner_keys', t: 'List the org\'s partner keys (metadata only — never the token).' },
    { grp: 'Keys', scope: 'owner', m: 'post', p: '/v1/partner_keys', t: 'Mint a partner key — the token is returned once, at creation.',
      body: { partner_id: 'acme', label: 'Acme production' } },
    { grp: 'Keys', scope: 'owner', m: 'del', p: '/v1/partner_keys/{id}', t: 'Revoke a partner key.' },
    { grp: 'Keys', scope: 'owner', m: 'del', p: '/v1/end_users/{id}', t: 'Erase one end-user\'s memory + state across every per-user table and in-memory cache (GDPR right-to-erasure).' },

    // ── Per-end-user MCP tokens ──
    { grp: 'MCP tokens', m: 'post', p: '/v1/mcp/tokens', t: 'Store a per-end-user MCP access token (vault-encrypted at rest) after the partner completes the OAuth flow, so managed agents can build per-user Vaults.',
      body: { end_user_id: 'u_8821', server_name: 'gmail', server_url: 'https://mcp.example.com', access_token: '…', expires_at: null } },
    { grp: 'MCP tokens', m: 'get', p: '/v1/mcp/tokens/{end_user_id}', t: 'List an end-user\'s connected MCP servers (metadata only — never the token).' },
    { grp: 'MCP tokens', m: 'del', p: '/v1/mcp/tokens/{end_user_id}/{server_name}', t: 'Delete one stored MCP token connection.' },
  ];
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
  function renderReference(main) {
    // Build the rail grouped by section, preserving declaration order.
    const groups = [];
    ENDPOINTS.forEach((e, i) => {
      let g = groups.find(x => x.name === (e.grp || ''));
      if (!g) { g = { name: e.grp || '', items: [] }; groups.push(g); }
      g.items.push({ e, i });
    });
    const railHtml = groups.map(g =>
      `<div class="rail-sect-lab">${esc(g.name)}</div>` +
      g.items.map(({ e, i }) => `<button class="rail-item ep-item ${i===0?'on':''}" data-i="${i}" style="padding:9px 14px;"><span class="ri-name" style="font-size:12px; gap:9px;"><span class="method ${e.m}">${e.m.toUpperCase()}</span><span class="data" style="font-size:11px;">${esc(e.p)}</span></span>${e.tag?`<span class="ri-meta" style="margin-left:auto;">${e.tag}</span>`:''}</button>`).join('')
    ).join('');
    main.innerHTML = `<div style="display:grid; grid-template-columns:320px 1fr; grid-template-rows:minmax(0,1fr); height:100%;">
      <div class="ws-rail" style="border-right:1px solid var(--line-soft); overflow-y:auto;">
        <div class="rail-head"><h2>Reference</h2><span class="n">v1 · ${ENDPOINTS.length} endpoints</span></div>
        <div class="rail-sect" id="ep-list" style="padding-top:0;">${railHtml}</div>
      </div>
      <div class="ws-main"><div class="main-pad" style="max-width:680px;" id="ep-detail"></div></div></div>`;
    const detail = main.querySelector('#ep-detail');
    const show = (i) => {
      const e = ENDPOINTS[i];
      const owner = e.scope === 'owner';
      const authNote = owner
        ? 'This route requires the <b>owner credential</b> (the org/superadmin key set via <span class="data" style="font-size:11px;">BRAIN_API_KEYS</span>), not a partner key.'
        : 'Authenticated with a <b>partner key</b> (<span class="data" style="font-size:11px;">Authorization: Bearer sk_…</span>). Mint one under <b>Partner Keys</b>. The owner credential works on every route too.';
      const example = e.m === 'ws'
        ? `<span class="k">GET</span> ${esc(e.p)}\n<span class="k">Upgrade</span>: websocket\n<span class="k">Authorization</span>: Bearer <span class="p">sk_•••</span>`
        : `<span class="k">${e.m.toUpperCase()}</span> ${esc(e.p)}\n<span class="k">Authorization</span>: Bearer <span class="p">sk_•••</span>${e.body?`\n\n${hlJson(e.body)}`:''}`;
      detail.innerHTML = `<div class="row" style="gap:12px;"><span class="method ${e.m}">${e.m.toUpperCase()}</span><span class="data" style="font-size:15px; color:var(--ink);">${esc(e.p)}</span>${e.tag?`<span class="chip">${e.tag}</span>`:''}${owner?'<span class="chip role">owner</span>':''}</div>
        <p class="page-lede">${esc(e.t)}</p>
        <div class="note" style="margin-top:18px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 16v-4M12 8h.01"/><circle cx="12" cy="12" r="10"/></svg><p>${authNote}</p></div>
        <div class="label" style="margin:22px 0 8px;">Example ${e.m==='ws'?'handshake':'request'}</div>
        <div class="code">${example}</div>`;
    };
    main.querySelectorAll('.ep-item').forEach(b => b.addEventListener('click', () => { main.querySelectorAll('.ep-item').forEach(x => x.classList.remove('on')); b.classList.add('on'); show(+b.dataset.i); }));
    show(0);
  }

  // ─────────────────────────────────────────────────────────── Skills ──
  const SKILL_STATUS_COLOR = { enabled: 'var(--ok)', flagged: 'var(--warn)', rejected: 'var(--danger)', pending: 'var(--ink-4)' };
  const skStatColor = (s) => SKILL_STATUS_COLOR[s] || 'var(--ink-4)';
  async function loadSkills() {
    try { const r = await fetch('/skills'); skillsData = r.ok ? await r.json() : { enabled: false, is_admin: false, skills: [], flagged: [] }; }
    catch (e) { skillsData = { enabled: false, is_admin: false, skills: [], flagged: [] }; }
    if (apiView === 'skills') renderSkills(document.getElementById('api-main'));
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
        <div class="page-eyebrow">API · skills</div><div class="page-title">Skills</div>
        <p class="page-lede" style="max-width:600px;">App-provided skills your apps register over the engine API — reusable instructions the agent selects per turn (or you pin per session). Submissions over the API are screened before they go live; anything the screener can't auto-clear waits in the review queue below. Add your own here, or review submissions.</p>
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
    const rows = skills.map(s => `<div class="ag-row" style="grid-template-columns:${GRID}; cursor:default;">
      <span>${nameCell(s)}</span>
      <span class="ar-status"><span class="dot-status" style="background:${skStatColor(s.status)}"></span>${esc(s.status || 'pending')}</span>
      <span>${d.is_admin ? `<button class="link sk-scope" data-id="${esc(s.id)}">${esc(appliesLabel(s))}</button>` : `<span class="data" style="font-size:11px;">${esc(appliesLabel(s))}</span>`}</span>
      <span class="data" style="font-size:11px;">${esc(s.submitted_by || '—')}</span>
      <span class="ar-chev">${d.is_admin ? `<button class="link sk-del" data-id="${esc(s.id)}" title="Remove skill">✕</button>` : ''}</span></div>`).join('') || '<div style="padding:22px; text-align:center;" class="data">No skills registered yet.</div>';
    main.innerHTML = `<div class="main-pad" style="max-width:820px;">${head}${queue}
      <div class="label" style="margin:28px 0 10px;">Library · ${skills.length}</div>
      <div class="ag-table" style="grid-template-columns:none;">
        <div class="ag-table-head" style="grid-template-columns:${GRID};"><span>Skill</span><span>Status</span><span>Applies to</span><span>Submitted by</span><span></span></div>
        ${rows}
      </div>
      ${!d.is_admin ? '<p class="data" style="margin-top:14px; font-size:11px; opacity:.6;">Reviewing and approving skills is an org-admin action.</p>' : ''}</div>`;
    main.querySelectorAll('.sk-approve').forEach(b => b.addEventListener('click', () => skillApprove(b.dataset.id)));
    main.querySelectorAll('.sk-reject').forEach(b => b.addEventListener('click', () => skillReject(b.dataset.id)));
    main.querySelectorAll('.sk-del').forEach(b => b.addEventListener('click', () => skillDelete(b.dataset.id)));
    main.querySelectorAll('.sk-scope').forEach(b => b.addEventListener('click', () => { const sk = skills.find(x => x.id === b.dataset.id); if (sk) openSkillScope(sk); }));
    main.querySelectorAll('.sk-open').forEach(b => b.addEventListener('click', () => openSkillEditor(b.dataset.id)));
    const newBtn = main.querySelector('#sk-new'); if (newBtn) newBtn.addEventListener('click', openNewSkill);
  }
  async function openSkillEditor(id) {
    const modal = document.getElementById('ws-api-modal');
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
    const modal = document.getElementById('ws-api-modal');
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
    const modal = document.getElementById('ws-api-modal');
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
    wireSwitcher();
    loadGating();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
