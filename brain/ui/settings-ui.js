/* =====================================================================
   BRAIN SETTINGS — UI engine (live-wired)
   Renders the category rail + section cards from window.SETTINGS and wires
   sliders/toggles, master couplings, per-section reset, unsaved tracking.

   Adapted from the Claude Design redesign to the LIVE app:
     • real GET/POST /settings, POST /settings/reset, POST /restart
     • master-expressiveness coupling ported from the app (design omitted it)
     • real persona system (chemistry profiles, voice list mirrored from the
       header voice picker, decay-model toggle, header-badge sync)
   Self-contained: owns its own value state and reads the server as source of
   truth, so it never fights the header persona picker (which restarts + reloads).
   ===================================================================== */
(function () {
  const S = window.SETTINGS;
  const cats = S.categories;

  // ---- real persona chemistry profiles (mirror brain/run.py PERSONAS) ----
  const PERSONA_CHEM = {
    'The Visionary': { DA:0.62, ACh:0.45, GABA:0.12, Glu:0.40, NE:0.35, '5HT':0.55, CORT:0.05, OXT:0.45, AEA:0.20 },
    'The Empath':    { DA:0.45, ACh:0.18, GABA:0.12, Glu:0.18, NE:0.15, '5HT':0.70, CORT:0.03, OXT:0.70, AEA:0.45 },
    'The Analyst':   { DA:0.35, ACh:0.35, GABA:0.30, Glu:0.25, NE:0.25, '5HT':0.55, CORT:0.14, OXT:0.22, AEA:0.30 },
    'The Poet':      { DA:0.32, ACh:0.55, GABA:0.12, Glu:0.38, NE:0.42, '5HT':0.28, CORT:0.15, OXT:0.22, AEA:0.38 },
    'The Sage':      { DA:0.35, ACh:0.18, GABA:0.28, Glu:0.12, NE:0.12, '5HT':0.72, CORT:0.03, OXT:0.50, AEA:0.55 },
  };
  const PERSONA_CHANNELS = ['DA','ACh','GABA','Glu','NE','5HT','CORT','OXT','AEA'];
  const personaSlug = (name) => 'persona_voice_' + String(name).toLowerCase().replace(/\s+/g,'_').replace(/[^a-z0-9_]/g,'');

  // ---- inline icon set --------------------------------------------------
  const ICONS = {
    user:  '<circle cx="12" cy="8" r="4"/><path d="M4 20c0-4.2 3.6-6.5 8-6.5s8 2.3 8 6.5"/>',
    key:   '<circle cx="7.5" cy="15.5" r="4.5"/><path d="M10.7 12.3 20 3"/><path d="M16 7l3 3M14 9l2 2"/>',
    flask: '<path d="M9 3v5.5L4.2 17a2 2 0 0 0 1.8 3h12a2 2 0 0 0 1.8-3L15 8.5V3"/><line x1="8" y1="3" x2="16" y2="3"/><line x1="7.2" y1="14" x2="16.8" y2="14"/>',
    cpu:   '<rect x="7" y="7" width="10" height="10" rx="1.5"/><rect x="10" y="10" width="4" height="4" rx="0.5"/><path d="M10 3v2M14 3v2M10 19v2M14 19v2M3 10h2M3 14h2M19 10h2M19 14h2"/>',
    mic:   '<rect x="9" y="2" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><line x1="12" y1="18" x2="12" y2="21"/>',
    eye:   '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>',
    moon:  '<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/>',
    reset: '<polyline points="1 4 1 10 7 10"/><path d="M3.5 15a9 9 0 1 0 2.1-9.4L1 10"/>',
    chev:  '<polyline points="9 6 15 12 9 18"/>',
  };
  const svg = (d, w) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${w||2}" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;

  // ---- state ------------------------------------------------------------
  const defaults = {};   // key -> default value
  const values   = {};   // key -> current value
  const saved    = {};   // key -> last-saved value
  const meta     = {};   // key -> row config
  const sectionOf = {};  // key -> section id
  const catOf     = {};  // key -> category id
  let   secretsSet = {}; // key -> bool (which API keys are already stored)

  const PSEUDO = { persona_select: '', persona_voice: '' };

  cats.forEach(cat => {
    (cat.sections || []).forEach(sec => {
      [...(sec.rows || []), ...(sec.advanced || [])].forEach(r => {
        if (!r.key || r.type === 'group') return;
        defaults[r.key] = r.def; values[r.key] = r.def; saved[r.key] = r.def;
        meta[r.key] = r; sectionOf[r.key] = sec.id; catOf[r.key] = cat.id;
      });
    });
  });
  Object.entries(PSEUDO).forEach(([k, v]) => { defaults[k] = v; values[k] = v; saved[k] = v; catOf[k] = 'persona'; });

  const VIRTUAL = (k) => k.startsWith('master-');
  const isDirty = (k) => values[k] !== saved[k];
  const eqDef   = (k) => Math.abs((+values[k]) - (+defaults[k])) < 1e-9 || values[k] === defaults[k];

  // ---- value formatting -------------------------------------------------
  function humanizeSeconds(v) {
    v = +v;
    if (v < 60) return v + 's';
    if (v < 3600) { const m = v / 60; return (Number.isInteger(m) ? m : m.toFixed(1)) + ' min'; }
    const h = v / 3600; return (Number.isInteger(h) ? h : h.toFixed(1)) + ' h';
  }
  function fmt(r, v) {
    if (r.type === 'toggle') return (+v >= 0.5) ? 'On' : 'Off';
    if (r.type === 'time') return humanizeSeconds(v);
    const step = +r.step;
    if (Number.isInteger(step) && step >= 1) { const n = Math.round(+v); return n >= 1000 ? n.toLocaleString() : String(n); }
    const num = +v; return num < 0.1 ? num.toFixed(3) : num.toFixed(2);
  }

  const reg = {};        // key -> { input, valEl, rowEl, toggle }
  const sectionEls = {}; // secId -> { badge, resetBtn, keys, advKeys, advChanged }
  const navEls = {};     // catId -> { btn, dot }
  let dirtyPill, dirtyText, saveBtn, restartBanner, scroll;
  let activeCat = cats[0].id;

  // =====================================================================
  //  NETWORK — real load / save / reset / restart
  // =====================================================================
  function recomputeVirtualSeeds() {
    // master virtual sliders are seeded from their children when rendered;
    // nothing persistent needed here.
  }

  async function loadFromServer() {
    try {
      const res = await fetch('/settings');
      if (!res.ok) return;
      const data = await res.json();
      const s = data.settings || {};
      const d = data.defaults || {};
      secretsSet = data.secrets_set || {};
      Object.keys(s).forEach(k => { values[k] = s[k]; saved[k] = s[k]; });
      Object.keys(d).forEach(k => { defaults[k] = d[k]; });
      // persona pseudo keys reflect real keys
      values.persona_select = saved.persona_select = s.persona_name || '';
      values.persona_voice  = saved.persona_voice  = s.persona_voice_id || '';
    } catch (e) {
      console.warn('Settings: load failed', e);
    }
    recomputeVirtualSeeds();
    renderCat(activeCat);
    refreshDirty();
  }

  function realChangedPatch() {
    const patch = {};
    Object.keys(values).forEach(k => {
      if (VIRTUAL(k) || k === 'persona_select' || k === 'persona_voice') return;
      if (isDirty(k)) patch[k] = values[k];
    });
    return patch;
  }

  async function doSave() {
    const patch = realChangedPatch();
    if (!Object.keys(patch).length) return;
    saveBtn.textContent = 'Saving…';
    try {
      const res = await fetch('/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
      });
      if (res.ok) {
        Object.keys(values).forEach(k => saved[k] = values[k]);
        Object.keys(reg).forEach(refreshRow);
        Object.keys(sectionEls).forEach(refreshSection);
        refreshDirty();
        if (restartBanner) restartBanner.classList.add('on');
        saveBtn.textContent = 'Saved ✓';
        setTimeout(() => { saveBtn.textContent = 'Save Settings'; }, 1600);
      } else {
        saveBtn.textContent = 'Error ' + res.status;
        setTimeout(() => { saveBtn.textContent = 'Save Settings'; }, 2200);
      }
    } catch (e) {
      console.error('Settings save error', e);
      saveBtn.textContent = 'Error (no server)';
      setTimeout(() => { saveBtn.textContent = 'Save Settings'; }, 2200);
    }
  }

  async function doResetAll() {
    try {
      const res = await fetch('/settings/reset', { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        const s = data.settings || {};
        Object.keys(s).forEach(k => { values[k] = s[k]; saved[k] = s[k]; });
        Object.keys(defaults).forEach(k => { if (k in s) values[k] = s[k]; });
        values.persona_select = saved.persona_select = s.persona_name || '';
        values.persona_voice  = saved.persona_voice  = s.persona_voice_id || '';
        if (restartBanner) restartBanner.classList.add('on');
      }
    } catch (e) {
      // offline: fall back to in-memory defaults
      Object.keys(defaults).forEach(k => { values[k] = defaults[k]; });
    }
    selectCat(activeCat);
    refreshDirty();
  }

  async function doRestart() {
    if (!restartBanner || restartBanner.dataset.busy) return;
    restartBanner.dataset.busy = '1';
    restartBanner.textContent = 'Restarting…';
    restartBanner.style.opacity = '0.6';
    try { await fetch('/restart', { method: 'POST' }); } catch (_) {}
    const poll = setInterval(async () => {
      try {
        const r = await fetch('/settings', { method: 'GET' });
        if (r.ok) { clearInterval(poll); setTimeout(() => window.location.reload(), 400); }
      } catch (_) {}
    }, 800);
  }

  // ---- DOM build: nav ---------------------------------------------------
  function buildNav() {
    const nav = document.getElementById('rail-nav');
    nav.innerHTML = '';
    cats.forEach(cat => {
      const n = (cat.sections || []).length;
      const b = document.createElement('button');
      b.className = 'nav-item' + (cat.id === activeCat ? ' active' : '');
      b.innerHTML =
        `<span class="nav-ico">${svg(ICONS[cat.icon] || ICONS.cpu)}</span>` +
        `<span class="nav-label">${cat.name}</span>` +
        `<span class="nav-changed" data-navdot="${cat.id}"></span>` +
        `<span class="nav-count">${cat.id === 'persona' ? '·' : n}</span>`;
      b.addEventListener('click', () => selectCat(cat.id));
      nav.appendChild(b);
      navEls[cat.id] = { btn: b, dot: b.querySelector('[data-navdot]') };
    });
  }

  function selectCat(id) {
    activeCat = id;
    document.querySelectorAll('.nav-item').forEach((el, i) => el.classList.toggle('active', cats[i].id === id));
    renderCat(id);
    if (scroll) scroll.scrollTop = 0;
  }

  // ---- control row factory ----------------------------------------------
  function makeRow(r) {
    if (r.type === 'group') {
      const g = document.createElement('div');
      g.className = 'crow-group';
      g.innerHTML = `<span class="g-label">${r.label}</span>` + (r.hint ? `<span class="g-hint">${r.hint}</span>` : '');
      return g;
    }
    const row = document.createElement('div');
    row.className = 'crow' + (r.type === 'master' ? ' master' : '') + (r.type === 'toggle' ? ' toggle' : '') + (r.master ? ' master' : '');

    const metaEl = document.createElement('div');
    metaEl.className = 'crow-meta';
    const label = document.createElement('span');
    label.className = 'crow-label';
    label.innerHTML = `<span class="moddot"></span>${r.label}`;
    metaEl.appendChild(label);
    if (r.hint) { const h = document.createElement('span'); h.className = 'crow-hint'; h.textContent = r.hint; metaEl.appendChild(h); }
    row.appendChild(metaEl);

    const ctrl = document.createElement('div');
    ctrl.className = 'crow-control';

    if (r.type === 'apikey') {
      const input = document.createElement('input');
      input.type = 'password';
      input.autocomplete = 'off';
      input.spellcheck = false;
      input.value = values[r.key] || '';
      input.placeholder = secretsSet[r.key] ? '•••••••••• saved — leave blank to keep' : 'not set';
      input.style.cssText = 'flex:1 1 240px;min-width:200px;padding:7px 10px;border-radius:8px;border:1px solid rgba(255,255,255,0.15);background:rgba(255,255,255,0.05);color:inherit;font:inherit;';
      input.addEventListener('input', () => setValue(r.key, input.value));
      ctrl.appendChild(input);
      row.appendChild(ctrl);
      reg[r.key] = { rowEl: row, input, isText: true };
    } else if (r.type === 'toggle') {
      const tog = document.createElement('button');
      tog.className = 'tog' + (+values[r.key] >= 0.5 ? ' on' : '');
      tog.setAttribute('role', 'switch');
      tog.addEventListener('click', () => {
        const nv = (+values[r.key] >= 0.5) ? 0 : 1;
        setValue(r.key, nv);
        tog.classList.toggle('on', nv === 1);
      });
      ctrl.appendChild(tog);
      row.appendChild(ctrl);
      reg[r.key] = { rowEl: row, toggle: tog };
    } else {
      const input = document.createElement('input');
      input.type = 'range'; input.className = 'r';
      input.min = r.min; input.max = r.max; input.step = r.step;
      input.value = values[r.key];
      setFill(input, r, values[r.key]);
      input.addEventListener('input', () => { setValue(r.key, parseFloat(input.value)); applyCoupling(r.key); });
      ctrl.appendChild(input);
      const valEl = document.createElement('span'); valEl.className = 'crow-val'; valEl.textContent = fmt(r, values[r.key]);
      ctrl.appendChild(valEl);
      row.appendChild(ctrl);
      reg[r.key] = { rowEl: row, input, valEl };
    }
    refreshRow(r.key);
    return row;
  }

  function setFill(input, r, v) {
    const pct = ((+v - +r.min) / (+r.max - +r.min)) * 100;
    input.style.setProperty('--pct', Math.max(0, Math.min(100, pct)) + '%');
  }

  function setValue(key, v) {
    values[key] = v;
    const r = meta[key], e = reg[key];
    if (e) {
      if (e.input) { e.input.value = v; if (!e.isText) setFill(e.input, r, v); }
      if (e.valEl) e.valEl.textContent = fmt(r, v);
      if (e.toggle) e.toggle.classList.toggle('on', +v >= 0.5);
    }
    refreshRow(key);
    refreshSection(sectionOf[key]);
    refreshDirty();
  }

  function refreshRow(key) { const e = reg[key]; if (e && e.rowEl) e.rowEl.classList.toggle('changed', isDirty(key)); }

  function refreshSection(secId) {
    const se = sectionEls[secId]; if (!se) return;
    const dirtyN = se.keys.filter(isDirty).length;
    const offDef = se.keys.some(k => !eqDef(k));
    if (se.badge) { se.badge.classList.toggle('on', dirtyN > 0); if (dirtyN > 0) se.badgeText.textContent = dirtyN + ' unsaved'; }
    if (se.resetBtn) se.resetBtn.classList.toggle('on', offDef);
    if (se.advChanged) se.advChanged.classList.toggle('on', se.advKeys.some(isDirty));
  }

  function refreshDirty() {
    cats.forEach(cat => {
      const keys = Object.keys(catOf).filter(k => catOf[k] === cat.id);
      const d = keys.some(isDirty);
      if (navEls[cat.id]) navEls[cat.id].dot.classList.toggle('on', d);
    });
    const total = Object.keys(values).filter(isDirty).length;
    if (dirtyPill) { dirtyPill.classList.toggle('on', total > 0); dirtyText.textContent = total + ' unsaved'; }
    if (saveBtn) saveBtn.classList.toggle('idle', total === 0);
  }

  // ---- master couplings -------------------------------------------------
  const DECAY_KEYS = ['valence_to_DA_decay','threat_to_GABA_decay','novelty_to_ACh_decay','arousal_homeostat_decay','satiation_inhibitor_decay'];
  const DECAY_CENTER = 0.876;
  const SUPPRESS = { ach_suppression_weight: 1.0, glu_suppression_weight: 0.30 };
  const LR_DEF = 0.02, WS_DEF = 0.01, LR_COUPLE = 0.4;
  // master-expressiveness (ported from the app — the design omitted this)
  const VOICE_BASE = { stability: 0.45, style: 0.40, speed: 1.0 };
  const VOICE_OVERRIDES = {
    threat:   { voice_stability_threat:   0.65, voice_style_threat:   0.25, voice_speed_threat:   0.95 },
    bright:   { voice_stability_bright:   0.35, voice_style_bright:   0.55, voice_speed_bright:   1.05 },
    low_mood: { voice_stability_low_mood: 0.55, voice_style_low_mood: 0.30, voice_speed_low_mood: 0.93 },
  };
  const vmaster = {};

  function applyMasterHomeostasis(v) {
    const offset = v - DECAY_CENTER;
    DECAY_KEYS.forEach(k => {
      const r = meta[k]; if (!r) return;
      setValue(k, +Math.max(0.5, Math.min(0.99, r.def + offset)).toFixed(3));
    });
  }
  function applyMasterSuppression(scale) {
    for (const [k, base] of Object.entries(SUPPRESS)) {
      const r = meta[k]; if (!r) continue;
      setValue(k, +Math.max(+r.min, Math.min(+r.max, base * scale)).toFixed(3));
    }
  }
  function applyMasterExpressiveness(scale) {
    for (const overrideMap of Object.values(VOICE_OVERRIDES)) {
      for (const [k, overrideVal] of Object.entries(overrideMap)) {
        const dim = (k.match(/_(stability|style|speed)_/) || [])[1] || 'style';
        const baseKey = 'voice_' + dim + '_default';
        const baseVal = (baseKey in values) ? +values[baseKey] : VOICE_BASE[dim];
        const delta = overrideVal - VOICE_BASE[dim];
        const nv = Math.max(0, Math.min(1.5, baseVal + delta * scale));
        // voice override keys have no slider — write straight to values so Save sends them
        values[k] = +nv.toFixed(2);
      }
    }
    refreshDirty();
  }
  function applyCoupling(key) {
    if (key === 'hebbian_delta') {
      const ratio = values.hebbian_delta / LR_DEF;
      const r = meta.decay_toward_rest_rate;
      setValue('decay_toward_rest_rate', +Math.max(+r.min, Math.min(+r.max, WS_DEF / Math.pow(ratio || 1e-6, LR_COUPLE))).toFixed(3));
    } else if (key === 'decay_toward_rest_rate') {
      const ratio = WS_DEF / Math.max(0.001, values.decay_toward_rest_rate);
      const r = meta.hebbian_delta;
      setValue('hebbian_delta', +Math.max(+r.min, Math.min(+r.max, LR_DEF * Math.pow(ratio, LR_COUPLE))).toFixed(3));
    }
    if (DECAY_KEYS.includes(key) && vmaster['master-homeostasis']) {
      syncVMaster('master-homeostasis', DECAY_KEYS.reduce((a, k) => a + (+values[k]), 0) / DECAY_KEYS.length);
    }
    if (Object.keys(SUPPRESS).includes(key) && vmaster['master-suppression']) {
      syncVMaster('master-suppression', (+values.ach_suppression_weight) / SUPPRESS.ach_suppression_weight);
    }
  }
  function syncVMaster(id, v) {
    const m = vmaster[id]; if (!m) return;
    v = Math.max(+m.r.min, Math.min(+m.r.max, v));
    m.input.value = v; setFill(m.input, m.r, v);
    if (m.valEl) m.valEl.textContent = (+v).toFixed(m.r.step < 0.1 ? 3 : 2);
  }
  function makeVirtualMaster(r) {
    const row = document.createElement('div'); row.className = 'crow master';
    const metaEl = document.createElement('div'); metaEl.className = 'crow-meta';
    metaEl.innerHTML = `<span class="crow-label"><span class="moddot"></span>${r.label}</span>` + (r.hint ? `<span class="crow-hint">${r.hint}</span>` : '');
    row.appendChild(metaEl);
    const ctrl = document.createElement('div'); ctrl.className = 'crow-control';
    const input = document.createElement('input'); input.type = 'range'; input.className = 'r';
    input.min = r.min; input.max = r.max; input.step = r.step;
    let init = r.def;
    if (r.key === 'master-homeostasis') init = DECAY_KEYS.reduce((a,k)=>a+(+values[k]),0)/DECAY_KEYS.length;
    if (r.key === 'master-suppression') init = (+values.ach_suppression_weight) / SUPPRESS.ach_suppression_weight;
    input.value = init; setFill(input, r, init);
    ctrl.appendChild(input);
    const valEl = document.createElement('span'); valEl.className = 'crow-val'; valEl.textContent = (+init).toFixed(r.step < 0.1 ? 3 : 2);
    ctrl.appendChild(valEl);
    input.addEventListener('input', () => {
      const v = parseFloat(input.value); setFill(input, r, v); valEl.textContent = v.toFixed(r.step < 0.1 ? 3 : 2);
      if (r.key === 'master-homeostasis') applyMasterHomeostasis(v);
      else if (r.key === 'master-suppression') applyMasterSuppression(v);
      else if (r.key === 'master-expressiveness') applyMasterExpressiveness(v);
    });
    row.appendChild(ctrl);
    vmaster[r.key] = { input, valEl, r };
    return row;
  }

  // ---- section card -----------------------------------------------------
  function makeSection(sec) {
    const card = document.createElement('div'); card.className = 'scard fade-in';
    const head = document.createElement('div'); head.className = 'scard-head';
    head.innerHTML =
      `<span class="scard-num">${sec.num}</span>` +
      `<div class="scard-titles"><div class="scard-title">${sec.title}</div><div class="scard-desc">${sec.desc || ''}</div></div>` +
      `<div class="scard-tools">` +
        `<span class="changed-badge"><span class="chip"></span><span class="badge-text">0 unsaved</span></span>` +
        `<button class="scard-reset" title="Reset this section to defaults">${svg(ICONS.reset, 2)}</button>` +
        `<span class="scard-chev">${svg(ICONS.chev, 2.2)}</span>` +
      `</div>`;
    card.appendChild(head);

    const body = document.createElement('div'); body.className = 'scard-body';
    const allKeys = [...(sec.rows||[]), ...(sec.advanced||[])].filter(r => r.key && r.type !== 'group').map(r => r.key);
    const advKeys = (sec.advanced||[]).filter(r => r.key && r.type !== 'group').map(r => r.key);

    (sec.rows || []).forEach(r => body.appendChild((r.type === 'master' && r.virtual) ? makeVirtualMaster(r) : makeRow(r)));

    let advChanged = null;
    if ((sec.advanced || []).length) {
      const adv = document.createElement('div'); adv.className = 'adv';
      const cnt = (sec.advanced || []).filter(r => r.type !== 'group').length;
      const tog = document.createElement('button'); tog.className = 'adv-toggle';
      tog.innerHTML = `<span class="chev">${svg(ICONS.chev, 2.2)}</span><span>Advanced</span><span class="adv-changed"></span><span class="adv-count">${cnt}</span>`;
      advChanged = tog.querySelector('.adv-changed');
      const abody = document.createElement('div'); abody.className = 'adv-body';
      if (sec.custom === 'personaChem') renderChemCols(abody, sec.advanced);
      else (sec.advanced || []).forEach(r => abody.appendChild((r.type === 'master' && r.virtual) ? makeVirtualMaster(r) : makeRow(r)));
      tog.addEventListener('click', () => adv.classList.toggle('open'));
      adv.appendChild(tog); adv.appendChild(abody); body.appendChild(adv);
    }
    card.appendChild(body);

    head.addEventListener('click', (e) => { if (e.target.closest('.scard-reset')) return; card.classList.toggle('collapsed'); });
    const resetBtn = head.querySelector('.scard-reset');
    resetBtn.addEventListener('click', (e) => { e.stopPropagation(); resetSection(sec); });

    sectionEls[sec.id] = { badge: head.querySelector('.changed-badge'), badgeText: head.querySelector('.badge-text'), resetBtn, keys: allKeys, advKeys, advChanged };
    refreshSection(sec.id);
    return card;
  }

  function renderChemCols(container, rows) {
    // Consolidated view: one set of "resting chemistry" sliders (the baseline
    // trait). Boot levels follow the baseline by default; the rare case of a
    // different at-boot value lives behind a sub-disclosure.
    const baseRows = rows.filter(r => r.key && r.key.indexOf('chem_baseline_') === 0);
    const initRows = rows.filter(r => r.key && r.key.indexOf('chem_init_') === 0);

    // Start in "follow" mode unless the saved boot values already differ.
    let followBaseline = baseRows.every(r => {
      const ch = r.key.slice('chem_baseline_'.length);
      return +values['chem_init_' + ch] === +values['chem_baseline_' + ch];
    });

    const wrap = document.createElement('div'); wrap.className = 'chem-cols';
    const col = document.createElement('div');
    const h = document.createElement('div'); h.className = 'crow-group';
    h.innerHTML = `<span class="g-label">Resting chemistry — the trait the brain holds and relaxes toward</span>`;
    col.appendChild(h);
    baseRows.forEach(r => {
      const rowEl = makeRow(r);
      const input = rowEl.querySelector('input.r');
      if (input) {
        const ch = r.key.slice('chem_baseline_'.length);
        input.addEventListener('input', () => {
          if (followBaseline) setValue('chem_init_' + ch, parseFloat(input.value));
        });
      }
      col.appendChild(rowEl);
    });
    wrap.appendChild(col);
    container.appendChild(wrap);

    if (!initRows.length) return;

    // Sub-disclosure: independent at-boot levels.
    const advWrap = document.createElement('div'); advWrap.className = 'chem-boot';
    const tog = document.createElement('button'); tog.className = 'chem-boot-toggle';
    tog.innerHTML = `<span class="chev">${svg(ICONS.chev, 2.2)}</span><span>Set boot levels separately</span>`;
    const bootBody = document.createElement('div'); bootBody.className = 'chem-boot-body';
    const note = document.createElement('div'); note.className = 'chem-boot-note';
    note.textContent = 'By default the brain boots at its resting baseline. Set different at-boot values here for a brain that starts elevated (or low) and settles.';
    bootBody.appendChild(note);
    initRows.forEach(r => bootBody.appendChild(makeRow(r)));
    advWrap.appendChild(tog); advWrap.appendChild(bootBody);

    if (!followBaseline) advWrap.classList.add('open');
    tog.addEventListener('click', () => {
      const willOpen = !advWrap.classList.contains('open');
      advWrap.classList.toggle('open', willOpen);
      followBaseline = !willOpen;
      // when re-following, snap boot back to the resting baseline
      if (followBaseline) {
        baseRows.forEach(r => {
          const ch = r.key.slice('chem_baseline_'.length);
          setValue('chem_init_' + ch, +values['chem_baseline_' + ch]);
        });
      }
    });
    container.appendChild(advWrap);
  }

  function resetSection(sec) {
    [...(sec.rows||[]), ...(sec.advanced||[])].forEach(r => { if (r.key && r.type !== 'group') setValue(r.key, r.def); });
    if (vmaster['master-homeostasis'] && sec.id === 'sec-2') syncVMaster('master-homeostasis', DECAY_CENTER);
    if (vmaster['master-suppression'] && sec.id === 'sec-4') syncVMaster('master-suppression', 1.0);
    if (vmaster['master-expressiveness'] && sec.id === 'sec-7') syncVMaster('master-expressiveness', 1.0);
  }

  // ---- persona category (LIVE) -----------------------------------------
  function _voiceOptions() {
    // Mirror the header voice picker's options so the catalog matches the app.
    const src = document.getElementById('voice-select');
    const out = [];
    if (src) [...src.options].forEach(o => { if (o.value) out.push({ value: o.value, label: o.textContent }); });
    return out;
  }
  function _updateHeaderBadge(name) {
    const badge = document.getElementById('persona-header-badge');
    if (!badge) return;
    if (name) { badge.textContent = name.replace(/^The\s+/i, ''); badge.classList.remove('neutral'); }
    else { badge.textContent = 'Default'; badge.classList.add('neutral'); }
  }

  function renderPersona(cat, grid) {
    // intro cards
    const intro = document.createElement('div'); intro.className = 'persona-intro fade-in';
    const cardsWrap = document.createElement('div'); cardsWrap.className = 'persona-cards';
    function selectPersona(p) {
      values.persona_select = p ? p.id : '';
      values.persona_name = p ? p.id : '';
      if (p) {
        const chem = PERSONA_CHEM[p.id] || {};
        PERSONA_CHANNELS.forEach(ch => {
          if ('chem_baseline_' + ch in values) setValue('chem_baseline_' + ch, chem[ch]);
          if ('chem_init_' + ch in values) setValue('chem_init_' + ch, chem[ch]);
        });
        if ('persona_born' in defaults || 'persona_born' in values) values.persona_born = new Date().toISOString();
      } else {
        PERSONA_CHANNELS.forEach(ch => {
          if ('chem_baseline_' + ch in defaults) setValue('chem_baseline_' + ch, defaults['chem_baseline_' + ch]);
          if ('chem_init_' + ch in defaults) setValue('chem_init_' + ch, defaults['chem_init_' + ch]);
        });
        values.persona_born = '';
      }
      cardsWrap.querySelectorAll('.pcard').forEach(x => x.classList.toggle('sel', p && x.dataset.id === p.id));
      statusEl.innerHTML = p ? `Active persona: <b>${p.name}</b> — ${p.tag}` : `<b>Neutral</b> — default chemistry · no persona set`;
      _updateHeaderBadge(p ? p.id : '');
      refreshDirty();
    }
    S.personas.forEach(p => {
      const c = document.createElement('button');
      c.className = 'pcard' + (values.persona_select === p.id ? ' sel' : '');
      c.dataset.id = p.id;
      c.innerHTML = `<div class="pcard-name">${p.name}</div><div class="pcard-tag">${p.tag}</div><div class="pcard-note">${p.note}</div>`;
      c.addEventListener('click', () => selectPersona(p));
      cardsWrap.appendChild(c);
    });
    intro.appendChild(cardsWrap);
    grid.appendChild(intro);

    // bar: status + neutral + voice + decay model
    const bar = document.createElement('div'); bar.className = 'persona-bar fade-in';
    const cur = S.personas.find(p => p.id === values.persona_select);
    var statusEl = document.createElement('div'); statusEl.className = 'persona-status';
    statusEl.innerHTML = cur ? `Active persona: <b>${cur.name}</b> — ${cur.tag}` : `<b>Neutral</b> — default chemistry · no persona set`;
    bar.appendChild(statusEl);

    const neutralBtn = document.createElement('button'); neutralBtn.className = 'persona-neutral-btn'; neutralBtn.textContent = 'Return to neutral';
    neutralBtn.addEventListener('click', () => selectPersona(null));
    bar.appendChild(neutralBtn);

    const sp = document.createElement('div'); sp.className = 'spacer'; bar.appendChild(sp);

    // (Floor-decay mode was removed — chemistry always decays toward baseline.)

    // voice (real catalog, mirrored from header)
    const voiceField = document.createElement('div'); voiceField.className = 'persona-field';
    voiceField.innerHTML = `<label>Voice</label>`;
    const sel = document.createElement('select'); sel.className = 'sel-input';
    const noOpt = document.createElement('option'); noOpt.value = ''; noOpt.textContent = '— no preference —'; sel.appendChild(noOpt);
    _voiceOptions().forEach(v => { const o = document.createElement('option'); o.value = v.value; o.textContent = v.label; sel.appendChild(o); });
    sel.value = values.persona_voice || '';
    sel.addEventListener('change', () => {
      values.persona_voice = sel.value;
      values.persona_voice_id = sel.value;
      const name = values.persona_name || values.persona_select || '';
      if (name) values[personaSlug(name)] = sel.value;
      refreshDirty();
    });
    voiceField.appendChild(sel);
    bar.appendChild(voiceField);

    grid.appendChild(bar);
  }

  // ---- category render --------------------------------------------------
  function renderCat(id) {
    const cat = cats.find(c => c.id === id);
    document.getElementById('bar-title').textContent = cat.name;
    document.getElementById('bar-blurb').textContent = cat.blurb || '';
    const wrap = document.getElementById('cat-wrap'); wrap.innerHTML = '';
    const grid = document.createElement('div'); grid.className = 'cat-grid';

    if (cat.summary) {
      const s = document.createElement('div'); s.className = 'cat-summary fade-in';
      s.innerHTML = `<div class="ico">${svg(ICONS[cat.icon] || ICONS.cpu)}</div><div class="txt"><div class="h">In plain language</div><p>${cat.summary}</p></div>`;
      grid.appendChild(s);
    }
    if (cat.custom === 'persona') renderPersona(cat, grid);
    (cat.sections || []).forEach(sec => grid.appendChild(makeSection(sec)));
    wrap.appendChild(grid);
    refreshDirty();
  }

  // ---- boot -------------------------------------------------------------
  function boot() {
    if (!document.getElementById('rail-nav')) return; // settings markup not present
    dirtyPill = document.getElementById('dirty-pill');
    dirtyText = document.getElementById('dirty-text');
    saveBtn = document.getElementById('settings-save-btn');
    restartBanner = document.getElementById('settings-restart-banner');
    scroll = document.getElementById('scroll');

    buildNav();
    renderCat(activeCat);

    if (saveBtn) saveBtn.addEventListener('click', doSave);
    // NOTE: the restart banner's click is handled by the app's existing header
    // restart logic (it owns #settings-restart-banner). The engine only shows it.
    const resetAll = document.getElementById('settings-reset-btn');
    if (resetAll) resetAll.addEventListener('click', () => { if (confirm('Reset ALL settings across every category to their defaults?')) doResetAll(); });

    // Expose an open hook for the SPA: refetch live values whenever settings opens.
    window.__settingsUI = { open: loadFromServer, reload: loadFromServer };

    // Initial load (also runs if the page boots straight into settings).
    loadFromServer();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
