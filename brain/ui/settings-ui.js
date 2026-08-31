/* =====================================================================
   BRAIN SETTINGS — persona-first engine (live-wired)
   Adapted from the Elyceum "Persona & Temperament" redesign to the LIVE app:
     • Personas are the top-level entity (left rail); each settings category
       is a TAB within the active persona. The Temperament tab holds the
       radial trait dials (a macro layer over the chemistry); other tabs
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
    'The Visionary': { DA: 0.62, ACh: 0.45, GABA: 0.32, Glu: 0.40, NE: 0.35, '5HT': 0.55, CORT: 0.05, OXT: 0.45, AEA: 0.20 },
    'The Empath':    { DA: 0.45, ACh: 0.18, GABA: 0.12, Glu: 0.18, NE: 0.15, '5HT': 0.70, CORT: 0.03, OXT: 0.70, AEA: 0.45 },
    'The Analyst':   { DA: 0.35, ACh: 0.35, GABA: 0.30, Glu: 0.25, NE: 0.25, '5HT': 0.55, CORT: 0.14, OXT: 0.22, AEA: 0.30 },
    'The Poet':      { DA: 0.32, ACh: 0.55, GABA: 0.12, Glu: 0.38, NE: 0.42, '5HT': 0.28, CORT: 0.15, OXT: 0.22, AEA: 0.38 },
    'The Sage':      { DA: 0.35, ACh: 0.18, GABA: 0.28, Glu: 0.12, NE: 0.12, '5HT': 0.72, CORT: 0.03, OXT: 0.50, AEA: 0.55 },
    'The Companion': { DA: 0.52, ACh: 0.35, GABA: 0.24, Glu: 0.32, NE: 0.25, '5HT': 0.60, CORT: 0.05, OXT: 0.65, AEA: 0.30 },
    'The Adversary': { DA: 0.30, ACh: 0.30, GABA: 0.40, Glu: 0.30, NE: 0.40, '5HT': 0.40, CORT: 0.20, OXT: 0.12, AEA: 0.15 },
    'The Mentor':    { DA: 0.45, ACh: 0.45, GABA: 0.35, Glu: 0.26, NE: 0.22, '5HT': 0.64, CORT: 0.04, OXT: 0.50, AEA: 0.30 },
    'The Concierge': { DA: 0.38, ACh: 0.28, GABA: 0.45, Glu: 0.18, NE: 0.22, '5HT': 0.60, CORT: 0.05, OXT: 0.35, AEA: 0.40 },
    'The Jester':    { DA: 0.55, ACh: 0.48, GABA: 0.16, Glu: 0.42, NE: 0.28, '5HT': 0.55, CORT: 0.04, OXT: 0.40, AEA: 0.50 },
    'The Stoic':     { DA: 0.35, ACh: 0.25, GABA: 0.42, Glu: 0.15, NE: 0.15, '5HT': 0.60, CORT: 0.05, OXT: 0.25, AEA: 0.45 },
    'The Cynic':     { DA: 0.25, ACh: 0.30, GABA: 0.30, Glu: 0.22, NE: 0.28, '5HT': 0.42, CORT: 0.18, OXT: 0.20, AEA: 0.22 },
    'The Admin':     { DA: 0.38, ACh: 0.45, GABA: 0.42, Glu: 0.30, NE: 0.25, '5HT': 0.60, CORT: 0.04, OXT: 0.35, AEA: 0.25 },
  };
  // Expose the (live) persona→chemistry table so Labs observe-mode can seed an
  // observed agent's resting bars. Same object reference, so custom personas added
  // below stay visible to readers.
  window.PERSONA_CHEM = PERSONA_CHEM;
  const CHANNELS = [
    { ch: 'DA', name: 'Dopamine' }, { ch: 'ACh', name: 'Acetylcholine' }, { ch: 'GABA', name: 'GABA' },
    { ch: 'Glu', name: 'Glutamate' }, { ch: 'NE', name: 'Norepinephrine' }, { ch: '5HT', name: 'Serotonin' },
    { ch: 'CORT', name: 'Cortisol' }, { ch: 'OXT', name: 'Oxytocin' }, { ch: 'AEA', name: 'Anandamide' },
  ];
  const CHEM_MIN = 0, CHEM_MAX = 0.8, CHEM_STEP = 0.01;

  /* ---- the temperament dials: a core temperament block, then Lingering, then the
     Motivation and Risk-posture groups (each flagged by its own comment below).
     Each map row: { key, dir, span }. Every key is a real backend settings key, so
     every dial both moves a real control and produces a valid /settings patch. (The
     Learning Rate dial is NOT here — learning rate is not a chemistry trait, so it
     lives in the Cognitive Style box below where it rests at neutral.) ---- */
  const TRAIT_DIALS = [
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
      // Two halves: a resting-mood tilt toward amused (the chem baselines) AND
      // a reward-side component — how much landing a laugh actually pays
      // (reward_weight_levity scales the laughter→DA event, which feeds the
      // Hebbian funnel: a high-Humor persona reinforces what earned laughs).
      map: [ { key: 'chem_baseline_DA', dir: +1, span: 0.10 }, { key: 'chem_baseline_AEA', dir: +1, span: 0.10 }, { key: 'chem_baseline_ACh', dir: +1, span: 0.05 }, { key: 'chem_baseline_GABA', dir: -1, span: 0.05 }, { key: 'chem_baseline_CORT', dir: -1, span: 0.05 }, { key: 'reward_weight_levity', dir: +1, span: 0.5 } ] },
    { id: 'sociability', label: 'Sociability', sub: 'outgoing · initiates', glyph: 'social',
      // voice expressiveness deliberately NOT here — it belongs to Empathy alone,
      // so two dials never silently sum into one voice parameter.
      map: [ { key: 'dmn_interval', dir: -1, span: 8 }, { key: 'proactive_idle_threshold', dir: -1, span: 90 }, { key: 'ach_suppression_weight', dir: -1, span: 0.35 } ] },
    { id: 'caution', label: 'Caution', sub: 'trusting ↔ guarded', glyph: 'shield',
      map: [ { key: 'hostility_GABA_threshold_high', dir: -1, span: 0.12 }, { key: 'cort_threat_increment', dir: +1, span: 0.012 }, { key: 'ne_hostility_weight', dir: +1, span: 0.06 }, { key: 'chem_baseline_OXT', dir: -1, span: 0.08 } ] },
    { id: 'lingering', label: 'Lingering', sub: 'resets fast ↔ carries the moment', glyph: 'echo',
      // Affect carryover: how strongly the LAST exchange's emotional residue
      // colors the next turn. Higher dial → lower trigger threshold → more
      // moments linger. (The two-phase rule: residue hints the NEXT turn only.)
      map: [ { key: 'affect_carryover_da_threshold', dir: -1, span: 0.06 } ] },
    /* ---- Motivation: what this persona finds REWARDING (reward-source
       valuation, multiplied onto its innate leaning) — distinct from Empathy/
       Drive, which shape expression and reward SENSITIVITY, not what counts as
       reward in the first place. ---- */
    { id: 'warmth-seeking', label: 'Warmth-seeking', sub: 'rewarded by connection', glyph: 'bond',
      map: [ { key: 'reward_weight_connection', dir: +1, span: 0.5 } ] },
    { id: 'curiosity-seeking', label: 'Curiosity-seeking', sub: 'rewarded by discovery', glyph: 'compass',
      map: [ { key: 'reward_weight_novelty', dir: +1, span: 0.5 } ] },
    { id: 'mastery-seeking', label: 'Mastery-seeking', sub: 'rewarded by being right', glyph: 'arrow',
      map: [ { key: 'reward_weight_correctness', dir: +1, span: 0.4 }, { key: 'reward_weight_mastery', dir: +1, span: 0.4 } ] },
    { id: 'beauty-seeking', label: 'Beauty-seeking', sub: 'rewarded by elegance', glyph: 'star',
      map: [ { key: 'reward_weight_aesthetic', dir: +1, span: 0.5 } ] },
    { id: 'relief-seeking', label: 'Relief-seeking', sub: 'rewarded by resolving tension', glyph: 'level',
      map: [ { key: 'reward_weight_relief', dir: +1, span: 0.5 } ] },
    /* ---- Risk posture: the AVOIDANCE-side mirror of Motivation — what the persona
       is wired to fear, independent of how much reward it feels. Loss-sensitivity is
       the asymmetric twin of reward sensitivity (a loss bites harder than an equal
       gain); Uncertainty-aversion is dread of a wide spread of outcomes, which also
       shapes how conservatively it DECIDES. Both pose from neuron._PERSONA_RISK_POSTURE
       (like Motivation); their *_scale multipliers rest neutral until you turn them. ---- */
    { id: 'loss-sensitivity', label: 'Loss-sensitivity', sub: 'shrugs it off ↔ losses cut deep', glyph: 'loss',
      map: [ { key: 'loss_aversion_scale', dir: +1, span: 0.5 } ] },
    { id: 'uncertainty-aversion', label: 'Uncertainty-aversion', sub: 'embraces unknown ↔ needs certainty', glyph: 'fork',
      map: [ { key: 'uncertainty_aversion_scale', dir: +1, span: 0.6 } ] },
  ];

  /* ---- cognitive-style dials — how the mind WORKS (vs temperament = who it
     is). A per-persona override that rests at NEUTRAL 0.5 (these mostly don't
     touch chemistry, so they aren't posed from the persona's chemistry spread).
     Same radial knobs, shown in a separate box below Temperament. ---- */
  const COGNITIVE_DIALS = [
    // The learning-rate knob. Low = static / traditional (near-frozen weights);
    // high = fast, accumulating learner. Bundles the real plasticity machinery,
    // plus live within-session learning + faster consolidation at the top end.
    { id: 'learning-rate', label: 'Learning Rate', sub: 'static ↔ fast-learning', glyph: 'spark',
      map: [
        // Step and forgetting move TOGETHER, not against each other. Their RATIO sets
        // every equilibrium and the rate alone sets how fast it is reached, so moving
        // them in lockstep makes this dial mean "how fast does it adapt" — which is what
        // static ↔ fast-learning promises. The old map drove them in OPPOSITE directions,
        // which compounded: at the top it pinned path edges at weight_max, and siblings
        // equal at the cap is uniform routing, i.e. the dial's fast end destroyed the
        // very differentiation it was supposed to sharpen. Range: 33→17 turns to settle.
        { key: 'hebbian_outcome_delta', dir: +1, span: 0.03 },
        { key: 'decay_toward_rest_rate_per_turn', dir: +1, span: 0.015 },
        { key: 'plasticity_arousal_weight', dir: +1, span: 0.30 },
        { key: 'plasticity_intensity_weight', dir: +1, span: 0.30 },
        { key: 'plasticity_turn_max', dir: +1, span: 0.40 },    // deeper per-turn encoding
        // Span was 1.50, which drove the ceiling to 1.50 at the LOW end — below the
        // fragment inject threshold (1.30) and far below promote (2.20), silently
        // clamping an economy this dial does not claim to touch. 0.50 keeps the floor
        // at 2.50, clear of both.
        { key: 'weight_max', dir: +1, span: 0.50 },
        { key: 'sleep_min_turns', dir: -1, span: 3 },           // consolidate more often
        { key: 'colony_trail_gain', dir: +1, span: 0.10 },      // strength of live trail reinforcement
      ],
      toggles: [
        { key: 'graded_plasticity', at: 0.55, mode: 'set' },        // intensity-scaled encoding once leaning up
        { key: 'colony_features', at: 0.80, mode: 'enableHigh' },   // top end: live within-session learning…
        { key: 'colony_trail_apply', at: 0.80, mode: 'enableHigh' },// …reinforce paths that pay off immediately
      ] },
    // Structural-plasticity appetite: how eagerly this persona grows NEW structure
    // (fragment attachments, recruited specialist units) vs keeping its wiring fixed.
    // High = explores more attachments and demands less proof before crystallizing a
    // node — both the proven-cluster bar and the workspace-ignition pressure floor.
    { id: 'plasticity', label: 'Plasticity', sub: 'fixed wiring ↔ grows new structure', glyph: 'cycle',
      map: [ { key: 'fragment_explore_rate', dir: +1, span: 0.10 },
             { key: 'node_promote_threshold', dir: -1, span: 0.30 },
             { key: 'ignition_recruit_min_score', dir: -1, span: 1.0 } ] },
    { id: 'focus', label: 'Focus', sub: 'scattered ↔ single-minded', glyph: 'target',
      // salience_workspace_threshold is a minimum BAR for workspace entry, so
      // single-minded = HIGHER bar (fewer topics promoted), dir +1.
      map: [ { key: 'ne_scatter_threshold', dir: +1, span: 0.10 }, { key: 'topic_activation_decay', dir: +1, span: 0.12 }, { key: 'dmn_overlap_threshold', dir: +1, span: 0.10 }, { key: 'salience_workspace_threshold', dir: +1, span: 0.12 } ] },
    { id: 'curiosity', label: 'Curiosity', sub: 'novelty-seeking', glyph: 'compass',
      map: [ { key: 'frontal_ach_weight', dir: +1, span: 0.15 }, { key: 'surprise_threshold', dir: -1, span: 0.12 }, { key: 'salience_ACh_weight', dir: +1, span: 0.06 } ] },
    { id: 'introspection', label: 'Introspection', sub: 'self-appraisal', glyph: 'spiral',
      map: [ { key: 'meta_interval', dir: -1, span: 15 }, { key: 'meta_cooldown_turns', dir: -1, span: 1.5 } ] },
    { id: 'memory', label: 'Memory', sub: 'in-the-moment ↔ recall', glyph: 'node',
      map: [ { key: 'hippocampus_priority_base', dir: +1, span: 0.18 }, { key: 'topic_activation_decay', dir: +1, span: 0.10 } ] },
    { id: 'emotionality', label: 'Emotionality', sub: 'logic ↔ feeling-driven', glyph: 'balance',
      // With flock_dynamics ON the controller owns modulation_gain per turn, so
      // this dial sets the controller's OPERATING BAND instead of fighting it:
      // the resting responsiveness target (sigma at low arousal) and where the
      // gain is allowed to travel. modulation_gain stays as the direct lever
      // for flag-off personas (the controller simply overwrites it when on).
      map: [ { key: 'flock_sigma_target_low', dir: +1, span: 0.05 },
             { key: 'flock_gain_max', dir: +1, span: 0.30 },
             { key: 'flock_gain_min', dir: +1, span: 0.20 },
             { key: 'modulation_gain', dir: +1, span: 1.0 } ] },
    { id: 'hindsight', label: 'Hindsight', sub: 'in-the-moment ↔ connects the dots', glyph: 'rewind',
      // Eligibility traces: how far back an outcome reaches when crediting the
      // turns that set it up. Low = only the immediate turn learns; high =
      // lessons attribute across the whole exchange.
      map: [ { key: 'eligibility_lookback', dir: +1, span: 2 },
             { key: 'eligibility_tau_turns', dir: +1, span: 1.2 } ] },
  ];
  const ALL_DIALS = TRAIT_DIALS.concat(COGNITIVE_DIALS);
  // Non-chemistry dials whose KEY VALUES are materialized per persona (cognitive
  // style + lingering). Motivation dials (warmth/curiosity/mastery-seeking) only
  // pose the needle — their backend is neuron._PERSONA_REWARD_WEIGHTS and the
  // reward_weight_* multipliers stay neutral. Must match the materializable set
  // in persona_chem._NONCHEM_DIAL_MAP.
  const _materializableDialIds = COGNITIVE_DIALS.map(d => d.id).concat(['lingering']);

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
    echo:   '<circle cx="9" cy="12" r="3"/><path d="M14.5 8.5a5 5 0 0 1 0 7M17.5 6a8.5 8.5 0 0 1 0 12"/>',
    rewind: '<polygon points="11 6 4 12 11 18" fill="currentColor" stroke="none"/><polygon points="20 6 13 12 20 18" fill="currentColor" stroke="none"/>',
    loss:   '<line x1="12" y1="4" x2="12" y2="18"/><polyline points="6 12 12 18 18 12"/>',
    fork:   '<path d="M12 21v-7"/><path d="M12 14L6 5"/><path d="M12 14l6-9"/><circle cx="6" cy="4" r="1.4" fill="currentColor" stroke="none"/><circle cx="18" cy="4" r="1.4" fill="currentColor" stroke="none"/>',
  };
  const ico = d => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
  const chevSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>';
  const lockSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
  const eyeSvg  = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
  const voiceSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Z"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>';
  // Per-persona voice key (matches index.html _personaSlug + backend resolution).
  const voiceKeyFor = name => 'persona_voice_' + String(name || '').toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '');
  let _voiceList = null;        // cached /voices response: [{voice_id,name}]
  let _voiceUnavailable = false;
  // Per-persona non-chemistry dial positions (cognitive + motivation + lingering),
  // from /settings. Temperament dials pose from chemistry; these have none, so
  // without this every persona shows them flat-neutral. { persona: { dialId: 0..1 } }.
  let PERSONA_POS = {};
  // Dials that pose from PERSONA_POS rather than chemistry (everything not in
  // REST_WEIGHTS). Built once from ALL_DIALS below.
  const _posedDialIds = new Set();
  const resetSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.5 15a9 9 0 1 0 2.1-9.4L1 10"/></svg>';
  const checkSvgM = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
  const fileSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3v5h5"/><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/></svg>';
  const moonSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
  const pencilSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
  const sparkSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.7 4.6L18.5 9.3l-4.8 1.7L12 16l-1.7-5L5.5 9.3l4.8-1.7z"/><path d="M19 14l.6 1.7 1.9.6-1.9.7-.6 1.7-.6-1.7-1.9-.7 1.9-.6z"/></svg>';

  /* ---- key metadata (from the real settings model) ---- */
  const rowMeta = {};
  SET.categories.forEach(cat => (cat.sections || []).forEach(sec => [...(sec.rows || []), ...(sec.advanced || [])].forEach(r => { if (r.key && r.type !== 'group') rowMeta[r.key] = r; })));
  // dial keys that exist in settings.json but aren't surfaced as a UI row
  const FALLBACK = {
    dmn_overlap_threshold: { min: 0.1, max: 0.8, def: 0.35 },
    modulation_gain: { min: 0, max: 2.0, def: 1.0 },
    flock_sigma_target_low: { min: 0.70, max: 0.98, def: 0.90 },
    flock_gain_max: { min: 1.0, max: 2.5, def: 1.8 },
    flock_gain_min: { min: 0.2, max: 0.9, def: 0.5 },
    affect_carryover_da_threshold: { min: 0.02, max: 0.40, def: 0.10 },
    reward_weight_connection: { min: 0.2, max: 2.0, def: 1.0 },
    reward_weight_novelty: { min: 0.2, max: 2.0, def: 1.0 },
    reward_weight_correctness: { min: 0.2, max: 2.0, def: 1.0 },
    reward_weight_mastery: { min: 0.2, max: 2.0, def: 1.0 },
    reward_weight_levity: { min: 0.2, max: 2.0, def: 1.0 },
    reward_weight_aesthetic: { min: 0.2, max: 2.0, def: 1.0 },
    reward_weight_relief: { min: 0.2, max: 2.0, def: 1.0 },
    loss_aversion_scale: { min: 0.4, max: 2.0, def: 1.0 },
    uncertainty_aversion_scale: { min: 0.3, max: 2.5, def: 1.0 },
    eligibility_lookback: { min: 0, max: 5, def: 2 },
    eligibility_tau_turns: { min: 0.5, max: 5.0, def: 2.0 },
  };
  const keyMeta = k => rowMeta[k] || FALLBACK[k] || { min: 0, max: 1, def: 0 };
  const clampKey = (k, v) => { const m = keyMeta(k); return Math.max(+m.min, Math.min(+m.max, v)); };

  /* ---- state ---- */
  const values = {}, saved = {}, refDefault = {}, dialCenter = {}, dial = {}, rest = {};
  let secretsSet = {};
  let isAdmin = false;                   // from /auth/me — platform super-admin (unrestricted ceilings)
  let orgAdmin = false;                  // from /auth/me — this org's admin; gates the operational/ops pages
  let persona = PERSONAS[0].id;
  let runningPersona = '';              // the persona the brain is actually running (vs. configured)
  let activeTab = 'persona';
  let view = 'persona';                 // 'persona' | 'system'
  const manualState = {};
  let manualOpen = false;
  let scaffolded = false;
  let systemPage = null;                // 'apikeys' | 'operational' when view==='system'

  // Per-persona saved knob setups (built-in overrides + customs), hydrated from
  // the unified spec store (GET /settings "personas"; per-persona files shared
  // with the engine API). id -> { custom, persisted, tag, note, chem:{9},
  // vals:{key:val} }. Custom ids ARE their slugs; rename only relabels.
  const personaStore = {};
  const BUILTIN_IDS = SET.personas.map(p => p.id);
  const isBuiltin = id => BUILTIN_IDS.includes(id);
  let storeChanged = false;             // persona created/renamed (unsaved) since last save
  // The unified store keys personas by SLUG (matches brain/persona_key.py).
  function slugify(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'default'; }
  // Catalogue id from an id, display name, or slug (settings may hold any of
  // them — persona_name written by older builds used display names).
  function resolvePersonaId(any) {
    if (!any) return null;
    const q = String(any), qs = slugify(q);
    const p = PERSONAS.find(x => x.id === q || x.name === q || x.slug === qs || slugify(x.id) === qs);
    return p ? p.id : null;
  }
  // Snapshot of `values` at the last persona select/save — the reference the
  // dirty count compares against in persona view. Comparing against the global
  // server `saved` map made merely VIEWING a non-running persona read as "N
  // unsaved" (its profile differs from the running persona's settings by
  // construction), which broke the leave-without-saving guard.
  let viewBaseline = null;

  // Per-persona self.md ("Sense of Self" tab). selfStore = live text, selfSaved
  // = last-saved text (dirty = differs). Seeded lazily from SET.selfModel, or
  // from personaStore[id].selfMd when one was saved earlier.
  const selfStore = {}, selfSaved = {};
  let selfMode = 'edit';                 // 'edit' | 'preview'  (Seed page only)
  let selfPage = 'seed';                 // 'seed' | 'living'   (sub-page of the tab)

  const allKeys = (() => { const s = new Set(); ALL_DIALS.forEach(d => d.map.forEach(t => s.add(t.key))); return [...s]; })();
  // toggle keys a dial flips at a threshold (not part of the additive map)
  const toggleKeys = (() => { const s = new Set(); ALL_DIALS.forEach(d => (d.toggles || []).forEach(t => s.add(t.key))); return [...s]; })();
  const isChem = k => k.indexOf('chem_baseline_') === 0;
  const chemOf = k => k.slice('chem_baseline_'.length);
  function personaBaseline(k) {
    if (!isChem(k)) return refDefault[k];
    const c = PERSONA_CHEM[persona]; return (c && c[chemOf(k)] != null) ? c[chemOf(k)] : refDefault[k];
  }
  // keys captured in a persona's saved snapshot: all 9 chem baselines + boot
  // levels, plus every non-chem key the dials touch (cognitive + global).
  function snapKeys() { const s = new Set(); CHANNELS.forEach(c => { s.add('chem_baseline_' + c.ch); s.add('chem_init_' + c.ch); }); allKeys.forEach(k => { if (!isChem(k)) s.add(k); }); toggleKeys.forEach(k => s.add(k)); return [...s]; }
  function snapshotVals() { const o = {}; snapKeys().forEach(k => { if (k in values) o[k] = values[k]; }); return o; }
  function currentChem() { const o = {}; CHANNELS.forEach(c => { o[c.ch] = +values['chem_baseline_' + c.ch]; }); return o; }

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
  // Pose a dial from a RESTING chemistry profile (the base rate). `chem` is the
  // persona's resting baseline, NOT its live/current state — so the needle shows
  // where the persona naturally sits, the same regardless of how its chemistry
  // has evolved this session.
  function dialRest(chem, d) {
    const w = REST_WEIGHTS[d.id];
    if (w && chem) {
      let s = 0, tw = 0;
      w.forEach(([ch, wt, dir]) => { const x = dir > 0 ? nrm(chem[ch]) : 1 - nrm(chem[ch]); s += wt * x; tw += wt; });
      return tw ? Math.max(0, Math.min(1, s / tw)) : 0.5;
    }
    // Non-chemistry dials (cognitive · motivation · lingering) pose from the
    // per-persona positions the server supplies. Absent → neutral (the Stoic).
    const p = (PERSONA_POS[persona] || {})[d.id];
    return (p != null) ? Math.max(0, Math.min(1, +p)) : 0.5;
  }
  // The key VALUES a persona's cognitive fingerprint implies. Sums each
  // materializable dial's offset into its keys (shared keys accumulate, matching
  // persona_chem._apply_cog_positions and recomputeTraits), then base + offset.
  // So a built-in persona's cognitive keys differ per persona even before a save,
  // and the loaded values match the posed needle. `persona` must be set to the
  // target id before calling.
  function cogFingerprintValues() {
    const pos = PERSONA_POS[persona] || {};
    const offset = {};
    _materializableDialIds.forEach(did => {
      if (pos[did] == null) return;
      const dial = ALL_DIALS.find(x => x.id === did); if (!dial) return;
      dial.map.forEach(t => { offset[t.key] = (offset[t.key] || 0) + t.dir * t.span * (+pos[did] - 0.5) * 2; });
    });
    const out = {};
    Object.keys(offset).forEach(k => {
      const base = (k in refDefault) ? +refDefault[k] : keyMeta(k).def;
      out[k] = base + offset[k];
    });
    return out;
  }
  // The persona's resting baseline as actually loaded/stored (chem_baseline_*),
  // which IS the base rate — used to pose the needles. Falls back to the
  // hardcoded canonical only if a channel is somehow missing.
  function personaRestChem() {
    const canon = PERSONA_CHEM[persona] || {};
    const o = {};
    CHANNELS.forEach(c => { const v = values['chem_baseline_' + c.ch]; o[c.ch] = (v != null && v !== '') ? +v : canon[c.ch]; });
    return o;
  }

  /* ---- recompute: dial positions -> real key values ---- */
  function recomputeTraits() {
    const offset = {}; allKeys.forEach(k => { offset[k] = 0; });
    ALL_DIALS.forEach(d => d.map.forEach(t => { offset[t.key] += t.dir * t.span * (dial[d.id] - rest[d.id]) * 2; }));
    allKeys.forEach(k => { values[k] = clampKey(k, dialCenter[k] + offset[k]); if (isChem(k)) values['chem_init_' + chemOf(k)] = values[k]; });
    // threshold toggles: flip a 0/1 key once the dial crosses `at`. mode 'set'
    // forces 0 below the threshold; 'enableHigh' only turns it on (leaves it
    // alone below, so it never clobbers a switch the user set elsewhere).
    ALL_DIALS.forEach(d => (d.toggles || []).forEach(t => {
      if (dial[d.id] >= t.at) values[t.key] = 1;
      else if (t.mode !== 'enableHigh') values[t.key] = 0;
    }));
  }
  function dialOffsetFor(key) { let o = 0; ALL_DIALS.forEach(d => d.map.forEach(t => { if (t.key === key) o += t.dir * t.span * (dial[d.id] - rest[d.id]) * 2; })); return o; }
  const moved = id => Math.abs(dial[id] - rest[id]) > 1e-4;

  /* ---- seed dials/centers for the active persona ----
     snap=true (persona switch): chemistry snaps to the persona's canonical
     baseline. snap=false (initial load): keep the loaded values. ---- */
  function seedDials(snap) {
    if (snap) CHANNELS.forEach(c => { values['chem_baseline_' + c.ch] = PERSONA_CHEM[persona][c.ch]; values['chem_init_' + c.ch] = PERSONA_CHEM[persona][c.ch]; });
    // Capture the resting baseline once, here, so dial leans (which change
    // chem_baseline) don't drag the needle's anchor with them.
    const base = personaRestChem();
    ALL_DIALS.forEach(d => { rest[d.id] = dialRest(base, d); dial[d.id] = rest[d.id]; });
    allKeys.forEach(k => { dialCenter[k] = (k in values) ? +values[k] : keyMeta(k).def; });
  }

  /* =====================================================================
     NETWORK — load / save / reset
     ===================================================================== */
  let saveBtn, resetBtn, restartBanner, dirtyPill, dirtyText, scroll;

  async function loadFromServer() {
    let s = {}, d = {}, serverSelfMd = '', serverPersonas = null;
    try {
      const res = await fetch('/settings');
      if (res.ok) { const data = await res.json(); s = data.settings || {}; d = data.defaults || {}; secretsSet = data.secrets_set || {}; serverSelfMd = data.self_md || ''; PERSONA_POS = data.persona_dial_positions || {}; if (Array.isArray(data.personas)) serverPersonas = data.personas; }
    } catch (e) { console.warn('Settings: load failed', e); }
    // Admin flag gates the operational/system pages. Best-effort: a normal user
    // (or a failed fetch) stays non-admin and gets the curated view.
    try {
      const me = await fetch('/auth/me');
      if (me.ok) { const j = await me.json(); isAdmin = !!j.is_admin; orgAdmin = !!(j.org_admin ?? j.is_admin); }
    } catch (e) { isAdmin = false; orgAdmin = false; }
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

    // Hydrate the persona catalogue from the UNIFIED store (per-persona spec
    // files, shared with the engine API — the server folds any legacy
    // persona_store blob into it on first read). Custom personas are keyed by
    // SLUG (their storage key everywhere); display_name is just a label, so a
    // rename never re-keys learned state. Built-ins keep their canonical seed
    // entry; an override spec's saved knob setup lands in personaStore[id].
    Object.keys(personaStore).forEach(k => delete personaStore[k]);
    Object.keys(selfStore).forEach(k => delete selfStore[k]);
    Object.keys(selfSaved).forEach(k => delete selfSaved[k]);
    PERSONAS.length = 0; SET.personas.forEach(p => PERSONAS.push({ ...p, slug: slugify(p.id) }));
    Object.keys(PERSONA_CHEM).forEach(k => { if (!isBuiltin(k)) delete PERSONA_CHEM[k]; });
    if (serverPersonas) {
      serverPersonas.forEach(spec => {
        if (!spec || !spec.slug) return;
        if (spec.builtin) {
          const meta = PERSONAS.find(p => p.slug === spec.slug);
          if (meta) meta.overridden = !!spec.overridden;
          if (!spec.overridden || !meta) return;
          // Override spec: restore its knob setup on select. An API-authored
          // override may carry only a baseline — synthesize chem vals so
          // applyPersonaVals restores it (PERSONA_CHEM stays canonical, so the
          // off-baseline badge + Restore defaults still work).
          const vals = { ...(spec.vals || {}) };
          if (!Object.keys(vals).length && spec.baseline) {
            CHANNELS.forEach(c => {
              if (spec.baseline[c.ch] == null) return;
              vals['chem_baseline_' + c.ch] = spec.baseline[c.ch];
              vals['chem_init_' + c.ch] = spec.baseline[c.ch];
            });
          }
          personaStore[meta.id] = {
            custom: false, persisted: true,
            tag: spec.tag || '', note: spec.note || '',
            vals: Object.keys(vals).length ? vals : undefined,
          };
        } else {
          const id = spec.slug;
          const name = spec.display_name || id;
          PERSONAS.push({
            id, slug: id, name,
            tag: spec.tag || 'Custom persona',
            note: spec.note || spec.disposition || '',
          });
          if (spec.baseline && Object.keys(spec.baseline).length) PERSONA_CHEM[id] = spec.baseline;
          personaStore[id] = {
            custom: true, persisted: true,
            tag: spec.tag || '', note: spec.note || '',
            chem: spec.baseline,
            vals: (spec.vals && Object.keys(spec.vals).length) ? spec.vals : undefined,
          };
        }
      });
    } else {
      // Old server (no unified catalogue in the response) — legacy blob path.
      try {
        const ps = s.persona_store ? JSON.parse(s.persona_store) : {};
        Object.entries(ps).forEach(([name, e]) => {
          if (!e || typeof e !== 'object') return;
          personaStore[name] = e;
          if (e.custom && !PERSONAS.find(p => p.id === name)) {
            PERSONAS.push({ id: name, name, slug: slugify(name), tag: e.tag || 'Custom persona', note: e.note || '' });
            if (e.chem) PERSONA_CHEM[name] = e.chem;
          }
        });
      } catch (err) { console.warn('persona_store parse failed', err); }
    }
    storeChanged = false;

    // The stored persona_name may predate slug-keyed customs (an old display
    // name) — resolve it through the catalogue instead of requiring an exact hit.
    persona = resolvePersonaId(s.persona_name) || PERSONAS[0].id;
    // The persona the brain is actually RUNNING (vs. the one being configured). Used to
    // decouple configure-from-switch: saving a non-running persona persists its profile
    // without restarting the live brain (the switch stays the explicit MRI action).
    runningPersona = persona;
    if (!('persona_name' in saved)) { values.persona_name = saved.persona_name = persona; }
    if (!(persona in manualState)) manualState[persona] = false;
    // seed the active persona's self.md from the server response
    if (serverSelfMd) { selfStore[persona] = serverSelfMd; selfSaved[persona] = serverSelfMd; }

    seedDials(false);
    view = 'persona'; activeTab = 'persona';
    captureViewBaseline();
    // Render only if a persona scaffold is currently mounted (the Personas workspace
    // pane). At boot the scaffold doesn't exist yet — mountPersona() builds + renders
    // it on demand — so loadFromServer is data-only there. This also prevents a stray
    // boot-time scaffold in the (now slim) Settings page and the duplicate-id clash.
    if (document.getElementById('pers-cat-wrap')) {
      buildScaffold();
      renderTabs();
      renderAllDials();
      renderChem();
      applyChemDisplay(false);
      syncPersonaHead();
      refreshManualUI();
      selectTab('persona');
      refreshDirty();
    }
    // The catalogue now holds the org's custom personas (the built-in seed alone
    // doesn't). Surfaces that painted before this landed are showing a short list.
    notifyPersonaCatalogue({});
  }

  function realChangedPatch() {
    const patch = {};
    Object.keys(saved).forEach(k => { if (values[k] !== saved[k]) patch[k] = values[k]; });
    Object.keys(values).forEach(k => { if (!(k in saved) && values[k] !== '' && values[k] != null) patch[k] = values[k]; });
    return patch;
  }
  // Persona view: edits since the persona was selected (viewBaseline), so just
  // LOOKING at a non-running persona is clean. System pages: diff vs the server.
  function dirtyCount() {
    let n = 0;
    if (view === 'persona' && viewBaseline) {
      const ks = new Set([...Object.keys(viewBaseline), ...Object.keys(values)]);
      ks.forEach(k => { if (values[k] !== viewBaseline[k]) n++; });
    } else {
      n = Object.keys(realChangedPatch()).length;
    }
    return n + (storeChanged ? 1 : 0) + selfDirtyCount();
  }
  function captureViewBaseline() { viewBaseline = { ...values }; }

  // The viewed persona's unified-spec write: slug + display name + catalogue
  // metadata + resolved baseline + the full knob snapshot. The server routes it
  // to the per-persona spec file (brain/personas.py) — the same store the
  // engine API authors — so both surfaces read and write ONE catalogue.
  // Built-ins send no identity fields (the spec store keeps those canonical;
  // baseline/vals land as an override spec).
  function personaSpecPayload() {
    const meta = PERSONAS.find(p => p.id === persona) || {};
    const payload = { slug: meta.slug || slugify(persona), baseline: currentChem(), vals: snapshotVals() };
    if (!isBuiltin(persona)) {
      payload.display_name = meta.name || persona;
      payload.tag = meta.tag || '';
      payload.note = meta.note || '';
    }
    return payload;
  }
  function markPersonaPersisted() {
    const e = personaStore[persona] || (personaStore[persona] = { custom: !isBuiltin(persona) });
    e.persisted = true;
  }
  // Persist self.md edits made to OTHER personas this session (each rides its
  // own config-only save; the blob that used to carry them all is gone).
  async function flushOtherDirtySelfs() {
    const ids = Object.keys(selfStore).filter(id => id !== persona && selfStore[id] !== selfSaved[id]);
    for (const id of ids) {
      try {
        const res = await fetch('/settings', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ config_persona: id, config_self_md: selfStore[id] }),
        });
        if (res.ok) selfSaved[id] = selfStore[id];
      } catch (e) { console.warn('self.md save failed for', id, e); }
    }
  }

  async function doSave() {
    // Configuring a NON-running persona: persist its profile WITHOUT switching the live
    // brain (the decouple). The server writes this persona's own chemistry file + its
    // unified spec and does NOT re-exec; switching stays the explicit
    // Open-in-MRI / "Switch to this persona" action.
    if (view === 'persona' && persona && persona !== runningPersona) {
      syncStoreFromCurrent();
      const body = { config_persona: persona, config_chem: {}, config_chem_init: {}, persona_spec: personaSpecPayload() };
      CHANNELS.forEach(c => { body.config_chem[c.ch] = +values['chem_baseline_' + c.ch]; body.config_chem_init[c.ch] = +values['chem_init_' + c.ch]; });
      if (selfStore[persona] != null && selfStore[persona] !== selfSaved[persona]) body.config_self_md = selfStore[persona];
      if (saveBtn) saveBtn.textContent = 'Saving…';
      try {
        const res = await fetch('/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (res.ok) {
          Object.keys(values).forEach(k => saved[k] = values[k]);
          if (selfStore[persona] != null) selfSaved[persona] = selfStore[persona];
          await flushOtherDirtySelfs();
          markPersonaPersisted();
          storeChanged = false; captureViewBaseline(); refreshDirty();
          if (saveBtn) { saveBtn.textContent = 'Saved ✓'; setTimeout(() => saveBtn.textContent = 'Save', 1600); }
        } else if (saveBtn) { saveBtn.textContent = 'Error ' + res.status; setTimeout(() => saveBtn.textContent = 'Save', 2200); }
      } catch (e) {
        console.error('Settings config-save error', e);
        if (saveBtn) { saveBtn.textContent = 'Error (no server)'; setTimeout(() => saveBtn.textContent = 'Save', 2200); }
      }
      return;
    }
    const patch = realChangedPatch();
    // On a persona, always carry its chemistry + unified spec so the active
    // persona's full setup persists (a brand-new persona has no dirty keys vs
    // the one it cloned, but still needs its chem written + spec stored).
    if (view === 'persona' && persona) {
      CHANNELS.forEach(c => { patch['chem_baseline_' + c.ch] = values['chem_baseline_' + c.ch]; patch['chem_init_' + c.ch] = values['chem_init_' + c.ch]; });
      patch.persona_name = persona;
      syncStoreFromCurrent();
      if (selfStore[persona] !== selfSaved[persona]) patch.self_md = selfStore[persona];
      patch.persona_spec = personaSpecPayload();
    }
    const selfChanged = selfDirtyCount() > 0;
    const meaningful = Object.keys(patch).some(k => k !== 'persona_spec' && k !== 'persona_name' && !(k.startsWith('chem_') && values[k] === saved[k]));
    if (!meaningful && !storeChanged && !selfChanged) return;
    if (saveBtn) saveBtn.textContent = 'Saving…';
    try {
      const res = await fetch('/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
      if (res.ok) {
        let data = null; try { data = await res.json(); } catch (_) {}
        Object.keys(values).forEach(k => saved[k] = values[k]);
        if (view === 'persona' && persona && selfStore[persona] != null) selfSaved[persona] = selfStore[persona];
        await flushOtherDirtySelfs();
        if (view === 'persona' && persona) markPersonaPersisted();
        storeChanged = false; captureViewBaseline();
        applyGenericDisplay(); refreshDirty();
        if (data && data.restarting) {
          // The save switched the active persona — the server is re-execing to
          // re-namespace per-persona state. Say so, poll until it's back, then
          // re-sync so the page reflects what the new boot actually loaded.
          if (saveBtn) saveBtn.textContent = 'Switching persona…';
          const poll = () => fetch('/settings')
            .then(r => { if (!r.ok) throw new Error('not up'); })
            .then(() => { if (saveBtn) saveBtn.textContent = 'Save Settings'; loadFromServer(); })
            .catch(() => setTimeout(poll, 2000));
          setTimeout(poll, 2500);
        } else {
          if (restartBanner) restartBanner.classList.add('on', 'visible');
          if (saveBtn) { saveBtn.textContent = 'Saved ✓'; setTimeout(() => saveBtn.textContent = 'Save Settings', 1600); }
        }
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
        `<div class="tdrives">drives ${d.map.length + (d.toggles ? d.toggles.length : 0)} controls</div>`;
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
    } else if (r.type === 'select') {
      const sel = document.createElement('select'); sel.className = 'es-select';
      sel.style.cssText = 'flex:0 0 auto;min-width:180px;padding:6px 10px;border-radius:8px;border:1px solid rgba(255,255,255,0.15);background:rgba(255,255,255,0.05);color:inherit;font:inherit;';
      (r.options || []).forEach(o => { const opt = document.createElement('option'); opt.value = o.v; opt.textContent = o.l; sel.appendChild(opt); });
      sel.value = (values[r.key] != null && values[r.key] !== '') ? String(values[r.key]) : (r.def || '');
      sel.addEventListener('change', () => { if (!manualOpen && !systemPage) { sel.value = String(values[r.key] != null ? values[r.key] : (r.def || '')); return; } values[r.key] = sel.value; refreshDirty(); });
      ctrl.appendChild(sel); genReg[r.key] = { row, select: sel };
    } else if (r.type === 'text') {
      const ta = document.createElement('textarea'); ta.className = 'es-textarea'; ta.rows = r.rows || 4; ta.spellcheck = false;
      ta.value = (values[r.key] != null) ? values[r.key] : ''; if (r.placeholder) ta.placeholder = r.placeholder;
      ta.style.cssText = 'flex:1 1 100%;min-width:220px;resize:vertical;';
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
    if (e.select) e.select.value = String(values[key] != null ? values[key] : (r.def || ''));
    if (e.toggle) e.toggle.classList.toggle('on', +values[key] >= 0.5);
    e.row.classList.toggle('changed', isChanged(key));
  }
  function applyGenericDisplay() { Object.keys(genReg).forEach(updateGen); }
  function genSection(sec) {
    const card = document.createElement('div'); card.className = 'es-card';
    const head = document.createElement('div'); head.className = 'es-card-head';
    head.innerHTML = `<span class="es-num">${sec.num}</span><div class="es-ct"><div class="es-card-title">${sec.title}</div><div class="es-card-desc">${sec.desc || ''}</div></div><span class="es-chev">${chevSvg}</span>`;
    const body = document.createElement('div'); body.className = 'es-card-body';
    // adminOnly rows are org-wide operational switches; the server strips them from
    // non-admin saves, so hiding them here avoids a control that silently no-ops.
    const visible = r => !r.adminOnly || orgAdmin || isAdmin;
    (sec.rows || []).filter(visible).forEach(r => body.appendChild(genRow(r)));
    const advRows = (sec.advanced || []).filter(visible);
    if (advRows.length) {
      const adv = document.createElement('div'); adv.className = 'es-adv';
      const cnt = advRows.filter(r => r.type !== 'group').length;
      const tg = document.createElement('button'); tg.className = 'es-adv-toggle'; tg.innerHTML = `<span class="es-ac">${chevSvg}</span><span>Advanced</span><span class="es-advn">${cnt}</span>`;
      const ab = document.createElement('div'); ab.className = 'es-adv-body';
      advRows.forEach(r => ab.appendChild(genRow(r)));
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
    const wrap = document.getElementById('settings-generic'); if (!wrap) return;
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
    // Additional sections on the API Keys page (e.g. provider selection) render
    // through the generic section builder — select/toggle/range rows all work.
    (cat.sections || []).slice(1).forEach(sec => wrap.appendChild(genSection(sec)));
  }

  /* ---- API docs (System) ---- */
  function renderApiDocs() {
    const wrap = document.getElementById('settings-generic'); if (!wrap) return;
    wrap.innerHTML = ''; Object.keys(genReg).forEach(k => delete genReg[k]);

    const origin = window.location.origin;

    function card(title, desc, bodyHtml) {
      const c = document.createElement('div'); c.className = 'es-card';
      c.innerHTML = `<div class="es-card-head static"><span class="es-num">✦</span><div class="es-ct"><div class="es-card-title">${title}</div><div class="es-card-desc">${desc}</div></div></div><div class="es-card-body">${bodyHtml}</div>`;
      return c;
    }
    function ep(method, path, desc) {
      return `<div class="apidoc-endpoint"><span class="apidoc-method ${method.toLowerCase()}">${method}</span><span class="apidoc-path">${path}</span><span class="apidoc-desc">${desc}</span></div>`;
    }
    function code(text, label) {
      const id = 'adc-' + Math.random().toString(36).slice(2);
      return (label ? `<div class="apidoc-label">${label}<button class="apidoc-copy" onclick="(function(b){navigator.clipboard.writeText(document.getElementById('${id}').textContent).then(()=>{b.textContent='copied';setTimeout(()=>b.textContent='copy',1400)});})(this)">copy</button></div>` : '') +
        `<div class="apidoc-code" id="${id}">${text}</div>`;
    }

    // Auth card
    wrap.appendChild(card(
      'Authentication',
      'Every route checks the session cookie set by POST /auth/login. For API clients, pass the access_token as a Bearer header instead.',
      code(
        `# 1. Obtain a token\ncurl -s -X POST ${origin}/auth/login \\\n  -H 'Content-Type: application/json' \\\n  -d '{"email":"you@example.com","password":"…"}'\n\n# 2. Use it on subsequent calls\ncurl -s ${origin}/settings \\\n  -H 'Authorization: Bearer <access_token>'`,
        'Example'
      )
    ));

    // Core endpoints card
    wrap.appendChild(card(
      'Core endpoints',
      'Standard JSON over HTTP. All responses are application/json unless otherwise noted.',
      ep('GET',    '/health',             'Readiness check — returns {"ok":true}') +
      ep('GET',    '/settings',           'Current brain settings + defaults + which API keys are set') +
      ep('POST',   '/settings',           'Patch settings — body is a partial key→value object') +
      ep('POST',   '/settings/reset',     "Reset one persona's settings to factory defaults") +
      ep('GET',    '/voices',             'List available ElevenLabs voices')
    ));

    // Auth endpoints card
    wrap.appendChild(card(
      'Auth endpoints',
      'Login, logout, and password reset flows.',
      ep('POST',   '/auth/login',         'Email + password → sets session cookies; returns access_token') +
      ep('POST',   '/auth/logout',        'Clears session cookies') +
      ep('POST',   '/auth/forgot',        'Send a password-reset email') +
      ep('GET',    '/auth/me',            'Current session claims (sub, email, is_admin)')
    ));

    // Key vault card
    wrap.appendChild(card(
      'Key vault',
      'Provider API keys are stored encrypted per-user in Supabase Vault. Values are write-only — the API only confirms whether each key is set.',
      ep('GET',    '/api/keys',           'Status map: {"anthropic":true,"elevenlabs":false,…}') +
      ep('POST',   '/api/keys',           'Set a key — body: {"provider":"anthropic","value":"sk-…"}') +
      ep('DELETE', '/api/keys/{provider}','Remove a stored key; provider ∈ anthropic · elevenlabs · deepgram · google')
    ));

    // WebSocket card
    wrap.appendChild(card(
      'WebSocket',
      'Real-time conversation and brain-state stream. The browser UI connects here; external clients can too.',
      code(`wss://${window.location.host}/ws\n\n# Auth: the session cookie is sent automatically by the browser.\n# API clients: pass the token as a query param or Sec-WebSocket-Protocol header\n#   (server reads the 'access' cookie; set it before connecting).`, 'Endpoint') +
      code(
        `// Outbound — send a chat message\n{ "type": "chat", "text": "hello" }\n\n// Outbound — image attachment\n{ "type": "chat", "text": "describe this", "image_url": "data:image/png;base64,…" }\n\n// Inbound — streaming token\n{ "type": "token", "text": "…" }\n\n// Inbound — full brain-state tick\n{ "type": "state", "mood": "…", "chem": {…}, "emotion": "…" }\n\n// Inbound — turn complete\n{ "type": "done" }`,
        'Message shapes'
      )
    ));
  }

  /* =====================================================================
     RENDER — Sense of Self (per-persona self.md editor)
     ===================================================================== */
  function personaMeta(id) { return PERSONAS.find(p => p.id === id) || { id, name: id, tag: '', note: '' }; }
  function buildSelf(id) {
    const SM = SET.selfModel || {}, base = SM.base || {};
    const p = personaMeta(id);
    const blk = (SM.personas && SM.personas[id]) || SM.fallback || { personality: '', speaking: '' };
    const tagLine = p.tag ? p.tag.replace(/\s*·\s*/g, ', ') : '';
    return [
      `# Self-Model — ${p.name}`, '',
      `> Seeded from the shared identity scaffold + the ${p.name} archetype${tagLine ? ' (' + tagLine + ')' : ''}. The brain rewrites this for itself at sleep consolidation.`, '',
      `## Who I am`, '', base.whatIAm || '', '',
      `## Core drives`, '', base.drives || '', '',
      `## Guiding principles (non-negotiable)`, '', base.principles || '', '',
      `## Personality`, '', blk.personality || '', '',
      `## Speaking style`, '', blk.speaking || '', '',
      `## Values`, '', base.values || '',
    ].join('\n');
  }
  function ensureSelf(id) {
    if (id in selfStore) return;
    const stored = personaStore[id] && personaStore[id].selfMd;
    const txt = (typeof stored === 'string') ? stored : buildSelf(id);
    selfStore[id] = txt; selfSaved[id] = txt;
    // Hosted source of truth: pull the persona's stored self.md from the server
    // and replace the local template/persona_store copy — unless the user has
    // already started editing this persona's seed in this session.
    fetch('/self-model?persona=' + encodeURIComponent(id))
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        const remote = data && data.content && data.content.trim();
        if (!remote) return;
        if (selfStore[id] !== selfSaved[id]) return; // dirty — don't clobber edits
        if (selfStore[id] === remote) return;
        selfStore[id] = remote; selfSaved[id] = remote;
        if (persona === id && activeTab === 'self' && selfPage === 'seed') renderSelf();
      })
      .catch(() => {});
  }
  function selfDirtyCount() { return Object.keys(selfStore).filter(id => selfStore[id] !== selfSaved[id]).length; }

  /* tiny markdown renderer for the Preview pane (headings, bold, code, lists,
     blockquote, hr) — scoped to what self.md actually uses. */
  function mdEsc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function mdInline(s) { return mdEsc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/\*([^*]+)\*/g, '<em>$1</em>'); }
  function mdToHtml(src) {
    const lines = (src || '').split('\n'); let html = '', list = null;
    const close = () => { if (list) { html += `</${list}>`; list = null; } };
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, ''); let m;
      if (!line.trim()) { close(); continue; }
      if (/^---+$/.test(line.trim())) { close(); html += '<hr>'; continue; }
      if ((m = line.match(/^(#{1,3})\s+(.*)$/))) { close(); const lv = m[1].length; html += `<h${lv}>${mdInline(m[2])}</h${lv}>`; continue; }
      if ((m = line.match(/^>\s?(.*)$/))) { close(); html += `<blockquote>${mdInline(m[1])}</blockquote>`; continue; }
      if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { if (list !== 'ul') { close(); html += '<ul>'; list = 'ul'; } html += `<li>${mdInline(m[1])}</li>`; continue; }
      if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) { if (list !== 'ol') { close(); html += '<ol>'; list = 'ol'; } html += `<li>${mdInline(m[1])}</li>`; continue; }
      close(); html += `<p>${mdInline(line)}</p>`;
    }
    close();
    return html || '<div class="empty">Nothing here yet — write this persona\u2019s sense of self in the editor.</div>';
  }

  function renderSelf() {
    const wrap = document.getElementById('tab-generic'); if (!wrap) return;
    wrap.innerHTML = ''; Object.keys(genReg).forEach(k => delete genReg[k]);
    ensureSelf(persona);
    const cat = SET.categories.find(c => c.id === 'self');
    if (cat && cat.summary) { const b = document.createElement('div'); b.className = 'es-cat-blurb'; b.textContent = cat.summary; wrap.appendChild(b); }

    const sw = document.createElement('div'); sw.className = 'self-wrap';
    const nav = document.createElement('div'); nav.className = 'self-pages';
    const pageBtn = (pg, ic, title, sub) =>
      `<button type="button" class="self-page" data-pg="${pg}"><span class="pg-ic">${ic}</span><span class="pg-t"><span class="pg-title">${title}</span><span class="pg-sub">${sub}</span></span></button>`;
    nav.innerHTML = pageBtn('seed', pencilSvg, 'Seed', 'You author it') + pageBtn('living', sparkSvg, 'Living self-model', 'It authors itself');
    sw.appendChild(nav);
    const host = document.createElement('div'); host.id = 'self-host'; sw.appendChild(host);
    wrap.appendChild(sw);

    const paintNav = () => nav.querySelectorAll('.self-page').forEach(b => b.classList.toggle('on', b.dataset.pg === selfPage));
    const renderBody = () => { host.innerHTML = ''; if (selfPage === 'living') renderSelfLiving(host); else renderSelfSeed(host); };
    nav.querySelectorAll('.self-page').forEach(b => b.addEventListener('click', () => { selfPage = b.dataset.pg; paintNav(); renderBody(); }));
    paintNav(); renderBody();
  }

  // Seed page — editable starting self-model the human authors.
  function renderSelfSeed(host) {
    const p = personaMeta(persona);
    const ed = document.createElement('div'); ed.className = 'self-editor';
    ed.innerHTML =
      `<div class="self-bar">` +
        `<span class="self-file">${fileSvg}<b>self.md</b> · <span style="color:var(--ink-4)">seed</span></span>` +
        `<span class="self-modepill" id="self-dirty"><i></i>edited</span>` +
        `<span class="spacer"></span>` +
        `<span class="self-meta" id="self-count"></span>` +
        `<div class="self-seg" id="self-seg"><button type="button" data-m="edit">Edit</button><button type="button" data-m="preview">Preview</button></div>` +
        `<button class="self-revert" id="self-revert" title="Restore the seeded self-model">${resetSvg}Restore seed</button>` +
      `</div>` +
      `<textarea class="self-area" id="self-area" spellcheck="false" placeholder="Write this persona\u2019s sense of self…"></textarea>` +
      `<div class="self-preview" id="self-preview" hidden></div>`;
    host.appendChild(ed);
    const foot = document.createElement('div'); foot.className = 'self-foot';
    foot.innerHTML = `${pencilSvg}<span>The starting point you hand the brain. It's loaded into working memory at the start of every session — then the brain grows from it on its own. Change where it begins by editing here; what it has become so far lives on the <b>Living self-model</b> page.</span>`;
    host.appendChild(foot);

    const area = ed.querySelector('#self-area'), preview = ed.querySelector('#self-preview');
    const count = ed.querySelector('#self-count'), dpill = ed.querySelector('#self-dirty');
    area.value = selfStore[persona];
    const updateMeta = () => {
      const t = selfStore[persona] || '';
      const words = (t.trim().match(/\S+/g) || []).length;
      count.textContent = `${words} words · ${t.length.toLocaleString()} chars`;
      dpill.classList.toggle('on', selfStore[persona] !== selfSaved[persona]);
    };
    const applyMode = () => {
      const pre = selfMode === 'preview';
      area.hidden = pre; preview.hidden = !pre;
      if (pre) preview.innerHTML = mdToHtml(selfStore[persona]);
      ed.querySelectorAll('#self-seg button').forEach(b => b.classList.toggle('on', b.dataset.m === selfMode));
    };
    updateMeta(); applyMode();
    area.addEventListener('input', () => { selfStore[persona] = area.value; updateMeta(); refreshDirty(); });
    ed.querySelectorAll('#self-seg button').forEach(b => b.addEventListener('click', () => { selfMode = b.dataset.m; applyMode(); }));
    ed.querySelector('#self-revert').addEventListener('click', () => {
      if (!window.confirm(`Restore ${p.name}'s self-model to the seeded version? Edits to this persona's self.md will be discarded.`)) return;
      selfStore[persona] = buildSelf(persona); area.value = selfStore[persona];
      if (selfMode === 'preview') preview.innerHTML = mdToHtml(selfStore[persona]);
      updateMeta(); refreshDirty();
    });
  }

  // Living page — read-only view of what the brain has rewritten for itself.
  function renderSelfLiving(host) {
    const ed = document.createElement('div'); ed.className = 'self-editor self-living';
    ed.innerHTML =
      `<div class="self-bar">` +
        `<span class="self-file">${fileSvg}<b>self.md</b> · <span style="color:var(--ink-4)">living</span></span>` +
        `<span class="self-ro">${lockSvg}read-only</span>` +
        `<span class="spacer"></span>` +
        `<span class="self-meta" id="self-living-meta"></span>` +
      `</div>` +
      `<div class="self-preview" id="self-living-body"><span style="opacity:.45">Loading…</span></div>`;
    host.appendChild(ed);
    const foot = document.createElement('div'); foot.className = 'self-foot';
    foot.innerHTML = `${moonSvg}<span>The brain wrote this for itself — revised over sleep passes. It's read here, not edited: the brain owns this document. To change where it starts from, edit the <b>Seed</b> — it grows from there.</span>`;
    host.appendChild(foot);
    fetch('/self-model?persona=' + encodeURIComponent(persona)).then(r => r.ok ? r.json() : null).then(data => {
      const bodyEl = ed.querySelector('#self-living-body');
      const metaEl = ed.querySelector('#self-living-meta');
      const content = (data && data.content) ? data.content.trim() : '';
      if (content) {
        const words = (content.match(/\S+/g) || []).length;
        metaEl.textContent = words + ' words';
        bodyEl.innerHTML = mdToHtml(content);
      } else {
        bodyEl.innerHTML = `<span style="opacity:.45">No living self-model yet — the brain will build one during sleep passes.</span>`;
      }
    }).catch(() => {
      const bodyEl = ed.querySelector('#self-living-body');
      if (bodyEl) bodyEl.innerHTML = `<span style="opacity:.45">Could not load living self-model.</span>`;
    });
  }

  // "Sense of You" tab — read-only view of the persona's model of the people it
  // talks to: user.md plus one living model per identified person (user_<slug>.md
  // — voice-identified companions and every API end user), all written during
  // sleep consolidation. Mirrors the Living page.
  function renderSenseOfYou() {
    const wrap = document.getElementById('tab-generic'); if (!wrap) return;
    wrap.innerHTML = ''; Object.keys(genReg).forEach(k => delete genReg[k]);
    const cat = SET.categories.find(c => c.id === 'you');
    if (cat && cat.summary) { const b = document.createElement('div'); b.className = 'es-cat-blurb'; b.textContent = cat.summary; wrap.appendChild(b); }
    const holder = document.createElement('div');
    holder.innerHTML = `<div class="self-editor self-living"><div class="self-preview"><span style="opacity:.45">Loading…</span></div></div>`;
    wrap.appendChild(holder);
    const foot = document.createElement('div'); foot.className = 'self-foot';
    foot.innerHTML = `${moonSvg}<span>What this persona has learned about the people it talks to — one living model per person it has met, in conversation here or as an end user over the API. Gathered from conversations, revised over sleep passes. Read here, not edited: the brain keeps these models for itself.</span>`;
    wrap.appendChild(foot);
    const modelBlock = (title, sub, content) => {
      const words = (content.match(/\S+/g) || []).length;
      return `<div class="self-editor self-living" style="margin-bottom:12px">` +
        `<div class="self-bar">` +
          `<span class="self-file">${fileSvg}<b>${mdEsc(title)}</b> · <span style="color:var(--ink-4)">${mdEsc(sub)}</span></span>` +
          `<span class="self-ro">${lockSvg}read-only</span>` +
          `<span class="spacer"></span>` +
          `<span class="self-meta">${words} words</span>` +
        `</div>` +
        `<div class="self-preview">${mdToHtml(content)}</div>` +
      `</div>`;
    };
    fetch('/user-model?persona=' + encodeURIComponent(persona)).then(r => r.ok ? r.json() : null).then(data => {
      if (!holder.isConnected) return; // tab switched away before the fetch resolved
      const content = (data && data.content) ? data.content.trim() : '';
      const speakers = (data && Array.isArray(data.speakers)) ? data.speakers : [];
      let html = '';
      if (content) html += modelBlock('user.md', 'living', content);
      for (const s of speakers) {
        const body = ((s && s.content) || '').trim(); if (!body) continue;
        html += modelBlock(s.name || s.file, (s.file || '') + ' · living', body);
      }
      if (!html) {
        html = `<div class="self-editor self-living">` +
          `<div class="self-bar"><span class="self-file">${fileSvg}<b>user.md</b> · <span style="color:var(--ink-4)">living</span></span><span class="self-ro">${lockSvg}read-only</span><span class="spacer"></span></div>` +
          `<div class="self-preview"><span style="opacity:.45">No model of you yet — the persona builds one per person from your conversations (companion and API sessions alike) during sleep consolidation.</span></div></div>`;
      }
      holder.innerHTML = html;
    }).catch(() => {
      if (holder.isConnected) holder.innerHTML = `<div class="self-editor self-living"><div class="self-preview"><span style="opacity:.45">Could not load the user model.</span></div></div>`;
    });
  }

  /* =====================================================================
     SCAFFOLD + TABS + RAIL + HEAD
     ===================================================================== */
  function buildScaffold() {
    if (scaffolded) return;
    // Persona config now lives in the Personas workspace (#pers-cat-wrap); the old
    // settings-page #cat-wrap is the fallback (kept only while migrating).
    const wrap = document.getElementById('pers-cat-wrap') || document.getElementById('cat-wrap'); if (!wrap) return;
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
        `<div class="pdetail-voice" id="st-voicebar">` +
          `<span class="pv-icon">${voiceSvg}</span>` +
          `<label class="pv-label" for="st-voice">Voice</label>` +
          `<select class="pv-select" id="st-voice" title="This persona's speaking voice"></select>` +
        `</div>` +
        `<button class="es-del-persona" id="st-delete" style="display:none">Delete persona</button>` +
        `<nav class="tabbar" id="st-tabbar"></nav>` +
      `</div>` +
      `<div id="tab-temperament"><div class="es-card">` +
        `<div class="es-card-head static"><span class="es-num">00</span>` +
          `<div class="es-ct"><div class="es-card-title">Temperament</div>` +
          `<div class="es-card-desc">The dials that shape who this persona is — its felt traits, what it's drawn toward (motivation), and what it's wired to fear (risk posture). Each rests where this persona naturally sits and quietly turns a whole bundle of underlying controls at once — turn one to lean the temperament that way.</div></div>` +
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
    document.getElementById('st-delete').addEventListener('click', () => deletePersona(persona));
    const nm = document.getElementById('st-name');
    nm.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); nm.blur(); } });
    nm.addEventListener('blur', () => { if (view === 'persona' && !isBuiltin(persona)) renamePersona(nm.textContent); });
    wirePersonaVoice();
    scaffolded = true;
  }

  // persona tabs exclude system-level categories (rendered on a System page)
  function tabCats() { return SET.categories.filter(c => c.id !== 'apikeys' && !c.system && !c.motor); }
  function renderTabs() {
    const bar = document.getElementById('st-tabbar'); if (!bar) return;
    bar.innerHTML = '';
    tabCats().forEach(cat => {
      const label = cat.id === 'persona' ? 'Temperament' : cat.name;
      const b = document.createElement('button'); b.className = 'tab' + (cat.id === activeTab ? ' on' : ''); b.dataset.t = cat.id;
      b.innerHTML = `<span>${label}</span>` + ((cat.id === 'persona' || cat.id === 'self' || cat.id === 'you') ? '' : `<span class="tlock">${lockSvg}</span>`);
      b.addEventListener('click', () => selectTab(cat.id));
      bar.appendChild(b);
    });
  }
  function selectTab(id) {
    activeTab = id; view = 'persona'; systemPage = null;
    const sp = document.getElementById('settings-page'); if (sp) sp.classList.remove('system');
    document.querySelectorAll('#st-tabbar .tab').forEach(t => t.classList.toggle('on', t.dataset.t === id));
    const temp = document.getElementById('tab-temperament'), gen = document.getElementById('tab-generic');
    const mode = document.getElementById('st-mode'); if (mode) mode.style.display = (id === 'self' || id === 'you') ? 'none' : '';
    // The per-persona voice picker belongs to persona views only (selectSystem hides it).
    const vb = document.getElementById('st-voicebar'); if (vb) vb.style.display = '';
    if (id === 'persona') { if (temp) temp.hidden = false; if (gen) gen.hidden = true; }
    else { if (temp) temp.hidden = true; if (gen) gen.hidden = false; if (id === 'self') renderSelf(); else if (id === 'you') renderSenseOfYou(); else renderGeneric(id); }
    refreshManualUI();   // restore this persona's manual/guided gate (was forced editable in system view)
    if (scroll) scroll.scrollTop = 0;
  }

  function renderPersonaRail() {
    const rail = document.getElementById('rail-nav'); if (!rail) return;
    rail.innerHTML = '';
    // Settings is now persona-config only — Agents/API/Roles/Account-Limits live
    // in the top-level workspaces (workspaces.js), and provider keys in the
    // account menu. The rail = personas + the System (Operational) page.
    renderLabsRail(rail);
    if (isAdmin) {
      const sys = document.createElement('div'); sys.className = 'pmenu-system';
      const sh = document.createElement('div'); sh.className = 'pmenu-syshead'; sh.textContent = 'System'; sys.appendChild(sh);
      const ops = document.createElement('button'); ops.className = 'pmenu-item sys'; ops.dataset.sys = 'operational';
      ops.innerHTML = '<div class="pmenu-name">Operational</div><div class="pmenu-tag">Perception · resources · maintenance</div>';
      ops.addEventListener('click', () => selectSystem('operational')); sys.appendChild(ops);
      rail.appendChild(sys);
    }
    syncRailSel();
  }


  // Labs — the persona studio (the unique craft, unchanged).
  function renderLabsRail(rail) {
    const list = document.createElement('div'); list.className = 'pmenu-list pmenu-scroll';
    PERSONAS.forEach(p => {
      const custom = !isBuiltin(p.id);
      const c = document.createElement('button'); c.className = 'pmenu-item' + (custom ? ' custom' : ''); c.dataset.p = p.id;
      c.innerHTML = `<div class="pmenu-name">${p.name}</div><div class="pmenu-tag">${p.tag}</div>` +
        (custom ? '<span class="pmenu-del" title="Delete persona">×</span>' : '');
      c.addEventListener('click', (e) => {
        if (e.target.closest('.pmenu-del')) { e.stopPropagation(); deletePersona(p.id); return; }
        if (!(view === 'persona' && p.id === persona)) selectPersona(p.id);
      });
      list.appendChild(c);
    });
    const add = document.createElement('button'); add.className = 'pmenu-add'; add.innerHTML = '<span>+</span> New persona';
    add.addEventListener('click', createPersona);
    list.appendChild(add);
    rail.appendChild(list);
  }


  function syncRailSel() {
    document.querySelectorAll('#rail-nav .pmenu-item:not(.sys):not(.agent)').forEach(c => c.classList.toggle('sel', view === 'persona' && c.dataset.p === persona));
    document.querySelectorAll('#rail-nav .pmenu-item.sys').forEach(c => c.classList.toggle('sel', view === 'system' && c.dataset.sys === systemPage));
  }

  function syncPersonaHead() {
    const p = PERSONAS.find(x => x.id === persona) || { name: persona, tag: '', note: '' };
    const builtin = isBuiltin(persona);
    const set = (id, t) => { const el = document.getElementById(id); if (el != null && el) el.textContent = t; };
    set('st-eyebrow', builtin ? 'Persona' : 'Custom persona'); set('st-name', p.name); set('st-tag', p.tag); set('st-note', p.note);
    const nm = document.getElementById('st-name'); if (nm) nm.setAttribute('contenteditable', builtin ? 'false' : 'true');
    const del = document.getElementById('st-delete'); if (del) del.style.display = builtin ? 'none' : '';
    const bt = document.getElementById('bar-title'); if (bt) bt.textContent = p.name;
    const bb = document.getElementById('bar-blurb'); if (bb) bb.textContent = p.tag || '';
    syncPersonaVoice();
  }

  // ── Per-persona voice picker (above the tabs, under the description) ────────
  async function loadPersonaVoices() {
    if (_voiceList || _voiceUnavailable) return;
    try {
      const res = await fetch('/voices');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (data.reason === 'no_elevenlabs_key') { _voiceUnavailable = true; return; }
      _voiceList = data.voices || [];
    } catch (e) { /* leave null — retried on next persona render */ }
  }
  function syncPersonaVoice() {
    const bar = document.getElementById('st-voicebar'), sel = document.getElementById('st-voice');
    if (!bar || !sel) return;
    if (_voiceUnavailable) {
      bar.classList.add('disabled');
      sel.innerHTML = '<option>Voice off — add an ElevenLabs key in API Keys</option>'; sel.disabled = true;
      return;
    }
    if (!_voiceList) { loadPersonaVoices().then(syncPersonaVoice); return; }
    bar.classList.remove('disabled'); sel.disabled = false;
    // The active persona's voice: persona_voice_<slug> → persona_voice_id → first.
    const chosen = (values[voiceKeyFor(persona)] || values.persona_voice_id || '').trim();
    sel.innerHTML = '';
    _voiceList.forEach(v => { const o = document.createElement('option'); o.value = v.voice_id; o.textContent = v.name; sel.appendChild(o); });
    if (chosen && !_voiceList.find(v => v.voice_id === chosen)) {
      const o = document.createElement('option'); o.value = chosen; o.textContent = '(custom voice)'; sel.appendChild(o);
    }
    sel.value = chosen || (_voiceList[0] && _voiceList[0].voice_id) || '';
  }
  function wirePersonaVoice() {
    const sel = document.getElementById('st-voice');
    if (!sel) return;
    sel.addEventListener('change', () => {
      const vid = sel.value; if (!vid) return;
      // Save-gated like every other persona setting: just mark the values dirty.
      // Save posts persona_voice_* and the server applies it live (_on_voice_change).
      values.persona_voice_id = vid; values[voiceKeyFor(persona)] = vid;
      refreshDirty();
    });
  }

  // Restore a persona's full saved knob setup (chemistry + cognitive + globals).
  // No stored snapshot (a fresh built-in) → snap chem to canonical and reset the
  // dial-touched globals to their defaults.
  function applyPersonaVals(id) {
    const e = personaStore[id];
    if (e && e.vals) {
      Object.entries(e.vals).forEach(([k, v]) => { values[k] = v; });
    } else {
      const c = PERSONA_CHEM[id] || {};
      CHANNELS.forEach(ch => { if (c[ch.ch] != null) { values['chem_baseline_' + ch.ch] = c[ch.ch]; values['chem_init_' + ch.ch] = c[ch.ch]; } });
      allKeys.forEach(k => { if (!isChem(k)) values[k] = refDefault[k]; });
      toggleKeys.forEach(k => { values[k] = refDefault[k] != null ? refDefault[k] : 0; });
      // Cognitive fingerprint: a built-in persona's cognitive/lingering keys
      // differ per persona even before any save (mirrors the backend boot
      // materialization). Motivation dials are NOT applied here — their backend
      // is the reward table; their reward_weight_* multipliers stay at default
      // and only the needle poses (from PERSONA_POS). `persona` is `id` here.
      const _prevPersona = persona; persona = id;
      Object.entries(cogFingerprintValues()).forEach(([k, v]) => {
        if (k in values) values[k] = clampKey(k, v);
      });
      persona = _prevPersona;
    }
  }
  function selectPersona(id) {
    persona = id; view = 'persona';
    if (!(persona in manualState)) manualState[persona] = false;
    values.persona_name = id;
    applyPersonaVals(id);
    seedDials(false);
    renderAllDials(); renderChem(); applyChemDisplay(false);
    syncPersonaHead(); renderTabs(); refreshManualUI();
    if (activeTab !== 'persona') renderGeneric(activeTab);
    selectTab(activeTab); syncRailSel();
    captureViewBaseline(); refreshDirty();
  }

  /* ---- create / rename / delete custom personas ---- */
  function markStore() { storeChanged = true; }
  // This module owns the persona catalogue, but it's no longer the only thing that
  // draws it — the Personas workspace rail does too. Announce every change that
  // happens outside that rail's knowledge (server load, rename, delete) so it can
  // refresh instead of going stale. `from`/`to` describe a rename; to === null means
  // deleted; both absent means "the whole catalogue was rebuilt".
  function notifyPersonaCatalogue(change) {
    try { document.dispatchEvent(new CustomEvent('personas-changed', { detail: change || {} })); }
    catch (e) { /* non-fatal: the rail refreshes on its next paint */ }
  }
  // Unique across display names, ids AND slugs — customs are slug-keyed, so a
  // renamed persona still occupies its original slug.
  function uniqueName(base) {
    let n = base, i = 2;
    const taken = x => PERSONAS.find(p => p.id === x || p.name === x || p.slug === slugify(x)) || personaStore[x] || personaStore[slugify(x)];
    while (taken(n)) { n = base + ' ' + i; i++; }
    return n;
  }
  function syncStoreFromCurrent() {
    if (!persona || view !== 'persona') return;
    const e = personaStore[persona] || { custom: !isBuiltin(persona) };
    e.vals = snapshotVals();
    const meta = PERSONAS.find(p => p.id === persona);
    if (meta) { e.tag = meta.tag; e.note = meta.note; }
    if (e.custom) e.chem = currentChem();
    if (persona in selfStore) e.selfMd = selfStore[persona];
    personaStore[persona] = e;
  }
  // Clone the selected persona into a new custom one and return its id. Model only —
  // no rail render, no selection — so either host can own the UI that follows: the
  // Personas workspace mounts it in its own pane; the rail path below selects it here.
  function createPersonaRecord() {
    const fromName = (PERSONAS.find(p => p.id === persona) || {}).name || persona;
    const name = uniqueName('New Persona');
    const id = slugify(name);   // the slug is the key from birth; rename only relabels
    const chem = currentChem();
    PERSONA_CHEM[id] = chem;
    const tag = 'Custom · cloned from ' + fromName;
    const note = 'A new persona, cloned from ' + fromName + '. Rename it, then shape it with the dials — or switch on Manual mode to set the chemistry by hand.';
    PERSONAS.push({ id, slug: id, name, tag, note });
    personaStore[id] = { custom: true, tag, note, chem, vals: snapshotVals() };
    // clone the source persona's self.md, retitled for the new persona
    ensureSelf(persona);
    const cloned = (selfStore[persona] || buildSelf(persona))
      .replace(/^# Self-Model —.*$/m, `# Self-Model — ${name}`)
      .replace(/^> .*$/m, `> Cloned from ${fromName}. Edit freely — the brain revises it for itself at sleep consolidation.`);
    selfStore[id] = cloned; selfSaved[id] = cloned;
    markStore();
    return name;
  }
  // Put the caret in the persona-name field so a fresh persona can be renamed on the spot.
  function focusPersonaName() {
    const nm = document.getElementById('st-name');
    if (!nm) return;
    nm.focus();
    const r = document.createRange(); r.selectNodeContents(nm);
    const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
  }
  function createPersona() {
    const name = createPersonaRecord();
    renderPersonaRail(); selectPersona(resolvePersonaId(name));
    focusPersonaName();
  }
  // Rename = relabel. The slug is the storage key for everything the persona has
  // learned, so it never changes: only display_name moves (persisted by the next
  // Save via the unified spec). The one exception is a persona created THIS
  // session and never saved — its provisional slug came from the placeholder
  // name, so re-key it from the real name before anything is stored under it.
  function renamePersona(newName) {
    newName = (newName || '').trim();
    const meta = PERSONAS.find(p => p.id === persona);
    if (!newName || !meta || isBuiltin(persona) || newName === meta.name ||
        PERSONAS.find(p => p !== meta && (p.id === newName || p.name === newName))) { syncPersonaHead(); return; }
    const oldName = meta.name;
    const entry = personaStore[persona] || {};
    if (!entry.persisted) {
      const old = persona;
      const id = (() => { let s = slugify(newName), i = 2; while (PERSONAS.find(p => p !== meta && (p.id === s || p.slug === s))) { s = slugify(newName) + '_' + i; i++; } return s; })();
      personaStore[id] = personaStore[old]; delete personaStore[old];
      if (old in selfStore) { selfStore[id] = selfStore[old]; delete selfStore[old]; }
      if (old in selfSaved) { selfSaved[id] = selfSaved[old]; delete selfSaved[old]; }
      PERSONA_CHEM[id] = PERSONA_CHEM[old]; delete PERSONA_CHEM[old];
      if (manualState[old] != null) { manualState[id] = manualState[old]; delete manualState[old]; }
      meta.id = id; meta.slug = id;
      persona = id; values.persona_name = id;
      if (viewBaseline) viewBaseline.persona_name = id;
    }
    meta.name = newName;   // persisted by the next Save via the unified spec
    markStore(); renderPersonaRail(); syncPersonaHead(); refreshDirty();
    notifyPersonaCatalogue({ from: oldName, to: newName });
  }
  function deletePersona(id) {
    if (isBuiltin(id)) return;
    const meta = PERSONAS.find(p => p.id === id) || {};
    const label = meta.name || id;
    if (!window.confirm('Delete persona "' + label + '"? This removes its saved knob setup.')) return;
    const finish = () => {
      delete personaStore[id]; delete PERSONA_CHEM[id];
      delete selfStore[id]; delete selfSaved[id];
      const i = PERSONAS.findIndex(p => p.id === id); if (i >= 0) PERSONAS.splice(i, 1);
      if (persona === id) selectPersona(PERSONAS[0].id);
      renderPersonaRail(); refreshDirty();
      notifyPersonaCatalogue({ from: label, to: null });
    };
    // Persisted personas live in the unified spec store — delete there first so
    // the catalogue every surface reads (including the engine API) agrees.
    if (personaStore[id] && personaStore[id].persisted) {
      fetch('/settings/personas/' + encodeURIComponent(meta.slug || slugify(id)), { method: 'DELETE' })
        .then(res => { if (res.ok || res.status === 404) finish(); else window.alert('Delete failed (' + res.status + ')'); })
        .catch(e => window.alert('Delete failed: ' + e.message));
    } else {
      finish();   // never saved — local-only cleanup
    }
  }
  function selectSystem(which) {
    view = 'system'; systemPage = which; syncRailSel();
    const tb = document.getElementById('st-tabbar'); if (tb) tb.hidden = true;
    const temp = document.getElementById('tab-temperament'); if (temp) temp.hidden = true;
    const gen = document.getElementById('tab-generic'); if (gen) gen.hidden = false;
    const modeEl = document.getElementById('st-mode'); if (modeEl) modeEl.style.display = 'none';
    // System pages share the persona scaffold head — hide the per-persona voice picker here.
    const vb = document.getElementById('st-voicebar'); if (vb) vb.style.display = 'none';
    const sp = document.getElementById('settings-page'); if (sp) { sp.classList.remove('manual'); sp.classList.add('system'); }
    manualOpen = true;   // system settings are always editable (no per-persona gate)
    const set = (id, t) => { const el = document.getElementById(id); if (el) el.textContent = t; };
    const bt = document.getElementById('bar-title'), bb = document.getElementById('bar-blurb');
    if (which === 'operational') {
      set('st-eyebrow', 'System'); set('st-name', 'Operational'); set('st-tag', '');
      set('st-note', 'Org-wide operational controls, shared across every persona — compute & spend budgets, what the brain is authorized to do with its motor cortex (which folders it may read/write, which tool families are enabled), perception, and self-maintenance. Not part of any one persona’s temperament.');
      if (bt) bt.textContent = 'Operational'; if (bb) bb.textContent = 'System · budgets & permissions';
      renderOperational();
    } else if (which === 'apidocs') {
      set('st-eyebrow', 'System'); set('st-name', 'API Reference'); set('st-tag', '');
      set('st-note', 'HTTP and WebSocket endpoints exposed by the brain server. All routes require an active session cookie or a Bearer token obtained from POST /auth/login.');
      if (bt) bt.textContent = 'API Reference'; if (bb) bb.textContent = 'System · endpoints & auth';
      renderApiDocs();
    } else {
      set('st-eyebrow', 'System'); set('st-name', 'API Keys'); set('st-tag', '');
      set('st-note', 'Provider credentials for language models, voice, and background services — shared across every persona, not part of any one’s temperament.');
      if (bt) bt.textContent = 'API Keys'; if (bb) bb.textContent = 'System · shared providers';
      renderApiKeys();
    }
    if (scroll) scroll.scrollTop = 0;
  }
  // Replace the free-text connector allowlists with toggles built from the
  // connectors actually configured in Claude (GET /connectors). Storage is
  // unchanged: '' = all (including ones connected later); otherwise a
  // newline-separated allowlist of the enabled names.
  function upgradeConnectorRows() {
    fetch('/connectors').then(r => r.ok ? r.json() : null).then(data => {
      const live = (data && data.connectors) || [];
      if (!live.length) return; // nothing configured — keep the textarea
      ['motor_user_connectors', 'motor_self_connectors'].forEach(key => {
        const reg = genReg[key]; if (!reg || !reg.text) return;
        const ta = reg.text; ta.style.display = 'none';
        const stored = String(values[key] || '').split('\n').map(s => s.trim()).filter(Boolean);
        // Stale names the user saved but Claude no longer has — keep visible.
        const all = [...live, ...stored.filter(s => !live.some(n => n.toLowerCase() === s.toLowerCase()))];
        const isOn = name => !stored.length || stored.some(s => s.toLowerCase() === name.toLowerCase());
        const box = document.createElement('div'); box.className = 'conn-chips';
        const state = document.createElement('div'); state.className = 'conn-state';
        const syncState = () => {
          state.textContent = String(values[key] || '').trim()
            ? 'Only the highlighted services exist for these tasks.'
            : 'All connectors allowed — including ones you connect later.';
        };
        all.forEach(name => {
          const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'conn-chip';
          chip.textContent = name;
          if (!live.some(n => n.toLowerCase() === name.toLowerCase())) chip.classList.add('stale');
          chip.classList.toggle('on', isOn(name));
          chip.addEventListener('click', () => {
            chip.classList.toggle('on');
            const on = [...box.querySelectorAll('.conn-chip.on')].map(c => c.textContent);
            // every live connector on (and no stale picks) → '' = all
            const allOn = live.every(n => on.some(o => o.toLowerCase() === n.toLowerCase())) &&
                          on.length === live.length;
            values[key] = allOn ? '' : on.join('\n');
            ta.value = values[key];
            syncState(); refreshDirty();
          });
          box.appendChild(chip);
        });
        syncState();
        ta.parentNode.insertBefore(box, ta);
        ta.parentNode.insertBefore(state, ta);
      });
    }).catch(() => {});
  }
  function renderOperational() {
    const wrap = document.getElementById('settings-generic'); if (!wrap) return;
    wrap.innerHTML = ''; Object.keys(genReg).forEach(k => delete genReg[k]);
    const note = document.createElement('div'); note.className = 'es-cat-blurb';
    note.textContent = 'Org-wide operational settings — the same for every persona. Compute & spend budgets, motor permissions (which folders the brain may read/write and which tool families are enabled), perception, and self-maintenance.';
    wrap.appendChild(note);
    // System categories (budgets, perception, maintenance) + the motor-permission
    // category (write/host authorization) — the operations the org admin controls.
    SET.categories.filter(c => c.system || c.motor).forEach(cat => {
      const h = document.createElement('div'); h.className = 'es-group';
      h.innerHTML = `<span>${cat.name}</span>` + (cat.blurb ? `<em>${cat.blurb}</em>` : '');
      wrap.appendChild(h);
      (cat.sections || []).forEach(sec => wrap.appendChild(genSection(sec)));
    });
    upgradeConnectorRows();   // turn the connector allowlists into live toggles
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
    const cb = document.getElementById('st-chembadge'); if (cb) cb.classList.toggle('on', CHANNELS.some(c => Math.abs((+values['chem_baseline_' + c.ch]) - ((PERSONA_CHEM[persona] || {})[c.ch] || 0)) > 0.005));
  }
  function resetPersona() {
    // A persisted built-in override lives in the unified spec store — restoring
    // defaults must remove it there too, or the override resurrects on reload.
    const meta = PERSONAS.find(p => p.id === persona) || {};
    const entry = personaStore[persona];
    const serverOverride = isBuiltin(persona) && entry && entry.persisted;
    if (serverOverride && !window.confirm(`Restore ${meta.name || persona}'s canonical defaults? This removes the saved override for this persona.`)) return;
    CHANNELS.forEach(c => { values['chem_baseline_' + c.ch] = PERSONA_CHEM[persona][c.ch]; values['chem_init_' + c.ch] = PERSONA_CHEM[persona][c.ch]; });
    allKeys.forEach(k => { if (!isChem(k)) values[k] = refDefault[k]; });
    toggleKeys.forEach(k => { values[k] = refDefault[k] != null ? refDefault[k] : 0; });
    ALL_DIALS.forEach(d => { dial[d.id] = rest[d.id]; });
    allKeys.forEach(k => { dialCenter[k] = +values[k]; });
    if (serverOverride) {
      fetch('/settings/personas/' + encodeURIComponent(meta.slug || slugify(persona)), { method: 'DELETE' })
        .then(res => {
          if (!res.ok && res.status !== 404) { window.alert('Restore failed (' + res.status + ')'); return; }
          delete personaStore[persona];
          meta.overridden = false;
          captureViewBaseline(); refreshDirty();
        })
        .catch(e => window.alert('Restore failed: ' + e.message));
    }
    ALL_DIALS.forEach(d => paintDial(d.id)); applyChemDisplay(false); applyGenericDisplay(); refreshDirty();
  }

  /* =====================================================================
     BOOT
     ===================================================================== */
  // Re-point the save/dirty/restart chrome refs to whichever surface is currently
  // mounted (the Personas workspace persona detail, or the slim Settings page) and
  // wire the save/reset buttons idempotently (onclick, never duplicate listeners).
  function bindChrome(prefix) {
    const byId = (id) => document.getElementById(id);
    if (prefix === 'pers') {
      saveBtn = byId('pers-save-btn'); restartBanner = byId('pers-restart-banner');
      dirtyPill = byId('pers-dirty-pill'); dirtyText = byId('pers-dirty-text');
      scroll = byId('pers-scroll'); resetBtn = byId('pers-reset-btn');
    } else {
      saveBtn = byId('settings-save-btn'); restartBanner = byId('settings-restart-banner');
      dirtyPill = byId('dirty-pill'); dirtyText = byId('dirty-text');
      scroll = byId('scroll'); resetBtn = byId('settings-reset-btn');
    }
    if (saveBtn) saveBtn.onclick = doSave;
    if (resetBtn) resetBtn.onclick = () => { if (confirm('Reset ALL settings across every category to their defaults?')) doResetAll(); };
  }

  // Mount the persona-config engine into the Personas workspace pane (#pers-cat-wrap),
  // re-homing the scaffold there and focusing persona `id`. The workspace re-renders
  // its pane on every persona click, so rebuild the scaffold each mount (scaffolded
  // reset). Data is loaded once at boot; load on demand if mounted before that.
  async function mountPersona(id) {
    if (!PERSONAS.length) await loadFromServer();
    bindChrome('pers');
    const target = id ? resolvePersonaId(id) : (PERSONAS[0] && PERSONAS[0].id);
    if (!target) {
      // Unknown persona (a stale rail row, or a catalogue race). Never fall back
      // to PERSONAS[0] silently — mounting the WRONG persona under the clicked
      // name is how "all my personas look identical" happens.
      const wrap = document.getElementById('pers-cat-wrap');
      if (wrap) wrap.innerHTML = `<div class="es-cat-blurb" style="padding:24px;">Persona "${String(id)}" isn't in the catalogue yet. If it was just created through the API, reload the page.</div>`;
      return;
    }
    scaffolded = false; buildScaffold();
    selectPersona(target);
  }

  // The slim Settings rail: provider keys (everyone) + Operational (admin). Persona
  // config is no longer here — it lives in the Personas workspace.
  function renderSlimRail() {
    const rail = document.getElementById('rail-nav'); if (!rail) return;
    rail.innerHTML = '';
    const item = (sys, name, tag) => {
      const b = document.createElement('button'); b.className = 'pmenu-item sys'; b.dataset.sys = sys;
      b.innerHTML = `<div class="pmenu-name">${name}</div><div class="pmenu-tag">${tag}</div>`;
      b.addEventListener('click', () => { selectSystem(sys); syncRailSel(); });
      rail.appendChild(b);
    };
    item('apikeys', 'API Keys', 'Providers · credentials');
    item('apidocs', 'API Reference', 'Endpoints · auth');
    if (orgAdmin) item('operational', 'Operational', 'Budgets · permissions · maintenance');
  }

  // Open the slim Settings surface (gear): operational + keys only.
  async function openSlim() {
    bindChrome('settings');
    if (!Object.keys(values).length) await loadFromServer();
    renderSlimRail();
    selectSystem(orgAdmin ? 'operational' : 'apikeys');
    syncRailSel();
  }

  function boot() {
    // Always boot: the persona surface is hosted in the Personas workspace now, and
    // the slim Settings page hosts operational + keys. Bind the slim-settings chrome
    // up front (the gear opens it); mountPersona re-points to the workspace on demand.
    if (!document.getElementById('settings-page') && !document.getElementById('ws-personas')) return;
    bindChrome('settings');
    window.__settingsUI = {
      open: openSlim,
      reload: loadFromServer,
      openSlim,
      openApiKeys: async () => { bindChrome('settings'); if (!Object.keys(values).length) await loadFromServer(); renderSlimRail(); selectSystem('apikeys'); syncRailSel(); },
      // Render persona `id`'s full config inline in the Personas workspace pane.
      mountPersona,
      // The LIVE persona catalogue — built-ins plus the org's custom personas from
      // persona_store. window.SETTINGS.personas is only the built-in seed (this module
      // copies it at boot and never writes back), so any surface that lists personas
      // must read this or custom ones stay invisible to it.
      listPersonas: () => PERSONAS.map(p => ({ ...p })),
      // Clone the selected persona; returns the new id. The caller mounts + focuses it.
      createPersona: createPersonaRecord,
      focusPersonaName,
      // The compact markdown renderer used by the self.md + role editors. Exported so
      // the Roles surface in the workspace shares ONE renderer instead of duplicating it.
      mdToHtml,
      // Whether the currently-mounted persona has unsaved config edits — the workspace
      // uses this to warn before switching personas/views would silently drop them.
      hasUnsavedPersona: () => { try { return dirtyCount() > 0; } catch (e) { return false; } },
      // Back-compat: the slim Settings no longer hosts persona config, so this just
      // mounts into the workspace surface (callers that still ask for it get it there).
      openPersona: mountPersona,
    };
    loadFromServer();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
