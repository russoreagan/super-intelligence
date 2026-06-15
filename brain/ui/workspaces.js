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

  const WS_ICONS = {
    labs: '<path d="M9 3v6.5L4.2 18a2 2 0 0 0 1.8 3h12a2 2 0 0 0 1.8-3L15 9.5V3M8 3h8M9 14h6"/>',
    agents: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    api: '<path d="m7 8-4 4 4 4M17 8l4 4-4 4M14 4l-4 16"/>',
  };
  const WS_NAMES = { labs: 'Labs', agents: 'Agents', api: 'API' };

  let workspace = 'labs';
  let isAdmin = false;
  let mandatesEnabled = false;
  let agentsData = null;      // { agents, roles, ceilings }
  let agentSel = null;        // open agent_id
  let partnerKeys = null;

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
    const api = document.getElementById('ws-api');
    const labs = ws === 'labs';
    if (main) main.style.display = labs ? '' : 'none';
    if (ticker) ticker.style.display = labs ? '' : 'none';
    agents.classList.toggle('on', ws === 'agents');
    api.classList.toggle('on', ws === 'api');
    if (ws === 'agents') ensureAgents();
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
      if (me.ok) isAdmin = !!(await me.json()).is_admin;
    } catch (e) { isAdmin = false; }
    try {
      const mr = await fetch('/agents');
      if (mr.ok) { const d = await mr.json(); mandatesEnabled = !!d.enabled; }
    } catch (e) { mandatesEnabled = false; }
    applyGating();
  }
  function applyGating() {
    // Agents: admin + hosted backend. API: admin (partner keys need the backend;
    // reference is informational). Member/companion fall back to Labs.
    const show = { labs: true, agents: isAdmin && mandatesEnabled, api: isAdmin };
    $$('.ws-opt').forEach((t) => t.classList.toggle('locked', !show[t.dataset.ws]));
    if (!show[workspace]) setWorkspace('labs');
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

  function ensureAgents() { if (!agentsData) loadAgents(); else paintAgents(); }
  async function loadAgents() {
    const host = document.getElementById('ws-agents');
    host.innerHTML = '<div class="ws-grid"><div class="ws-main"><div class="main-pad"><div class="empty"><h3>Loading…</h3></div></div></div></div>';
    try {
      const r = await fetch('/agents');
      agentsData = r.ok ? await r.json() : { enabled: false, agents: [], roles: [], ceilings: {} };
    } catch (e) { agentsData = { enabled: false, agents: [], roles: [], ceilings: {} }; }
    paintAgents();
  }
  // which sub-view is active in Agents
  let agView = 'list';
  function paintAgents() {
    const host = document.getElementById('ws-agents');
    const ags = (agentsData && agentsData.agents) || [];
    const roles = (agentsData && agentsData.roles) || [];
    host.innerHTML = `
      <div class="ws-grid" style="grid-template-columns:268px 1fr;">
        <div class="ws-rail">
          <div class="rail-head"><h2>Agents</h2><span class="n">admin</span></div>
          <div class="rail-sect-lab" style="padding-left:22px;">Governance</div>
          <div class="rail-sect"><button class="rail-item ag-nav ${agView==='limits'?'on':''}" data-view="limits"><span class="ri-name">Account Limits</span><span class="ri-meta">org ceilings · motor + spend</span></button></div>
          <div class="rail-sect-lab" style="padding-left:22px;">Library</div>
          <div class="rail-sect"><button class="rail-item ag-nav ${agView==='roles'?'on':''}" data-view="roles"><span class="ri-name">Roles</span><span class="ri-meta">${roles.length} reusable job spec${roles.length===1?'':'s'}</span></button></div>
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
    else renderAgentsList(main);
  }
  function railAgent(a) {
    const sc = a.enabled === false ? 'var(--ink-4)' : 'var(--ok)';
    return `<button class="rail-item rail-agent" data-agent="${esc(a.agent_id)}"><span class="ri-name"><span class="dot-status" style="background:${sc}"></span>${esc(a.name || a.agent_id)}</span><span class="ri-meta">${esc(a.agent_id)}</span></button>`;
  }
  function personaName(slug) {
    const p = (window.SETTINGS && window.SETTINGS.personas || []).find(x => personaSlug(x.id) === slug);
    return p ? p.name : slug;
  }
  function personaSlug(id) { return String(id || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'default'; }

  function renderAgentsList(main) {
    const ags = (agentsData && agentsData.agents) || [];
    main.innerHTML = `<div class="main-pad" style="max-width:none;">
      <div class="page-eyebrow">Agents · deployment layer</div>
      <div class="page-title">Agents</div>
      <p class="page-lede">An agent is a persona paired with a role — a job with its own instructions, permissions, and spend caps. The same persona can power many agents; each is reachable through the engine API.</p>
      <div class="ag-table" style="margin-top:26px;">
        <div class="ag-table-head"><span>Agent</span><span>Persona × Role</span><span>Status</span><span>Permissions</span><span></span></div>
        ${ags.map(a => agentRow(a)).join('') || '<div style="padding:22px; text-align:center;" class="data">No agents yet — pair a persona with a role.</div>'}
      </div></div>`;
    main.querySelectorAll('.ag-row').forEach(r => r.addEventListener('click', () => { agentSel = r.dataset.agent; agView = 'detail'; paintAgents(); }));
  }
  function agentRow(a) {
    const sc = a.enabled === false ? 'var(--ink-4)' : 'var(--ok)';
    const status = a.enabled === false ? 'paused' : 'live';
    const nperm = a.permissions && typeof a.permissions === 'object' ? Object.keys(a.permissions).length : 0;
    return `<button class="ag-row" data-agent="${esc(a.agent_id)}">
      <span><span class="serif-h" style="font-size:15.5px;">${esc(a.name || a.agent_id)}</span><span class="data" style="font-size:9px; display:block; margin-top:2px;">${esc(a.agent_id)}</span></span>
      <span class="ar-px"><span class="chip persona" style="padding:3px 8px;"><span class="dot"></span>${esc(personaName(a.persona))}</span><span class="chip role" style="padding:3px 8px;"><span class="dot"></span>${esc(a.mandate_id)}</span></span>
      <span class="ar-status"><span class="dot-status" style="background:${sc}"></span>${status}</span>
      <span class="data" style="font-size:11px;">${nperm ? nperm + ' override' + (nperm===1?'':'s') : 'inherits'}</span>
      <span class="ar-chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 18 6-6-6-6"/></svg></span>
    </button>`;
  }

  function renderAgentDetail(main) {
    const a = ((agentsData && agentsData.agents) || []).find(x => x.agent_id === agentSel);
    const ceilings = (agentsData && agentsData.ceilings) || {};
    if (!a) { main.innerHTML = '<div class="main-pad"><div class="empty"><h3>Agent not found</h3></div></div>'; return; }
    const perms = (a.permissions && typeof a.permissions === 'object') ? { ...a.permissions } : {};
    main.innerHTML = `<div class="main-pad" style="max-width:760px;">
      <button class="link ag-back" style="margin-bottom:18px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg> All agents</button>
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
        <button class="link" id="ag-view-persona" style="margin-top:8px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M7 17 17 7M7 7h10v10"/></svg> View persona in Labs</button>
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
      <div class="row" style="margin-top:16px; gap:10px;">
        <button class="btn btn-primary" id="ag-save"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Save</button>
        <button class="btn" id="ag-remove">Remove agent</button>
      </div></div>`;
    const body = main.querySelector('#ag-perm-body');
    AGENT_PERM_FIELDS.forEach(f => body.appendChild(permRow(f, perms, ceilings)));
    main.querySelector('.ag-back').addEventListener('click', () => { agView = 'list'; agentSel = null; paintAgents(); });
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
      agentSel = null; agView = 'list'; await loadAgents();
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
    main.querySelectorAll('.role-pick').forEach(p => p.addEventListener('click', () => { main.querySelectorAll('.role-pick').forEach(x => x.classList.remove('on')); p.classList.add('on'); openRole(main, p.dataset.id); }));
    main.querySelector('#role-new').addEventListener('click', () => {
      const id = (window.prompt('New role id (lowercase letters, digits, "_" or "-"):', '') || '').trim();
      if (!id) return;
      if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(id)) { window.alert('Invalid id.'); return; }
      openRole(main, id, '');
    });
    if (roles.length) { main.querySelector('.role-pick')?.classList.add('on'); openRole(main, roles[0].id); }
  }
  function openRole(main, id, forceText) {
    const roles = (agentsData && agentsData.roles) || [];
    const role = roles.find(r => r.id === id);
    const text = forceText != null ? forceText : (role ? role.role_text || '' : '');
    const ed = main.querySelector('#role-editor');
    ed.innerHTML = `<div class="md-editor">
      <div class="md-bar"><span class="data" style="font-size:9px;">${esc(id)}.md</span><button class="btn btn-sm" id="role-save">Save role</button></div>
      <textarea class="md-area" id="role-text" spellcheck="false" placeholder="Describe this role — the job, tone, and rules for it…">${esc(text)}</textarea>
    </div>`;
    ed.querySelector('#role-save').addEventListener('click', async () => {
      try {
        const r = await fetch('/mandates', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, role_text: ed.querySelector('#role-text').value }) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
        await loadAgents();
      } catch (e) { window.alert('Could not save role: ' + e.message); }
    });
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
    main.innerHTML = `<div class="main-pad" style="max-width:720px;">
      <div class="page-eyebrow">Governance · org-level</div>
      <div class="page-title">Account Limits</div>
      <p class="page-lede">The ceilings every agent is bounded by. Set the maximum motor reach and operational spend for the whole organization; per-agent editors can grant any value up to these — never beyond.</p>
      <div class="card" style="margin-top:24px;">
        <div class="card-head"><span class="ch-num">A</span><div><div class="ch-title">Account ceilings</div><div class="ch-desc">the outer bound on what any agent may touch</div></div></div>
        <div class="card-body" id="limit-body"></div>
      </div>
      <div class="row" style="margin-top:16px;"><button class="btn btn-primary" id="limit-save"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Save limits</button></div></div>`;
    const body = main.querySelector('#limit-body');
    LIMIT_FIELDS.forEach(f => {
      const cur = ceilings[f.key];
      const row = document.createElement('div'); row.className = 'ctrl';
      row.innerHTML = `<div class="ctrl-meta"><div class="lab">${f.label}</div><div class="hint">${f.hint}</div></div>`;
      const field = document.createElement('div'); field.className = 'ctrl-field'; field.style.justifyContent = 'flex-end';
      let input;
      if (f.type === 'bool') {
        const on = !!(typeof cur === 'string' ? +cur : cur);
        input = document.createElement('div'); input.className = 'toggle' + (on ? ' on' : '');
        input.addEventListener('click', () => { const nv = !input.classList.contains('on'); input.classList.toggle('on', nv); patch[f.key] = nv ? 1 : 0; });
      } else if (f.type === 'dirs') {
        input = document.createElement('textarea'); input.className = 'ctrl-input'; input.value = cur || '';
        input.addEventListener('input', () => { patch[f.key] = input.value; });
        field.style.width = '100%';
      } else {
        input = document.createElement('input'); input.type = 'number'; input.className = 'ctrl-input'; input.step = 'any'; input.value = (cur ?? '');
        input.addEventListener('input', () => { patch[f.key] = input.value === '' ? 0 : +input.value; });
      }
      field.appendChild(input); row.appendChild(field); body.appendChild(row);
    });
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

  function openNewAgent() {
    const personas = (window.SETTINGS && window.SETTINGS.personas) || [];
    const roles = (agentsData && agentsData.roles) || [];
    if (!roles.length) { window.alert('Create a role first (Roles).'); return; }
    const modal = document.getElementById('ws-new-agent-modal');
    let pSel = personas[0] ? personas[0].id : '';
    let rSel = roles[0] ? roles[0].id : '';
    const draw = () => {
      modal.innerHTML = `<div class="modal">
        <div class="modal-head"><div class="serif-h" style="font-size:19px;">New agent</div><button class="tool-x" id="na-x"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>
        <p class="page-lede" style="margin-top:4px; font-size:14px;">Pair a persona with a role. The id is derived automatically.</p>
        <div style="margin-top:18px;"><div class="label" style="margin-bottom:9px;">Persona</div><div class="pick-row" id="na-personas">${personas.map(p => `<button class="pick ${p.id===pSel?'on':''}" data-id="${esc(p.id)}">${esc(p.name)}</button>`).join('')}</div>
        <div class="label" style="margin:18px 0 9px;">Role</div><div class="pick-row" id="na-roles">${roles.map(r => `<button class="pick ${r.id===rSel?'on':''}" data-id="${esc(r.id)}">${esc(r.id)}</button>`).join('')}</div>
        <div class="id-preview"><span class="label">Derived id</span><span class="data" style="color:var(--signal); font-size:13px;">${esc(personaSlug(pSel))}.${esc(rSel)}</span></div></div>
        <div class="row" style="justify-content:flex-end; margin-top:22px; gap:10px;"><button class="btn" id="na-cancel">Cancel</button><button class="btn btn-primary" id="na-create">Create agent</button></div></div>`;
      modal.querySelectorAll('#na-personas .pick').forEach(b => b.addEventListener('click', () => { pSel = b.dataset.id; draw(); }));
      modal.querySelectorAll('#na-roles .pick').forEach(b => b.addEventListener('click', () => { rSel = b.dataset.id; draw(); }));
      modal.querySelector('#na-x').addEventListener('click', close);
      modal.querySelector('#na-cancel').addEventListener('click', close);
      modal.querySelector('#na-create').addEventListener('click', create);
    };
    const close = () => { modal.classList.remove('open'); modal.innerHTML = ''; };
    const create = async () => {
      try {
        const r = await fetch('/agents', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ persona: pSel, mandate_id: rSel }) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
        close(); agentSel = `${personaSlug(pSel)}.${rSel}`; agView = 'detail'; await loadAgents();
      } catch (e) { window.alert('Could not create agent: ' + e.message); }
    };
    draw(); modal.classList.add('open');
    modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  }

  // ══════════════════════════════════════════════════════════ API ═════════
  let apiView = 'reference';
  function ensureApi() { renderApi(); if (apiView === 'partner') loadPartnerKeys(); }
  function renderApi() {
    const host = document.getElementById('ws-api');
    host.innerHTML = `<div class="ws-grid" style="grid-template-columns:256px 1fr;">
      <div class="ws-rail">
        <div class="rail-head"><h2>API</h2><span class="n">integration</span></div>
        <div class="rail-sect">
          <button class="rail-item api-nav ${apiView==='reference'?'on':''}" data-view="reference"><span class="ri-name">API Reference</span><span class="ri-meta">engine endpoints</span></button>
          <button class="rail-item api-nav ${apiView==='partner'?'on':''}" data-view="partner"><span class="ri-name">Partner Keys</span><span class="ri-meta">customer-facing tokens</span></button>
        </div>
      </div>
      <div class="ws-main" id="api-main"></div></div>`;
    host.querySelectorAll('.api-nav').forEach(n => n.addEventListener('click', () => { apiView = n.dataset.view; ensureApi(); }));
    const main = host.querySelector('#api-main');
    if (apiView === 'partner') renderPartnerKeys(main); else renderReference(main);
  }
  const ENDPOINTS = [
    { m: 'post', p: '/v1/sessions', t: 'Start a session for an end-user on an agent.' },
    { m: 'post', p: '/v1/sessions/{id}/turns', t: 'Run one turn; returns the response + mood (and a confirmation block if a write is pending).' },
    { m: 'post', p: '/v1/sessions/{id}/turns/stream', t: 'Stream the turn over SSE — inner thoughts + mood deltas, then a final done event.', tag: 'SSE' },
    { m: 'post', p: '/v1/sessions/{id}/confirm', t: 'Approve or discard a pending cloud-write action.' },
    { m: 'get', p: '/v1/agents', t: 'List the org\'s agents and the account permission ceilings.' },
    { m: 'post', p: '/v1/partner_keys', t: 'Mint a partner key (the token is returned once).' },
    { m: 'del', p: '/v1/end_users/{id}', t: 'Erase one end-user\'s memory + state (owner key).' },
  ];
  function renderReference(main) {
    main.innerHTML = `<div style="display:grid; grid-template-columns:300px 1fr; grid-template-rows:minmax(0,1fr); height:100%;">
      <div class="ws-rail" style="border-right:1px solid var(--line-soft);">
        <div class="rail-head"><h2>Reference</h2><span class="n">v1</span></div>
        <div class="rail-sect" id="ep-list">${ENDPOINTS.map((e, i) => `<button class="rail-item ep-item ${i===0?'on':''}" data-i="${i}" style="padding:9px 14px;"><span class="ri-name" style="font-size:12px; gap:9px;"><span class="method ${e.m}">${e.m.toUpperCase()}</span><span class="data" style="font-size:11px;">${esc(e.p)}</span></span>${e.tag?`<span class="ri-meta" style="margin-left:auto;">${e.tag}</span>`:''}</button>`).join('')}</div>
      </div>
      <div class="ws-main"><div class="main-pad" style="max-width:680px;" id="ep-detail"></div></div></div>`;
    const detail = main.querySelector('#ep-detail');
    const show = (i) => {
      const e = ENDPOINTS[i];
      detail.innerHTML = `<div class="row" style="gap:12px;"><span class="method ${e.m}">${e.m.toUpperCase()}</span><span class="data" style="font-size:15px; color:var(--ink);">${esc(e.p)}</span>${e.tag?`<span class="chip">${e.tag}</span>`:''}</div>
        <p class="page-lede">${esc(e.t)}</p>
        <div class="note" style="margin-top:18px;"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 16v-4M12 8h.01"/><circle cx="12" cy="12" r="10"/></svg><p>All engine routes are authenticated with a <b>partner key</b> (<span class="data" style="font-size:11px;">Authorization: Bearer ely_pk_…</span>). Mint one under <b>Partner Keys</b>.</p></div>
        <div class="label" style="margin:22px 0 8px;">Example</div>
        <div class="code"><span class="k">${e.m.toUpperCase()}</span> ${esc(e.p)}\n<span class="k">Authorization</span>: Bearer <span class="p">ely_pk_•••</span>${e.m==='post'&&e.p==='/v1/sessions'?`\n\n{\n  <span class="k">"agent_id"</span>: <span class="s">"the_visionary.research_lead"</span>,\n  <span class="k">"end_user_id"</span>: <span class="s">"u_8821"</span>\n}`:''}</div>`;
    };
    main.querySelectorAll('.ep-item').forEach(b => b.addEventListener('click', () => { main.querySelectorAll('.ep-item').forEach(x => x.classList.remove('on')); b.classList.add('on'); show(+b.dataset.i); }));
    show(0);
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
    wireSwitcher();
    loadGating();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
