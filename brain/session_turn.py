"""Turn processing methods for BrainSession — imported as _TurnMixin."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from brain.emotion_hierarchy import core_of
from brain.security import EGRESS_MODE

logger = logging.getLogger("brain.run")

_CANCEL_WORDS = frozenset(["never mind", "nevermind", "skip", "cancel", "forget it",
                            "don't bother", "no thanks", "not now"])


class _TurnMixin:
    # ── Turn processing ───────────────────────────────────────────────────────

    async def process_turn(self, user_input: str, image_path: str | None = None) -> tuple[str, dict]:
        from brain.brainstem import TURN_TIMEOUT
        try:
            return await asyncio.wait_for(
                self._process_turn_body(user_input, image_path),
                timeout=TURN_TIMEOUT,
            )
        except TimeoutError:
            logger.warning(
                "Turn timed out after %.1fs — sending fallback response. "
                "If Ollama is slow, increase BRAIN_TURN_TIMEOUT_SECONDS (currently %.1fs).",
                TURN_TIMEOUT, TURN_TIMEOUT,
            )
            timeout_msg = "I'm taking too long to think. Let me try again."
            with contextlib.suppress(Exception):
                self.brainstem.end_turn()
            if self._emitter:
                with contextlib.suppress(Exception):
                    await self._emitter.emit_turn_end("timeout", timeout_msg, TURN_TIMEOUT, 0)
            if self.dmn:
                self.dmn.resume()
            return timeout_msg, {}

    async def _process_turn_body(self, user_input: str, image_path: str | None = None) -> tuple[str, dict]:
        from brain.observability.firing_path import reset_current_trace, set_current_trace
        from brain.observability.timeline import TurnTrace

        if self.dmn:
            self.dmn.pause()
            try:
                _interim_context = (
                    f"{self.parietal.recent_turns_text()}\n\nUser just said: {user_input}"
                )
                self.dmn.update_context(_interim_context)
            except Exception:
                pass

        turn = self.brainstem.begin_turn()
        turn_id = turn.turn_id
        self.obs.begin_turn(turn_id, user_input)

        trace = TurnTrace(
            turn_id=turn_id,
            session_id=self.session_id,
            user_input=user_input,
        )
        trace.prior_neuromod = self.bus.neuromod.snapshot()
        _ctx_token = set_current_trace(trace)

        if self._emitter:
            await self._emitter.emit_turn_start(turn_id, user_input, session_id=self.session_id)

        # Reset integrators
        self.temporal._understanding.reset_turn(turn_id)
        self.frontal._executive.reset_turn(turn_id)
        for d in self.frontal._drafters:
            d.reset_turn(turn_id)
        self.frontal._critic.reset_turn(turn_id)

        await self.pns.receive_text(user_input, image_path)

        # ── Temporal: language understanding ──────────────────────────────────
        await self._emit("temporal", 0.7, "parsing input", turn_id)
        features = await self.temporal.run(turn_id)
        await self._emit_end("temporal", turn_id)

        if features is None:
            self.brainstem.end_turn()
            if self._emitter:
                await self._emitter.emit_turn_end(turn_id, "...", 0.0, 0)
            return "..."

        # ── Enrollment (multi-speaker, single shared mic) ─────────────────────
        if self.ears is not None:
            completed: list[dict] = []
            while True:
                try:
                    ec = self._enrollment_complete_inbox.get_nowait()
                    if not ec.expired and ec.payload.get("action") in ("enrolled", "merged"):
                        completed.append(ec.payload)
                except asyncio.QueueEmpty:
                    break

            pending = self.ears.enrollment_pending_speakers
            if pending and not completed:
                if _is_enrollment_cancellation(user_input):
                    for spk in pending:
                        self.ears.cancel_enrollment(spk.session_key)
                elif len(pending) == 1:
                    name = _extract_identity_name(user_input, features)
                    if name:
                        result = self.ears.complete_enrollment(pending[0].session_key, name)
                        if result.get("action") in ("enrolled", "merged"):
                            completed.append(result)
                            logger.info("Enrollment: %s '%s'", result["action"], name)

            if completed:
                features = dict(features)
                features["_enrollment_result"] = completed[0]
                features["_enrollment_results"] = completed
                for _ec in completed:
                    _enrolled_name = _ec.get("name", "")
                    _session_key = _ec.get("session_key", "")
                    if _enrolled_name:
                        _sf = self.hippocampus._schema.ensure_speaker_schema(_enrolled_name)
                        asyncio.ensure_future(
                            self.hippocampus._schema.aappend_fact(
                                _sf, f"User's name is {_enrolled_name}"
                            )
                        )
                        if _session_key:
                            _placeholder = self.hippocampus._schema.speaker_filename(_session_key)
                            _placeholder_content = self.hippocampus._schema.read(_placeholder)
                            if _placeholder_content:
                                asyncio.ensure_future(
                                    self.hippocampus._schema.migrate_placeholder(_placeholder, _sf)
                                )

        # ── Surface latest speaker identity + song match ───────────────────────
        latest_speaker = None
        while True:
            try:
                sm = self._speaker_id_inbox.get_nowait()
                if not sm.expired:
                    latest_speaker = sm.payload
            except asyncio.QueueEmpty:
                break
        latest_song = None
        while True:
            try:
                mm = self._song_match_inbox.get_nowait()
                if not mm.expired and mm.payload.get("matched"):
                    latest_song = mm.payload
            except asyncio.QueueEmpty:
                break
        if latest_speaker or latest_song:
            features = dict(features)
            if latest_speaker:
                features["_speaker_match_score"] = latest_speaker.get("match_score", 0.0)
                if latest_speaker.get("identified") and latest_speaker.get("speaker_name"):
                    features["speaker_name"] = latest_speaker["speaker_name"]
                elif not latest_speaker.get("identified"):
                    from brain.settings import settings as _settings_ref
                    _soft_threshold = float(_settings_ref.get("speaker_primary_soft_threshold"))
                    _match_score = latest_speaker.get("match_score", 0.0)
                    _closest = latest_speaker.get("closest_match") or ""
                    _primary = self.hippocampus._schema.primary_user_name()
                    if (_primary and _closest.lower() == _primary.lower()
                            and _match_score >= _soft_threshold):
                        pass
                    else:
                        _session_key = latest_speaker.get("session_key", "unknown")
                        features["speaker_name"] = _session_key
                        features["_speaker_unknown"] = True
            if latest_song:
                features["song_match"] = latest_song

        # ── Hypothalamus + Thalamus: parallel ─────────────────────────────────
        await self._emit("hypothalamus", 0.6, "updating affect", turn_id)
        await self._emit("thalamus", 0.55, "routing attention", turn_id)
        affect_task = asyncio.create_task(self.hypothalamus.process(features))
        thalamus_task = asyncio.create_task(self.thalamus.route(features, {}))
        results = await asyncio.gather(affect_task, thalamus_task, return_exceptions=True)
        affect, routing = results
        if isinstance(affect, BaseException):
            logger.warning("Emotion analysis failed — using neutral defaults: %s", affect)
            affect = {"emotion": "neutral", "user_emotion": "unknown"}
        if isinstance(routing, BaseException):
            logger.warning("Attention routing failed — using defaults: %s", routing)
            routing = {}
        await self._emit_end("hypothalamus", turn_id)
        await self._emit_end("thalamus", turn_id)

        if self.ears is not None and isinstance(affect, dict):
            pending = self.ears.enrollment_pending_speakers
            affect["enrollment_pending"] = len(pending) > 0
            affect["enrollment_pending_count"] = len(pending)
            affect["enrollment_closest_match"] = pending[0].closest_match if pending else None

        if self._emitter and affect.get("emotion"):
            await self._emitter.emit_emotion(affect["emotion"])
            await self._emitter.emit_neuromod(self.bus.neuromod.snapshot())
            if affect.get("hormonal"):
                await self._emitter.emit_hormonal(affect["hormonal"])
        if self._emitter:
            user_tone = affect.get("vocal_tone") or features.get("user_tone_toward_ai") or ""
            if user_tone:
                await self._emitter.emit_user_emotion(user_tone)

        # ── Occipital: vision ─────────────────────────────────────────────────
        vision_features = None
        if image_path:
            await self._emit("occipital", 0.9, "processing image", turn_id)
            vision_features = await self.occipital.process(image_path, user_input, turn_id)
            await self._emit_end("occipital", turn_id)

        # ── Hippocampus: recall ───────────────────────────────────────────────
        memory: dict = {}
        if features.get("requires_memory") or features.get("epistemic_action"):
            await self._emit("hippocampus", 0.75, "recalling memory", turn_id)
            memory = await self.hippocampus.recall(
                query=user_input,
                entities=features.get("entities", []),
                turn_id=turn_id,
                embedding_fn=self.router.embed,
            )
            await self._emit_end("hippocampus", turn_id)
        else:
            memory = {"core": self._core_context, "schema": "", "episodes": ""}

        if vision_features:
            memory["vision"] = (
                f"Image: {vision_features.get('description', '')}\n"
                f"Text in image: {vision_features.get('text_in_image', '')}\n"
                f"Context: {vision_features.get('context_for_response', '')}"
            )

        # ── Motor Cortex: tool execution ──────────────────────────────────────
        if self.motor:
            cloud = getattr(self.motor, "_cloud", None)
            if cloud and cloud.has_pending:
                raw_text = features.get("raw_text", user_input)
                if cloud.is_user_confirming(raw_text):
                    await self._emit("motor_cortex", 0.9, "executing confirmed action", turn_id)
                    try:
                        tool_result = await cloud.execute_pending(turn_id)
                        if tool_result:
                            output = tool_result.get("output", "")
                            memory["tool_result"] = f"[cloud_action — confirmed]\n{output}"
                            logger.info("[CloudExecutor] Confirmed write executed (success=%s)",
                                        tool_result.get("success"))
                    except Exception as _ce:
                        logger.error("Cloud executor failed on confirmed write: %s", _ce)
                    await self._emit_end("motor_cortex", turn_id)
                elif cloud.is_user_denying(raw_text):
                    cloud.clear_pending()
                    memory["tool_result"] = "[cloud_action — cancelled by user]"
                    logger.info("[CloudExecutor] Pending write action cancelled by user")
            elif features.get("requires_action"):
                await self._emit("motor_cortex", 0.85, "executing tool", turn_id)
                self.motor.reset_turn(turn_id)
                if features.get("response_type") == "task":
                    memory["tool_result"] = "[task_queued]\nTask acknowledged — working on it now."
                    logger.info("[MotorCortex] Task mode — deferring planning to background")
                else:
                    try:
                        tool_result = await self.motor.execute(features, turn_id)
                        if tool_result:
                            output = tool_result.get("output", "")
                            tool_name = tool_result.get("tool", "tool")
                            if tool_result.get("pending"):
                                desc = output.replace("CONFIRMATION_NEEDED:", "").strip()
                                memory["tool_result"] = (
                                    f"[confirmation_needed]\n"
                                    f"You are about to: {desc}\n"
                                    "Ask the user to confirm before proceeding."
                                )
                            else:
                                memory["tool_result"] = f"[{tool_name}]\n{output}"
                            logger.info("[MotorCortex] %s → %d chars (success=%s)",
                                        tool_name, len(output), tool_result.get("success"))
                    except Exception as _mc_err:
                        logger.error("Motor cortex failed this turn: %s", _mc_err)
                await self._emit_end("motor_cortex", turn_id)

        parietal_context = self.parietal.recent_turns_text()

        if self.dmn:
            from brain.metacognition import read_affection_score, read_familiarity
            _speaker_name_for_dmn = features.get("speaker_name") if isinstance(features, dict) else None
            _schema_for_dmn = getattr(self.hippocampus, "_schema", None)
            _relationship = {
                "score": read_affection_score(_schema_for_dmn, _speaker_name_for_dmn or ""),
                "familiarity": read_familiarity(_schema_for_dmn, _speaker_name_for_dmn or ""),
            }
            self.dmn.update_context(parietal_context, affect.get("emotion", "neutral"),
                                    self._core_context.get("self", ""),
                                    speaker_name=_speaker_name_for_dmn,
                                    relationship=_relationship)

            ABSENCE_THRESHOLD_S = 300.0
            absence_s = time.time() - self._last_turn_ts
            if absence_s >= ABSENCE_THRESHOLD_S and self.dmn.has_deferred_content():
                deferred = self.dmn.take_deferred_thoughts()
                proposals = self.dmn.list_proposals()
                returning_context_parts = []
                if deferred:
                    returning_context_parts.append(
                        f"Thoughts and questions saved while you were away:\n{deferred}"
                    )
                if proposals:
                    awaiting = [p for p in proposals if "awaiting_review" in p.get("status", "")]
                    if awaiting:
                        prop_lines = "\n".join(
                            f"- {p['title']} ({p['proposed']}) — {p['path']}"
                            for p in awaiting
                        )
                        returning_context_parts.append(
                            f"Work proposals ready for your review:\n{prop_lines}"
                        )
                if returning_context_parts:
                    memory["returning_content"] = "\n\n".join(returning_context_parts)
                    logger.info("[DMN] Surfacing deferred content on user return (absent %.0fs)",
                                absence_s)

            thoughts = self.dmn.recent_thoughts_tagged(n=4)
            if thoughts:
                memory["recent_thoughts"] = thoughts
            anticipations = self.dmn.take_anticipations()
            if anticipations:
                memory["anticipations"] = anticipations
                logger.info("[Anticipator] Surfacing %d pre-prepared scenarios to drafters",
                            len(anticipations))
            prefetched = self.dmn.take_prefetched()
            if prefetched:
                memory["prefetched_context"] = prefetched
                logger.info("[Prefetcher] Surfacing %d pre-fetched topics to drafters",
                            len(prefetched))
                from brain.voice_bridge import bleed_overlap as _word_overlap
                useful: list[tuple[str, float]] = []
                for entry in thoughts:
                    t = entry["thought"] if isinstance(entry, dict) else entry
                    o = _word_overlap(user_input, t)
                    if o >= 0.35:
                        useful.append((t, o))
                if useful:
                    for thought_text, overlap in useful:
                        asyncio.create_task(self.hippocampus.encode_idle_thought(
                            session_id=self.session_id,
                            thought=thought_text,
                            overlap_with_user_input=overlap,
                            user_input=user_input,
                            embedding_fn=self.router.embed,
                        ))

        # ── Per-turn speaker context injection ────────────────────────────────
        _speaker = features.get("speaker_name", "")
        if _speaker:
            _speaker_schema = self.hippocampus._schema.load_speaker_context(_speaker)
            memory = dict(memory)
            memory["core"] = dict(memory.get("core", {}))
            memory["core"]["user"] = _speaker_schema

        # ── Egress pseudonymisation ───────────────────────────────────────────
        if EGRESS_MODE != "off":
            ps_memory = dict(memory)
            ps_schema, _ = self._egress.pseudonymize(memory.get("schema", ""))
            ps_episodes, _ = self._egress.pseudonymize(memory.get("episodes", ""))
            if memory.get("recent_thoughts"):
                ps_memory["recent_thoughts"] = [
                    {**entry, "thought": self._egress.pseudonymize(entry["thought"])[0]}
                    for entry in memory["recent_thoughts"]
                ]
            ps_core_self, _ = self._egress.pseudonymize(memory.get("core", {}).get("self", ""))
            ps_core_user, _ = self._egress.pseudonymize(memory.get("core", {}).get("user", ""))
            ps_memory["schema"] = ps_schema
            ps_memory["episodes"] = ps_episodes
            ps_core = dict(memory.get("core", {}))
            ps_core["self"] = ps_core_self
            ps_core["user"] = ps_core_user
            ps_memory["core"] = ps_core
            ps_user_input, _ = self._egress.pseudonymize(user_input)
            ps_parietal_context, _ = self._egress.pseudonymize(parietal_context)
        else:
            ps_memory = memory
            ps_user_input = user_input
            ps_parietal_context = parietal_context

        # ── Frontal: Multiple Drafts engine ───────────────────────────────────
        draft_scores: list[dict] = []
        if not self.brainstem.check_budget():
            response = "I've reached my thinking limit for this turn."
        else:
            await self._emit("frontal", 0.9, "drafting response", turn_id)
            ps_features = dict(features)
            ps_affect = affect
            if EGRESS_MODE != "off":
                ps_features["raw_text"] = ps_user_input
                if ps_features.get("speaker_name"):
                    ps_name, _ = self._egress.pseudonymize(
                        ps_features["speaker_name"],
                        known_entities=[ps_features["speaker_name"]],
                    )
                    ps_features["speaker_name"] = ps_name
                for key in ("_enrollment_result", "_enrollment_results"):
                    val = ps_features.get(key)
                    if not val:
                        continue
                    items = val if isinstance(val, list) else [val]
                    ps_items = []
                    for item in items:
                        if isinstance(item, dict) and item.get("name"):
                            ps_item = dict(item)
                            ps_item["name"], _ = self._egress.pseudonymize(
                                item["name"], known_entities=[item["name"]]
                            )
                            ps_items.append(ps_item)
                        else:
                            ps_items.append(item)
                    ps_features[key] = ps_items if isinstance(val, list) else ps_items[0]
                if affect.get("appraisal"):
                    ps_appraisal, _ = self._egress.pseudonymize(affect["appraisal"])
                    ps_affect = dict(affect)
                    ps_affect["appraisal"] = ps_appraisal
            response = await self.frontal.process(
                ps_features, ps_affect, ps_memory, ps_parietal_context, turn_id,
                image_path=image_path,
            )
            draft_scores = list(self.frontal.last_turn_draft_scores)
            response = self._egress.depseudonymize(response)
            if self._egress.vault_size > 0:
                logger.debug("Egress: %s", self._egress.audit_summary())
            await self._emit_end("frontal", turn_id)

        # ── Brainstem: articulation ───────────────────────────────────────────
        await self._emit("brainstem", 0.4, "articulating", turn_id)
        if not turn.committed:
            self.brainstem.add_draft(f"final_{turn_id}", response, 0.9)
            self.brainstem.endorse(f"final_{turn_id}")
        final = await self.brainstem.articulation_gate(turn)
        await self._emit_end("brainstem", turn_id)

        await self._emit("parietal", 0.3, "updating context", turn_id)
        self.parietal.update(features, user_input, final)
        await self._emit_end("parietal", turn_id)
        self.hypothalamus.decay_turn()
        turn_result = self.brainstem.end_turn()

        nm_snap = self.bus.neuromod.snapshot()
        llm_calls = self.router.turn_calls_excluding_background()

        cluster_tokens: dict[str, dict] = {}
        for _entry in self.router._call_log:
            _cl = _entry.get("cluster", "unknown")
            if _cl not in cluster_tokens:
                cluster_tokens[_cl] = {"in": 0, "out": 0, "calls": 0}
            cluster_tokens[_cl]["in"] += _entry.get("in", 0)
            cluster_tokens[_cl]["out"] += _entry.get("out", 0)
            cluster_tokens[_cl]["calls"] += 1

        memory_recalled = bool(memory.get("episodes") or memory.get("schema"))
        memory_hit_count = len([ln for ln in (memory.get("episodes") or "").splitlines() if ln.strip()])

        selected_draft = next((d for d in draft_scores if d.get("selected")), {})
        selected_coherence = selected_draft.get("coherence", 0.5)
        selected_emotional_fit = (selected_draft.get("empathy_score")
                                  or selected_draft.get("tone_fit", 0.5))
        selected_draft_id = selected_draft.get("draft_id", "")

        if self._emitter:
            await self._emitter.emit_neuromod(nm_snap)
            h_snap_final = self.bus.hormonal.snapshot()
            await self._emitter.emit_hormonal(h_snap_final)
            await self._emitter.emit_turn_end(turn_id, final, turn_result.elapsed(), llm_calls)

        if self._emitter and draft_scores:
            try:
                await self._emitter.emit_event({
                    "type": "quality_score",
                    "turn_id": turn_id,
                    "score": round(selected_draft.get("overall", 0.5), 3),
                    "coherence": round(selected_coherence, 3),
                    "emotional_fit": round(selected_emotional_fit, 3),
                    "drafter_count": len(draft_scores),
                    "memory_used": memory_recalled,
                })
            except Exception as _qe:
                logger.debug("quality_score emit failed: %s", _qe)

        trace.response = final
        trace.llm_calls = llm_calls
        trace.elapsed_s = turn_result.elapsed()
        trace.emotion = affect.get("emotion", "neutral")
        trace.emotion_core = core_of(affect.get("emotion", "neutral"))
        trace.neuromod = nm_snap
        trace.hormonal = affect.get("hormonal") or self.bus.hormonal.snapshot()
        trace.draft_scores = draft_scores
        trace.selected_draft_id = selected_draft_id
        trace.drafter_count = len(draft_scores)
        trace.cluster_tokens = cluster_tokens
        trace.memory_recalled = memory_recalled
        trace.memory_hit_count = memory_hit_count
        trace.user_emotion = features.get("user_emotion", "") if isinstance(features, dict) else ""
        trace.speaker_name = features.get("speaker_name", "")
        trace.speaker_score = features.get("_speaker_match_score", 0.0)
        trace.prosody_tone = affect.get("vocal_tone") or ""
        trace.prosody_f0_hz = affect.get("prosody_f0_hz", 0.0)
        trace.prosody_energy = affect.get("prosody_energy", 0.0)
        trace.prosody_jitter = affect.get("prosody_jitter", 0.0)
        trace.prosody_shimmer = affect.get("prosody_shimmer", 0.0)
        self.obs.record_turn(trace)
        self._session_traces_full.append(trace)

        if self._follow_through:
            async def _follow_through_check() -> None:
                deferred_goal = self._pending_task.take() if self._pending_task else None
                if deferred_goal:
                    try:
                        extracted = await self._follow_through.extract(user_input, final, turn_id)
                    except Exception:
                        extracted = None
                    if extracted:
                        goal = extracted
                    else:
                        # extract() returned None — the assistant's ack was too brief
                        # to anchor a commitment. Fall back to the topic_summary-based
                        # goal set by FrontalTaskSubsystem (already cleaner than raw_text).
                        goal = deferred_goal
                        logger.debug("[FollowThrough] No commitment found in response — using topic goal: %s", goal[:80])
                    self._task_queue.enqueue(goal, source="user", priority=1)
                    logger.info("[FollowThrough] Task enqueued (task-mode): %s", goal[:120])
                    return
                try:
                    goal = await self._follow_through.extract(user_input, final, turn_id)
                    if goal:
                        self._task_queue.enqueue(goal, source="user", priority=1)
                        logger.info("[FollowThrough] Task enqueued (reactive): %s", goal[:120])
                except Exception as _e:
                    logger.warning("[FollowThrough] failed: %s", _e)
            asyncio.create_task(_follow_through_check())

        if self._emotion_judge:
            self._emotion_judge.fire(trace)
        if self._learning_monitor:
            self._learning_monitor.record_turn(trace)
        if self._baseline_runner:
            memory_ctx = ((memory.get("episodes") or "") + "\n" + (memory.get("schema") or ""))
            self._baseline_runner.fire(
                turn_id, user_input, final,
                memory_ctx[:1000], selected_coherence, selected_emotional_fit,
                trace=trace,
            )
        if self.meta:
            self.meta.record_turn(
                turn_id=turn_id,
                llm_calls=llm_calls,
                elapsed_s=turn_result.elapsed(),
                emotion=affect.get("emotion", "neutral"),
                neuromod=nm_snap,
                surprise_score=features.get("surprise_score", 0.5),
                features=features,
                draft_scores=draft_scores,
            )

        self._session_traces.append({
            "user_input": user_input,
            "entity_response": final,
            "emotion": affect.get("emotion", "neutral"),
            "topic_tags": features.get("entities", []),
            "speaker_name": features.get("speaker_name", ""),
        })

        await self._emit("hippocampus", 0.45, "encoding episode", turn_id)
        encode_task = asyncio.create_task(self.hippocampus.encode(
            session_id=self.session_id,
            turn_id=turn_id,
            user_input=user_input,
            entity_response=final,
            features=features,
            affect=affect,
            neuromod_snap=nm_snap,
            surprise_score=features.get("surprise_score", 0.5),
            embedding_fn=self.router.embed,
        ))
        self._track_encode(encode_task)

        if self.dmn:
            self.dmn.note_last_response(final)
            self.dmn.resume()

        self._last_turn_ts = time.time()

        logger.info("Turn %s: %d LLM calls | %.2fs | emotion=%s",
                    turn_id, llm_calls, turn_result.elapsed(), affect.get("emotion"))

        with contextlib.suppress(Exception):
            reset_current_trace(_ctx_token)

        return final, affect

    async def _run_task(self, task) -> None:
        job_turn_id = f"task_{task.id}"
        is_self = getattr(task, "source", "") == "self"
        if is_self:
            self.router.enter_background_mode()
        try:
            summary = await self.motor.execute_internal_job(task.goal, job_turn_id)
        except Exception as _e:
            logger.warning("[TaskWorker] Task [%s] execution failed: %s", task.id, _e)
            self._task_queue.mark_done(task.id, success=False)
            return
        finally:
            if is_self:
                self.router.exit_background_mode()

        if summary.get("clarification"):
            question = summary["clarification"]
            self._task_queue.mark_blocked(task.id, reason=question)
            logger.info("[TaskWorker] Task [%s] blocked on clarification: %s",
                        task.id, question[:120])
            if self._emitter:
                await self._emitter.emit_proactive_speech(question)
            await self.pns.emit(question, {"emotion": "curious"})
            return

        self._task_queue.mark_done(task.id, success=bool(summary.get("success")))
        spoken_summary = await self._result_reporter.report(summary, job_turn_id)
        if not spoken_summary:
            spoken_summary = (
                "Done — but I don't have a clean summary to share."
                if summary.get("success")
                else "I couldn't finish that — something went wrong."
            )
        logger.info("[TaskWorker] Reporting result [%s]: %s", task.id, spoken_summary[:160])
        if self._emitter:
            await self._emitter.emit_event({
                "type": "task_summary",
                "job_id": summary.get("job_id"),
                "summary": spoken_summary,
            })
            await self._emitter.emit_proactive_speech(spoken_summary)
        await self.pns.emit(spoken_summary,
                            {"emotion": "lively" if summary.get("success") else "concerned"})


# ── Module-level helpers (used inside _process_turn_body) ─────────────────────

def _extract_identity_name(text: str, features: dict) -> str | None:
    from brain.clusters.audio_dsp import extract_identity_name
    name = extract_identity_name(text)
    if name:
        return name
    entities = features.get("entities", [])
    if len(entities) == 1:
        candidate = entities[0].strip()
        if 2 <= len(candidate) <= 30 and candidate.replace(" ", "").isalpha():
            return candidate.title()
    return None


def _is_enrollment_cancellation(text: str) -> bool:
    return text.lower().strip() in _CANCEL_WORDS or any(w in text.lower() for w in _CANCEL_WORDS)
