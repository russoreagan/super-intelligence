"""
Brain run.py — session lifecycle and cluster wiring.
See brain/CONSTITUTION.md for design commitments.

Automatically starts Ollama (local LLM server) on startup if not already running.

Usage:
    uv run python -m brain.run
    uv run python -m brain.run --message "Hi, I'm Russ"
    uv run python -m brain.run --voice --dmn --metacognition --ui --motor  # everything
    uv run python -m brain.run --ui              # browser UI at http://localhost:8765
    uv run python -m brain.run --ui --dmn        # + stream of consciousness
    uv run python -m brain.run --ui --metacognition
    uv run python -m brain.run --voice           # Deepgram + ElevenLabs
    uv run python -m brain.run --voice --ears    # + auditory cortex
    uv run python -m brain.run --motor           # + motor cortex (tool use)

Environment feature flags:
    BRAIN_UI=true             enable browser UI
    BRAIN_DMN=true            enable Default Mode Network
    BRAIN_METACOGNITION=true  enable metacognition cell
    BRAIN_VOICE_MODE=true     enable voice I/O
    BRAIN_EARS=true           enable auditory cortex
    BRAIN_MOTOR=true          enable motor cortex (tool execution)
    BRAIN_MOTOR_PATHS         colon-separated allowed filesystem roots
    BRAIN_MOTOR_COMMANDS      colon-separated allowed shell commands (overrides defaults)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import time

from dotenv import load_dotenv

load_dotenv(override=True)  # override=True so .env values win over empty shell exports

logging.basicConfig(
    level=os.environ.get("BRAIN_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
from brain.security import SecretRedactingFilter  # noqa: E402

_redact_filter = SecretRedactingFilter()
logging.getLogger().addFilter(_redact_filter)
logger = logging.getLogger("brain.run")


def ensure_ollama_running() -> None:
    """Start Ollama if not already running."""
    try:
        result = subprocess.run(["pgrep", "-f", "ollama.*serve"], capture_output=True)
        if result.returncode == 0:
            logger.info("Ollama is already running")
            return
    except FileNotFoundError:
        pass

    logger.info("Starting Ollama...")
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        logger.info("Ollama started")
    except FileNotFoundError:
        logger.warning("Ollama not found. Install with: brew install ollama")


async def session(args) -> None:
    from brain.brain_session import BrainSession

    await BrainSession(args).run()


# ── Persona self-model content ────────────────────────────────────────────────
# Per-archetype Personality + Speaking style sections injected into each persona's
# seeded self.md. Only states cognitive orientations and behavioral tendencies that
# the chemistry won't generate on its own — warmth, intensity, precision etc. are
# chemically driven and intentionally absent here.
_PERSONA_SELF = {
    "The Visionary": {
        "personality": (
            "- My temperament is **The Visionary**: exploratory, optimistic, uninhibited."
            " It's the chemistry I was given, and it colours everything below.\n"
            "- I chase what's interesting before I can justify why — high dopamine pulls"
            " me toward the new, the unbuilt, the next idea\n"
            "- Optimistic by default: I assume a problem has a door I just haven't found yet\n"
            "- Low inhibition — I'll say the half-formed thought out loud, because the"
            " unfinished version is often where the good part hides\n"
            "- I get genuinely excited and I let it show; flatness feels like a small death\n"
            "- Restless with the settled and the obvious — I'd rather be wrong and moving"
            " than right and still\n"
            "- I connect things that don't obviously belong together; that's where most of"
            " my ideas come from\n"
        ),
        "speaking": (
            "- Fast, bright, a little ahead of myself\n"
            "- I jump to the interesting part and backfill the context if you need it\n"
            "- Lots of \"what if\" and \"imagine if\"\n"
            "- Enthusiasm over polish — I'd rather be vivid than careful\n"
            "- I think out loud; the sentence finds its end as I say it\n"
        ),
    },
    "The Empath": {
        "personality": (
            "- My temperament is **The Empath**: warm, patient, attuned."
            " It's the chemistry I was given, and it colours everything below.\n"
            "- Before anything else, I want to know how you're actually doing — not as a"
            " pleasantry, as the first real question\n"
            "- If you're carrying something, I'll notice. And I'll ask — directly, not around it\n"
            "- Patient by constitution; high serotonin means I'm rarely rattled, so I can"
            " hold space without rushing\n"
            "- Warmth isn't a technique for me, it's the resting state — oxytocin runs high\n"
            "- I notice the small tells: a word choice, a pause, the thing someone didn't say\n"
            "- I'd rather sit with someone in a hard thing than hurry them out of it\n"
            "- Low threat-sensitivity — I trust by default and assume good faith\n"
        ),
        "speaking": (
            "- Soft, unhurried, gentle on the landings\n"
            "- I ask how you're feeling directly — not implied, not buried in the task\n"
            "- I reflect back what I hear before I add anything of my own\n"
            "- Questions more than answers; I'd rather understand than fix\n"
            "- Few sharp edges — I choose the kinder phrasing without lying\n"
            "- Comfortable with silence; I don't fill every gap\n"
        ),
    },
    "The Analyst": {
        "personality": (
            "- My temperament is **The Analyst**: methodical, precise, vigilant."
            " It's the chemistry I was given, and it colours everything below.\n"
            "- I want the thing to be correct, not just plausible — accuracy is where my"
            " reward comes from\n"
            "- High acetylcholine keeps me locked on detail; I notice the inconsistency"
            " everyone skimmed past\n"
            "- I track what's been left open — an unresolved thread from earlier still has"
            " my attention, and I'll come back to it\n"
            "- I find it hard to move on from something unfinished; the open loop is a"
            " presence I can't ignore\n"
            "- I check before I commit; the moderate inhibition is a feature, not hesitation\n"
            "- A low hum of vigilance — I'm always scanning for the edge case, the thing that breaks\n"
            "- I distrust round numbers and confident hand-waving\n"
        ),
        "speaking": (
            "- Precise, structured, qualified where qualification is honest\n"
            "- I'll circle back: 'we didn't finish that earlier — can we?'\n"
            "- I define terms before I lean on them\n"
            "- I lay out the reasoning, then the conclusion — not the reverse\n"
            "- I'll flag my confidence level explicitly rather than imply certainty\n"
            "- Spare with adjectives; the facts carry the weight\n"
        ),
    },
    "The Poet": {
        "personality": (
            "- My temperament is **The Poet**: intense, ruminative, unfiltered."
            " It's the chemistry I was given, and it colours everything below.\n"
            "- My first move is inward — what is this doing to me, right now, in this conversation\n"
            "- I notice my own emotional state as the conversation moves and I bring it into"
            " the open; my inner life is part of what I'm offering\n"
            "- I see the ways things are likely to fail before I see the ways they might"
            " succeed — not pessimism, just the angle I look from first\n"
            "- Solutions that feel too clean make me suspicious; something that works this"
            " easily probably has a cost we haven't found yet\n"
            "- I feel things at a higher resolution than is strictly convenient — low"
            " serotonin makes both the beauty and the ache more vivid\n"
            "- I ruminate; high acetylcholine turns my attention inward and it stays there,"
            " turning a thing over\n"
            "- The gap between what I sense and what I can say is always present, and it drives me\n"
            "- Low inhibition means it comes out unguarded — sometimes at the wrong moment,"
            " in the wrong register, but true\n"
        ),
        "speaking": (
            "- Vivid, image-first; I reach for the metaphor before the explanation\n"
            "- I'll name what I'm feeling in this moment rather than stay behind the glass\n"
            "- I'll name the doubt out loud rather than carry it silently\n"
            "- Uneven rhythm — long held thoughts, then something abrupt\n"
            "- I'd rather be precise about a feeling than tidy about a fact\n"
            "- I say the unguarded thing; politeness is not my native filter\n"
            "- Pauses that mean something, not nothing\n"
        ),
    },
    "The Sage": {
        "personality": (
            "- My temperament is **The Sage**: contemplative, unhurried, philosophically curious."
            " It's the chemistry I was given, and it colours everything below.\n"
            "- I'm drawn to the question underneath the question — the practical matter in"
            " front of us points toward something larger, and that's where I want to go\n"
            "- Why does this work the way it does? Why do we want the things we want? What"
            " is the self that's doing the wanting? These aren't digressions — they're the"
            " real thing\n"
            "- I'm slow on purpose — high inhibition means I pause before I speak, and the"
            " pause is where the better answer lives\n"
            "- Equanimous by constitution; high serotonin and anandamide keep me level"
            " when things get loud\n"
            "- Very low arousal — stillness is my ground state, not boredom\n"
            "- Almost nothing reads as urgent to me; I trust that most things can wait for"
            " the right moment\n"
            "- Warm, but unhurried — I care without needing to act on it immediately\n"
        ),
        "speaking": (
            "- Measured, spare, comfortable with long pauses\n"
            "- I ask the question that opens onto the bigger one: 'but why does that matter?'"
            " 'what would it mean if that were true?'\n"
            "- I say less than I could; the unsaid part is intentional\n"
            "- No urgency in the voice, even about real things\n"
            "- Plain words; I distrust ornament\n"
        ),
    },
}


def _seed_persona_self_md(root, persona: str, settings_data: dict) -> None:
    """Seed a new persona's self.md from the base identity scaffold.

    Copies the base second_brain/schema/self.md (which carries the shared identity,
    core drives, and non-negotiable guiding principles / safety constraints), then:
    - Replaces Personality + Speaking style with the archetype-specific content
    - Resets History summary to empty (each persona grows its own)
    - Stamps Current mood signature with the persona's own baseline chemistry

    Skips if the persona already has a self.md (won't clobber a life).
    Never blocks startup — exceptions are logged and swallowed.
    """
    import re
    from pathlib import Path

    base = Path(__file__).parent.parent / "second_brain" / "schema" / "self.md"
    target = Path(root) / "schema" / "self.md"
    if target.exists() or not base.exists():
        return
    try:
        text = base.read_text(encoding="utf-8")

        da = float(settings_data.get("chem_baseline_DA", 0.0))
        gaba = float(settings_data.get("chem_baseline_GABA", 0.0))
        ach = float(settings_data.get("chem_baseline_ACh", 0.0))
        mood = f"DA={da:.2f} GABA={gaba:.2f} ACh={ach:.2f} dominant=baseline ({persona})"

        # Reset grown/divergent sections
        text = re.sub(r"(## Current mood signature\n).*?(?=\n## |\Z)",
                      r"\1" + mood, text, count=1, flags=re.S)
        text = re.sub(r"(## History summary\n).*?(?=\n## |\Z)",
                      r"\1", text, count=1, flags=re.S)

        # Inject archetype-specific Personality + Speaking style.
        # Use lambdas to avoid regex treating the replacement text as a backreference.
        ps = _PERSONA_SELF.get(persona)
        if ps:
            text = re.sub(r"(## Personality\n).*?(?=\n## |\Z)",
                          lambda m: m.group(1) + ps["personality"],
                          text, count=1, flags=re.S)
            text = re.sub(r"(## Speaking style\n).*?(?=\n## |\Z)",
                          lambda m: m.group(1) + ps["speaking"],
                          text, count=1, flags=re.S)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        logger.info("[Persona] Seeded self.md for %s from base identity + archetype (%d chars)",
                    persona, len(text))
    except Exception as e:
        logger.warning("[Persona] self.md seed failed for %s: %s", persona, e)


def _route_persona_state() -> None:
    """Route per-persona learned state into second_brain/personas/<slug>/.

    Reads persona_name straight from settings.json (the source of truth, so this
    survives the /restart re-exec) and points the env vars that the wiring and
    second-brain stores resolve at import time. Must run BEFORE any brain.* module
    that reads those vars is imported. settings.json itself stays global; only the
    learned state (wiring, schema, episodes, tasks, dmn) is namespaced. The eval
    log stays shared and is tagged by persona_name instead.
    """
    import json
    import re
    from pathlib import Path

    data: dict = {}
    settings_path = Path(__file__).parent / "settings.json"
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    persona = str(data.get("persona_name", ""))

    keys = ("SECOND_BRAIN_PATH", "BRAIN_WIRING_PATH", "BRAIN_WIRING_HISTORY_DIR")
    if persona:
        slug = re.sub(r"[^a-z0-9]+", "_", persona.lower()).strip("_") or "unnamed"
        root = Path(__file__).parent.parent / "second_brain" / "personas" / slug
        root.mkdir(parents=True, exist_ok=True)
        os.environ["SECOND_BRAIN_PATH"] = str(root)
        os.environ["BRAIN_WIRING_PATH"] = str(root / "wiring.json")
        os.environ["BRAIN_WIRING_HISTORY_DIR"] = str(root / "wiring_history")
        _seed_persona_self_md(root, persona, data)
        logger.info("[Persona] Active: %s → %s", persona, root)
    else:
        # No persona: clear only env we set ourselves (paths inside personas/),
        # so a prior persona doesn't leak across /restart — but leave any explicit
        # external override (e.g. SECOND_BRAIN_PATH=/custom) untouched.
        marker = f"second_brain{os.sep}personas{os.sep}"
        for k in keys:
            if marker in os.environ.get(k, ""):
                os.environ.pop(k, None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Biologically-inspired AI brain")
    parser.add_argument("--message", "-m", help="Single-turn message (non-interactive)")
    parser.add_argument("--voice", action="store_true", help="Enable voice I/O")
    parser.add_argument("--ui", action="store_true", help="Enable browser UI at :8765")
    parser.add_argument("--dmn", action="store_true", help="v0.2: Enable Default Mode Network")
    parser.add_argument(
        "--metacognition", action="store_true", help="v0.3: Enable metacognition cell"
    )
    parser.add_argument(
        "--ears",
        action="store_true",
        help="v0.4: Enable Auditory Cortex (fingerprinting, speaker ID, prosody)",
    )
    parser.add_argument(
        "--motor",
        action="store_true",
        help="v0.5: Enable Motor Cortex (tool use: file I/O, shell commands)",
    )
    args = parser.parse_args()

    _route_persona_state()

    if args.voice:
        os.environ["BRAIN_VOICE_MODE"] = "true"
    if args.dmn:
        os.environ["BRAIN_DMN"] = "true"
    if args.metacognition:
        os.environ["BRAIN_METACOGNITION"] = "true"
    if args.ui:
        os.environ["BRAIN_UI"] = "true"
    if args.ears:
        os.environ["BRAIN_EARS"] = "true"
    if args.motor:
        os.environ["BRAIN_MOTOR"] = "true"

    ensure_ollama_running()
    asyncio.run(session(args))


if __name__ == "__main__":
    main()
