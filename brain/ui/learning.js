// Learning surface — the MRI workspace's second view. A plain-language feed of
// what the brain learned ("stories"), each claim expandable into the evidence
// behind it (edge drift sparkline + the decision records), over a dashboard of
// the learning system's vitals (reward-source mix, plasticity, switch bands,
// top edge movers, chunks, predictor). Read-only: everything comes from the
// /learning/* routes; nothing here can perturb the learning path.
//
// All text lands via textContent (fields are model-derived — same XSS posture
// as the chat/trading renderers). Sparklines are inline SVG.
(function () {
  'use strict';

  let personaSel = '';       // '' = active persona
  let personas = [];
  let loaded = false;
  let learningOn = false;

  const $id = (i) => document.getElementById(i);

  // ── styles (self-contained so index.html stays bounded) ──────────────────
  const CSS = `
  #learning-page { display:none; padding: 18px 26px 40px; overflow-y: auto; height: 100%; }
  #learning-page.on { display:block; }
  .lrn-head { display:flex; align-items:center; gap:14px; margin-bottom:16px; flex-wrap:wrap; }
  .lrn-head h2 { font-size:20px; }
  .lrn-headline { margin-left:auto; text-align:right; }
  .lrn-headline .v { font-size:22px; font-weight:700; color: var(--fg); font-variant-numeric: tabular-nums; }
  .lrn-headline .k { font-size:10px; color: var(--fg-faint); letter-spacing:.06em; text-transform:uppercase; }
  .lrn-feed { display:flex; flex-direction:column; gap:10px; max-width: 860px; }
  .lrn-card { border:1px solid var(--line-soft); border-left:3px solid var(--line-soft); border-radius:8px; background:var(--bg-1); padding:11px 14px; }
  .lrn-card .claim { font-size:14px; line-height:1.5; color:var(--fg); cursor:pointer; }
  .lrn-meta { display:flex; gap:8px; align-items:center; margin-top:6px; }
  .lrn-chip { font-size:9px; font-family:var(--mono); letter-spacing:.07em; text-transform:uppercase; padding:2px 7px; border-radius:9px; border:1px solid var(--line-soft); color:var(--fg-mute); }
  .lrn-chip.gen-template { opacity:.65; }
  .lrn-when { font-size:10px; color:var(--fg-faint); }
  .lrn-evidence { display:none; margin-top:10px; border-top:1px dashed var(--line-soft); padding-top:9px; }
  .lrn-card.open .lrn-evidence { display:block; }
  .lrn-edge-row { display:flex; align-items:center; gap:10px; font-family:var(--mono); font-size:11px; color:var(--fg-dim); margin-bottom:4px; flex-wrap:wrap; }
  .lrn-spark { flex-shrink:0; }
  .lrn-recs { margin-top:6px; font-family:var(--mono); font-size:10px; color:var(--fg-faint); max-height:130px; overflow-y:auto; white-space:pre-wrap; word-break:break-all; }
  /* Node/route names are read as identifiers, so they wrap at boundaries rather
     than mid-token the way the raw record dumps above are allowed to. */
  .lrn-recs.ident { word-break:normal; overflow-wrap:anywhere; line-height:1.5; }
  .lrn-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); gap:12px; margin-top:26px; max-width:1100px; }
  .lrn-panel { border:1px solid var(--line-soft); border-radius:8px; background:var(--bg-1); padding:11px 13px; }
  .lrn-panel h4 { font-size:10px; letter-spacing:.07em; text-transform:uppercase; color:var(--fg-faint); margin-bottom:8px; font-family:var(--mono); }
  .lrn-bar-row { display:flex; align-items:center; gap:8px; font-size:11px; color:var(--fg-dim); margin-bottom:5px; }
  .lrn-bar-row .lbl { width:110px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-family:var(--mono); font-size:10px; }
  .lrn-bar { flex:1; height:6px; background: var(--bg-2); border-radius:3px; position:relative; }
  .lrn-bar > span { position:absolute; left:0; top:0; height:100%; border-radius:3px; background: var(--fg-mute); }
  .lrn-band { flex:1; height:8px; background:var(--bg-2); border-radius:4px; position:relative; }
  .lrn-band .mark { position:absolute; top:-2px; width:3px; height:12px; border-radius:2px; background: oklch(0.74 0.15 75); }
  .lrn-band .rest { position:absolute; top:2px; width:1px; height:4px; background: var(--fg-faint); }
  .lrn-num { font-variant-numeric:tabular-nums; font-family:var(--mono); font-size:10px; color:var(--fg-faint); width:44px; text-align:right; }
  .lrn-empty { color: var(--fg-faint); font-size:11.5px; }
  .lrn-note { font-size:10px; color:var(--fg-faint); margin: 4px 0 14px; }
  #labs-tabs { display:flex; gap:2px; margin-left:14px; border:1px solid var(--line-soft); border-radius:7px; padding:2px; flex-shrink:0; }
  #labs-tabs button { border:none; background:transparent; color:var(--fg-mute); font-size:10.5px; padding:2px 10px; border-radius:5px; cursor:pointer; font-family:var(--mono); letter-spacing:.04em; }
  #labs-tabs button.on { background: var(--bg-2); color: var(--fg); }
  .lrn-select { background:var(--bg-2); color:var(--fg-dim); border:1px solid var(--line-soft); border-radius:6px; font-size:11px; padding:3px 8px; font-family:var(--mono); }
  `;

  function injectStyles() {
    if ($id('lrn-styles')) return;
    const s = document.createElement('style');
    s.id = 'lrn-styles';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  // ── tiny render helpers ───────────────────────────────────────────────────
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function agoText(ts) {
    if (!ts) return '';
    const s = Math.max(0, (Date.now() / 1000) - ts);
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
  }
  // Inline-SVG sparkline for an edge's weight series.
  function sparkline(series, w = 120, h = 26) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', w); svg.setAttribute('height', h);
    svg.setAttribute('class', 'lrn-spark');
    const vals = series.map(p => Number(p.w)).filter(v => isFinite(v));
    if (vals.length < 2) {
      const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      t.setAttribute('x', 0); t.setAttribute('y', h - 8);
      t.setAttribute('fill', 'currentColor'); t.setAttribute('font-size', '9');
      t.textContent = vals.length ? `w=${vals[0].toFixed(2)} (1 snapshot)` : 'no history yet';
      svg.appendChild(t);
      return svg;
    }
    const min = Math.min(...vals), max = Math.max(...vals);
    const span = (max - min) || 0.01;
    const pts = vals.map((v, i) =>
      `${(i / (vals.length - 1)) * (w - 4) + 2},${h - 3 - ((v - min) / span) * (h - 8)}`).join(' ');
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    line.setAttribute('points', pts);
    line.setAttribute('fill', 'none');
    line.setAttribute('stroke', 'oklch(0.74 0.15 75)');
    line.setAttribute('stroke-width', '1.5');
    svg.appendChild(line);
    return svg;
  }

  // ── data fetch ────────────────────────────────────────────────────────────
  const q = () => (personaSel ? '?persona=' + encodeURIComponent(personaSel) : '');
  async function fetchJson(url, fallback) {
    try { const r = await fetch(url); return r.ok ? await r.json() : fallback; }
    catch (e) { return fallback; }
  }

  async function render() {
    const page = $id('learning-page');
    if (!page) return;
    page.textContent = '';
    page.appendChild(el('div', 'lrn-empty', 'Reading the learning record…'));
    const [stories, summary, wiring] = await Promise.all([
      fetchJson('/learning/stories' + q(), { stories: [], personas: [] }),
      fetchJson('/learning/summary' + q(), {}),
      fetchJson('/learning/wiring' + q(), { top: [], deltas: [] }),
    ]);
    personas = stories.personas || [];
    page.textContent = '';

    // header: title · persona selector · self-graded headline
    const head = el('div', 'lrn-head');
    head.appendChild(el('h2', 'serif-h', 'Learning'));
    if (personas.length > 1) {
      const sel = el('select', 'lrn-select');
      personas.forEach(p => {
        const o = el('option', '', p.replace(/_/g, ' '));
        o.value = p;
        if (p === (personaSel || personas[0])) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener('change', () => { personaSel = sel.value; render(); });
      head.appendChild(sel);
    }
    const mix = (summary.reward_mix || {});
    if (mix.self_graded_pct != null) {
      const hl = el('div', 'lrn-headline');
      hl.appendChild(el('div', 'v', mix.self_graded_pct + '%'));
      hl.appendChild(el('div', 'k', 'of reward self-graded'));
      head.appendChild(hl);
    }
    page.appendChild(head);

    if (stories.generated_on_read) {
      page.appendChild(el('div', 'lrn-note',
        'Synthesized from session plasticity records — the sleep narrator will write richer stories after the next consolidation.'));
    }

    // narrative feed
    const feed = el('div', 'lrn-feed');
    const items = stories.stories || [];
    if (!items.length) {
      feed.appendChild(el('div', 'lrn-empty',
        'Nothing learned on record yet — run a few conversations and let a sleep consolidation pass.'));
    }
    items.forEach(st => feed.appendChild(storyCard(st)));
    page.appendChild(feed);

    // dashboard grid
    page.appendChild(dashboard(summary, wiring));
    loaded = true;
  }

  function storyCard(st) {
    const card = el('div', 'lrn-card');
    card.appendChild(el('div', 'claim', st.claim || ''));
    const meta = el('div', 'lrn-meta');
    meta.appendChild(el('span', 'lrn-chip', st.subsystem || 'learning'));
    if (st.generator) meta.appendChild(el('span', 'lrn-chip gen-' + st.generator, st.generator));
    if (st.persona) meta.appendChild(el('span', 'lrn-when', st.persona.replace(/_/g, ' ')));
    meta.appendChild(el('span', 'lrn-when', agoText(st.ts)));
    card.appendChild(meta);
    const evid = el('div', 'lrn-evidence');
    card.appendChild(evid);
    let evidLoaded = false;
    card.querySelector('.claim').addEventListener('click', async () => {
      card.classList.toggle('open');
      if (evidLoaded || !card.classList.contains('open')) return;
      evidLoaded = true;
      const edges = ((st.evidence || {}).edges || []);
      if (!edges.length) { evid.appendChild(el('div', 'lrn-empty', 'No edge evidence attached.')); return; }
      for (const e of edges.slice(0, 3)) {
        const name = e.edge || '';
        const row = el('div', 'lrn-edge-row');
        row.appendChild(el('span', '', name));
        if (e.from_w != null && e.to_w != null) row.appendChild(el('span', '', `${Number(e.from_w).toFixed(2)}→${Number(e.to_w).toFixed(2)}`));
        else if (e.delta != null) row.appendChild(el('span', '', `Δ ${Number(e.delta) >= 0 ? '+' : ''}${Number(e.delta).toFixed(3)}`));
        else if (e.w != null) row.appendChild(el('span', '', `w ${Number(e.w).toFixed(2)}`));
        evid.appendChild(row);
        if (name) {
          const d = await fetchJson('/learning/wiring' + (q() ? q() + '&' : '?') + 'edge=' + encodeURIComponent(name), {});
          row.appendChild(sparkline(d.edge_series || []));
          const recs = (d.edge_records || []).slice(-6);
          if (recs.length) {
            const pre = el('div', 'lrn-recs');
            pre.textContent = recs.map(r =>
              `${r.decision || 'update'}  ${r.from_weight ?? ''}→${r.to_weight ?? ''}  Δ${r.delta ?? ''}  outcome ${r.outcome ?? ''}`).join('\n');
            evid.appendChild(pre);
          }
        }
      }
      const m = (st.evidence || {}).metrics;
      if (m) evid.appendChild(el('div', 'lrn-recs', JSON.stringify(m)));
    });
    return card;
  }

  function barRow(label, frac, numText) {
    const row = el('div', 'lrn-bar-row');
    row.appendChild(el('span', 'lbl', label));
    const bar = el('div', 'lrn-bar');
    const fill = el('span');
    fill.style.width = Math.round(Math.max(0, Math.min(1, frac)) * 100) + '%';
    bar.appendChild(fill);
    row.appendChild(bar);
    row.appendChild(el('span', 'lrn-num', numText));
    return row;
  }

  function dashboard(summary, wiring) {
    const grid = el('div', 'lrn-grid');

    // reward-source mix
    const pm = el('div', 'lrn-panel');
    pm.appendChild(el('h4', '', 'Reward sources'));
    const byType = (summary.reward_mix || {}).by_signal_type || {};
    const totalMix = Object.values(byType).reduce((a, b) => a + b, 0);
    if (totalMix > 0) {
      Object.entries(byType).sort((a, b) => b[1] - a[1]).forEach(([k, v]) =>
        pm.appendChild(barRow(k.replace(/_/g, ' '), v / totalMix, String(Math.round(100 * v / totalMix)) + '%')));
      const hist = (summary.reward_mix || {}).emissions_per_turn_hist || {};
      if (Object.keys(hist).length) {
        pm.appendChild(el('h4', '', 'Emissions per turn'));
        const maxH = Math.max(...Object.values(hist));
        Object.entries(hist).sort().forEach(([k, v]) =>
          pm.appendChild(barRow(k + ' emission' + (k === '1' ? '' : 's'), v / maxH, String(v))));
      }
    } else pm.appendChild(el('div', 'lrn-empty', 'No attribution records yet — arrives with the reward-emission log.'));
    grid.appendChild(pm);

    // plasticity per session
    const pp = el('div', 'lrn-panel');
    pp.appendChild(el('h4', '', 'Plasticity per session'));
    const plast = summary.plasticity || [];
    if (plast.length) {
      const maxE = Math.max(...plast.map(p => p.edges_updated || 0), 1);
      plast.slice(-8).forEach(p =>
        pp.appendChild(barRow((p.session_id || '').slice(0, 8), (p.edges_updated || 0) / maxE,
          String(p.edges_updated || 0) + 'e')));
    } else pp.appendChild(el('div', 'lrn-empty', 'No consolidation on record.'));
    grid.appendChild(pp);

    // switch efficacy bands
    const ps = el('div', 'lrn-panel');
    ps.appendChild(el('h4', '', 'Switch efficacy (within safety bands)'));
    const switches = summary.switches || [];
    if (switches.length) {
      switches.forEach(sw => {
        const row = el('div', 'lrn-bar-row');
        row.appendChild(el('span', 'lbl', sw.name.replace(/_/g, ' ')));
        const band = el('div', 'lrn-band');
        const mark = el('span', 'mark');
        mark.style.left = 'calc(' + Math.round(sw.position * 100) + '% - 1px)';
        band.appendChild(mark);
        // rest = where weight 1.0 (untrained) sits inside the band
        const [lo, hi] = sw.band;
        if (hi > lo && 1.0 >= lo && 1.0 <= hi) {
          const rest = el('span', 'rest');
          rest.style.left = 'calc(' + Math.round(((1.0 - lo) / (hi - lo)) * 100) + '% - 0.5px)';
          band.appendChild(rest);
        }
        row.appendChild(band);
        row.appendChild(el('span', 'lrn-num', sw.weight.toFixed(2)));
        ps.appendChild(row);
      });
    } else ps.appendChild(el('div', 'lrn-empty', 'No efficacy-banded switches configured.'));
    grid.appendChild(ps);

    // top edge movers
    const pd = el('div', 'lrn-panel');
    pd.appendChild(el('h4', '', 'Edge movers (this session)'));
    const deltas = (wiring.deltas || []).slice(0, 8);
    if (deltas.length) {
      const maxD = Math.max(...deltas.map(d => Math.abs(d.delta || 0)), 0.001);
      deltas.forEach(d => pd.appendChild(barRow(d.edge || `${d.src}→${d.tgt}`,
        Math.abs(d.delta || 0) / maxD, (d.delta >= 0 ? '+' : '') + Number(d.delta || 0).toFixed(3))));
    } else pd.appendChild(el('div', 'lrn-empty', 'No weight movement this session yet.'));
    grid.appendChild(pd);

    // chunks
    const pc = el('div', 'lrn-panel');
    pc.appendChild(el('h4', '', 'Motor chunks (automatized sequences)'));
    const chunks = (summary.chunks || {});
    if (chunks.total) {
      (chunks.top || []).forEach(c =>
        pc.appendChild(barRow((c.tools || []).join(' → ') || '?',
          c.occurrences / Math.max(...(chunks.top || []).map(x => x.occurrences), 1),
          c.successes + '/' + c.occurrences)));
    } else pc.appendChild(el('div', 'lrn-empty', 'No promoted chunks yet — needs 3+ successful jobs on a repeated sequence.'));
    grid.appendChild(pc);

    // evidence gates — commits, and whether the avoidance beliefs held up
    const pg = el('div', 'lrn-panel');
    pg.appendChild(el('h4', '', 'Evidence gates (committed across turns)'));
    const gates = summary.gates || {};
    const byGate = gates.commits_by_gate || {};
    const av = gates.avoidance || {};
    if (gates.commits_total || av.armed) {
      const maxG = Math.max(...Object.values(byGate), 1);
      Object.entries(byGate).sort((a, b) => b[1] - a[1]).forEach(([k, v]) =>
        pg.appendChild(barRow(k.replace(/_/g, ' ') + ' commits', v / maxG, String(v))));
      if (av.armed) {
        pg.appendChild(el('h4', '', 'Avoidance beliefs'));
        pg.appendChild(barRow('armed', 1, String(av.armed)));
        if (av.resolved) {
          pg.appendChild(barRow('held up', av.confirmed / av.resolved, String(av.confirmed)));
          pg.appendChild(barRow('false alarms', av.refuted / av.resolved, String(av.refuted)));
        }
        const note = av.precision_pct != null
          ? `${av.precision_pct}% of graded beliefs held up · ${av.steering ? 'steering the idle mind' : 'learning only, not steering'}`
          : 'None graded yet — a belief is graded when the user returns to the subject or steps around it again.';
        pg.appendChild(el('div', 'lrn-note', note));
      }
    } else pg.appendChild(el('div', 'lrn-empty', 'No gate has committed yet — evidence accumulates across turns before one does.'));
    grid.appendChild(pg);

    // structural growth — recruited units (Tier 2)
    const pn = el('div', 'lrn-panel');
    pn.appendChild(el('h4', '', 'New units recruited (structural)'));
    const struct = summary.structure || {};
    if (struct.recruited_total) {
      const byTrig = struct.by_trigger || {};
      const maxT2 = Math.max(...Object.values(byTrig), 1);
      Object.entries(byTrig).sort((a, b) => b[1] - a[1]).forEach(([k, v]) =>
        pn.appendChild(barRow(k.replace(/_/g, ' '), v / maxT2, String(v))));
      (struct.recent || []).slice(-3).reverse().forEach(r => {
        const bits = [r.node, 'from ' + (r.source || '?'), r.fragments + ' skills'];
        if (r.ignition_score != null) bits.push('ignition ' + Number(r.ignition_score).toFixed(1));
        pn.appendChild(el('div', 'lrn-recs ident', bits.join('  ·  ')));
      });
    } else pn.appendChild(el('div', 'lrn-empty', 'No unit recruited yet — needs a proven cluster of skills or sustained workspace ignition.'));
    grid.appendChild(pn);

    // predictor
    const pq = el('div', 'lrn-panel');
    pq.appendChild(el('h4', '', 'Thought-sequence predictor'));
    const pred = summary.predictor || {};
    const trans = pred.top_transitions || [];
    if (trans.length) {
      const maxT = Math.max(...trans.map(t => t.count || 0), 1);
      trans.forEach(t => pq.appendChild(barRow(String(t.transition || ''), (t.count || 0) / maxT, String(t.count || 0))));
    } else pq.appendChild(el('div', 'lrn-empty', 'Not enough DMN history to predict transitions.'));
    grid.appendChild(pq);

    return grid;
  }

  // ── page/tab plumbing ─────────────────────────────────────────────────────
  function ensurePage() {
    injectStyles();
    if (!$id('learning-page')) {
      const sec = document.createElement('section');
      sec.id = 'learning-page';
      const main = $id('main');
      if (main && main.parentNode) main.parentNode.insertBefore(sec, main.nextSibling);
      else document.body.appendChild(sec);
    }
    if (!$id('labs-tabs')) {
      const ticker = $id('activity-ticker');
      if (ticker) {
        const tabs = document.createElement('div');
        tabs.id = 'labs-tabs';
        const mk = (label, on, fn) => {
          const b = document.createElement('button');
          b.textContent = label;
          if (on) b.classList.add('on');
          b.addEventListener('click', fn);
          return b;
        };
        // Route through setWorkspace when available so the masthead dropdown
        // label tracks the sub-view ('labs' ↔ 'learning' share the MRI surface).
        tabs.appendChild(mk('MRI', true, () =>
          typeof window.setWorkspace === 'function' ? window.setWorkspace('labs') : window.showLearning(false)));
        tabs.appendChild(mk('Learning', false, () =>
          typeof window.setWorkspace === 'function' ? window.setWorkspace('learning') : window.showLearning(true)));
        ticker.appendChild(tabs);
      }
    }
  }

  // Toggle between the MRI atlas (#main) and the Learning page. Off is always
  // safe to call — setWorkspace/openSettings use it to reset Labs state.
  window.showLearning = function (on) {
    ensurePage();
    learningOn = !!on;
    const main = $id('main');
    const page = $id('learning-page');
    const ws = (typeof window.getWorkspace === 'function') ? window.getWorkspace() : 'labs';
    const inLabs = ws === 'labs' || ws === 'learning';
    if (main) main.style.display = (learningOn || !inLabs) ? 'none' : '';
    if (page) page.classList.toggle('on', learningOn && inLabs);
    const tabs = $id('labs-tabs');
    if (tabs) [...tabs.children].forEach((b, i) => b.classList.toggle('on', (i === 1) === learningOn));
    if (learningOn && inLabs) render();
  };
  window.isLearningOn = () => learningOn;
  window.refreshLearning = () => { if (learningOn && $id('learning-page')?.classList.contains('on')) render(); };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ensurePage);
  else ensurePage();
})();
