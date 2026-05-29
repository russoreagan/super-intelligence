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
