/* =====================================================================
   ELYCEUM — mood engine
   Tints the whole UI to the brain's prevailing mood by swapping the paper,
   ink, and accent tokens. elyceum.css transitions the affected surfaces, so
   each mood change washes through over ~1.2s.

   Usage:
     ElyceumMood.apply('Curious');            // set a mood by name
     ElyceumMood.applyState({ DA:0.6, ... });  // derive a mood from chemistry
     ElyceumMood.MOODS                          // the palette table (extend freely)

   The six moods map to a hue family. Brain-region colours are deliberately
   NOT tinted — only the chrome (paper / ink / signal accent) moves, so the
   anatomy keeps its identity while the room changes feeling.
   ===================================================================== */
(function (global) {
  // name -> { sig:[L,C,H] accent, paperH, paperC, inkH }
  const MOODS = {
    Settling: { sig: [0.56, 0.185, 33],  paperH: 84,  paperC: 0.013, inkH: 54 },
    Curious:  { sig: [0.62, 0.150, 75],  paperH: 82,  paperC: 0.016, inkH: 70 },
    Warm:     { sig: [0.60, 0.170, 40],  paperH: 60,  paperC: 0.019, inkH: 46 },
    Content:  { sig: [0.55, 0.130, 150], paperH: 124, paperC: 0.015, inkH: 140 },
    Alert:    { sig: [0.58, 0.190, 25],  paperH: 42,  paperC: 0.019, inkH: 36 },
    Guarded:  { sig: [0.55, 0.115, 250], paperH: 250, paperC: 0.013, inkH: 250 },
  };

  // chemistry -> mood. First match wins; the last entry is the fallback.
  const RULES = [
    ['Curious', s => s.DA > 0.55 && s.ACh > 0.35],
    ['Warm',    s => s.OXT > 0.55],
    ['Content', s => s['5HT'] > 0.6 && s.GABA < 0.3],
    ['Alert',   s => s.NE > 0.5],
    ['Guarded', s => s.GABA > 0.45],
    ['Settling', () => true],
  ];

  function apply(name, root) {
    const t = MOODS[name] || MOODS.Settling;
    const r = (root || document.documentElement).style;
    const [L, C, H] = t.sig;
    r.setProperty('--signal',      `oklch(${L} ${C} ${H})`);
    r.setProperty('--signal-deep', `oklch(${(L - 0.08).toFixed(2)} ${C} ${H})`);
    r.setProperty('--signal-soft', `oklch(${L} ${C} ${H} / 0.13)`);
    r.setProperty('--paper',   `oklch(0.966 ${t.paperC} ${t.paperH})`);
    r.setProperty('--paper-2', `oklch(0.945 ${(t.paperC + 0.003).toFixed(3)} ${t.paperH})`);
    r.setProperty('--paper-3', `oklch(0.912 ${(t.paperC + 0.006).toFixed(3)} ${t.paperH})`);
    r.setProperty('--plate',   `oklch(0.978 ${(t.paperC * 0.7).toFixed(3)} ${t.paperH})`);
    r.setProperty('--ink',     `oklch(0.27 0.022 ${t.inkH})`);
    r.setProperty('--ink-2',   `oklch(0.42 0.020 ${t.inkH})`);
    return name;
  }

  function moodFor(state) {
    const hit = RULES.find(([, fn]) => fn(state));
    return hit ? hit[0] : 'Settling';
  }
  function applyState(state, root) { return apply(moodFor(state), root); }

  global.ElyceumMood = { MOODS, RULES, apply, moodFor, applyState };
})(window);
