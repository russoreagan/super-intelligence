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
    uv run python -m brain.run --voice --ears    # + auditory cortex

Environment feature flags:
    BRAIN_UI=true             enable browser UI
    BRAIN_DMN=true            enable Default Mode Network
    BRAIN_METACOGNITION=true  enable metacognition cell
    BRAIN_VOICE_MODE=true     enable voice I/O
    BRAIN_EARS=true           enable auditory cortex
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

load_dotenv(override=True)  # override=True so .env values win over empty shell exports

logging.basicConfig(
    level=os.environ.get("BRAIN_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
from brain.security import SecretRedactingFilter
_redact_filter = SecretRedactingFilter()
logging.getLogger().addFilter(_redact_filter)
logger = logging.getLogger("brain.run")


_CANCEL_WORDS = frozenset(["never mind", "nevermind", "skip", "cancel", "forget it",
                            "don't bother", "no thanks", "not now"])


def _extract_identity_name(text: str, features: dict) -> str | None:
    """Extract a person's name from a self-identification utterance."""
    from brain.clusters.audio_dsp import extract_identity_name
    name = extract_identity_name(text)
    if name:
        return name
    # Fallback: a single short alphabetic entity from the temporal parse
    entities = features.get("entities", [])
    if len(entities) == 1:
        candidate = entities[0].strip()
        if 2 <= len(candidate) <= 30 and candidate.replace(" ", "").isalpha():
            return candidate.title()
    return None


def _is_enrollment_cancellation(text: str) -> bool:
    """True if the user is declining enrollment."""
    return text.lower().strip() in _CANCEL_WORDS or any(w in text.lower() for w in _CANCEL_WORDS)


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
    obs = ObservabilityLayer(session_id)
    router = ModelRouter(obs=obs)
    brainstem = Brainstem(bus, router)
    pns = PNS(bus)

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

    from brain.security import PseudonymizationGateway, EGRESS_MODE
    egress = PseudonymizationGateway()

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

        ui_server = UIServer(emitter.get_queue(), on_user_message=_on_browser_message,
                             on_voice_change=pns.set_voice_id)
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

    # ── v0.4: Auditory Cortex ─────────────────────────────────────────────────
    ears = None
    enrollment_complete_inbox: asyncio.Queue = bus.subscribe("auditory.enrollment_complete")
    if args.ears or os.environ.get("BRAIN_EARS", "false").lower() == "true":
        from brain.clusters.auditory_cortex import AuditoryCluster
        ears = AuditoryCluster(bus)
        asyncio.create_task(ears.run())

    # Brainstem heartbeat
    hb_task = asyncio.create_task(brainstem.heartbeat())

    # Session trace accumulator for sleep consolidation
    session_traces: list[dict] = []

    # Background hippocampus encode tasks — must flush at shutdown
    pending_encodes: set[asyncio.Task] = set()

    def _track_encode(task: asyncio.Task) -> None:
        pending_encodes.add(task)
        def _done(t: asyncio.Task) -> None:
            pending_encodes.discard(t)
            exc = t.exception() if not t.cancelled() else None
            if exc:
                logger.error("Memory write failed for this turn — episode will not be saved to long-term memory: %s", exc)
        task.add_done_callback(_done)

    # ── Core turn processor ───────────────────────────────────────────────────
    from brain.brainstem import TURN_TIMEOUT

    async def process_turn(user_input: str, image_path: str | None = None) -> str:
        try:
            return await asyncio.wait_for(
                _process_turn_body(user_input, image_path),
                timeout=TURN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Turn timed out after %.1fs — sending fallback response. "
                "If Ollama is slow, increase BRAIN_TURN_TIMEOUT_SECONDS (currently %.1fs).",
                TURN_TIMEOUT, TURN_TIMEOUT,
            )
            timeout_msg = "I'm taking too long to think. Let me try again."
            try:
                brainstem.end_turn()
            except Exception:
                pass
            if emitter:
                # Best-effort UI signal that the turn ended
                try:
                    await emitter.emit_turn_end("timeout", timeout_msg, TURN_TIMEOUT, 0)
                except Exception:
                    pass
            if dmn:
                dmn.resume()
            return timeout_msg

    async def _process_turn_body(user_input: str, image_path: str | None = None) -> str:
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

        # ── Enrollment (multi-speaker, single shared mic) ─────────────────────
        if ears is not None:
            # 1. Collect auto-completions the auditory cortex already resolved
            #    (multi-speaker case — it knows which voice slice said which name).
            completed: list[dict] = []
            while True:
                try:
                    ec = enrollment_complete_inbox.get_nowait()
                    if not ec.expired and ec.payload.get("action") in ("enrolled", "merged"):
                        completed.append(ec.payload)
                except asyncio.QueueEmpty:
                    break

            # 2. Deterministic fallback: exactly one pending voice + a name in this
            #    turn's text → attribute it to that speaker. (Common 1-on-1 case.)
            pending = ears.enrollment_pending_speakers
            if pending and not completed:
                if _is_enrollment_cancellation(user_input):
                    for spk in pending:
                        ears.cancel_enrollment(spk.session_key)
                elif len(pending) == 1:
                    name = _extract_identity_name(user_input, features)
                    if name:
                        result = ears.complete_enrollment(pending[0].session_key, name)
                        if result.get("action") in ("enrolled", "merged"):
                            completed.append(result)
                            logger.info("Enrollment: %s '%s'", result["action"], name)

            if completed:
                features = dict(features)
                features["_enrollment_result"] = completed[0]
                features["_enrollment_results"] = completed

        # ── Hypothalamus + Thalamus: parallel ─────────────────────────────────
        await _emit("hypothalamus", 0.6, "updating affect", turn_id)
        await _emit("thalamus", 0.55, "routing attention", turn_id)
        affect_task = asyncio.create_task(hypothalamus.process(features))
        thalamus_task = asyncio.create_task(thalamus.route(features, {}))
        results = await asyncio.gather(affect_task, thalamus_task, return_exceptions=True)
        affect, routing = results
        if isinstance(affect, BaseException):
            logger.warning("Emotion analysis failed — using neutral defaults: %s", affect)
            affect = {"emotion": "neutral", "user_emotion": "unknown"}
        if isinstance(routing, BaseException):
            logger.warning("Attention routing failed — using defaults: %s", routing)
            routing = {}
        await _emit_end("hypothalamus", turn_id)
        await _emit_end("thalamus", turn_id)

        # Surface live enrollment state to the frontal lobe (registry is source of truth)
        if ears is not None and isinstance(affect, dict):
            pending = ears.enrollment_pending_speakers
            affect["enrollment_pending"] = len(pending) > 0
            affect["enrollment_pending_count"] = len(pending)
            affect["enrollment_closest_match"] = pending[0].closest_match if pending else None

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
                embedding_fn=router.embed,
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

        # ── Egress pseudonymisation (Phase 0H) ────────────────────────────────
        if EGRESS_MODE != "off":
            ps_memory = dict(memory)
            ps_schema, _ = egress.pseudonymize(memory.get("schema", ""))
            ps_episodes, _ = egress.pseudonymize(memory.get("episodes", ""))
            ps_core_self, _ = egress.pseudonymize(memory.get("core", {}).get("self", ""))
            ps_core_user, _ = egress.pseudonymize(memory.get("core", {}).get("user", ""))
            ps_memory["schema"] = ps_schema
            ps_memory["episodes"] = ps_episodes
            ps_core = dict(memory.get("core", {}))
            ps_core["self"] = ps_core_self
            ps_core["user"] = ps_core_user
            ps_memory["core"] = ps_core
            ps_user_input, _ = egress.pseudonymize(user_input)
            ps_parietal_context, _ = egress.pseudonymize(parietal_context)
        else:
            ps_memory = memory
            ps_user_input = user_input
            ps_parietal_context = parietal_context

        # ── Frontal: Multiple Drafts engine ───────────────────────────────────
        if not brainstem.check_budget():
            response = "I've reached my thinking limit for this turn."
        else:
            await _emit("frontal", 0.9, "drafting response", turn_id)
            # Pass pseudonymised context to cloud frontal cells
            ps_features = dict(features)
            if EGRESS_MODE != "off":
                ps_features["raw_text"] = ps_user_input
            response = await frontal.process(ps_features, affect, ps_memory, ps_parietal_context, turn_id)
            # Restore real values in response before delivering to user
            response = egress.depseudonymize(response)
            if egress.vault_size > 0:
                logger.debug("Egress: %s", egress.audit_summary())
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

        # Hippocampus: encode in background (tracked for shutdown flush)
        await _emit("hippocampus", 0.45, "encoding episode", turn_id)
        encode_task = asyncio.create_task(hippocampus.encode(
            session_id=session_id,
            turn_id=turn_id,
            user_input=user_input,
            entity_response=final,
            features=features,
            affect=affect,
            neuromod_snap=nm_snap,
            surprise_score=features.get("surprise_score", 0.5),
            embedding_fn=router.embed,
        ))
        _track_encode(encode_task)

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
            # response already sent to browser via turn_end event;
            # also speak it aloud if voice mode is enabled
            await pns.emit(response)

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

    # Flush pending hippocampus encodes so episodes aren't lost on exit
    if pending_encodes:
        logger.info("Waiting for %d in-progress memory writes to finish before exit...", len(pending_encodes))
        await asyncio.gather(*pending_encodes, return_exceptions=True)

    if session_traces:
        try:
            from brain.sleep import SleepConsolidation
            sleep = SleepConsolidation(router, hippocampus._schema, hippocampus._episodic)
            logger.info("Running end-of-session memory consolidation (summarising facts, updating self-model)...")
            await sleep.consolidate(session_id, session_traces)
        except Exception as e:
            logger.warning("End-of-session memory consolidation failed — recent facts may not be saved: %s", e)

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
    parser.add_argument("--ears", action="store_true",
                        help="v0.4: Enable Auditory Cortex (fingerprinting, speaker ID, prosody)")
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

    asyncio.run(session(args))


if __name__ == "__main__":
    main()
