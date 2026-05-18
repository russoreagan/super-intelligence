"""
Brain run.py — session lifecycle and cluster wiring.
See brain/CONSTITUTION.md for design commitments.

Usage:
    uv run python -m brain.run
    uv run python -m brain.run --message "Hi, I'm Russ"
    uv run python -m brain.run --ui              # browser UI at http://localhost:8765
    uv run python -m brain.run --ui --dmn        # + stream of consciousness
    uv run python -m brain.run --ui --metacognition
    uv run python -m brain.run --voice           # Deepgram + ElevenLabs

Environment feature flags:
    BRAIN_UI=true             enable browser UI
    BRAIN_DMN=true            enable Default Mode Network
    BRAIN_METACOGNITION=true  enable metacognition cell
    BRAIN_VOICE_MODE=true     enable voice I/O
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.environ.get("BRAIN_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("brain.run")


async def session(args) -> None:
    from brain.bus import Bus
    from brain.brainstem import Brainstem
    from brain.model_router import ModelRouter
    from brain.pns import PNS
    from brain.clusters.temporal import TemporalCluster
    from brain.clusters.thalamus import ThalamusCluster
    from brain.clusters.occipital import OccipitalCluster
    from brain.clusters.hypothalamus import HypothalamusCluster
    from brain.clusters.parietal import ParietalCluster
    from brain.clusters.hippocampus import HippocampusCluster
    from brain.clusters.frontal import FrontalCluster
    from brain.observability.timeline import ObservabilityLayer, TurnTrace

    session_id = str(uuid.uuid4())[:8]
    logger.info("Session %s starting", session_id)

    bus = Bus()
    router = ModelRouter()
    brainstem = Brainstem(bus, router)
    pns = PNS(bus)
    obs = ObservabilityLayer(session_id)

    # Clusters
    thalamus = ThalamusCluster(bus)
    temporal = TemporalCluster(bus, router)
    occipital = OccipitalCluster(bus, router)
    hypothalamus = HypothalamusCluster(bus)
    parietal = ParietalCluster(bus)
    hippocampus = HippocampusCluster(bus, router)
    frontal = FrontalCluster(bus, brainstem, router)

    # Boot: pre-load core schema
    core_context = await hippocampus.boot(session_id)

    # ── UI server (optional) ──────────────────────────────────────────────────
    ui_enabled = args.ui or os.environ.get("BRAIN_UI", "false").lower() == "true"
    ui_server = None
    emitter = None
    # Queue for WebSocket-driven user messages (replaces stdin when --ui)
    ui_message_queue: asyncio.Queue = asyncio.Queue()

    if ui_enabled:
        from brain.ui.emitter import emitter as _emitter
        from brain.ui.server import UIServer
        emitter = _emitter

        async def _on_browser_message(text: str) -> None:
            await ui_message_queue.put(text)

        ui_server = UIServer(emitter.get_queue(), on_user_message=_on_browser_message)
        asyncio.create_task(ui_server.start(port=8765))
        # Give the server a moment to bind
        await asyncio.sleep(0.3)

    async def _emit(cluster: str, intensity: float, note: str, turn_id: str = "") -> None:
        if emitter:
            await emitter.emit(cluster, intensity, note, turn_id)

    async def _emit_end(cluster: str, turn_id: str = "") -> None:
        if emitter:
            await emitter.emit(cluster, 0.0, "done", turn_id)

    # ── v0.2: Default Mode Network ────────────────────────────────────────────
    dmn = None
    if args.dmn or os.environ.get("BRAIN_DMN", "false").lower() == "true":
        from brain.dmn import DefaultModeNetwork
        dmn = DefaultModeNetwork(bus, router, hippocampus, parietal)

        # Hook DMN thoughts into UI stream
        if emitter:
            orig_tick = dmn._tick

            async def _dmn_tick_with_ui():
                await orig_tick()
                # After tick, surface any new thought to UI
                if emitter:
                    await emitter.emit("dmn", 0.25, "thinking...", "dmn")

            dmn._tick = _dmn_tick_with_ui

        await dmn.start(session_id)

    # ── v0.3: Metacognition ───────────────────────────────────────────────────
    meta = None
    if args.metacognition or os.environ.get("BRAIN_METACOGNITION", "false").lower() == "true":
        from brain.metacognition import MetacognitionCell
        meta = MetacognitionCell(bus, router, hippocampus._schema)
        await meta.start()

    # Brainstem heartbeat
    hb_task = asyncio.create_task(brainstem.heartbeat())

    # Session trace accumulator for sleep consolidation
    session_traces: list[dict] = []

    # ── Core turn processor ───────────────────────────────────────────────────
    async def process_turn(user_input: str, image_path: str | None = None) -> str:
        if dmn:
            dmn.pause()

        turn = brainstem.begin_turn()
        turn_id = turn.turn_id

        # Notify UI of turn start (before resetting integrators so session_id is ready)
        if emitter:
            await emitter.emit_turn_start(turn_id, user_input)
            # Attach session_id to the event retroactively via a direct queue push
            try:
                emitter.get_queue().put_nowait({
                    "type": "turn_start",
                    "turn_id": turn_id,
                    "user_input": user_input,
                    "session_id": session_id,
                })
            except Exception:
                pass

        # Reset integrators
        temporal._understanding.reset_turn(turn_id)
        frontal._executive.reset_turn(turn_id)
        for d in frontal._drafters:
            d.reset_turn(turn_id)
        frontal._critic.reset_turn(turn_id)

        # PNS → bus
        await pns.receive_text(user_input, image_path)

        # ── Temporal: language understanding ──────────────────────────────────
        await _emit("temporal", 0.7, "parsing input", turn_id)
        features = await temporal.run(turn_id)
        await _emit_end("temporal", turn_id)

        if features is None:
            brainstem.end_turn()
            if emitter:
                await emitter.emit_turn_end(turn_id, "...", 0.0, 0)
            return "..."

        # ── Hypothalamus + Thalamus: parallel ─────────────────────────────────
        await _emit("hypothalamus", 0.6, "updating affect", turn_id)
        await _emit("thalamus", 0.55, "routing attention", turn_id)
        affect_task = asyncio.create_task(hypothalamus.process(features))
        thalamus_task = asyncio.create_task(thalamus.route(features, {}))
        affect, routing = await asyncio.gather(affect_task, thalamus_task)
        await _emit_end("hypothalamus", turn_id)
        await _emit_end("thalamus", turn_id)

        # Emit emotion to UI
        if emitter and affect.get("emotion"):
            await emitter.emit_emotion(affect["emotion"])
            await emitter.emit_neuromod(bus.neuromod.snapshot())

        # ── Occipital: vision (only if image present) ─────────────────────────
        vision_features = None
        if image_path and features.get("requires_vision"):
            await _emit("occipital", 0.9, "processing image", turn_id)
            vision_features = await occipital.process(image_path, user_input, turn_id)
            await _emit_end("occipital", turn_id)

        # ── Hippocampus: recall ───────────────────────────────────────────────
        memory = {}
        if features.get("requires_memory") or features.get("epistemic_action"):
            await _emit("hippocampus", 0.75, "recalling memory", turn_id)
            memory = await hippocampus.recall(
                query=user_input,
                entities=features.get("entities", []),
                turn_id=turn_id,
            )
            await _emit_end("hippocampus", turn_id)
        else:
            memory = {"core": core_context, "schema": "", "episodes": ""}

        if vision_features:
            memory["vision"] = (
                f"Image: {vision_features.get('description', '')}\n"
                f"Text in image: {vision_features.get('text_in_image', '')}\n"
                f"Context: {vision_features.get('context_for_response', '')}"
            )

        parietal_context = parietal.recent_turns_text()

        if dmn:
            dmn.update_context(parietal_context, affect.get("emotion", "neutral"),
                               core_context.get("self", ""))

        # ── Frontal: Multiple Drafts engine ───────────────────────────────────
        if not brainstem.check_budget():
            response = "I've reached my thinking limit for this turn."
        else:
            await _emit("frontal", 0.9, "drafting response", turn_id)
            response = await frontal.process(features, affect, memory, parietal_context, turn_id)
            await _emit_end("frontal", turn_id)

        # ── Brainstem: articulation ───────────────────────────────────────────
        await _emit("brainstem", 0.4, "articulating", turn_id)
        if not turn.committed:
            brainstem.add_draft(f"final_{turn_id}", response, 0.9)
            brainstem.endorse(f"final_{turn_id}")
        final = await brainstem.articulation_gate(turn)
        await _emit_end("brainstem", turn_id)

        # Post-turn housekeeping
        parietal.update(features, user_input, final)
        hypothalamus.decay_turn()
        turn_result = brainstem.end_turn()

        nm_snap = bus.neuromod.snapshot()
        llm_calls = len(router._call_log)

        # Final neuromod push to UI
        if emitter:
            await emitter.emit_neuromod(nm_snap)
            await emitter.emit_turn_end(turn_id, final, turn_result.elapsed(), llm_calls)

        # Observability
        trace = TurnTrace(
            turn_id=turn_id,
            session_id=session_id,
            user_input=user_input,
            response=final,
            llm_calls=llm_calls,
            elapsed_s=turn_result.elapsed(),
            emotion=affect.get("emotion", "neutral"),
            neuromod=nm_snap,
        )
        obs.record_turn(trace)

        if meta:
            meta.record_turn(
                turn_id=turn_id,
                llm_calls=llm_calls,
                elapsed_s=turn_result.elapsed(),
                emotion=affect.get("emotion", "neutral"),
                neuromod=nm_snap,
                surprise_score=features.get("surprise_score", 0.5),
            )

        session_traces.append({
            "user_input": user_input,
            "entity_response": final,
            "emotion": affect.get("emotion", "neutral"),
            "topic_tags": features.get("entities", []),
        })

        # Hippocampus: encode in background
        await _emit("hippocampus", 0.45, "encoding episode", turn_id)
        asyncio.create_task(hippocampus.encode(
            session_id=session_id,
            turn_id=turn_id,
            user_input=user_input,
            entity_response=final,
            features=features,
            affect=affect,
            neuromod_snap=nm_snap,
            surprise_score=features.get("surprise_score", 0.5),
        ))

        if dmn:
            dmn.resume()

        logger.info("Turn %s: %d LLM calls | %.2fs | emotion=%s",
                    turn_id, llm_calls, turn_result.elapsed(), affect.get("emotion"))
        return final

    # ── Run modes ─────────────────────────────────────────────────────────────
    if args.message:
        response = await process_turn(args.message)
        await pns.emit(response)

    elif ui_enabled:
        # Browser drives the conversation via WebSocket
        print("Brain online. Open http://localhost:8765 to interact.\n")
        while True:
            try:
                user_input = await asyncio.wait_for(ui_message_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if not user_input:
                continue

            image_path = None
            if "[image:" in user_input:
                import re
                m = re.search(r'\[image:([^\]]+)\]', user_input)
                if m:
                    image_path = m.group(1).strip()
                    user_input = user_input.replace(m.group(0), "").strip()

            response = await process_turn(user_input, image_path)
            # response already emitted via turn_end event; no stdout needed

    else:
        # CLI REPL
        print("Brain online. Type your message, or 'quit' to exit.\n")
        while True:
            try:
                if args.voice:
                    user_input = await pns.mic_listen()
                    if not user_input:
                        continue
                    print(f"You: {user_input}")
                else:
                    user_input = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: input("You: ").strip()
                    )
            except (EOFError, KeyboardInterrupt):
                print("\nSession ending...")
                break

            if user_input.lower() in ("quit", "exit", "bye"):
                print("Brain: Goodbye.")
                break
            if not user_input:
                continue

            image_path = None
            if "[image:" in user_input:
                import re
                m = re.search(r'\[image:([^\]]+)\]', user_input)
                if m:
                    image_path = m.group(1).strip()
                    user_input = user_input.replace(m.group(0), "").strip()

            response = await process_turn(user_input, image_path)
            await pns.emit(response)

    # ── Shutdown ──────────────────────────────────────────────────────────────
    hb_task.cancel()

    if session_traces:
        try:
            from brain.sleep import SleepConsolidation
            sleep = SleepConsolidation(router, hippocampus._schema, hippocampus._episodic)
            logger.info("Running sleep consolidation...")
            await sleep.consolidate(session_id, session_traces)
        except Exception as e:
            logger.warning("Sleep consolidation failed: %s", e)

    obs.flush()
    logger.info("Session %s complete. Total LLM calls: %d",
                session_id, brainstem._session_cost_calls)

    if meta:
        summary = meta.summary()
        if summary:
            print(f"\nSession stats: {summary.get('turn_count')} turns | "
                  f"avg {summary.get('avg_llm_calls')} LLM calls | "
                  f"dominant emotion: {summary.get('dominant_emotion')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Biologically-inspired AI brain")
    parser.add_argument("--message", "-m", help="Single-turn message (non-interactive)")
    parser.add_argument("--voice", action="store_true", help="Enable voice I/O")
    parser.add_argument("--ui", action="store_true", help="Enable browser UI at :8765")
    parser.add_argument("--dmn", action="store_true", help="v0.2: Enable Default Mode Network")
    parser.add_argument("--metacognition", action="store_true",
                        help="v0.3: Enable metacognition cell")
    args = parser.parse_args()

    if args.voice:
        os.environ["BRAIN_VOICE_MODE"] = "true"
    if args.dmn:
        os.environ["BRAIN_DMN"] = "true"
    if args.metacognition:
        os.environ["BRAIN_METACOGNITION"] = "true"
    if args.ui:
        os.environ["BRAIN_UI"] = "true"

    asyncio.run(session(args))


if __name__ == "__main__":
    main()
