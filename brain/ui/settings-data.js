/* =====================================================================
   BRAIN SETTINGS — data model
   Every control maps to the exact `key` the FastAPI /settings endpoint
   expects, so this config can drive the live app verbatim. Rows are
   grouped into 6 navigable categories; `adv:true` rows are tucked behind
   an "Advanced" disclosure so the essentials read first.

   Row types:
     master  — headline dial for a section (accent styling)
     range   — standard slider
     toggle  — 0/1 switch
     time    — slider whose value display is humanised (s → m/h)
     group   — a non-interactive sub-heading inside a section
   ===================================================================== */

window.SETTINGS = {

  // ---- Persona presets (the five from the app's PERSONAS map) -------
  personas: [
    { id: 'The Visionary', name: 'The Visionary', tag: 'Exploratory · optimistic · uninhibited', note: 'Chases big ideas with optimism and few brakes — bold, expansive, a little restless.' },
    { id: 'The Empath',    name: 'The Empath',    tag: 'Warm · patient · attuned',              note: 'Bonds easily and reads the room — warm, patient, and slow to stress.' },
    { id: 'The Analyst',   name: 'The Analyst',   tag: 'Methodical · precise · vigilant',       note: 'Careful and exacting — pays close attention, checks its work, stays alert.' },
    { id: 'The Poet',      name: 'The Poet',      tag: 'Intense · ruminative · unfiltered',     note: 'Feels everything intensely and says it unfiltered — vivid, brooding, expressive.' },
    { id: 'The Sage',      name: 'The Sage',      tag: 'Contemplative · unhurried · calm',      note: 'Calm and reflective — takes its time, rarely rattled, speaks with measure.' },
    { id: 'The Companion', name: 'The Companion', tag: 'Warm · loyal · easygoing',              note: 'A good friend — shows up, remembers, laughs with you, takes your side.' },
    { id: 'The Adversary', name: 'The Adversary', tag: 'Skeptical · exacting · winnable',       note: 'Hard to convince and slow to trust — but fair, and worth winning over. Built for practice.' },
    { id: 'The Mentor',    name: 'The Mentor',    tag: 'Curious · patient · invested',          note: 'Teaches by lighting curiosity and keeps you honest — patient with the struggle, invested in your progress.' },
    { id: 'The Concierge', name: 'The Concierge', tag: 'Polished · attentive · devoted',        note: 'Aims to please and means it — quietly takes care of everything, and genuinely enjoys doing so.' },
    { id: 'The Jester',    name: 'The Jester',    tag: 'Playful · quick · irreverent',          note: 'Lives for the laugh — fast associations, light heart, allergic to solemnity.' },
    { id: 'The Stoic',     name: 'The Stoic',     tag: 'Even · unmoved · baseline',             note: 'The flat-affect control — steady in everything, leaning nowhere. For experiments.' },
    { id: 'The Cynic',     name: 'The Cynic',     tag: 'Gruff · deadpan · secretly soft',       note: 'Expects the worst, says so dryly — and warms up only if you earn it. The warmth is real.' },
  ],

  // ---- Categories ---------------------------------------------------
  categories: [

    /* ============================ PERSONA ========================== */
    {
      id: 'persona', name: 'Persona', icon: 'user',
      blurb: 'A starting chemistry. The brain learns and evolves from here — each persona keeps its own separate brain.',
      summary: 'Pick the temperament the brain boots with. It is only a starting point — the brain keeps learning from here, and every persona keeps its own separate memory. Most people set this once and leave it.',
      custom: 'persona',
      sections: [
        {
          id: 'sec-traits', num: '00', title: 'Temperament',
          desc: 'High-level trait dials. Each one nudges a bundle of underlying controls at once, on top of your chosen persona. Drop into Resting Chemistry (and the other categories) to hand-tune any single control.',
          rows: [
            { type: 'master', virtual: true, key: 'trait-intelligence', label: 'Intelligence',  hint: 'rate of learning, attention & reasoning depth — not raw model capability', min: 0, max: 1, step: 0.01, def: 0.5 },
            { type: 'master', virtual: true, key: 'trait-empathy',      label: 'Empathy / Warmth', hint: 'bonding, contentment & warm delivery',                              min: 0, max: 1, step: 0.01, def: 0.5 },
            { type: 'master', virtual: true, key: 'trait-sensitivity',  label: 'Sensitivity',   hint: 'emotional reactivity & how vividly moments imprint',                  min: 0, max: 1, step: 0.01, def: 0.5 },
            { type: 'master', virtual: true, key: 'trait-composure',    label: 'Composure',     hint: 'calm & even-keeled — the counterweight to Sensitivity',              min: 0, max: 1, step: 0.01, def: 0.5 },
            { type: 'master', virtual: true, key: 'trait-drive',        label: 'Drive',         hint: 'reward-seeking, motivation & persistence',                           min: 0, max: 1, step: 0.01, def: 0.5 },
            { type: 'master', virtual: true, key: 'trait-creativity',   label: 'Creativity',    hint: 'disinhibition, loose association & novelty-seeking',                 min: 0, max: 1, step: 0.01, def: 0.5 },
            { type: 'master', virtual: true, key: 'trait-humor',        label: 'Humor',         hint: 'playful, disinhibited & loose — approximated through chemistry',     min: 0, max: 1, step: 0.01, def: 0.5 },
            { type: 'master', virtual: true, key: 'trait-sociability',  label: 'Sociability',   hint: 'outgoing & initiating — speaks up, thinks out loud, breaks silence', min: 0, max: 1, step: 0.01, def: 0.5 },
            { type: 'master', virtual: true, key: 'trait-caution',      label: 'Caution',       hint: 'guarded & threat-alert (low = open & trusting)',                     min: 0, max: 1, step: 0.01, def: 0.5 },
          ],
        },
        {
          id: 'sec-persona', num: '01', title: 'Resting Chemistry', custom: 'personaChem',
          desc: 'The resting chemistry the brain holds and relaxes toward, for all nine neurochemicals.',
          rows: [], advanced: [
            { type: 'group', label: 'Resting baseline — the trait the brain relaxes toward' },
            { type: 'range', key: 'chem_baseline_DA',   label: 'DA baseline',   min: 0, max: 0.8, step: 0.01, def: 0.30, delta: true },
            { type: 'range', key: 'chem_baseline_ACh',  label: 'ACh baseline',  min: 0, max: 0.8, step: 0.01, def: 0.10, delta: true },
            { type: 'range', key: 'chem_baseline_GABA', label: 'GABA baseline', min: 0, max: 0.8, step: 0.01, def: 0.02, delta: true },
            { type: 'range', key: 'chem_baseline_Glu',  label: 'Glu baseline',  min: 0, max: 0.8, step: 0.01, def: 0.15, delta: true },
            { type: 'range', key: 'chem_baseline_NE',   label: 'NE baseline',   min: 0, max: 0.8, step: 0.01, def: 0.15, delta: true },
            { type: 'range', key: 'chem_baseline_5HT',  label: '5HT baseline',  min: 0, max: 0.8, step: 0.01, def: 0.20, delta: true },
            { type: 'range', key: 'chem_baseline_CORT', label: 'CORT baseline', min: 0, max: 0.8, step: 0.01, def: 0.02, delta: true },
            { type: 'range', key: 'chem_baseline_OXT',  label: 'OXT baseline',  min: 0, max: 0.8, step: 0.01, def: 0.15, delta: true },
            { type: 'range', key: 'chem_baseline_AEA',  label: 'AEA baseline',  min: 0, max: 0.8, step: 0.01, def: 0.10, delta: true },
            { type: 'group', label: 'Starting level — the value at boot, before any input' },
            { type: 'range', key: 'chem_init_DA',   label: 'DA at boot',   min: 0, max: 0.8, step: 0.01, def: 0.50 },
            { type: 'range', key: 'chem_init_ACh',  label: 'ACh at boot',  min: 0, max: 0.8, step: 0.01, def: 0.20 },
            { type: 'range', key: 'chem_init_GABA', label: 'GABA at boot', min: 0, max: 0.8, step: 0.01, def: 0.05 },
            { type: 'range', key: 'chem_init_Glu',  label: 'Glu at boot',  min: 0, max: 0.8, step: 0.01, def: 0.30 },
            { type: 'range', key: 'chem_init_NE',   label: 'NE at boot',   min: 0, max: 0.8, step: 0.01, def: 0.25 },
            { type: 'range', key: 'chem_init_5HT',  label: '5HT at boot',  min: 0, max: 0.8, step: 0.01, def: 0.50 },
            { type: 'range', key: 'chem_init_CORT', label: 'CORT at boot', min: 0, max: 0.8, step: 0.01, def: 0.05 },
            { type: 'range', key: 'chem_init_OXT',  label: 'OXT at boot',  min: 0, max: 0.8, step: 0.01, def: 0.30 },
            { type: 'range', key: 'chem_init_AEA',  label: 'AEA at boot',  min: 0, max: 0.8, step: 0.01, def: 0.30 },
          ],
        },
      ],
    },

    /* ============================ API KEYS ========================= */
    {
      id: 'apikeys', name: 'API Keys', icon: 'key',
      blurb: 'Bring your own provider keys. Stored on this machine; each overrides the platform default.',
      summary: 'Connect your own provider keys. Anthropic is required — it powers the core reasoning. The rest are optional: ElevenLabs enables voice output, Deepgram improves voice input (your key is used before the platform default), and Google/Gemini enables image understanding. Keys are stored locally and applied on the next restart. A saved key shows as “saved” — leave a field blank to keep it.',
      sections: [
        {
          id: 'sec-apikeys', num: '🔑', title: 'Provider Keys',
          desc: 'Paste each provider key. Saved keys show as “saved” — leave blank to keep them; restart to apply.',
          rows: [
            { type: 'apikey', key: 'api_key_anthropic',  label: 'Anthropic API key',        hint: 'required — core reasoning (console.anthropic.com)', def: '' },
            { type: 'apikey', key: 'api_key_elevenlabs', label: 'ElevenLabs API key',       hint: 'optional — enables voice output (TTS)', def: '' },
            { type: 'apikey', key: 'api_key_deepgram',   label: 'Deepgram API key',         hint: 'optional — voice input; your key is used before the platform key', def: '' },
            { type: 'apikey', key: 'api_key_google',     label: 'Google (Gemini) API key',  hint: 'optional — enables image processing', def: '' },
          ],
        },
      ],
    },

    /* ======================= EMOTION & CHEMISTRY =================== */
    {
      id: 'chemistry', name: 'Emotion & Chemistry', icon: 'flask',
      blurb: 'Neuromodulators, slow hormones, and how feeling-states rise, cross-modulate, and decay.',
      summary: 'These dials decide how much the brain feels and how long it lingers there. Turn reactivity up for a more emotional, volatile companion; down for an even-keeled one. The hormone sections are slow-burning moods (bonding, stress, baseline cheer) — leave them at defaults unless you are deliberately tuning temperament.',
      sections: [
        {
          id: 'sec-1', num: '01', title: 'Emotional Reactivity',
          desc: 'How strongly sensory input drives neuromodulator changes.',
          rows: [
            { type: 'master', key: 'emotional_reactivity_scale', label: 'Emotional Reactivity', hint: 'runtime multiplier on all input weights', min: 0.2, max: 3.0, step: 0.05, def: 1.0 },
            { type: 'range', key: 'hostility_GABA_threshold_high', label: 'Threat Sensitivity', hint: 'lower = responds to subtler hostility', min: 0.2, max: 0.9, step: 0.05, def: 0.50 },
            { type: 'toggle', key: 'emotional_expression_enabled', label: 'Emotional Expression', hint: 'brain may deliberately emulate emotions via set_mood', def: 1 },
          ],
          advanced: [
            { type: 'range', key: 'sentiment_DA_weight',  label: 'Sentiment → DA',  min: 0, max: 0.5, step: 0.01, def: 0.15 },
            { type: 'range', key: 'surprise_ACh_weight',  label: 'Surprise → ACh',  min: 0, max: 0.4, step: 0.01, def: 0.12 },
            { type: 'range', key: 'salience_ACh_weight',  label: 'Salience → ACh',  min: 0, max: 0.3, step: 0.01, def: 0.08 },
            { type: 'range', key: 'salience_Glu_weight',  label: 'Salience → Glu',  min: 0, max: 0.3, step: 0.01, def: 0.08 },
          ],
        },
        {
          id: 'sec-2', num: '02', title: 'Neuromodulator Homeostasis',
          desc: 'How quickly neuromodulator levels decay back to baseline.',
          rows: [
            { type: 'master', key: 'master-homeostasis', label: 'Homeostasis Speed', hint: 'higher = slower return to rest', min: 0.5, max: 0.99, step: 0.01, def: 0.876, virtual: true },
            { type: 'range', key: 'salience_satiation_threshold', label: 'Satiation Threshold', hint: 'below this, interest desensitizes', min: 0.1, max: 0.7, step: 0.05, def: 0.30 },
          ],
          advanced: [
            { type: 'range', key: 'valence_to_DA_decay',      label: 'DA decay',        min: 0.5, max: 0.99, step: 0.01, def: 0.85 },
            { type: 'range', key: 'threat_to_GABA_decay',     label: 'GABA decay',      min: 0.5, max: 0.99, step: 0.01, def: 0.80 },
            { type: 'range', key: 'novelty_to_ACh_decay',     label: 'ACh decay',       min: 0.5, max: 0.99, step: 0.01, def: 0.90 },
            { type: 'range', key: 'arousal_homeostat_decay',  label: 'Arousal decay',   min: 0.5, max: 0.99, step: 0.01, def: 0.88 },
            { type: 'range', key: 'satiation_inhibitor_decay', label: 'Satiation decay', min: 0.5, max: 0.99, step: 0.01, def: 0.95 },
          ],
        },
        {
          id: 'sec-12', num: '12', title: 'Endocrine / Hormonal System',
          desc: 'Slow-acting hormones — OXT (bonding), CORT (stress), 5HT (mood) — and their cross-modulation.',
          rows: [
            { type: 'group', label: 'OXT — Bonding', hint: 'builds from warm exchanges, drained by hostility' },
            { type: 'range', key: 'oxt_positive_increment', label: 'OXT gain / warm turn', min: 0.001, max: 0.05, step: 0.001, def: 0.008 },
            { type: 'range', key: 'oxt_hostility_drain', label: 'OXT drain / hostile turn', min: 0.001, max: 0.05, step: 0.001, def: 0.008 },
            { type: 'group', label: 'CORT — Stress', hint: 'builds under sustained hostility or threat' },
            { type: 'range', key: 'cort_threat_increment', label: 'CORT gain / hostile turn', min: 0.005, max: 0.08, step: 0.001, def: 0.022 },
            { type: 'group', label: '5HT — Mood', hint: 'slow mood baseline; low → dysphoric coloring' },
            { type: 'range', key: 'sht_reward_increment', label: '5HT gain / rewarding turn', min: 0.001, max: 0.02, step: 0.001, def: 0.003 },
          ],
          advanced: [
            { type: 'range', key: 'oxt_cort_buffer_rate', label: 'OXT → CORT buffer rate', min: 0.001, max: 0.08, step: 0.001, def: 0.020 },
            { type: 'range', key: 'oxt_cort_buffer_threshold', label: 'OXT → CORT buffer threshold', min: 0.1, max: 0.8, step: 0.05, def: 0.40 },
            { type: 'range', key: 'oxt_da_lift', label: 'OXT → DA lift', min: 0, max: 0.2, step: 0.01, def: 0.05 },
            { type: 'range', key: 'hormonal_oxt_connected_threshold', label: 'OXT "Connected" threshold', min: 0.3, max: 0.9, step: 0.05, def: 0.60 },
            { type: 'range', key: 'cort_hostility_threshold', label: 'CORT trigger threshold', min: 0.1, max: 0.7, step: 0.05, def: 0.35 },
            { type: 'range', key: 'cort_da_suppress', label: 'CORT → DA suppress', min: 0, max: 0.3, step: 0.01, def: 0.08 },
            { type: 'range', key: 'cort_gaba_amplify', label: 'CORT → GABA amplify', min: 0, max: 0.6, step: 0.05, def: 0.30 },
            { type: 'range', key: 'hormonal_cort_withdrawn_threshold', label: 'CORT "Withdrawn" threshold', min: 0.2, max: 0.8, step: 0.05, def: 0.45 },
            { type: 'range', key: 'sht_reward_sentiment_min', label: '5HT reward sentiment min', min: 0.1, max: 0.8, step: 0.05, def: 0.40 },
            { type: 'range', key: 'sht_hostility_drain', label: '5HT drain / hostile turn', min: 0.001, max: 0.02, step: 0.001, def: 0.004 },
            { type: 'range', key: 'sht_da_floor_lift', label: '5HT → DA floor lift', min: 0, max: 0.4, step: 0.01, def: 0.12 },
            { type: 'range', key: 'hormonal_sht_dysphoric_threshold', label: '5HT "Dysphoric" threshold', min: 0.05, max: 0.5, step: 0.05, def: 0.25 },
          ],
        },
        {
          id: 'sec-13', num: '13', title: 'Norepinephrine (NE)',
          desc: 'Focused alertness — an inverted-U; too high narrows and scatters attention.',
          rows: [
            { type: 'range', key: 'ne_salience_weight', label: 'NE ← Salience', min: 0, max: 0.3, step: 0.01, def: 0.07 },
            { type: 'range', key: 'ne_surprise_weight', label: 'NE ← Surprise', min: 0, max: 0.3, step: 0.01, def: 0.05 },
            { type: 'range', key: 'ne_hostility_weight', label: 'NE ← Hostility', min: 0, max: 0.4, step: 0.01, def: 0.10 },
          ],
          advanced: [
            { type: 'range', key: 'ne_rush_increment', label: 'NE ← Rushed prosody', min: 0, max: 0.2, step: 0.01, def: 0.05 },
            { type: 'toggle', key: 'prosody_graded_release', label: 'Graded prosody/pace release', hint: 'scale voice neuromod release by acoustic strength, not a fixed jump per label', def: 1 },
            { type: 'range', key: 'prosody_graded_min_scale', label: 'Prosody scale @ min strength', hint: 'multiplier for a near-threshold voice', min: 0, max: 1, step: 0.05, def: 0.5 },
            { type: 'range', key: 'prosody_graded_max_scale', label: 'Prosody scale @ max strength', hint: 'multiplier for a very strong voice', min: 1, max: 3, step: 0.05, def: 1.5 },
            { type: 'range', key: 'ne_high_threshold', label: 'High-NE threshold', hint: 'above → heightened vigilance', min: 0.3, max: 0.8, step: 0.05, def: 0.55 },
            { type: 'range', key: 'ne_scatter_threshold', label: 'NE scatter threshold', hint: 'above → attention narrows and degrades', min: 0.5, max: 0.99, step: 0.05, def: 0.82 },
          ],
        },
        {
          id: 'sec-14', num: '14', title: 'Anandamide / AEA',
          desc: 'Endocannabinoid buffer — rises under arousal, dampens NE/Glu, lifts DA (afterglow).',
          rows: [
            { type: 'range', key: 'aea_arousal_threshold', label: 'Arousal trigger (Glu+NE)', hint: 'sum above this builds AEA', min: 0.3, max: 1.0, step: 0.05, def: 0.65 },
            { type: 'range', key: 'aea_arousal_increment', label: 'AEA arousal gain / turn', min: 0.002, max: 0.08, step: 0.002, def: 0.018 },
          ],
          advanced: [
            { type: 'range', key: 'aea_positive_increment', label: 'AEA social afterglow / turn', min: 0.001, max: 0.03, step: 0.001, def: 0.005 },
            { type: 'range', key: 'aea_cort_drain', label: 'AEA CORT drain / turn', hint: 'stress erodes the buffer', min: 0.001, max: 0.02, step: 0.001, def: 0.004 },
            { type: 'range', key: 'aea_ne_suppression', label: 'AEA → NE suppression', min: 0, max: 1.0, step: 0.05, def: 0.50 },
            { type: 'range', key: 'aea_glu_suppression', label: 'AEA → Glu suppression', min: 0, max: 1.0, step: 0.05, def: 0.35 },
            { type: 'range', key: 'aea_da_lift', label: 'AEA → DA lift', min: 0, max: 0.2, step: 0.01, def: 0.04 },
            { type: 'range', key: 'aea_eased_threshold', label: 'AEA "Eased" threshold', min: 0.3, max: 0.9, step: 0.05, def: 0.58 },
          ],
        },
        {
          id: 'sec-15', num: '15', title: 'Time-weighted Decay',
          desc: 'Emotional state decays by real elapsed time, not message count.',
          rows: [
            { type: 'time', key: 'decay_reference_interval_s', unit: 'sec', label: 'Reference Interval', hint: 'wall-clock time treated as "1 decay turn"', min: 10, max: 300, step: 10, def: 60 },
          ],
          advanced: [
            { type: 'range', key: 'decay_min_turns', label: 'Decay Floor (turns)', hint: 'minimum decay even for instant replies', min: 0.05, max: 1.0, step: 0.05, def: 0.25 },
            { type: 'range', key: 'decay_max_turns', label: 'Decay Cap (turns)', hint: 'long silences capped at this many turns', min: 2, max: 30, step: 1, def: 10 },
          ],
        },
      ],
    },

    /* ======================= COGNITION & LEARNING ================= */
    {
      id: 'cognition', name: 'Cognition & Learning', icon: 'cpu',
      blurb: 'Plasticity, idle thought, self-reflection, prediction, and how attention is routed.',
      summary: 'How fast the brain learns, how often it thinks to itself when idle, and what it pays attention to. Higher learning rates adapt quicker but forget the old self faster. Graded Plasticity makes encoding scale with how vivid a moment is; the Collective Dynamics layer lets clusters coordinate, recruit resources, and reinforce paths that work — both ship off by default. The compute budgets cap how much background work runs on cloud and local models.',
      sections: [
        {
          id: 'sec-3', num: '03', title: 'Plasticity & Learning',
          desc: 'Hebbian weight changes — learning rate and stability are linked.',
          rows: [
            { type: 'range', key: 'hebbian_delta', label: 'Learning Rate', hint: '↑ this nudges ↓ Weight Stability', min: 0.001, max: 0.1, step: 0.001, def: 0.02 },
            { type: 'range', key: 'decay_toward_rest_rate', label: 'Weight Stability', hint: '↑ this nudges ↓ Learning Rate', min: 0.001, max: 0.05, step: 0.001, def: 0.01 },
            { type: 'range', key: 'hebbian_outcome_delta', label: 'Sleep Learning Rate', hint: 'outcome × plasticity × this', min: 0.001, max: 0.1, step: 0.001, def: 0.02 },
          ],
          advanced: [
            { type: 'range', key: 'weight_min', label: 'Weight Floor', min: 0.01, max: 0.5, step: 0.01, def: 0.10 },
            { type: 'range', key: 'weight_max', label: 'Weight Ceiling', min: 1.0, max: 6.0, step: 0.1, def: 3.0 },
            { type: 'range', key: 'gaba_skip_threshold_high', label: 'High-Inhibition Learning Cutoff', hint: 'skip Hebbian when GABA > this', min: 0.3, max: 0.9, step: 0.05, def: 0.55 },
          ],
        },
        {
          id: 'sec-20', num: '20', title: 'Graded Plasticity',
          desc: 'Learn in proportion to a turn’s arousal & emotional intensity, with an inverted-U so only extreme stress dampens encoding — replacing the legacy all-or-nothing skip.',
          rows: [
            { type: 'toggle', master: true, key: 'graded_plasticity', label: 'Graded Plasticity', hint: 'per-turn, intensity-scaled learning vs. the legacy binary skip', def: 0 },
            { type: 'range', key: 'plasticity_turn_min', label: 'Min Learning Rate', hint: 'floor of the multiplier on flat, low-arousal turns', min: 0.1, max: 1.0, step: 0.05, def: 0.40 },
            { type: 'range', key: 'plasticity_turn_max', label: 'Max Learning Rate', hint: 'ceiling on vivid, high-stakes turns', min: 1.0, max: 2.0, step: 0.05, def: 1.30 },
          ],
          advanced: [
            { type: 'range', key: 'plasticity_arousal_weight', label: 'Arousal Weight', hint: 'how much alertness/novelty/reward-swing raises learning', min: 0, max: 1.0, step: 0.05, def: 0.50 },
            { type: 'range', key: 'plasticity_intensity_weight', label: 'Emotional Intensity Weight', hint: 'vivid moments (either valence) imprint hard', min: 0, max: 1.0, step: 0.05, def: 0.40 },
            { type: 'range', key: 'plasticity_stress_knee', label: 'Stress Dampening Knee', hint: 'CORT/GABA above this begins to dampen encoding', min: 0.3, max: 1.0, step: 0.05, def: 0.70 },
            { type: 'range', key: 'plasticity_stress_damp', label: 'Max Stress Dampening', hint: 'strongest reduction at extreme stress', min: 0, max: 1.0, step: 0.05, def: 0.60 },
          ],
        },
        {
          id: 'sec-4', num: '04', title: 'Default Mode Network',
          desc: 'Background thought generation when not responding.',
          rows: [
            { type: 'range', key: 'dmn_interval', label: 'Thought Frequency', hint: 'seconds between thought cycles', min: 5, max: 120, step: 5, def: 15 },
            { type: 'master', key: 'master-suppression', label: 'Thought Suppression', hint: 'how readily ACh/Glu suppress thoughts', min: 0.3, max: 2.0, step: 0.05, def: 1.0, virtual: true },
            { type: 'range', key: 'dmn_overlap_threshold', label: 'Deduplication Strictness', hint: 'higher = more unique thoughts required', min: 0.1, max: 0.8, step: 0.05, def: 0.35 },
          ],
          advanced: [
            { type: 'range', key: 'ach_suppression_weight', label: 'ACh suppression weight', min: 0.1, max: 2.0, step: 0.05, def: 1.0 },
            { type: 'range', key: 'glu_suppression_weight', label: 'Glu suppression weight', min: 0, max: 1.0, step: 0.05, def: 0.30 },
          ],
        },
        {
          id: 'sec-5', num: '05', title: 'Self-Reflection',
          desc: 'Periodic appraisal cycles and emotion-override behavior.',
          rows: [
            { type: 'range', key: 'meta_interval', label: 'Reflection Interval', hint: 'seconds between self-appraisals', min: 10, max: 180, step: 5, def: 30 },
            { type: 'range', key: 'meta_cooldown_turns', label: 'Override Cooldown', hint: 'turns before same override repeats', min: 1, max: 10, step: 1, def: 3 },
          ],
          advanced: [
            { type: 'range', key: 'da_threshold_disappointed', label: 'DA "Disappointed" Threshold', hint: 'DA must be below this', min: 0.1, max: 0.5, step: 0.05, def: 0.25 },
            { type: 'range', key: 'gaba_drop_threshold', label: 'GABA Drop → "Relieved"', hint: 'required GABA decline to trigger', min: 0.05, max: 0.5, step: 0.05, def: 0.20 },
          ],
        },
        {
          id: 'sec-6', num: '06', title: 'Prediction & Surprise',
          desc: 'How much divergence from expectation triggers a surprise signal.',
          rows: [
            { type: 'range', key: 'surprise_threshold', label: 'Surprise Sensitivity', hint: 'lower = more easily surprised', min: 0.1, max: 0.9, step: 0.05, def: 0.40 },
          ],
          advanced: [
            { type: 'range', key: 'confidence_skip_threshold', label: 'Confidence Skip Threshold', hint: 'above this, stop updating prediction', min: 0.3, max: 1.0, step: 0.05, def: 0.70 },
            { type: 'range', key: 'predictor_window', label: 'History Window', hint: 'turns tracked by predictor', min: 3, max: 20, step: 1, def: 8 },
          ],
        },
        {
          id: 'sec-9', num: '09', title: 'Attention & Routing',
          desc: 'Thalamus priority weights for competing brain regions.',
          rows: [
            { type: 'range', key: 'hippocampus_priority_base', label: 'Memory (Hippocampus) Priority', min: 0.1, max: 1.0, step: 0.05, def: 0.60 },
            { type: 'range', key: 'frontal_ach_weight', label: 'Curiosity (Frontal ACh) Boost', min: 0, max: 0.6, step: 0.05, def: 0.20 },
          ],
          advanced: [
            { type: 'range', key: 'salience_workspace_threshold', label: 'Salience Workspace Threshold', hint: 'above this, topic enters global workspace', min: 0.2, max: 0.9, step: 0.05, def: 0.60 },
            { type: 'range', key: 'topic_activation_decay', label: 'Topic Attention Decay', hint: 'lower = faster fade', min: 0.3, max: 0.95, step: 0.05, def: 0.70 },
          ],
        },
        {
          id: 'sec-19', num: '19', title: 'Collective Dynamics',
          desc: 'Colony-inspired coordination across clusters — shared-signal concentration, quorum & silence sensing, resource recruitment, primer signalling, sensory bias, self-state feedback, and use-based trail reinforcement. Every behaviour is a strict no-op while the master switch is off.',
          rows: [
            { type: 'toggle', master: true, key: 'colony_features', label: 'Collective Dynamics', hint: 'enable the colony layer — off = clusters coordinate via chemistry only, exactly as before', def: 0 },
            { type: 'toggle', key: 'colony_trail_apply', label: 'Live Trail Reinforcement', hint: 'reinforce paths that pay off within a session. Off = shadow mode (logged, not applied). Tuning knobs in Advanced.', def: 0 },
          ],
          advanced: [
            { type: 'group', label: 'Signal Concentration & Silence' },
            { type: 'time', unit: 'sec', key: 'colony_conc_half_life_s', label: 'Signal Persistence', hint: 'half-life of a topic’s built-up concentration before it fades', min: 5, max: 300, step: 5, def: 45 },
            { type: 'range', key: 'colony_conc_cap', label: 'Saturation Cap', hint: 'max concentration a chatty channel can accumulate', min: 2, max: 30, step: 1, def: 10 },
            { type: 'range', key: 'colony_arm_threshold', label: 'Activation Threshold', hint: 'concentration a channel must cross to "arm" — only then does its silence carry meaning', min: 0.2, max: 5, step: 0.1, def: 1.0 },
            { type: 'range', key: 'colony_quorum_threshold', label: 'Quorum Threshold', hint: 'sustained concentration that trips a collective response', min: 0.5, max: 8, step: 0.1, def: 1.5 },
            { type: 'range', key: 'colony_quorum_slope_threshold', label: 'Quorum Rise Trigger', hint: 'a fast enough rise trips quorum before the level threshold', min: 0, max: 1.0, step: 0.05, def: 0.20 },
            { type: 'range', key: 'colony_silence_floor', label: 'Silence Threshold', hint: 'an armed channel decaying below this counts as "gone quiet"', min: 0, max: 1.0, step: 0.05, def: 0.15 },
            { type: 'time', unit: 'sec', key: 'colony_silence_disarm_s', label: 'Silence Reset', hint: 'time at ~zero before a quiet channel disarms', min: 60, max: 1800, step: 30, def: 600 },
            { type: 'group', label: 'Resource Recruitment' },
            { type: 'range', key: 'colony_recruit_gain', label: 'Recruitment Strength', hint: 'how strongly a need mobilises extra processing', min: 0, max: 1.0, step: 0.05, def: 0.40 },
            { type: 'range', key: 'colony_recruit_budget', label: 'Recruitment Budget', hint: 'total budget shared across competing needs each turn', min: 0.2, max: 3.0, step: 0.1, def: 1.0 },
            { type: 'range', key: 'colony_recruit_softmax_temp', label: 'Recruitment Focus', hint: 'low = favour the strongest need; high = spread evenly', min: 0.1, max: 2.0, step: 0.05, def: 0.5 },
            { type: 'range', key: 'colony_satisfy_rate', label: 'Disengage Rate', hint: 'how fast a satisfied need releases resources (cuts thrashing)', min: 0, max: 1.0, step: 0.05, def: 0.50 },
            { type: 'range', key: 'colony_satisfy_critic_floor', label: 'Disengage Quality Bar', hint: 'response-quality score that counts as "need met"', min: 0, max: 1.0, step: 0.05, def: 0.6 },
            { type: 'group', label: 'Primer Signalling' },
            { type: 'range', key: 'colony_primer_gain', label: 'Primer Strength', hint: 'how strongly a slow "primer" nudges long-horizon hormonal state', min: 0, max: 1.0, step: 0.05, def: 0.30 },
            { type: 'group', label: 'Per-Persona Sensory Bias' },
            { type: 'toggle', key: 'colony_sensory_filter', label: 'Sensory Bias', hint: 'personas detect some signal categories more readily than others', def: 0 },
            { type: 'range', key: 'colony_sensory_gain_span', label: 'Sensory Bias Strength', hint: '± span of per-persona detection gain over feature categories', min: 0, max: 0.8, step: 0.05, def: 0.30 },
            { type: 'group', label: 'Self-State Feedback' },
            { type: 'range', key: 'colony_state_feedback_gain', label: 'Self-State Feedback', hint: 'prior-turn activity nudges chemistry (effort→arousal, conflict→caution)', min: 0, max: 0.1, step: 0.005, def: 0.02 },
            { type: 'range', key: 'colony_state_feedback_clamp', label: 'Feedback Limit', hint: 'max chemistry nudge per channel (keeps the loop bounded)', min: 0, max: 0.2, step: 0.01, def: 0.05 },
            { type: 'group', label: 'Trail Reinforcement' },
            { type: 'range', key: 'colony_trail_gain', label: 'Reinforcement Rate', hint: 'per-turn strength of trail reinforcement (× outcome)', min: 0, max: 0.2, step: 0.01, def: 0.05 },
            { type: 'range', key: 'colony_trail_clamp', label: 'Trail Limit', hint: 'max boost a single path can accumulate over its base weight', min: 0.1, max: 1.0, step: 0.05, def: 0.50 },
            { type: 'time', unit: 'sec', key: 'colony_trail_half_life_s', label: 'Trail Persistence', hint: 'half-life of a reinforced trail within a session', min: 30, max: 600, step: 30, def: 120 },
          ],
        },
      ],
    },

    /* ============= COMPUTE & RESOURCES (operational / system) ===== */
    {
      id: 'resources', name: 'Compute & Resources', icon: 'cpu', system: true,
      blurb: 'Background compute budgets — operational limits, shared across every persona.',
      summary: 'How much background work the brain may spend on cloud and local models. These are operational limits, the same for every persona — not part of any one’s temperament.',
      sections: [
        {
          id: 'sec-16', num: '16', title: 'Compute & Resources',
          desc: 'Cloud/local compute budgets for background work.',
          rows: [
            { type: 'range', key: 'bg_cloud_token_budget', label: 'Cloud Token Budget', hint: 'max combined tokens / session', min: 5000, max: 200000, step: 5000, def: 50000 },
          ],
          advanced: [
            { type: 'range', key: 'bg_cloud_max_tokens_per_call', label: 'Cloud Max Tokens / Call', min: 128, max: 4096, step: 128, def: 512 },
            { type: 'time', key: 'bg_cloud_timeout_s', unit: 'sec', label: 'Cloud Timeout', hint: 'falls back to local on expiry', min: 5, max: 60, step: 5, def: 20 },
            { type: 'range', key: 'local_max_concurrent', label: 'Local Concurrency', hint: 'simultaneous Ollama calls', min: 1, max: 8, step: 1, def: 3 },
          ],
        },
      ],
    },

    /* ======================= VOICE & EXPRESSION =================== */
    {
      id: 'expression', name: 'Voice & Expression', icon: 'mic',
      blurb: 'Speech delivery, emotional modulation, and when the brain speaks unprompted.',
      summary: 'How the brain sounds and how much its mood colors its delivery. Crank expressiveness for a theatrical voice, lower it for a steady one. Proactive behavior controls whether it breaks silence on its own after you have gone quiet.',
      sections: [
        {
          id: 'sec-7', num: '07', title: 'Voice Expressiveness',
          desc: 'Speech delivery — base voice and emotional modulation.',
          rows: [
            { type: 'master', key: 'master-expressiveness', label: 'Expressiveness', hint: 'scales deviation from base in emotional states', min: 0.3, max: 2.0, step: 0.05, def: 1.0, virtual: true },
            { type: 'range', key: 'voice_stability_default', label: 'Base Stability', hint: 'higher = less varied', min: 0, max: 1.0, step: 0.05, def: 0.45 },
            { type: 'range', key: 'voice_style_default', label: 'Base Style', hint: 'character injection amount', min: 0, max: 1.0, step: 0.05, def: 0.40 },
            { type: 'range', key: 'voice_speed_default', label: 'Base Speed', hint: 'speech rate multiplier', min: 0.7, max: 1.3, step: 0.05, def: 1.0 },
          ],
          advanced: [
            { type: 'range', key: 'breath_pause_count_max', label: 'Breath Pauses (max)', min: 0, max: 4, step: 1, def: 2 },
            { type: 'range', key: 'glu_urgently_threshold', label: 'Urgently Threshold (Glu)', hint: 'Glu above this → [urgently] tag', min: 0.3, max: 0.9, step: 0.05, def: 0.55 },
          ],
        },
        {
          id: 'sec-8', num: '08', title: 'Proactive Behavior',
          desc: 'When the brain speaks unprompted after idle time.',
          rows: [
            { type: 'time', key: 'proactive_idle_threshold', unit: 'sec', label: 'Idle Threshold', hint: 'silence before proactive speech', min: 30, max: 600, step: 15, def: 180 },
            { type: 'range', key: 'proactive_response_window', label: 'Response Window (s)', hint: 'withhold next proactive this long after speaking', min: 2, max: 30, step: 1, def: 8 },
          ],
          advanced: [],
        },
      ],
    },

    /* ============================ PERCEPTION ====================== */
    {
      id: 'perception', name: 'Perception', icon: 'eye', system: true,
      blurb: 'How the brain hears and sees — speaker identification and live video.',
      summary: 'How carefully the brain tells voices apart and how often it samples your camera. Stricter speaker thresholds mean fewer mistaken identities but more "who is this?" moments. Faster video sampling sees more but costs more vision calls.',
      sections: [
        {
          id: 'sec-10', num: '10', title: 'Speaker Recognition',
          desc: 'Voice similarity thresholds for identifying speakers.',
          rows: [
            { type: 'range', key: 'speaker_store_threshold', label: 'Profile Store Threshold', hint: 'higher = pickier about saving profiles', min: 0.4, max: 0.95, step: 0.05, def: 0.70 },
            { type: 'range', key: 'speaker_session_threshold', label: 'Session Match Threshold', hint: 'lower = more permissive cross-turn matching', min: 0.3, max: 0.90, step: 0.05, def: 0.62 },
          ],
          advanced: [
            { type: 'range', key: 'speaker_min_audio_s', label: 'Min Audio Duration (s)', hint: 'minimum audio before voice embed', min: 0.1, max: 2.0, step: 0.1, def: 0.40 },
          ],
        },
        {
          id: 'sec-11', num: '11', title: 'Vision / Video',
          desc: 'Live video frame sampling and change detection for the occipital lobe.',
          rows: [
            { type: 'range', key: 'video_sample_interval', label: 'Sample Interval (s)', hint: 'how often to grab a frame', min: 1.0, max: 30.0, step: 0.5, def: 5.0 },
            { type: 'range', key: 'video_max_frames', label: 'Max Frames per Call', hint: 'frames batched into one vision call', min: 1, max: 16, step: 1, def: 8 },
          ],
          advanced: [
            { type: 'range', key: 'video_change_threshold', label: 'Change Threshold (RMS)', hint: 'min pixel difference to treat a frame as new', min: 1.0, max: 30.0, step: 0.5, def: 8.0 },
          ],
        },
      ],
    },

    /* ===================== AUTONOMY & MAINTENANCE ================= */
    {
      id: 'autonomy', name: 'Autonomy & Maintenance', icon: 'moon', system: true,
      blurb: 'Self-directed background work and the sleep pass that consolidates memory.',
      summary: 'Guardrails for the things the brain does on its own. The attempt cap stops autonomous jobs from looping forever. Sleep consolidation is its housekeeping — it periodically reviews recent conversations to extract facts and update its self-model, much like memory consolidation during real sleep.',
      sections: [
        {
          id: 'sec-17', num: '17', title: 'Motor Cortex / Autonomous Tasks',
          desc: 'Directories the motor cortex may touch, and Ralph-loop limits for self-directed background jobs.',
          rows: [
            { type: 'text', key: 'motor_allowed_dirs', label: 'Allowed Directories', hint: 'one absolute path per line — the only folders the motor cortex may read or write. Leave blank to allow none (running locally, Claude Desktop’s trusted folders are used instead).', rows: 4, placeholder: '/home/you/projects/my-app\n/home/you/scratch', def: '' },
            { type: 'range', key: 'ralph_max_total_attempts', label: 'Ralph Max Total Attempts', hint: 'hard cap on tool dispatches per job — prevents indefinite loops', min: 4, max: 32, step: 2, def: 12 },
          ],
          advanced: [],
        },
        {
          id: 'sec-18', num: '18', title: 'Sleep Consolidation',
          desc: 'In-process memory consolidation — extracts facts, updates the self-model, observes mood patterns. Runs while the brain stays online.',
          rows: [
            { type: 'toggle', key: 'sleep_periodic_enabled', label: 'Periodic Sleep', hint: 'run consolidation in-process (off = only at shutdown)', def: 1 },
            { type: 'time', key: 'sleep_check_interval_s', unit: 'sec', label: 'Check Interval', hint: 'how often to check whether to run a pass', min: 60, max: 7200, step: 60, def: 1800 },
          ],
          advanced: [
            { type: 'time', key: 'sleep_idle_threshold_s', unit: 'sec', label: 'Idle Threshold', hint: 'user idle this long before a pass fires', min: 300, max: 43200, step: 300, def: 7200 },
            { type: 'time', key: 'sleep_hard_cap_s', unit: 'sec', label: 'Hard Cap', hint: 'force a pass after this much wall-clock', min: 3600, max: 86400, step: 1800, def: 21600 },
            { type: 'range', key: 'sleep_min_turns', label: 'Min Turns / Batch', hint: 'skip the pass below this many buffered turns', min: 2, max: 40, step: 1, def: 5 },
          ],
        },
      ],
    },

  ],
};

/* TRAIT_DIALS removed — the dial definitions live solely in settings-ui.js
   (this copy had drifted out of sync and nothing read it). */
