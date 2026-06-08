/* =====================================================================
   BRAIN SETTINGS — persona-first engine (live-wired)
   Adapted from the Elyceum "Persona & Temperament" redesign to the LIVE app:
     • Personas are the top-level entity (left rail); each settings category
       is a TAB within the active persona. The Temperament tab holds the
       nine radial trait dials (a macro layer over the chemistry); other tabs
       expose that persona's raw controls, read-only until Manual mode is on.
     • Real GET/POST /settings, POST /settings/reset; restart via the app's
       existing restart banner. Voice + mood + theme live in the app's rail
       chrome and are owned by index.html — this engine does not touch them.
   Renders into #rail-nav (persona menu) and #cat-wrap (persona view); wires
   the app's #settings-save-btn / #settings-reset-btn / #settings-restart-banner.
   ===================================================================== */
(function () {
  'use strict';
  const SET = window.SETTINGS;

  /* ---- personas + canonical chemistry (mirror brain/run.py PERSONAS) ---- */
  const PERSONAS = SET.personas.map(p => ({ ...p }));
  const PERSONA_CHEM = {
    'The Visionary': { DA: 0.62, ACh: 0.45, GABA: 0.12, Glu: 0.40, NE: 0.35, '5HT': 0.55, CORT: 0.05, OXT: 0.45, AEA: 0.20 },
    'The Empath':    { DA: 0.45, ACh: 0.18, GABA: 0.12, Glu: 0.18, NE: 0.15, '5HT': 0.70, CORT: 0.03, OXT: 0.70, AEA: 0.45 },
    'The Analyst':   { DA: 0.35, ACh: 0.35, GABA: 0.30, Glu: 0.25, NE: 0.25, '5HT': 0.55, CORT: 0.14, OXT: 0.22, AEA: 0.30 },
    'The Poet':      { DA: 0.32, ACh: 0.55, GABA: 0.12, Glu: 0.38, NE: 0.42, '5HT': 0.28, CORT: 0.15, OXT: 0.22, AEA: 0.38 },
    'The Sage':      { DA: 0.35, ACh: 0.18, GABA: 0.28, Glu: 0.12, NE: 0.12, '5HT': 0.72, CORT: 0.03, OXT: 0.50, AEA: 0.55 },
  };
  const CHANNELS = [
    { ch: 'DA', name: 'Dopamine' }, { ch: 'ACh', name: 'Acetylcholine' }, { ch: 'GABA', name: 'GABA' },
    { ch: 'Glu', name: 'Glutamate' }, { ch: 'NE', name: 'Norepinephrine' }, { ch: '5HT', name: 'Serotonin' },
    { ch: 'CORT', name: 'Cortisol' }, { ch: 'OXT', name: 'Oxytocin' }, { ch: 'AEA', name: 'Anandamide' },
  ];
  const CHEM_MIN = 0, CHEM_MAX = 0.8, CHEM_STEP = 0.01;

  /* ---- the nine trait dials. Each map row: { key, dir, span }. Every key
     is a real backend settings key, so every dial both moves a real control
     and produces a valid /settings patch. ---- */
  const TRAIT_DIALS = [
    { id: 'intelligence', label: 'Intelligence', sub: 'learning · reasoning', glyph: 'spark',
      map: [ { key: 'chem_baseline_ACh', dir: +1, span: 0.12 }, { key: 'surprise_ACh_weight', dir: +1, span: 0.05 }, { key: 'frontal_ach_weight', dir: +1, span: 0.10 }, { key: 'plasticity_arousal_weight', dir: +1, span: 0.10 }, { key: 'plasticity_intensity_weight', dir: +1, span: 0.08 } ] },
    { id: 'empathy', label: 'Empathy', sub: 'warmth · bonding', glyph: 'bond',
      map: [ { key: 'chem_baseline_OXT', dir: +1, span: 0.15 }, { key: 'chem_baseline_5HT', dir: +1, span: 0.10 }, { key: 'oxt_positive_increment', dir: +1, span: 0.006 }, { key: 'voice_style_default', dir: +1, span: 0.10 }, { key: 'chem_baseline_CORT', dir: -1, span: 0.04 } ] },
    { id: 'sensitivity', label: 'Sensitivity', sub: 'reactivity', glyph: 'ripple',
      map: [ { key: 'emotional_reactivity_scale', dir: +1, span: 0.40 }, { key: 'chem_baseline_NE', dir: +1, span: 0.12 }, { key: 'chem_baseline_Glu', dir: +1, span: 0.08 }, { key: 'plasticity_intensity_weight', dir: +1, span: 0.10 }, { key: 'chem_baseline_GABA', dir: -1, span: 0.06 } ] },
    { id: 'composure', label: 'Composure', sub: 'steadiness', glyph: 'level',
      map: [ { key: 'chem_baseline_GABA', dir: +1, span: 0.10 }, { key: 'threat_to_GABA_decay', dir: +1, span: 0.04 }, { key: 'chem_baseline_CORT', dir: -1, span: 0.06 }, { key: 'emotional_reactivity_scale', dir: -1, span: 0.30 }, { key: 'cort_threat_increment', dir: -1, span: 0.008 } ] },
    { id: 'drive', label: 'Drive', sub: 'ambition · reward', glyph: 'arrow',
      map: [ { key: 'chem_baseline_DA', dir: +1, span: 0.15 }, { key: 'valence_to_DA_decay', dir: +1, span: 0.03 }, { key: 'plasticity_arousal_weight', dir: +1, span: 0.10 }, { key: 'sentiment_DA_weight', dir: +1, span: 0.06 } ] },
    { id: 'creativity', label: 'Creativity', sub: 'associative play', glyph: 'star',
      map: [ { key: 'chem_baseline_ACh', dir: +1, span: 0.08 }, { key: 'chem_baseline_AEA', dir: +1, span: 0.12 }, { key: 'chem_baseline_GABA', dir: -1, span: 0.06 }, { key: 'dmn_overlap_threshold', dir: +1, span: 0.05 }, { key: 'surprise_ACh_weight', dir: +1, span: 0.04 } ] },
    { id: 'humor', label: 'Humor', sub: 'levity', glyph: 'smile',
      map: [ { key: 'chem_baseline_DA', dir: +1, span: 0.10 }, { key: 'chem_baseline_AEA', dir: +1, span: 0.10 }, { key: 'chem_baseline_ACh', dir: +1, span: 0.05 }, { key: 'chem_baseline_GABA', dir: -1, span: 0.05 }, { key: 'chem_baseline_CORT', dir: -1, span: 0.05 } ] },
    { id: 'sociability', label: 'Sociability', sub: 'outgoing · initiates', glyph: 'social',
      map: [ { key: 'dmn_interval', dir: -1, span: 8 }, { key: 'proactive_idle_threshold', dir: -1, span: 90 }, { key: 'ach_suppression_weight', dir: -1, span: 0.35 }, { key: 'voice_style_default', dir: +1, span: 0.08 } ] },
    { id: 'caution', label: 'Caution', sub: 'trusting ↔ guarded', glyph: 'shield',
      map: [ { key: 'hostility_GABA_threshold_high', dir: -1, span: 0.12 }, { key: 'cort_threat_increment', dir: +1, span: 0.012 }, { key: 'ne_hostility_weight', dir: +1, span: 0.06 }, { key: 'chem_baseline_OXT', dir: -1, span: 0.08 } ] },
  ];

  /* ---- cognitive-style dials — how the mind WORKS (vs temperament = who it
     is). A per-persona override that rests at NEUTRAL 0.5 (these mostly don't
     touch chemistry, so they aren't posed from the persona's chemistry spread).
     Same radial knobs, shown in a separate box below Temperament. ---- */
  const COGNITIVE_DIALS = [
    { id: 'focus', label: 'Focus', sub: 'scattered ↔ single-minded', glyph: 'target',
      map: [ { key: 'ne_scatter_threshold', dir: +1, span: 0.10 }, { key: 'topic_activation_decay', dir: +1, span: 0.12 }, { key: 'dmn_overlap_threshold', dir: +1, span: 0.10 }, { key: 'salience_workspace_threshold', dir: -1, span: 0.12 } ] },
    { id: 'curiosity', label: 'Curiosity', sub: 'novelty-seeking', glyph: 'compass',
      map: [ { key: 'frontal_ach_weight', dir: +1, span: 0.15 }, { key: 'surprise_threshold', dir: -1, span: 0.12 }, { key: 'salience_ACh_weight', dir: +1, span: 0.06 } ] },
    { id: 'adaptability', label: 'Adaptability', sub: 'stable ↔ adaptive', glyph: 'cycle',
      map: [ { key: 'hebbian_delta', dir: +1, span: 0.03 }, { key: 'hebbian_outcome_delta', dir: +1, span: 0.03 }, { key: 'gaba_skip_threshold_high', dir: +1, span: 0.12 } ] },
    { id: 'introspection', label: 'Introspection', sub: 'self-appraisal', glyph: 'spiral',
      map: [ { key: 'meta_interval', dir: -1, span: 15 }, { key: 'meta_cooldown_turns', dir: -1, span: 1.5 } ] },
    { id: 'memory', label: 'Memory', sub: 'in-the-moment ↔ recall', glyph: 'node',
      map: [ { key: 'hippocampus_priority_base', dir: +1, span: 0.18 }, { key: 'topic_activation_decay', dir: +1, span: 0.10 } ] },
    { id: 'emotionality', label: 'Emotionality', sub: 'logic ↔ feeling-driven', glyph: 'balance',
      map: [ { key: 'modulation_gain', dir: +1, span: 1.0 } ] },
  ];
  const ALL_DIALS = TRAIT_DIALS.concat(COGNITIVE_DIALS);

  const GLYPHS = {
    spark:  '<path d="M12 3v6M12 15v6M3 12h6M15 12h6M6.5 6.5l3.2 3.2M14.3 14.3l3.2 3.2M17.5 6.5l-3.2 3.2M9.7 14.3l-3.2 3.2"/>',
    bond:   '<circle cx="9" cy="12" r="5"/><circle cx="15" cy="12" r="5"/>',
    ripple: '<circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"/><path d="M7.5 12a4.5 4.5 0 0 1 9 0M4.5 12a7.5 7.5 0 0 1 15 0"/>',
    level:  '<line x1="4" y1="12" x2="20" y2="12"/><circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none"/>',
    arrow:  '<line x1="12" y1="20" x2="12" y2="5"/><polyline points="6 11 12 5 18 11"/>',
    star:   '<path d="M12 3v18M3 12h18M5.5 5.5l13 13M18.5 5.5l-13 13"/>',
    smile:  '<path d="M7 13a5 5 0 0 0 10 0"/><circle cx="9" cy="9" r="0.6" fill="currentColor" stroke="none"/><circle cx="15" cy="9" r="0.6" fill="currentColor" stroke="none"/>',
    social: '<path d="M4 5.5h16v10H10l-4 3.5v-3.5H4z"/><circle cx="9" cy="10.5" r="0.7" fill="currentColor" stroke="none"/><circle cx="12" cy="10.5" r="0.7" fill="currentColor" stroke="none"/><circle cx="15" cy="10.5" r="0.7" fill="currentColor" stroke="none"/>',
    shield: '<path d="M12 3l7 3v5c0 4.4-3 7.4-7 8.8C8 17.4 5 14.4 5 10V6l7-3z"/>',
    target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.4"/><circle cx="12" cy="12" r="0.6" fill="currentColor" stroke="none"/>',
    compass:'<circle cx="12" cy="12" r="9"/><polygon points="12 7 14 12 12 17 10 12" fill="currentColor" stroke="none"/>',
    cycle:  '<path d="M4 12a8 8 0 0 1 13.7-5.6L20 8"/><polyline points="20 3 20 8 15 8"/><path d="M20 12a8 8 0 0 1-13.7 5.6L4 16"/><polyline points="4 21 4 16 9 16"/>',
    spiral: '<path d="M12 12a2 2 0 1 1 2 2 4 4 0 0 1-4-4 6 6 0 0 1 6-6 8 8 0 0 1 8 8"/>',
    node:   '<circle cx="6" cy="6" r="2"/><circle cx="18" cy="9" r="2"/><circle cx="9" cy="18" r="2"/><line x1="7.7" y1="7.2" x2="16.4" y2="8.2"/><line x1="7.2" y1="7.6" x2="8.4" y2="16.4"/>',
    balance:'<line x1="12" y1="4" x2="12" y2="20"/><line x1="5" y1="8" x2="19" y2="8"/><path d="M5 8l-2.5 5a2.5 2.5 0 0 0 5 0z"/><path d="M19 8l-2.5 5a2.5 2.5 0 0 0 5 0z"/>',
  };
  const ico = d => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
  const chevSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>';
  const lockSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
  const eyeSvg  = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
  const resetSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.5 15a9 9 0 1 0 2.1-9.4L1 10"/></svg>';

  /* ---- key metadata (from the real settings model) ---- */
  const rowMeta = {};
  SET.categories.forEach(cat => (cat.sections || []).forEach(sec => [...(sec.rows || []), ...(sec.advanced || [])].forEach(r => { if (r.key && r.type !== 'group') rowMeta[r.key] = r; })));
  // dial keys that exist in settings.json but aren't surfaced as a UI row
  const FALLBACK = { dmn_overlap_threshold: { min: 0.1, max: 0.8, def: 0.35 }, modulation_gain: { min: 0, max: 2.0, def: 1.0 } };
  const keyMeta = k => rowMeta[k] || FALLBACK[k] || { min: 0, max: 1, def: 0 };
  const clampKey = (k, v) => { const m = keyMeta(k); return Math.max(+m.min, Math.min(+m.max, v)); };

  /* ---- state ---- */
  const values = {}, saved = {}, refDefault = {}, dialCenter = {}, dial = {}, rest = {};
  let secretsSet = {};
  let persona = PERSONAS[0].id;
  let activeTab = 'persona';
  let view = 'persona';                 // 'persona' | 'system'
  const manualState = {};
  let manualOpen = false;
  let scaffolded = false;
  let systemPage = null;                // 'apikeys' | 'operational' when view==='system'

  const allKeys = (() => { const s = new Set(); ALL_DIALS.forEach(d => d.map.forEach(t => s.add(t.key))); return [...s]; })();
  const isChem = k => k.indexOf('chem_baseline_') === 0;
  const chemOf = k => k.slice('chem_baseline_'.length);
  function personaBaseline(k) { return isChem(k) ? PERSONA_CHEM[persona][chemOf(k)] : refDefault[k]; }

  /* ---- dial rest positions from chemistry ----------------------------
     Where a persona's needle naturally sits on each temperament dial. This
     is COSMETIC (the needle home + readout + zero-offset center) — it never
     changes the underlying chemistry. Derived from an ABSOLUTE, multi-channel
     blend so no single neurochemical dominates (the old version keyed a dial's
     whole rest off one channel's percentile-vs-other-personas, which made e.g.
     Intelligence = acetylcholine rank → the Sage read "6"). Each entry is
     [channel, weight, dir]; dir -1 counts the channel's absence. A dial not in
     this table (the cognitive dials) rests at neutral 0.5. Higher reading =
     more of the trait — see each dial's sub-label for which pole is which.   */
  const REST_WEIGHTS = {
    intelligence: [['ACh', 0.40, +1], ['DA', 0.25, +1], ['5HT', 0.35, +1]],
    empathy:      [['OXT', 0.45, +1], ['5HT', 0.30, +1], ['CORT', 0.25, -1]],
    sensitivity:  [['NE', 0.35, +1], ['Glu', 0.30, +1], ['GABA', 0.20, -1], ['CORT', 0.15, +1]],
    composure:    [['GABA', 0.35, +1], ['5HT', 0.25, +1], ['CORT', 0.25, -1], ['NE', 0.15, -1]],
    drive:        [['DA', 0.55, +1], ['Glu', 0.25, +1], ['ACh', 0.20, +1]],
    creativity:   [['ACh', 0.35, +1], ['AEA', 0.30, +1], ['GABA', 0.20, -1], ['DA', 0.15, +1]],
    humor:        [['DA', 0.30, +1], ['AEA', 0.30, +1], ['GABA', 0.20, -1], ['CORT', 0.20, -1]],
    sociability:  [['OXT', 0.35, +1], ['DA', 0.30, +1], ['Glu', 0.20, +1], ['GABA', 0.15, -1]],
    caution:      [['OXT', 0.40, -1], ['CORT', 0.30, +1], ['GABA', 0.20, +1], ['NE', 0.10, +1]],
  };
  const nrm = v => Math.max(0, Math.min(1, (+v || 0) / 0.8));   // absolute channel scale
  function dialRest(personaId, d) {
    const w = REST_WEIGHTS[d.id]; const chem = PERSONA_CHEM[personaId];
    if (!w || !chem) return 0.5;
    let s = 0, tw = 0;
    w.forEach(([ch, wt, dir]) => { const x = dir > 0 ? nrm(chem[ch]) : 1 - nrm(chem[ch]); s += wt * x; tw += wt; });
    return tw ? Math.max(0, Math.min(1, s / tw)) : 0.5;
  }

  /* ---- recompute: dial positions -> real key values ---- */
  function recomputeTraits() {
    const offset = {}; allKeys.forEach(k => { offset[k] = 0; });
    ALL_DIALS.forEach(d => d.map.forEach(t => { offset[t.key] += t.dir * t.span * (dial[d.id] - rest[d.id]) * 2; }));
    allKeys.forEach(k => { values[k] = clampKey(k, dialCenter[k] + offset[k]); if (isChem(k)) values['chem_init_' + chemOf(k)] = values[k]; });
  }
  function dialOffsetFor(key) { let o = 0; ALL_DIALS.forEach(d => d.map.forEach(t => { if (t.key === key) o += t.dir * t.span * (dial[d.id] - rest[d.id]) * 2; })); return o; }
  const moved = id => Math.abs(dial[id] - rest[id]) > 1e-4;

  /* ---- seed dials/centers for the active persona ----
     snap=true (persona switch): chemistry snaps to the persona's canonical
     baseline. snap=false (initial load): keep the loaded values. ---- */
  function seedDials(snap) {
    if (snap) CHANNELS.forEach(c => { values['chem_baseline_' + c.ch] = PERSONA_CHEM[persona][c.ch]; values['chem_init_' + c.ch] = PERSONA_CHEM[persona][c.ch]; });
    ALL_DIALS.forEach(d => { rest[d.id] = dialRest(persona, d); dial[d.id] = rest[d.id]; });
    allKeys.forEach(k => { dialCenter[k] = (k in values) ? +values[k] : keyMeta(k).def; });
  }

  /* =====================================================================
     NETWORK — load / save / reset
     ===================================================================== */
  let saveBtn, resetBtn, restartBanner, dirtyPill, dirtyText, scroll;

  async function loadFromServer() {
    let s = {}, d = {};
    try {
      const res = await fetch('/settings');
      if (res.ok) { const data = await res.json(); s = data.settings || {}; d = data.defaults || {}; secretsSet = data.secrets_set || {}; }
    } catch (e) { console.warn('Settings: load failed', e); }
    // seed every known key from server (fallback to row default)
    Object.keys(rowMeta).forEach(k => {
      const def = (k in d) ? d[k] : rowMeta[k].def;
      refDefault[k] = def;
      values[k] = (k in s) ? s[k] : def;
      saved[k] = values[k];
    });
    // server keys not in the UI model (persona_name, persona_voice_id, dial-only keys, …)
    Object.keys(s).forEach(k => { if (!(k in values)) { values[k] = s[k]; saved[k] = s[k]; refDefault[k] = (k in d) ? d[k] : s[k]; } });
    // ensure every dial-touched key exists
    allKeys.forEach(k => { if (!(k in values)) { const m = keyMeta(k); refDefault[k] = m.def; values[k] = m.def; saved[k] = values[k]; } });

    persona = (s.persona_name && PERSONA_CHEM[s.persona_name]) ? s.persona_name : PERSONAS[0].id;
    if (!('persona_name' in saved)) { values.persona_name = saved.persona_name = persona; }
    if (!(persona in manualState)) manualState[persona] = false;

    seedDials(false);
    view = 'persona'; activeTab = 'persona';
    buildScaffold();
    renderPersonaRail();
    renderTabs();
    renderAllDials();
    renderChem();
    applyChemDisplay(false);
    syncPersonaHead();
    refreshManualUI();
    selectTab('persona');
    refreshDirty();
  }

  function realChangedPatch() {
    const patch = {};
    Object.keys(saved).forEach(k => { if (values[k] !== saved[k]) patch[k] = values[k]; });
    Object.keys(values).forEach(k => { if (!(k in saved) && values[k] !== '' && values[k] != null) patch[k] = values[k]; });
    return patch;
  }
  function dirtyCount() { return Object.keys(realChangedPatch()).length; }

  async function doSave() {
    const patch = realChangedPatch();
    if (!Object.keys(patch).length) return;
    if (saveBtn) saveBtn.textContent = 'Saving…';
    try {
      const res = await fetch('/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
      if (res.ok) {
        Object.keys(values).forEach(k => saved[k] = values[k]);
        if (restartBanner) restartBanner.classList.add('on', 'visible');
        if (saveBtn) { saveBtn.textContent = 'Saved ✓'; setTimeout(() => saveBtn.textContent = 'Save Settings', 1600); }
        applyGenericDisplay(); refreshDirty();
      } else if (saveBtn) { saveBtn.textContent = 'Error ' + res.status; setTimeout(() => saveBtn.textContent = 'Save Settings', 2200); }
    } catch (e) {
      console.error('Settings save error', e);
      if (saveBtn) { saveBtn.textContent = 'Error (no server)'; setTimeout(() => saveBtn.textContent = 'Save Settings', 2200); }
    }
  }

  async function doResetAll() {
    try {
      const res = await fetch('/settings/reset', { method: 'POST' });
      if (res.ok && restartBanner) restartBanner.classList.add('on', 'visible');
    } catch (_) {}
    await loadFromServer();
  }

  /* =====================================================================
     RENDER — radial dials
     ===================================================================== */
  const A0 = -135, A1 = 135, R = 34, CX = 50, CY = 50;
  const ang = v => A0 + v * (A1 - A0);
  function pt(a, r) { r = r || R; const rad = a * Math.PI / 180; return [CX + r * Math.sin(rad), CY - r * Math.cos(rad)]; }
  function arcPath(aFrom, aTo, large) { const [x0, y0] = pt(aFrom), [x1, y1] = pt(aTo); return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${R} ${R} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`; }
  function tick(a) { const [x0, y0] = pt(a), [x1, y1] = pt(a, R + 5); return `<line x1="${x0.toFixed(1)}" y1="${y0.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}"/>`; }
  const dialEls = {};
  function renderAllDials() { renderDials('trait-grid', TRAIT_DIALS); renderDials('cog-grid', COGNITIVE_DIALS); }
  function renderDials(gridId, list) {
    const container = document.getElementById(gridId); if (!container) return;
    container.innerHTML = '';
    list.forEach(d => {
      const cell = document.createElement('div'); cell.className = 'tdial';
      cell.innerHTML =
        `<div class="tknob" tabindex="0" role="slider" aria-label="${d.label}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="50">` +
          `<svg viewBox="0 0 100 100" aria-hidden="true">` +
            `<path class="tk-track" d="${arcPath(A0, A1, 1)}"/><path class="tk-fill" d=""/>` +
            `<g class="tk-ticks" stroke-width="1.4">${tick(A0)}${tick(A1)}</g>` +
            `<circle class="tk-rest" r="2.3"/><g class="tk-needle"><line x1="50" y1="50" x2="50" y2="20"/></g>` +
            `<circle class="tk-hub" cx="50" cy="50" r="3.4"/>` +
          `</svg></div>` +
        `<div class="tname"><span class="tglyph">${ico(GLYPHS[d.glyph])}</span>${d.label}</div>` +
        `<div class="tsub">${d.sub}</div><div class="treadout" data-r>50</div>` +
        `<div class="tdrives">drives ${d.map.length} controls</div>`;
      container.appendChild(cell);
      const knob = cell.querySelector('.tknob');
      dialEls[d.id] = { cell, knob, fill: cell.querySelector('.tk-fill'), needle: cell.querySelector('.tk-needle'), restDot: cell.querySelector('.tk-rest'), readout: cell.querySelector('[data-r]') };
      bindKnob(d.id, knob);
      paintDial(d.id);
    });
  }
  function paintDial(id) {
    const v = dial[id], e = dialEls[id]; if (!e) return;
    const a = ang(v), ar = ang(rest[id]);
    e.needle.style.transform = `rotate(${a}deg)`;
    const [rx, ry] = pt(ar); e.restDot.setAttribute('cx', rx.toFixed(2)); e.restDot.setAttribute('cy', ry.toFixed(2));
    const lo = Math.min(ar, a), hi = Math.max(ar, a);
    e.fill.setAttribute('d', Math.abs(a - ar) < 0.5 ? '' : arcPath(lo, hi, hi - lo > 180 ? 1 : 0));
    e.readout.textContent = Math.round(v * 100);
    e.cell.classList.toggle('moved', moved(id));
    e.knob.setAttribute('aria-valuenow', Math.round(v * 100));
  }
  function bindKnob(id, knob) {
    let dragging = false;
    function fromEvent(ev) {
      const r = knob.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      let a = Math.atan2(ev.clientX - cx, -(ev.clientY - cy)) * 180 / Math.PI;
      a = Math.max(A0, Math.min(A1, a));
      setDial(id, (a - A0) / (A1 - A0));
    }
    knob.addEventListener('pointerdown', e => { dragging = true; knob.setPointerCapture(e.pointerId); knob.classList.add('grab'); fromEvent(e); e.preventDefault(); });
    knob.addEventListener('pointermove', e => { if (dragging) fromEvent(e); });
    knob.addEventListener('pointerup', () => { dragging = false; knob.classList.remove('grab'); });
    knob.addEventListener('pointercancel', () => { dragging = false; knob.classList.remove('grab'); });
    knob.addEventListener('keydown', e => {
      const step = e.shiftKey ? 0.1 : 0.02;
      if (e.key === 'ArrowUp' || e.key === 'ArrowRight') { setDial(id, dial[id] + step); e.preventDefault(); }
      else if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') { setDial(id, dial[id] - step); e.preventDefault(); }
      else if (e.key === 'Home') { setDial(id, rest[id]); e.preventDefault(); }
    });
    knob.addEventListener('dblclick', () => setDial(id, rest[id]));
  }
  function setDial(id, v) {
    v = Math.max(0, Math.min(1, v));
    if (v === dial[id]) return;
    dial[id] = v; paintDial(id); recomputeTraits(); applyChemDisplay(true); applyGenericDisplay(); refreshDirty();
  }

  /* =====================================================================
     RENDER — chemistry block (Manual mode, inside Temperament tab)
     ===================================================================== */
  const chemEls = {};
  const fmtChem = v => { v = +v; return v < 0.1 ? v.toFixed(3) : v.toFixed(2); };
  const fmtDelta = d => (d > 0 ? '+' : '−') + Math.abs(d).toFixed(2);
  function setFill(inp, v, min, max) { inp.style.setProperty('--pct', Math.max(0, Math.min(100, ((v - min) / (max - min)) * 100)) + '%'); }
  function renderChem() {
    const container = document.getElementById('chem-grid'); if (!container) return;
    container.innerHTML = '';
    CHANNELS.forEach(c => {
      const k = 'chem_baseline_' + c.ch;
      const row = document.createElement('div'); row.className = 'chrow';
      row.innerHTML = `<div class="ch-meta"><span class="ch-ab">${c.ch}</span><span class="ch-name">${c.name}</span></div>` +
        `<div class="ch-field"><input type="range" class="es-range" min="${CHEM_MIN}" max="${CHEM_MAX}" step="${CHEM_STEP}"><span class="ch-val">0.00</span><span class="ch-delta"></span></div>`;
      container.appendChild(row);
      const inp = row.querySelector('input');
      inp.addEventListener('input', () => {
        const nv = clampKey(k, parseFloat(inp.value));
        values[k] = nv; dialCenter[k] = nv - dialOffsetFor(k); values['chem_init_' + c.ch] = nv;
        updateChem(c.ch); refreshDirty();
      });
      chemEls[c.ch] = { row, inp, val: row.querySelector('.ch-val'), delta: row.querySelector('.ch-delta') };
    });
    applyChemDisplay(false);
  }
  function updateChem(ch) {
    const k = 'chem_baseline_' + ch, e = chemEls[ch]; if (!e) return;
    const v = values[k];
    e.inp.value = v; setFill(e.inp, v, CHEM_MIN, CHEM_MAX); e.val.textContent = fmtChem(v);
    const d = v - personaBaseline(k), show = Math.abs(d) >= 0.005;
    e.delta.textContent = show ? fmtDelta(d) : ''; e.delta.className = 'ch-delta' + (show ? (d > 0 ? ' up' : ' down') : '');
    e.row.classList.toggle('off-base', show);
  }
  function applyChemDisplay() { CHANNELS.forEach(c => updateChem(c.ch)); }

  /* =====================================================================
     RENDER — generic category tabs
     ===================================================================== */
  const genReg = {};
  function humanize(v) { v = +v; if (v < 60) return v + 's'; if (v < 3600) { const m = v / 60; return (Number.isInteger(m) ? m : m.toFixed(1)) + ' min'; } const h = v / 3600; return (Number.isInteger(h) ? h : h.toFixed(1)) + ' h'; }
  function fmtVal(r, v) {
    if (r.type === 'toggle') return (+v >= 0.5) ? 'On' : 'Off';
    if (r.type === 'time') return humanize(v);
    const step = +r.step;
    if (Number.isInteger(step) && step >= 1) { const n = Math.round(+v); return n >= 1000 ? n.toLocaleString() : '' + n; }
    const n = +v; return n < 0.1 ? n.toFixed(3) : n.toFixed(2);
  }
  const isChanged = k => Math.abs((+values[k]) - (+refDefault[k])) > 1e-9;
  function genRow(r) {
    if (r.type === 'group') { const g = document.createElement('div'); g.className = 'es-group'; g.innerHTML = `<span>${r.label}</span>` + (r.hint ? `<em>${r.hint}</em>` : ''); return g; }
    const row = document.createElement('div');
    row.className = 'es-row' + (r.type === 'master' || r.master ? ' es-master' : '') + (r.type === 'toggle' ? ' es-togglerow' : '');
    row.innerHTML = `<div class="es-row-meta"><span class="es-lab"><span class="es-mod"></span>${r.label}</span>` + (r.hint ? `<span class="es-hint">${r.hint}</span>` : '') + `</div>`;
    const ctrl = document.createElement('div'); ctrl.className = 'es-row-ctrl';
    if (r.type === 'toggle') {
      const t = document.createElement('button'); t.className = 'es-toggle' + (+values[r.key] >= 0.5 ? ' on' : ''); t.setAttribute('role', 'switch');
      t.addEventListener('click', () => { if (!manualOpen) return; const nv = (+values[r.key] >= 0.5) ? 0 : 1; values[r.key] = nv; if (allKeys.indexOf(r.key) >= 0) dialCenter[r.key] = nv - dialOffsetFor(r.key); updateGen(r.key); refreshDirty(); });
      ctrl.appendChild(t); genReg[r.key] = { row, toggle: t };
    } else if (r.type === 'text') {
      const ta = document.createElement('textarea'); ta.className = 'es-textarea'; ta.rows = r.rows || 4; ta.spellcheck = false;
      ta.value = (values[r.key] != null) ? values[r.key] : ''; if (r.placeholder) ta.placeholder = r.placeholder;
      ta.style.cssText = 'flex:1 1 100%;min-width:220px;padding:7px 10px;border-radius:8px;border:1px solid rgba(255,255,255,0.15);background:rgba(255,255,255,0.05);color:inherit;font:inherit;resize:vertical;';
      ta.addEventListener('input', () => { if (!manualOpen) { ta.value = (values[r.key] != null) ? values[r.key] : ''; return; } values[r.key] = ta.value; refreshDirty(); });
      ctrl.appendChild(ta); genReg[r.key] = { row, text: ta };
    } else {
      const inp = document.createElement('input'); inp.type = 'range'; inp.className = 'es-range'; inp.min = r.min; inp.max = r.max; inp.step = r.step; inp.value = values[r.key]; setFill(inp, +values[r.key], +r.min, +r.max);
      inp.addEventListener('input', () => {
        if (!manualOpen) { inp.value = values[r.key]; setFill(inp, +values[r.key], +r.min, +r.max); return; }
        const nv = clampKey(r.key, parseFloat(inp.value)); values[r.key] = nv; if (allKeys.indexOf(r.key) >= 0) dialCenter[r.key] = nv - dialOffsetFor(r.key); updateGen(r.key); refreshDirty();
      });
      const val = document.createElement('span'); val.className = 'es-val'; val.textContent = fmtVal(r, values[r.key]);
      ctrl.appendChild(inp); ctrl.appendChild(val); genReg[r.key] = { row, input: inp, val };
    }
    row.appendChild(ctrl); row.classList.toggle('changed', isChanged(r.key));
    return row;
  }
  function updateGen(key) {
    const e = genReg[key], r = rowMeta[key]; if (!e || !r) return;
    if (e.input) { e.input.value = values[key]; setFill(e.input, +values[key], +r.min, +r.max); if (e.val) e.val.textContent = fmtVal(r, values[key]); }
    if (e.text) e.text.value = (values[key] != null) ? values[key] : '';
    if (e.toggle) e.toggle.classList.toggle('on', +values[key] >= 0.5);
    e.row.classList.toggle('changed', isChanged(key));
  }
  function applyGenericDisplay() { Object.keys(genReg).forEach(updateGen); }
  function genSection(sec) {
    const card = document.createElement('div'); card.className = 'es-card';
    const head = document.createElement('div'); head.className = 'es-card-head';
    head.innerHTML = `<span class="es-num">${sec.num}</span><div class="es-ct"><div class="es-card-title">${sec.title}</div><div class="es-card-desc">${sec.desc || ''}</div></div><span class="es-chev">${chevSvg}</span>`;
    const body = document.createElement('div'); body.className = 'es-card-body';
    (sec.rows || []).forEach(r => body.appendChild(genRow(r)));
    if ((sec.advanced || []).length) {
      const adv = document.createElement('div'); adv.className = 'es-adv';
      const cnt = (sec.advanced || []).filter(r => r.type !== 'group').length;
      const tg = document.createElement('button'); tg.className = 'es-adv-toggle'; tg.innerHTML = `<span class="es-ac">${chevSvg}</span><span>Advanced</span><span class="es-advn">${cnt}</span>`;
      const ab = document.createElement('div'); ab.className = 'es-adv-body';
      (sec.advanced || []).forEach(r => ab.appendChild(genRow(r)));
      tg.addEventListener('click', () => adv.classList.toggle('open'));
      adv.appendChild(tg); adv.appendChild(ab); body.appendChild(adv);
    }
    card.appendChild(head); card.appendChild(body);
    head.addEventListener('click', () => card.classList.toggle('collapsed'));
    return card;
  }
  function renderGeneric(catId) {
    const cat = SET.categories.find(c => c.id === catId);
    const wrap = document.getElementById('tab-generic'); if (!wrap || !cat) return;
    wrap.innerHTML = ''; Object.keys(genReg).forEach(k => delete genReg[k]);
    const pn = (PERSONAS.find(p => p.id === persona) || {}).name || persona;
    const note = document.createElement('div'); note.className = 'gen-note';
    note.innerHTML = `<span class="ro-dot"></span><span>Read-only · these values are produced by <b>${pn}</b>'s Temperament dials. Switch on <b>Manual mode</b> (top right) to hand-tune them for this persona.</span>`;
    wrap.appendChild(note);
    if (cat.summary) { const b = document.createElement('div'); b.className = 'es-cat-blurb'; b.textContent = cat.summary; wrap.appendChild(b); }
    (cat.sections || []).forEach(sec => wrap.appendChild(genSection(sec)));
  }

  /* ---- API keys (System) ---- */
  function renderApiKeys() {
    const wrap = document.getElementById('tab-generic'); if (!wrap) return;
    wrap.innerHTML = ''; Object.keys(genReg).forEach(k => delete genReg[k]);
    const cat = SET.categories.find(c => c.id === 'apikeys'); if (!cat) return;
    if (cat.summary) { const b = document.createElement('div'); b.className = 'es-cat-blurb'; b.textContent = cat.summary; wrap.appendChild(b); }
    const card = document.createElement('div'); card.className = 'es-card';
    card.innerHTML = '<div class="es-card-head static"><span class="es-num">✦</span><div class="es-ct"><div class="es-card-title">Provider Keys</div><div class="es-card-desc">Stored on this machine; applied on restart. Leave a field blank to keep a saved key.</div></div></div>';
    const body = document.createElement('div'); body.className = 'es-card-body api-body';
    (cat.sections[0].rows || []).forEach(r => {
      if (r.type !== 'apikey') return;
      const isSet = !!secretsSet[r.key];
      const ph = isSet ? '•••••••••• saved — leave blank to keep' : 'paste key…';
      const row = document.createElement('div'); row.className = 'api-row';
      row.innerHTML = `<div class="api-meta"><span class="api-name"><span class="api-dot ${isSet ? 'on' : ''}"></span>${r.label}</span><span class="api-hint">${r.hint || ''}</span></div>` +
        `<div class="api-line"><input type="password" autocomplete="off" spellcheck="false" placeholder="${ph}"><button class="api-reveal" type="button" aria-label="Reveal key">${eyeSvg}</button></div>`;
      body.appendChild(row);
      const inp = row.querySelector('input'), rv = row.querySelector('.api-reveal'), dot = row.querySelector('.api-dot');
      rv.addEventListener('click', () => { inp.type = inp.type === 'password' ? 'text' : 'password'; });
      inp.addEventListener('input', () => { values[r.key] = inp.value; dot.classList.toggle('on', inp.value.trim().length > 0 || isSet); refreshDirty(); });
    });
    card.appendChild(body); wrap.appendChild(card);
  }

  /* =====================================================================
     SCAFFOLD + TABS + RAIL + HEAD
     ===================================================================== */
  function buildScaffold() {
    if (scaffolded) return;
    const wrap = document.getElementById('cat-wrap'); if (!wrap) return;
    wrap.innerHTML =
      `<div class="st-head">` +
        `<div class="ch-top"><div class="ch-id">` +
          `<div class="es-ch-k" id="st-eyebrow">Persona</div>` +
          `<div class="pdetail-name" id="st-name"></div>` +
          `<div class="pdetail-tag" id="st-tag"></div></div>` +
          `<div class="es-mode" id="st-mode"><div class="es-mode-text"><span class="es-mode-label">Manual mode</span>` +
          `<span class="es-mode-state" id="st-modestate">Guided · dials only</span></div>` +
          `<button class="es-toggle" id="st-manual" role="switch" aria-checked="false" aria-label="Manual mode"></button></div>` +
        `</div>` +
        `<div class="pdetail-note" id="st-note"></div>` +
        `<nav class="tabbar" id="st-tabbar"></nav>` +
      `</div>` +
      `<div id="tab-temperament"><div class="es-card">` +
        `<div class="es-card-head static"><span class="es-num">00</span>` +
          `<div class="es-ct"><div class="es-card-title">Temperament</div>` +
          `<div class="es-card-desc">Nine dials that shape the persona. Each rests where this persona naturally sits and quietly turns a whole bundle of underlying controls at once — turn one to lean the temperament that way.</div></div>` +
          `<div class="es-tools"><span class="es-badge" id="st-chembadge"><i></i><span>off baseline</span></span>` +
          `<button class="es-reset" id="st-personareset" title="Restore this persona's baseline">${resetSvg}</button></div>` +
        `</div>` +
        `<div class="es-card-body"><div class="trait-panel"><div class="trait-grid" id="trait-grid"></div>` +
          `<div class="trait-cap">The notch on each dial marks where this persona rests; the needle is the current setting. Switch personas and the dials re-pose to match · turn a dial to lean from its rest, double-click to return.</div></div>` +
          `<div class="manual-note"><span class="ro-dot"></span><span>Read-only · these values are set by the Temperament dials above. Switch on <b>Manual mode</b> (top right) to reveal and hand-tune the chemistry — and to make every other category editable too.</span></div>` +
          `<div class="manual-body" id="manual-body" hidden>` +
            `<p class="chem-intro">The nine neurochemical channels the dials write into — the resting baseline the brain relaxes toward. Turn a dial above and watch them move, or set any one by hand to override the macro layer for that channel. Deltas count from this persona's canonical baseline.</p>` +
            `<div class="chem-cols" id="chem-grid"></div>` +
            `<div class="chem-foot"><span><i class="up"></i> above persona baseline</span><span><i class="down"></i> below persona baseline</span></div>` +
          `</div>` +
        `</div></div>` +
        `<div class="es-card"><div class="es-card-head static"><span class="es-num">01</span>` +
          `<div class="es-ct"><div class="es-card-title">Cognitive Style</div>` +
          `<div class="es-card-desc">How this persona thinks and works — attention, learning, curiosity, and how much feeling steers thought. These rest at neutral; lean one to shape the cognitive style on top of the temperament above.</div></div></div>` +
          `<div class="es-card-body"><div class="trait-panel"><div class="trait-grid" id="cog-grid"></div>` +
          `<div class="trait-cap">Resting at center · turn a dial to lean from neutral, double-click to return.</div></div></div></div>` +
      `</div>` +
      `<div id="tab-generic" hidden></div>`;
    document.getElementById('st-manual').addEventListener('click', () => setManual(!manualState[persona]));
    document.getElementById('st-personareset').addEventListener('click', e => { e.stopPropagation(); resetPersona(); });
    scaffolded = true;
  }

  // persona tabs exclude system-level categories (rendered on a System page)
  function tabCats() { return SET.categories.filter(c => c.id !== 'apikeys' && !c.system); }
  function renderTabs() {
    const bar = document.getElementById('st-tabbar'); if (!bar) return;
    bar.innerHTML = '';
    tabCats().forEach(cat => {
      const label = cat.id === 'persona' ? 'Temperament' : cat.name;
      const b = document.createElement('button'); b.className = 'tab' + (cat.id === activeTab ? ' on' : ''); b.dataset.t = cat.id;
      b.innerHTML = `<span>${label}</span>` + (cat.id === 'persona' ? '' : `<span class="tlock">${lockSvg}</span>`);
      b.addEventListener('click', () => selectTab(cat.id));
      bar.appendChild(b);
    });
  }
  function selectTab(id) {
    activeTab = id; view = 'persona'; systemPage = null;
    const sp = document.getElementById('settings-page'); if (sp) sp.classList.remove('system');
    document.querySelectorAll('#st-tabbar .tab').forEach(t => t.classList.toggle('on', t.dataset.t === id));
    const temp = document.getElementById('tab-temperament'), gen = document.getElementById('tab-generic');
    const mode = document.getElementById('st-mode'); if (mode) mode.style.display = '';
    if (id === 'persona') { if (temp) temp.hidden = false; if (gen) gen.hidden = true; }
    else { if (temp) temp.hidden = true; if (gen) gen.hidden = false; renderGeneric(id); }
    refreshManualUI();   // restore this persona's manual/guided gate (was forced editable in system view)
    if (scroll) scroll.scrollTop = 0;
  }

  function renderPersonaRail() {
    const rail = document.getElementById('rail-nav'); if (!rail) return;
    rail.innerHTML = '';
    const list = document.createElement('div'); list.className = 'pmenu-list';
    PERSONAS.forEach(p => {
      const c = document.createElement('button'); c.className = 'pmenu-item'; c.dataset.p = p.id;
      c.innerHTML = `<div class="pmenu-name">${p.name}</div><div class="pmenu-tag">${p.tag}</div>`;
      c.addEventListener('click', () => { if (view === 'persona' && p.id === persona) return; selectPersona(p.id); });
      list.appendChild(c);
    });
    rail.appendChild(list);
    const sh = document.createElement('div'); sh.className = 'pmenu-syshead'; sh.textContent = 'System'; rail.appendChild(sh);
    const api = document.createElement('button'); api.className = 'pmenu-item sys'; api.dataset.sys = 'apikeys';
    api.innerHTML = '<div class="pmenu-name">API Keys</div><div class="pmenu-tag">Models · voice · services</div>';
    api.addEventListener('click', () => selectSystem('apikeys'));
    rail.appendChild(api);
    const ops = document.createElement('button'); ops.className = 'pmenu-item sys'; ops.dataset.sys = 'operational';
    ops.innerHTML = '<div class="pmenu-name">Operational</div><div class="pmenu-tag">Perception · resources · maintenance</div>';
    ops.addEventListener('click', () => selectSystem('operational'));
    rail.appendChild(ops);
    syncRailSel();
  }
  function syncRailSel() {
    document.querySelectorAll('#rail-nav .pmenu-item:not(.sys)').forEach(c => c.classList.toggle('sel', view === 'persona' && c.dataset.p === persona));
    document.querySelectorAll('#rail-nav .pmenu-item.sys').forEach(c => c.classList.toggle('sel', view === 'system' && c.dataset.sys === systemPage));
  }

  function syncPersonaHead() {
    const p = PERSONAS.find(x => x.id === persona) || { name: persona, tag: '', note: '' };
    const set = (id, t) => { const el = document.getElementById(id); if (el != null && el) el.textContent = t; };
    set('st-eyebrow', 'Persona'); set('st-name', p.name); set('st-tag', p.tag); set('st-note', p.note);
    const bt = document.getElementById('bar-title'); if (bt) bt.textContent = p.name;
    const bb = document.getElementById('bar-blurb'); if (bb) bb.textContent = p.tag || '';
  }

  function selectPersona(id) {
    persona = id; view = 'persona';
    if (!(persona in manualState)) manualState[persona] = false;
    values.persona_name = id;
    seedDials(true);
    renderAllDials(); renderChem(); applyChemDisplay(false);
    syncPersonaHead(); renderTabs(); refreshManualUI();
    if (activeTab !== 'persona') renderGeneric(activeTab);
    selectTab(activeTab); syncRailSel(); refreshDirty();
  }
  function selectSystem(which) {
    view = 'system'; systemPage = which; syncRailSel();
    const tb = document.getElementById('st-tabbar'); if (tb) tb.hidden = true;
    const temp = document.getElementById('tab-temperament'); if (temp) temp.hidden = true;
    const gen = document.getElementById('tab-generic'); if (gen) gen.hidden = false;
    const mode = document.getElementById('st-mode'); if (mode) mode.style.display = 'none';
    const sp = document.getElementById('settings-page'); if (sp) { sp.classList.remove('manual'); sp.classList.add('system'); }
    manualOpen = true;   // system settings are always editable (no per-persona gate)
    const set = (id, t) => { const el = document.getElementById(id); if (el) el.textContent = t; };
    const bt = document.getElementById('bar-title'), bb = document.getElementById('bar-blurb');
    if (which === 'operational') {
      set('st-eyebrow', 'System'); set('st-name', 'Operational'); set('st-tag', '');
      set('st-note', 'System-wide settings shared across every persona — perception, background compute budgets, and self-maintenance. Not part of any one persona’s temperament.');
      if (bt) bt.textContent = 'Operational'; if (bb) bb.textContent = 'System · shared settings';
      renderOperational();
    } else {
      set('st-eyebrow', 'System'); set('st-name', 'API Keys'); set('st-tag', '');
      set('st-note', 'Provider credentials for language models, voice, and background services — shared across every persona, not part of any one’s temperament.');
      if (bt) bt.textContent = 'API Keys'; if (bb) bb.textContent = 'System · shared providers';
      renderApiKeys();
    }
    if (scroll) scroll.scrollTop = 0;
  }
  function renderOperational() {
    const wrap = document.getElementById('tab-generic'); if (!wrap) return;
    wrap.innerHTML = ''; Object.keys(genReg).forEach(k => delete genReg[k]);
    const note = document.createElement('div'); note.className = 'es-cat-blurb';
    note.textContent = 'System-wide operational settings — the same for every persona. Perception (how it hears/sees), background compute budgets, and self-maintenance.';
    wrap.appendChild(note);
    SET.categories.filter(c => c.system).forEach(cat => {
      const h = document.createElement('div'); h.className = 'es-group';
      h.innerHTML = `<span>${cat.name}</span>` + (cat.blurb ? `<em>${cat.blurb}</em>` : '');
      wrap.appendChild(h);
      (cat.sections || []).forEach(sec => wrap.appendChild(genSection(sec)));
    });
  }

  /* =====================================================================
     MANUAL MODE / DIRTY / RESET
     ===================================================================== */
  function refreshManualUI() {
    const on = view === 'persona' && !!manualState[persona];
    manualOpen = on;
    const tg = document.getElementById('st-manual'); if (tg) { tg.classList.toggle('on', on); tg.setAttribute('aria-checked', on ? 'true' : 'false'); }
    const mb = document.getElementById('manual-body'); if (mb) mb.hidden = !on;
    const sp = document.getElementById('settings-page'); if (sp) sp.classList.toggle('manual', on);
    const ms = document.getElementById('st-modestate'); if (ms) ms.textContent = on ? 'Manual · this persona' : 'Guided · dials only';
    if (on) applyChemDisplay(false);
  }
  function setManual(v) { manualState[persona] = v; refreshManualUI(); }

  function refreshDirty() {
    const n = dirtyCount();
    if (dirtyPill) dirtyPill.classList.toggle('on', n > 0);
    if (dirtyText) dirtyText.textContent = n + ' unsaved';
    if (saveBtn) saveBtn.classList.toggle('idle', n === 0);
    const offBase = allKeys.some(k => Math.abs((+values[k]) - personaBaseline(k)) > 0.005) || ALL_DIALS.some(d => moved(d.id));
    const pr = document.getElementById('st-personareset'); if (pr) pr.classList.toggle('on', offBase);
    const cb = document.getElementById('st-chembadge'); if (cb) cb.classList.toggle('on', CHANNELS.some(c => Math.abs((+values['chem_baseline_' + c.ch]) - PERSONA_CHEM[persona][c.ch]) > 0.005));
  }
  function resetPersona() {
    CHANNELS.forEach(c => { values['chem_baseline_' + c.ch] = PERSONA_CHEM[persona][c.ch]; values['chem_init_' + c.ch] = PERSONA_CHEM[persona][c.ch]; });
    allKeys.forEach(k => { if (!isChem(k)) values[k] = refDefault[k]; });
    ALL_DIALS.forEach(d => { dial[d.id] = rest[d.id]; });
    allKeys.forEach(k => { dialCenter[k] = +values[k]; });
    ALL_DIALS.forEach(d => paintDial(d.id)); applyChemDisplay(false); applyGenericDisplay(); refreshDirty();
  }

  /* =====================================================================
     BOOT
     ===================================================================== */
  function boot() {
    if (!document.getElementById('rail-nav')) return;
    saveBtn = document.getElementById('settings-save-btn');
    resetBtn = document.getElementById('settings-reset-btn');
    restartBanner = document.getElementById('settings-restart-banner');
    dirtyPill = document.getElementById('dirty-pill');
    dirtyText = document.getElementById('dirty-text');
    scroll = document.getElementById('scroll');
    if (saveBtn) saveBtn.addEventListener('click', doSave);
    if (resetBtn) resetBtn.addEventListener('click', () => { if (confirm('Reset ALL settings across every category to their defaults?')) doResetAll(); });
    window.__settingsUI = { open: loadFromServer, reload: loadFromServer };
    loadFromServer();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
