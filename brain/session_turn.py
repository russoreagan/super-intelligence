"""Turn processing methods for BrainSession — imported as _TurnMixin."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time

from brain.emotion_hierarchy import core_of
from brain.emotion_presets import strip_reaction_tags
from brain.security import EGRESS_MODE
from brain.settings import settings

# Strip [mood:X]...[/mood] markup from display text — TTS handles its own expansion.
_MOOD_MARKUP_RE = re.compile(r"\[mood:[^\]]+\](.*?)\[/mood\]", re.DOTALL | re.IGNORECASE)

# Strip hallucinated tool-call markup. A drafter may emit a pseudo tool call as
# prose ("<cloud_action>...</cloud_action>") when a tool was needed but motor
# cortex didn't run — e.g. when LLM feature-extraction falls back under a cloud
# outage and requires_action collapses to false. These XML action blocks are NOT
# the real motor protocol (that's JSON, dispatched before drafting); any
# angle-bracket action block in spoken prose is a confabulation and must never
# reach display or TTS. First regex removes balanced blocks; second mops up an
# unclosed/truncated opener through end-of-text.
_TOOL_TAGS = "cloud_action|tool_call|tool|action_block"
_TOOL_MARKUP_RE = re.compile(rf"<({_TOOL_TAGS})\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_TOOL_MARKUP_DANGLING_RE = re.compile(rf"<(?:{_TOOL_TAGS})\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)


def _scrub_tool_markup(text: str) -> tuple[str, bool]:
    """Remove hallucinated tool-call markup. Returns (cleaned, stripped_anything)."""
    cleaned = _TOOL_MARKUP_RE.sub("", text)
    cleaned = _TOOL_MARKUP_DANGLING_RE.sub("", cleaned)
    if cleaned == text:
        return text, False
    # Collapse the whitespace the removed block left behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, True


logger = logging.getLogger("brain.run")

_CANCEL_WORDS = frozenset(
    [
        "never mind",
        "nevermind",
        "skip",
        "cancel",
        "forget it",
        "don't bother",
        "no thanks",
        "not now",
    ]
)


class _TurnMixin:
    # ── Turn processing ───────────────────────────────────────────────────────

    async def api_turn(
        self, message: str, end_user_id: str, mandate_id: str | None = None, persona: str | None = None
    ) -> tuple[str, dict]:
        """Engine entry point: run one turn for a specific end-user (the partner's
        customer), under the assignment selected by mandate_id (the catalog is
        cached; this just names which one). Turns are serialized per process — the
        turn-execution state (brainstem, cluster integrators) is process-global even
        though chemistry is now per-client, so concurrent API requests queue rather
        than corrupt each other's turn. The customer's chemistry is bound inside
        process_turn via end_user_id, so each queued turn runs in its own mood.
        `persona` (multi-persona Path B) binds that persona's memory + mandate +
        chemistry for the turn, so one process serves many personas; None = the
        process persona, unchanged."""
        lock = getattr(self, "_api_turn_lock", None)
        if lock is None:
            lock = self._api_turn_lock = asyncio.Lock()
        async with lock:
            text, affect = await self.process_turn(
                message, end_user_id=end_user_id, mandate_id=mandate_id, persona=persona
            )
            # A cloud WRITE that needs confirmation parks itself on the executor's
            # process-global pending slot. Move that pending action out to the
            # caller (the API stores it on the durable session) and clear the slot,
            # so concurrent sessions never collide on it. Auto-confirmed writes
            # already executed, so nothing is pending for them.
            cloud = getattr(getattr(self, "motor", None), "_cloud", None)
            if cloud is not None and getattr(cloud, "has_pending", False):
                affect = dict(affect) if isinstance(affect, dict) else {}
                affect["pending"] = cloud.get_pending()
                cloud.clear_pending()
            return text, affect

    async def api_confirm(
        self,
        pending: dict,
        end_user_id: str,
        mandate_id: str | None = None,
        approve: bool = True,
    ) -> tuple[str, dict]:
        """Resolve a session's parked cloud-write. approve=True re-injects the
        pending action into the executor and runs it (under the session's agent
        scope); approve=False discards it. Serialized on the same turn lock."""
        lock = getattr(self, "_api_turn_lock", None)
        if lock is None:
            lock = self._api_turn_lock = asyncio.Lock()
        async with lock:
            cloud = getattr(getattr(self, "motor", None), "_cloud", None)
            if not approve or cloud is None or not pending:
                return ("Discarded the pending action.", {"emotion": "neutral"})
            cloud.set_pending(pending)
            agent_id = None
            if mandate_id:
                try:
                    from brain.second_brain.store import _persona_key, _resolve_persona

                    agent_id = f"{_persona_key(_resolve_persona(''))}.{mandate_id}"
                except Exception:
                    agent_id = None
            from brain.agent_ctx import bind_agent

            with bind_agent(agent_id):
                result = await cloud.execute_pending("api-confirm")
            output = (result or {}).get("output", "") if isinstance(result, dict) else ""
            success = (
                bool((result or {}).get("success", False)) if isinstance(result, dict) else False
            )
            return (output or "Done.", {"emotion": "neutral", "action_success": success})

    async def api_purge_end_user(self, end_user_id: str) -> dict:
        """Erase one end-user's footprint (ops / GDPR): their durable rows across
        every per-end-user table, plus this process's in-memory caches for them.
        Returns a per-table deleted summary. Serialized on the turn lock so a purge
        can't race a turn for the same customer."""
        end_user_id = (end_user_id or "").strip()
        if not end_user_id:
            return {"ok": False, "error": "end_user_id required"}
        lock = getattr(self, "_api_turn_lock", None)
        if lock is None:
            lock = self._api_turn_lock = asyncio.Lock()
        async with lock:
            # In-process caches first (so a concurrent reload can't repopulate from
            # a row we're about to delete).
            reg = getattr(self, "_client_chem", None)
            if reg is not None:
                with contextlib.suppress(Exception):
                    reg.forget(end_user_id)
            um = getattr(self, "_engine_um_cache", None)
            if isinstance(um, dict):
                um.pop(end_user_id, None)

            deleted: dict = {}
            try:
                from brain.second_brain import supabase_client

                if supabase_client.is_enabled():
                    client = supabase_client.get_client()
                    org = supabase_client.get_org_id()
                    # Every table carrying end_user_id. brain_schemas rows keyed by a
                    # non-empty end_user_id are the per-speaker model; the persona's
                    # own self.md/user.md use end_user_id='' and are untouched.
                    for table in (
                        "episodes",
                        "tasks",
                        "dmn_state",
                        "speaker_profiles",
                        "brain_schemas",
                        "api_sessions",
                    ):
                        try:
                            res = (
                                client.table(table)
                                .delete()
                                .eq("org_id", org)
                                .eq("end_user_id", end_user_id)
                                .execute()
                            )
                            deleted[table] = len(res.data or [])
                        except Exception as e:
                            deleted[table] = f"error: {e}"
            except Exception as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "end_user_id": end_user_id, "deleted": deleted}

    def _engine_user_model(self, end_user_id: str) -> str:
        """The customer's user-model for an engine turn — their per-speaker schema
        (the same store the relationship/sleep system already populates), cached per
        session. "" if unavailable, in which case the drafter falls back to the
        process-level user.md. Companion turns never call this."""
        cache = getattr(self, "_engine_um_cache", None)
        if cache is None:
            cache = self._engine_um_cache = {}
        if end_user_id in cache:
            return cache[end_user_id]
        text = ""
        try:
            schema = getattr(self.hippocampus, "_schema", None)
            if schema is not None:
                text = schema.read(schema.speaker_filename(end_user_id)) or ""
        except Exception:
            text = ""
        cache[end_user_id] = text
        return text

    def _client_chem_registry(self):
        """Lazily build the per-(persona, end_user) chemistry registry for this
        session. Only ever touched in engine mode (a turn carrying an end_user_id);
        companion turns never call this. Defaults to in-memory storage; a durable
        backend is injected by the engine layer when it lands."""
        reg = getattr(self, "_client_chem", None)
        if reg is None:
            from brain.client_chem import ClientChemRegistry

            reg = ClientChemRegistry(self.bus, persona=self.persona_name)
            self._client_chem = reg
        return reg

    def _persona_chem_pair(self, persona: str, end_user_id: str):
        """A per-(persona, end_user) ChemPair carrying THAT persona's temperament
        (baselines + current levels), cached for the session so its mood evolves
        across turns (multi-persona Path B). Seeded from the persona's chemistry
        profile, so each debate seat reasons in its own mood — the lean comes from
        the persona, not the prompt."""
        cache = getattr(self, "_persona_chem", None)
        if cache is None:
            cache = self._persona_chem = {}
        key = f"{persona}:{end_user_id}"
        pair = cache.get(key)
        if pair is None:
            from brain.persona_chem import load as _load_persona_chem

            state = _load_persona_chem(persona) or {}
            pair = self.bus.new_chem_for(state.get("resting"), state.get("current"))
            cache[key] = pair
        return pair

    async def process_turn(
        self,
        user_input: str,
        image_path: str | None = None,
        end_user_id: str | None = None,
        mandate_id: str | None = None,
        persona: str | None = None,
    ) -> tuple[str, dict]:
        from brain.second_brain.store import bind_persona

        # Multi-persona Path B: when a persona is bound, scope memory + mandate to it
        # (via bind_persona → _resolve_persona) and bind THAT persona's own chemistry
        # — so one process serves many personas, each in its own mood. With no persona
        # it's the existing per-customer path; with neither it's the single resting
        # chemistry, byte-for-byte as before (nullcontext is a true no-op).
        persona = (persona or "").strip()
        registry = None
        if persona and end_user_id is not None:
            bind_cm = self.bus.bind(self._persona_chem_pair(persona, end_user_id))
        elif end_user_id is not None:
            registry = self._client_chem_registry()
            pair = registry.get_or_create(end_user_id)
            registry.note_interaction(end_user_id)
            bind_cm = self.bus.bind(pair)
        else:
            bind_cm = contextlib.nullcontext()

        try:
            with bind_persona(persona), bind_cm:
                return await self._run_turn_guarded(user_input, image_path, end_user_id, mandate_id)
        finally:
            if registry is not None:
                registry.persist(end_user_id)

    async def _run_turn_guarded(
        self,
        user_input: str,
        image_path: str | None = None,
        end_user_id: str | None = None,
        mandate_id: str | None = None,
    ) -> tuple[str, dict]:
        from brain.brainstem import TURN_TIMEOUT

        try:
            return await asyncio.wait_for(
                self._process_turn_body(user_input, image_path, end_user_id, mandate_id),
                timeout=TURN_TIMEOUT,
            )
        except TimeoutError:
            logger.warning(
                "Turn timed out after %.1fs — sending fallback response. "
                "If Ollama is slow, increase BRAIN_TURN_TIMEOUT_SECONDS (currently %.1fs).",
                TURN_TIMEOUT,
                TURN_TIMEOUT,
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

    def _emit_accomplishment_reward(self, summary: dict) -> None:
        """Stage 6: terminal mastery DA at job completion, scaled by effort overcome × the
        expectation-gap curve × the persona's mastery valuation. Difficulty is the MEASURED
        effort (continuous), anti-flailing — productive work + confirmed hypotheses (depth), not
        raw retries. Success-gated with an asymmetry: failing a hard task stings less than
        succeeding rewards, so the entity stays drawn TO challenge. Best-effort."""
        from brain.neuron import accomplishment_factor, loss_aversion, reward_weight

        complexity = str(summary.get("complexity", "medium"))
        productive = float(
            summary.get("productive_steps", summary.get("steps_taken_count", 0)) or 0
        )
        confirmed = float(summary.get("predictions_confirmed", 0) or 0)
        measured_effort = productive + 0.5 * confirmed  # depth counts; thrashing (retries) doesn't
        if measured_effort <= 0:
            return
        expected = float(
            settings.get(
                f"accomplishment_expected_{complexity}",
                settings.get("accomplishment_expected_medium"),
            )
        )
        difficulty, modifier = accomplishment_factor(measured_effort, expected)
        persona = str(settings.get("persona_name", ""))
        w = reward_weight(persona, "mastery")
        er = float(settings.get("emotional_reactivity_scale"))
        base = float(settings.get("accomplishment_base"))
        if bool(summary.get("success")):
            self.bus.neuromod.add("DA", base * difficulty * modifier * w * er)
        else:
            # Failing a braced-for hard task is a loss — weight the dip by this persona's loss
            # aversion (λ), independently of the symmetric mastery reward weight (w). The reward
            # branch above is never λ-scaled; the asymmetry IS loss aversion.
            la = loss_aversion(persona)
            fail_ratio = float(settings.get("accomplishment_fail_ratio"))
            self.bus.neuromod.add("DA", -base * difficulty * fail_ratio * w * er * la)
            self.bus.hormonal.add("5HT", -float(settings.get("correctness_5ht_drain")) * w * er * la)

    async def _verify_world_prediction(self, pred: dict, actual_input: str) -> None:
        """Stage 5 Tier B: did our idle prediction of the user's next message hold? Embed-compare
        the DMN's predicted_next against what the user actually said and reward a confident hit
        (or mildly dip a confident miss) — self-verified correctness about the world, no user
        verdict. Predicting free-form input is inherently non-trivial, so informativeness is high
        and `correct` is decided by semantic similarity. Best-effort; never raises into the turn."""
        try:
            import math

            from brain.neuron import prediction_reward, reward_weight

            predicted_text = str(pred.get("predicted_input", "")).strip()
            confidence = float(pred.get("confidence", 0.0) or 0.0)
            if not predicted_text or confidence < float(settings.get("prediction_confidence_min")):
                return
            va = await self.router.embed(actual_input)
            vp = await self.router.embed(predicted_text)
            if not va or not vp:
                return
            dot = sum(a * b for a, b in zip(va, vp, strict=False))
            na = math.sqrt(sum(a * a for a in va)) or 1.0
            nb = math.sqrt(sum(b * b for b in vp)) or 1.0
            sim = dot / (na * nb)
            correct = sim >= 0.6  # semantic-match threshold for "the world confirmed it"
            pr = prediction_reward(confidence, correct, informativeness=1.0)
            if not pr:
                return
            persona = str(settings.get("persona_name", ""))
            delta = (
                pr
                * float(settings.get("prediction_reward_base"))
                * reward_weight(persona, "correctness")
                * float(settings.get("emotional_reactivity_scale"))
            )
            cap = float(settings.get("prediction_reward_turn_cap"))
            self.bus.neuromod.add("DA", max(-cap, min(cap, delta)))
        except Exception:
            pass

    async def _process_turn_body(
        self,
        user_input: str,
        image_path: str | None = None,
        end_user_id: str | None = None,
        mandate_id: str | None = None,
    ) -> tuple[str, dict]:
        from brain.observability.firing_path import reset_current_trace, set_current_trace
        from brain.observability.timeline import TurnTrace

        if self.dmn:
            self.dmn.pause()
            # Stage 5 Tier B: before we overwrite context, check whether the DMN's idle
            # prediction of this turn's input actually held. Self-verified correctness about the
            # world — reward a confident hit, no user verdict needed. Fire-and-forget.
            _pred = getattr(self.dmn, "predicted_next", None)
            if _pred:
                with contextlib.suppress(Exception):
                    asyncio.create_task(self._verify_world_prediction(_pred, user_input))
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
            persona_name=self.persona_name,
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
                    _soft_threshold = float(settings.get("speaker_primary_soft_threshold"))
                    _match_score = latest_speaker.get("match_score", 0.0)
                    _closest = latest_speaker.get("closest_match") or ""
                    _primary = self.hippocampus._schema.primary_user_name()
                    if (
                        _primary
                        and _closest.lower() == _primary.lower()
                        and _match_score >= _soft_threshold
                    ):
                        pass
                    else:
                        _session_key = latest_speaker.get("session_key", "unknown")
                        features["speaker_name"] = _session_key
                        features["_speaker_unknown"] = True
            if latest_song:
                features["song_match"] = latest_song

        # ── Engine mode: explicit end_user identity is authoritative ──────────
        # When an API caller supplies an end_user_id, it IS the speaker — the
        # partner's customer — overriding voice/primary-user inference so that
        # relationship, memory, and affect key on that customer. Companion turns
        # pass None and fall through to the inference below unchanged.
        if end_user_id:
            features = dict(features) if isinstance(features, dict) else {}
            features["speaker_name"] = end_user_id
            # Mark this as an engine turn so the prompt assembly keeps the cached
            # context process-stable (identity + mandate catalog) and moves the
            # per-customer user-model to the per-turn message.
            features["end_user_id"] = end_user_id
            _eum = self._engine_user_model(end_user_id)
            if _eum:
                features["engine_user_model"] = _eum

        # Engine mode: the partner-assigned MANDATE is selected by id — the catalog
        # is cached, the per-turn message just names the active assignment. Companion
        # turns pass none → no change.
        if mandate_id:
            features = dict(features) if isinstance(features, dict) else {}
            features["mandate_id"] = mandate_id
            # The agent IS (this process's persona, mandate). Derive its id here so
            # the motor layer can resolve per-agent permissions at enforcement time
            # (Phase 3). One process = one persona, so the persona half is implicit.
            try:
                from brain.second_brain.store import _persona_key, _resolve_persona

                features["agent_id"] = f"{_persona_key(_resolve_persona(''))}.{mandate_id}"
            except Exception:
                pass

        # ── Default speaker for typed input ───────────────────────────────────
        # When ears are off (or no diarization arrived) there is no voice-based
        # identity. Treat the keyboard as the primary user — otherwise DMN /
        # metacognition / hippocampus all default to "the user" and lose the
        # relationship binding that's already loaded for that name.
        if not (isinstance(features, dict) and features.get("speaker_name")):
            try:
                _primary = self.hippocampus._schema.primary_user_name()
            except Exception:
                _primary = ""
            if _primary:
                features = dict(features)
                features["speaker_name"] = _primary
                features["_speaker_assumed_primary"] = True

        # ── Presence-shadow: speaker-arrival chemistry nudge ──────────────────
        # When a newly identified speaker differs from the previous turn's speaker,
        # apply a small neuromod drag proportional to their stored affection.
        # Negative relationship → GABA (wariness) + NE (alertness). Mild by design:
        # max deltas are 0.08/0.04, so a deeply negative relationship barely moves
        # the needle on a single turn rather than instantly flipping the mood.
        # Only fires on voice-identified speakers (not the typed-input primary-user
        # fallback), so text-only sessions are unaffected.
        _resolved_speaker = features.get("speaker_name") if isinstance(features, dict) else None
        if (
            _resolved_speaker
            and not features.get("_speaker_assumed_primary")
            and _resolved_speaker != self._last_speaker_name
        ):
            try:
                from brain.metacognition import read_affection_score

                _arr_schema = getattr(self.hippocampus, "_schema", None)
                _arr_affection = read_affection_score(_arr_schema, _resolved_speaker)
                if _arr_affection < -5:
                    _arr_weight = min(1.0, (-_arr_affection - 5) / 45.0)
                    _arr_gaba = round(_arr_weight * 0.08, 4)
                    _arr_ne = round(_arr_weight * 0.04, 4)
                    self.bus.neuromod.add("GABA", _arr_gaba)
                    self.bus.neuromod.add("NE", _arr_ne)
                    _arr_snap = self.bus.neuromod.snapshot()
                    trace.neuromod_midturn.append(
                        {"trigger": "presence_shadow", "snapshot": _arr_snap}
                    )
                    if self._emitter:
                        await self._emitter.emit_neuromod(_arr_snap)
                    logger.debug(
                        "[PresenceShadow] %s arrived (affection=%d) → GABA+%.3f NE+%.3f",
                        _resolved_speaker,
                        _arr_affection,
                        _arr_gaba,
                        _arr_ne,
                    )
            except Exception:
                pass

        # ── Input modality + text paralinguistics ─────────────────────────────
        # Modality is derived from speaker detection: if the primary-user default
        # was applied (_speaker_assumed_primary) or ears are off → text turn.
        # This must run before hypothalamus.process() so the modality can be
        # passed in features for channel calibration.
        _is_text_input = features.get("_speaker_assumed_primary", False) or (self.ears is None)
        _input_modality = "text" if _is_text_input else "voice"
        features = dict(features)
        features["input_modality"] = _input_modality

        if _is_text_input and settings.get("enable_text_paralinguistics"):
            from brain.clusters.text_paralinguistics import extract_text_paralinguistics

            _text_para = extract_text_paralinguistics(user_input)
            features["text_paralinguistics"] = _text_para.to_dict()
        elif settings.get("enable_text_paralinguistics"):
            # Voice turn: the full text-para extractor stays off (prosody owns
            # the acoustic channel), but laughter markers in the TRANSCRIPT are
            # still evidence — Deepgram often transcribes a real laugh as
            # "ha ha". Hypothalamus composes this with the acoustic laughter
            # tiers via max(), feeding the same levity-scaled DA path.
            from brain.clusters.text_paralinguistics import extract_laughter

            features["transcript_laughter"] = extract_laughter(user_input)

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
            # Only prompt for voices we haven't already asked — the "who are you?"
            # question fires once per voice, not every turn it stays unenrolled.
            unprompted = [s for s in pending if not getattr(s, "enrollment_prompted", False)]
            affect["enrollment_pending"] = len(pending) > 0
            affect["enrollment_pending_count"] = len(unprompted)
            affect["enrollment_closest_match"] = unprompted[0].closest_match if unprompted else None
            for _s in unprompted:
                self.ears.mark_enrollment_prompted(_s.session_key)

        if self._emitter and affect.get("emotion"):
            _arousal = (affect.get("affect_dims") or {}).get("arousal")
            if _arousal is None:
                from brain.emotion_vocabulary import compute_affect_dims

                _arousal = compute_affect_dims(
                    self.bus.neuromod.snapshot(), self.bus.hormonal.snapshot()
                ).get("arousal")
            await self._emitter.emit_emotion(affect["emotion"], _arousal)
            await self._emitter.emit_neuromod(self.bus.neuromod.snapshot())
            if affect.get("hormonal"):
                await self._emitter.emit_hormonal(affect["hormonal"])
        if self._emitter:
            user_tone = affect.get("vocal_tone") or features.get("user_tone_toward_ai") or ""
            if user_tone:
                await self._emitter.emit_user_emotion(user_tone)
            # Numeric prosody for the "reading the speaker" energy/pace meters.
            energy = affect.get("prosody_energy", 0.0)
            pace = affect.get("prosody_speech_rate", 0.0)
            if energy or pace:
                await self._emitter.emit_user_prosody(energy, pace)

        # ── Occipital: vision ─────────────────────────────────────────────────
        vision_features = None
        if image_path:
            await self._emit("occipital", 0.9, "processing image", turn_id)
            vision_features = await self.occipital.process(image_path, user_input, turn_id)
            await self._emit_end("occipital", turn_id)

        # ── Hippocampus: recall ───────────────────────────────────────────────
        # Novelty also opens recall: a genuinely new topic may not request memory,
        # but that is exactly when the structural (cross-domain) pass should try to
        # surface a problem-shape the brain has solved before. On routine turns the
        # structural gate stays shut, so this adds no cost there.
        from brain.predictor import should_bypass_gating

        _bypass, _ = should_bypass_gating(affect, features)
        novelty = bool(_bypass) or float(features.get("surprise_score", 0.0) or 0.0) >= 0.6
        memory: dict = {}
        if features.get("requires_memory") or features.get("epistemic_action") or novelty:
            await self._emit("hippocampus", 0.75, "recalling memory", turn_id)
            memory = await self.hippocampus.recall(
                query=user_input,
                entities=features.get("entities", []),
                turn_id=turn_id,
                embedding_fn=self.router.embed,
                novelty=novelty,
                features=features,
            )
            await self._emit_end("hippocampus", turn_id)
            # Record which recall pathway produced hits so the Hebbian sleep pass can
            # credit the productive schema-vs-episode split (recall fan-out surface).
            trace.recall_contrib = memory.get("recall_contrib", {})
            # Emotional weight of recalled episodes: strong positive memory → ACh
            # (recognition/warmth), strong negative → GABA (threat). Only fires
            # when hippocampus finds a memory that clears the significance threshold.
            recall_affect = memory.get("recall_affect", {})
            if recall_affect:
                for channel, delta in recall_affect.items():
                    self.bus.neuromod.add(channel, delta)
                _snap = self.bus.neuromod.snapshot()
                trace.neuromod_midturn.append({"trigger": "hippocampus_recall", "snapshot": _snap})
                if self._emitter:
                    await self._emitter.emit_neuromod(_snap)
                # Recall affect moved the chemistry AFTER process() named the
                # emotion — re-derive the label so the drafters see one that
                # matches the neuromod state they're modulated by. Pre-draft, so
                # this is expression-phase chemistry (see the two-phase rule in
                # CONSTITUTION.md), not reward.
                try:
                    _emotion, _tendency = self.hypothalamus.refresh_emotion()
                    if _emotion != affect.get("emotion"):
                        logger.debug(
                            "[Affect] Recall affect shifted emotion %s → %s",
                            affect.get("emotion"),
                            _emotion,
                        )
                        affect["emotion"] = _emotion
                        affect["tendency"] = _tendency
                except Exception:
                    pass
        else:
            memory = {"core": self.hippocampus._active_core_context(), "schema": "", "episodes": ""}

        if vision_features:
            memory["vision"] = (
                f"Image: {vision_features.get('description', '')}\n"
                f"Text in image: {vision_features.get('text_in_image', '')}\n"
                f"Context: {vision_features.get('context_for_response', '')}"
            )

        # ── Recent background task results ────────────────────────────────────
        # Inject completed async task results so the LLM knows what actually
        # happened — prevents confabulation when the user asks about past tasks.
        if getattr(self, "_recent_task_results", None):
            lines = []
            for r in self._recent_task_results:
                status = "completed" if r["success"] else "failed"
                lines.append(f"- [{status}] {r['goal'][:80]}: {r['summary']}")
            memory["recent_task_results"] = "\n".join(lines)

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
                            logger.info(
                                "[CloudExecutor] Confirmed write executed (success=%s)",
                                tool_result.get("success"),
                            )
                    except Exception as _ce:
                        logger.error("Cloud executor failed on confirmed write: %s", _ce)
                    await self._emit_end("motor_cortex", turn_id)
                elif cloud.is_user_denying(raw_text):
                    cloud.clear_pending()
                    memory["tool_result"] = "[cloud_action — cancelled by user]"
                    logger.info("[CloudExecutor] Pending write action cancelled by user")
            elif features.get("requires_action"):
                await self._emit("motor_cortex", 0.85, "executing tool", turn_id)
                tool_result = None
                if features.get("response_type") == "task":
                    self.motor.reset_turn(turn_id)
                    memory["tool_result"] = "[task_queued]\nTask acknowledged — working on it now."
                    logger.info("[MotorCortex] Task mode — deferring planning to background")
                else:
                    # Always deferred: don't block the turn on motor planning/execution.
                    # Frontal produces an acknowledgment; result surfaces via proactive speech.
                    memory["tool_result"] = "[task_queued]\nI'm working on this."
                    asyncio.create_task(self._run_motor_reactive(features, turn_id))
                    logger.info("[MotorCortex] Reactive — deferred to background")
                await self._emit_end("motor_cortex", turn_id)
                # tool_result is always None in deferred mode; neuromod fires in _run_motor_reactive.

        parietal_context = self.parietal.recent_turns_text()

        if self.dmn:
            from brain.metacognition import read_affection_score, read_familiarity

            _speaker_name_for_dmn = (
                features.get("speaker_name") if isinstance(features, dict) else None
            )
            _schema_for_dmn = getattr(self.hippocampus, "_schema", None)
            _relationship = {
                "score": read_affection_score(_schema_for_dmn, _speaker_name_for_dmn or ""),
                "familiarity": read_familiarity(_schema_for_dmn, _speaker_name_for_dmn or ""),
            }
            self.dmn.update_context(
                parietal_context,
                affect.get("emotion", "neutral"),
                self.hippocampus._active_core_context().get("self", ""),
                speaker_name=_speaker_name_for_dmn,
                relationship=_relationship,
            )

            # Conversational ledger intents (B5): manual project assignment, or
            # confirming/correcting a conclusion the DMN raised. Best-effort and
            # non-blocking — never holds up the response.
            try:
                _ledger_evt = await self.dmn.process_user_message_for_ledger(user_input)
                if _ledger_evt:
                    memory["ledger_event"] = _ledger_evt
                    logger.info("[DMN] Ledger intent handled: %s", _ledger_evt.get("action"))
            except Exception as _li_err:
                logger.debug("[DMN] Ledger-intent handling skipped: %s", _li_err)

            # Stop-work intent: the user asked to stop/cancel the brain's
            # background work mid-conversation. Same kill switch as the UI
            # Clear button; the context note keeps the spoken acknowledgement
            # honest about what actually got stopped.
            try:
                if features.get("stop_work"):
                    _q = getattr(self, "_task_queue", None)
                    _had_work = bool(
                        (_q is not None and (_q.is_running() or _q.has_pending()))
                        or getattr(self, "_task_exec", None) is not None
                        or (self.dmn is not None and len(self.dmn._self_task_q) > 0)
                    )
                    if _had_work:
                        _stats = self.kill_self_directed_work()
                        memory["stop_work_ack"] = (
                            "You just STOPPED your background jobs at the user's request "
                            f"(running job killed: {_stats['killed_running']}, "
                            f"queued tasks cleared: {_stats['cleared']}). Acknowledge "
                            "briefly and naturally; do not restart the work unless asked."
                        )
                        logger.info("[TaskWorker] Stop-work intent honored: %s", _stats)
            except Exception as _sw_err:
                logger.debug("[TaskWorker] Stop-work handling failed: %s", _sw_err)

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
                            f"- {p['title']} ({p['proposed']}) — {p['path']}" for p in awaiting
                        )
                        returning_context_parts.append(
                            f"Work proposals ready for your review:\n{prop_lines}"
                        )
                if returning_context_parts:
                    memory["returning_content"] = "\n\n".join(returning_context_parts)
                    logger.info(
                        "[DMN] Surfacing deferred content on user return (absent %.0fs)", absence_s
                    )

            thoughts = self.dmn.recent_thoughts_tagged(n=4)
            if thoughts:
                memory["recent_thoughts"] = thoughts
            anticipations = self.dmn.take_anticipations()
            if anticipations:
                memory["anticipations"] = anticipations
                logger.info(
                    "[Anticipator] Surfacing %d pre-prepared scenarios to drafters",
                    len(anticipations),
                )
            prefetched = self.dmn.take_prefetched()
            if prefetched:
                memory["prefetched_context"] = prefetched
                logger.info(
                    "[Prefetcher] Surfacing %d pre-fetched topics to drafters", len(prefetched)
                )
                from brain.voice_bridge import bleed_overlap as _word_overlap

                useful: list[tuple[str, float]] = []
                for entry in thoughts:
                    t = entry["thought"] if isinstance(entry, dict) else entry
                    o = _word_overlap(user_input, t)
                    if o >= 0.35:
                        useful.append((t, o))
                if useful:
                    for thought_text, overlap in useful:
                        asyncio.create_task(
                            self.hippocampus.encode_idle_thought(
                                session_id=self.session_id,
                                thought=thought_text,
                                overlap_with_user_input=overlap,
                                user_input=user_input,
                                embedding_fn=self.router.embed,
                            )
                        )

            # ── Live-work thread routing (B8/B9) ──────────────────────────────
            # Surface open threads relevant to what the user is working on now,
            # volume gated by the AI's focus + the user's load. Injected like
            # prefetched context; closed-out after the response (note_threads_used).
            try:
                self.dmn.observe_user_turn(features, user_input)
                _budget = self.dmn.compute_routing_budget()
                _activity = " ".join(
                    str(x)
                    for x in (
                        user_input,
                        features.get("topic_summary", ""),
                        " ".join(features.get("entities", []) or []),
                        getattr(self.dmn, "_last_projects", ""),
                    )
                )
                _routed = self.dmn.route_threads_for_turn(_activity, budget=_budget)
                if _routed:
                    memory["open_threads"] = [
                        {"id": t.id, "summary": t.summary, "progress": t.progress[-1:]}
                        for t in _routed
                    ]
                    self._routed_threads = _routed
                    logger.info("[DMN] Routed %d open thread(s) into the turn", len(_routed))
            except Exception as _rt_err:
                logger.debug("[DMN] Thread routing skipped: %s", _rt_err)

        # ── Established cross-learning principles ─────────────────────────────
        # De-identified, k-corroborated lessons from the hypothesis store. Loaded
        # once per session (the store only changes at sleep consolidation) and
        # injected as background guidance the drafters may draw on.
        if settings.get("cross_learning", 0):
            if not hasattr(self, "_established_principles"):
                try:
                    from brain import cross_learning

                    self._established_principles = cross_learning.established_principles()
                except Exception as _xl_err:
                    logger.debug("[Cross-learning] principle load skipped: %s", _xl_err)
                    self._established_principles = []
            if self._established_principles:
                memory["established_principles"] = list(self._established_principles)

        # ── Affect carryover from the previous turn ───────────────────────────
        # A large post-draft chemistry swing last turn carries forward as a one-
        # line interoceptive hint (consumed once). See the two-phase chemistry
        # rule in CONSTITUTION.md.
        _carry = getattr(self, "_carryover_affect", None)
        if _carry:
            memory["affect_carryover"] = _carry
            self._carryover_affect = None

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
                ps_features,
                ps_affect,
                ps_memory,
                ps_parietal_context,
                turn_id,
                image_path=image_path,
            )
            draft_scores = list(self.frontal.last_turn_draft_scores)
            response = self._egress.depseudonymize(response)
            if self._egress.vault_size > 0:
                logger.debug("Egress: %s", self._egress.audit_summary())
            # Draft quality feeds back into chemistry: struggling to form a good
            # response is cognitively effortful (GABA/NE up); nailing it feels good (DA up).
            # The reward/penalty magnitude scales by how much THIS persona values being right
            # (reward_weight "correctness") — the Analyst is buoyed/stung far more than the
            # Empath — and is intrinsic: it fires on self-judged quality, no user praise needed.
            if draft_scores:
                from brain.neuron import loss_aversion, reward_weight

                best = max(draft_scores, key=lambda d: d.get("overall", 0.5))
                overall = best.get("overall", 0.5)
                _persona = str(settings.get("persona_name", ""))
                _w = reward_weight(_persona, "correctness")
                _er = float(settings.get("emotional_reactivity_scale"))
                if overall < 0.4:
                    # Effort cost (unchanged) PLUS the self-standard penalty: falling short of
                    # its own bar dips DA and drains 5HT (the lingering disappointed-in-self /
                    # guilt component). Falling short is a LOSS, so it scales by this persona's
                    # loss aversion (λ) on top of how much it values being right — the high-λ
                    # identity broods harder over its own miss. Flavor from resting chem.
                    _la = loss_aversion(_persona)
                    self.bus.neuromod.add("GABA", 0.06)
                    self.bus.neuromod.add("NE", 0.04)
                    self.bus.neuromod.add(
                        "DA", -float(settings.get("correctness_penalty_base")) * _w * _er * _la
                    )
                    self.bus.neuromod.add(
                        "5HT", -float(settings.get("correctness_5ht_drain")) * _w * _er * _la
                    )
                    _trigger = "draft_quality_low"
                elif overall > 0.7:
                    self.bus.neuromod.add(
                        "DA", float(settings.get("correctness_self_base")) * _w * _er
                    )
                    _trigger = "draft_quality_high"
                else:
                    _trigger = None
                if _trigger:
                    _snap = self.bus.neuromod.snapshot()
                    trace.neuromod_midturn.append({"trigger": _trigger, "snapshot": _snap})
                    if self._emitter:
                        await self._emitter.emit_neuromod(_snap)
            await self._emit_end("frontal", turn_id)

        # ── Brainstem: articulation ───────────────────────────────────────────
        await self._emit("brainstem", 0.4, "articulating", turn_id)
        if not turn.committed:
            self.brainstem.add_draft(f"final_{turn_id}", response, 0.9)
            self.brainstem.endorse(f"final_{turn_id}")
        final = await self.brainstem.articulation_gate(turn)
        # Belt-and-braces: strip any hallucinated tool-call markup before it can
        # reach TTS (raw_final) or display (final). Scrub raw_final first so both
        # derived forms are clean. If anything was stripped, the routing safety
        # net missed a tool request — log it so the gap is visible.
        final, _stripped_markup = _scrub_tool_markup(final)
        if _stripped_markup:
            logger.warning(
                "[Articulation] Stripped hallucinated tool-call markup from response "
                "(turn %s) — a tool request likely failed to route to motor cortex.",
                turn_id,
            )
        # Split raw (for PNS TTS — needs [mood:X] + bare reaction tags) from
        # display (for chat/memory/traces — must have all audio tags removed).
        raw_final = final
        final = _MOOD_MARKUP_RE.sub(lambda m: m.group(1).strip(), raw_final)
        final = strip_reaction_tags(final)
        await self._emit_end("brainstem", turn_id)

        await self._emit("parietal", 0.3, "updating context", turn_id)
        self.parietal.update(features, user_input, final)
        # Update per-modality user style tracking (style synchrony feature)
        if settings.get("enable_style_synchrony") and isinstance(features, dict):
            _style_modality = features.get("input_modality", "text")
            _style_alpha = (
                float(settings.get("style_ema_alpha_voice"))
                if _style_modality == "voice"
                else float(settings.get("style_ema_alpha_text"))
            )
            _style_sentiment = float(features.get("sentiment", 0.0))
            self.parietal.update_user_style(
                user_input, _style_modality, _style_sentiment, _style_alpha
            )
            # Fold this turn's discrete register tag into the rolling per-speaker
            # register profile (persisted alongside the style vectors at sleep).
            self.parietal.update_register(
                features.get("user_register", "neutral"),
                float(settings.get("register_ema_alpha")),
            )
        await self._emit_end("parietal", turn_id)
        self.hypothalamus.decay_turn()
        turn_result = self.brainstem.end_turn()

        nm_snap = self.bus.neuromod.snapshot()

        # Affect carryover (two-phase rule, CONSTITUTION.md): post-draft chemistry
        # never re-colors THIS response, but a large swing shouldn't vanish either
        # — a person still feels the last exchange's miss or win at the start of
        # the next one. Stash the DA delta; the next turn's drafter prompt gets a
        # one-line interoceptive hint.
        if settings.get("affect_carryover", 1):
            try:
                _pre_da = float((affect.get("neuromod") or {}).get("DA", nm_snap.get("DA", 0.5)))
                _carry_delta = float(nm_snap.get("DA", 0.5)) - _pre_da
                if abs(_carry_delta) >= float(settings.get("affect_carryover_da_threshold", 0.1)):
                    self._carryover_affect = {
                        "da_delta": round(_carry_delta, 3),
                        "feeling": (
                            "a lingering lift from how the last exchange landed"
                            if _carry_delta > 0
                            else "a lingering dip from how the last exchange landed"
                        ),
                    }
            except Exception:
                pass

        # Persist the active persona's evolved chemistry so a restart / persona
        # switch resumes from here instead of snapping back to the resting
        # profile. Throttled (>=5s) and best-effort: a single ~300-byte file per
        # persona, overwritten in place — never a growing log. /restart does a
        # raw os.execv that skips _shutdown, so per-turn saving is what survives.
        try:
            _persona = str(settings.get("persona_name", ""))
            if _persona:
                _now = time.monotonic()
                if _now - getattr(self, "_last_chem_save_ts", 0.0) >= 5.0:
                    from brain import persona_chem

                    # Off the event loop: this is a read-modify-write of a file on
                    # disk, and the turn shouldn't stall on filesystem latency.
                    _hs_snap = self.bus.hormonal.snapshot()
                    await asyncio.get_running_loop().run_in_executor(
                        None, persona_chem.save_current, _persona, nm_snap, _hs_snap
                    )
                    self._last_chem_save_ts = _now
        except Exception:
            pass

        # N1 (colony-features-ii): live trail reinforcement. Reinforce the edges this
        # turn actually fired, scaled by a lightweight outcome (per-turn DA delta —
        # the dopaminergic "did this pay off" signal). Fast within-session plasticity
        # over the slow sleep-consolidated weights; the overlay decays and evaporates
        # at session end. Always recorded; applied to live reads only when
        # colony_trail_apply=1 (otherwise shadow mode for the audit gate).
        if settings.get("colony_features", 0) and getattr(self, "wiring", None) is not None:
            try:
                _da_now = float(nm_snap.get("DA", 0.5))
                _da_prior = float((trace.prior_neuromod or {}).get("DA", _da_now))
                _outcome = max(-1.0, min(1.0, (_da_now - _da_prior) * 4.0))
                _path_names = [n["name"] for n in (trace.fired_path or []) if n.get("name")]
                self.wiring.decay_trails()
                if abs(_outcome) > 0.02 and len(_path_names) >= 2:
                    _amt = _outcome * float(settings.get("colony_trail_gain", 0.05))
                    _n = self.wiring.reinforce_trail(_path_names, _amt)
                    if _n:
                        from brain.observability.decisions import decisions as _decisions

                        _decisions.log(
                            "trail_reinforced",
                            turn_id=turn_id,
                            outcome=round(_outcome, 3),
                            amount=round(_amt, 4),
                            edges=_n,
                            applied=int(settings.get("colony_trail_apply", 0)),
                        )
            except Exception:
                pass

        # flock_dynamics: criticality observable (2) + closed-loop control (3).
        # Measure σ from this turn's firing path, smooth over a rolling window,
        # then nudge the global modulation_gain toward the arousal-modulated
        # setpoint σ*. Arousal is the knob; measured σ is the feedback; the gain
        # is clamped + EMA-smoothed and never steers super-critical. Flag-off:
        # this whole block is skipped and modulation_gain stays static.
        if settings.get("flock_dynamics", 0) and getattr(self, "wiring", None) is not None:
            try:
                from brain.emotion_vocabulary import compute_affect_dims
                from brain.observability.criticality import FlockCriticality

                if getattr(self, "_flock_criticality", None) is None:
                    self._flock_criticality = FlockCriticality()
                _fc = self._flock_criticality
                _fm = _fc.observe(trace.fired_path, self.wiring)
                trace.branching_sigma = _fm["sigma"]
                trace.sigma_smoothed = _fm["sigma_smoothed"]
                trace.avalanche_size = _fm["avalanche"]
                _arousal = float(
                    compute_affect_dims(nm_snap, self.bus.hormonal.snapshot()).get("arousal", 0.3)
                )
                _ctrl = _fc.control(_arousal)
                trace.criticality_setpoint = _ctrl["sigma_star"]
                trace.modulation_gain_applied = _ctrl["gain"]
                from brain.observability.decisions import decisions as _decisions

                _decisions.log(
                    "flock_criticality",
                    turn_id=turn_id,
                    sigma=_fm["sigma"],
                    sigma_smoothed=_fm["sigma_smoothed"],
                    avalanche=_fm["avalanche"],
                    heavy_tail=_fm["heavy_tail"],
                    arousal=round(_arousal, 3),
                    sigma_star=_ctrl["sigma_star"],
                    gain=_ctrl["gain"],
                )
            except Exception:
                pass

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
        memory_hit_count = len(
            [ln for ln in (memory.get("episodes") or "").splitlines() if ln.strip()]
        )

        selected_draft = next((d for d in draft_scores if d.get("selected")), {})
        selected_coherence = selected_draft.get("coherence", 0.5)
        selected_emotional_fit = selected_draft.get("empathy_score") or selected_draft.get(
            "tone_fit", 0.5
        )
        selected_draft_id = selected_draft.get("draft_id", "")

        if self._emitter:
            await self._emitter.emit_neuromod(nm_snap)
            h_snap_final = self.bus.hormonal.snapshot()
            await self._emitter.emit_hormonal(h_snap_final)
            await self._emitter.emit_turn_end(turn_id, final, turn_result.elapsed(), llm_calls)

        if self._emitter and draft_scores:
            try:
                await self._emitter.emit_event(
                    {
                        "type": "quality_score",
                        "turn_id": turn_id,
                        "score": round(selected_draft.get("overall", 0.5), 3),
                        "coherence": round(selected_coherence, 3),
                        "emotional_fit": round(selected_emotional_fit, 3),
                        "drafter_count": len(draft_scores),
                        "memory_used": memory_recalled,
                    }
                )
            except Exception as _qe:
                logger.debug("quality_score emit failed: %s", _qe)

        # Drain deliberate mood expressions published this turn (set_mood tool
        # or [mood:X] inline markup) and attach to the trace for Langfuse.
        mood_inbox = getattr(self, "_mood_expression_inbox", None)
        if mood_inbox is not None:
            import asyncio as _asyncio

            # Give any call_soon-scheduled publishes a chance to land first.
            await _asyncio.sleep(0)
            while True:
                try:
                    mx = mood_inbox.get_nowait()
                    if not mx.expired:
                        trace.deliberate_emotions.append(
                            {
                                "emotion": mx.payload.get("emotion", ""),
                                "source": mx.payload.get("source", ""),
                                **(
                                    {"preview": mx.payload["preview"]}
                                    if mx.payload.get("preview")
                                    else {}
                                ),
                            }
                        )
                except Exception:
                    break

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
        trace.user_emotion = affect.get("user_emotion", "") or (
            features.get("user_emotion", "") if isinstance(features, dict) else ""
        )
        trace.speaker_name = features.get("speaker_name", "")
        trace.speaker_score = features.get("_speaker_match_score", 0.0)
        trace.prosody_tone = affect.get("vocal_tone") or ""
        trace.prosody_f0_hz = affect.get("prosody_f0_hz", 0.0)
        trace.prosody_energy = affect.get("prosody_energy", 0.0)
        trace.prosody_jitter = affect.get("prosody_jitter", 0.0)
        trace.prosody_shimmer = affect.get("prosody_shimmer", 0.0)
        # Modality: SINGLE SOURCE OF TRUTH — use the value derived pre-hypothalamus
        # that actually drove channel calibration (features["input_modality"]),
        # not a second post-hoc derivation. Keeps logged modality == processed
        # modality even on degraded turns (e.g. voice turn with no usable prosody).
        trace.input_modality = (
            features.get("input_modality", "text") if isinstance(features, dict) else "text"
        )
        trace.text_paralinguistics = features.get("text_paralinguistics", {})
        # Relationship instrumentation: did OXT clear the "connected" threshold?
        _hormonal = affect.get("hormonal") or {}
        trace.oxt_connected_reached = bool(
            _hormonal.get("OXT", 0.0) >= settings.get("hormonal_oxt_connected_threshold")
        )
        trace.user_sentiment = (
            float(features.get("sentiment", 0.0)) if isinstance(features, dict) else 0.0
        )
        # Relationship STAGE snapshot — capture affection/tier AT TURN TIME. The
        # schema is overwritten continuously, so this cannot be recovered later;
        # without it no behaviour can be correlated against relationship depth.
        try:
            from brain.metacognition import relationship_stage_from_content

            _user_model = (
                (memory.get("core") or {}).get("user", "") if isinstance(memory, dict) else ""
            )
            _stage = relationship_stage_from_content(_user_model)
            trace.affection = _stage.affection
            trace.affection_label = _stage.affection_label
            trace.familiarity_tier = _stage.tier
            if not trace.bond:  # bond not set by a reunion boost this turn
                trace.bond = round(_stage.bond, 1)
        except Exception:
            pass
        # Hoist the selected draft's empathy score for easy aggregation.
        try:
            _sel = next(
                (d for d in (trace.draft_scores or []) if d.get("selected")),
                None,
            )
            if _sel:
                trace.selected_empathy_score = float(_sel.get("empathy_score", 0.0) or 0.0)
        except Exception:
            pass
        # Reciprocation proxy: resolve the PREVIOUS turn's disclosure one turn late.
        # If the prior turn fired a self-disclosure, did the user's sentiment rise
        # on this (the following) turn? A coarse but logged signal for §2.4 testing.
        if self._session_traces_full:
            _prev = self._session_traces_full[-1]
            if getattr(_prev, "disclosure_fired", False) and _prev.disclosure_reciprocated is None:
                _prev.disclosure_reciprocated = trace.user_sentiment > _prev.user_sentiment
        self.obs.record_turn(trace)
        self._session_traces_full.append(trace)

        if self._follow_through:

            async def _follow_through_check() -> None:
                deferred_goal = self._pending_task.take() if self._pending_task else None
                if deferred_goal:
                    try:
                        extracted, asking_user = await self._follow_through.extract(
                            user_input, final, turn_id
                        )
                    except Exception:
                        extracted, asking_user = None, False
                    if asking_user:
                        # The AI asked the user "Should I…?" — do NOT start the task.
                        # The user hasn't said yes yet. Discard the deferred goal;
                        # it will be re-deposited if the user confirms on the next turn.
                        logger.info(
                            "[FollowThrough] AI asked permission — deferred goal discarded, waiting for user answer"
                        )
                        return
                    if extracted:
                        goal = extracted
                    else:
                        # extract() returned None with no question — the assistant's
                        # ack was too brief to anchor a commitment. Fall back to the
                        # topic_summary-based goal set by FrontalTaskSubsystem.
                        goal = deferred_goal
                        logger.debug(
                            "[FollowThrough] No commitment found — using topic goal: %s", goal[:80]
                        )
                    self._task_queue.enqueue(goal, source="user", priority=1)
                    logger.info("[FollowThrough] Task enqueued (task-mode): %s", goal[:120])
                    return
                try:
                    goal, asking_user = await self._follow_through.extract(
                        user_input, final, turn_id
                    )
                    if goal and not asking_user:
                        self._task_queue.enqueue(goal, source="user", priority=1)
                        logger.info("[FollowThrough] Task enqueued (reactive): %s", goal[:120])
                except Exception as _e:
                    logger.warning("[FollowThrough] failed: %s", _e)

            asyncio.create_task(_follow_through_check())

        if self._emotion_judge:
            self._emotion_judge.fire(trace)
        if getattr(self, "_relationship_judge", None):
            self._relationship_judge.fire(trace)  # no-op unless BRAIN_EVAL_RELATIONSHIP=true
        if self._learning_monitor:
            self._learning_monitor.record_turn(trace)
        if self._baseline_runner:
            memory_ctx = (memory.get("episodes") or "") + "\n" + (memory.get("schema") or "")
            self._baseline_runner.fire(
                turn_id,
                user_input,
                final,
                memory_ctx[:1000],
                selected_coherence,
                selected_emotional_fit,
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

        self._last_speaker_name = features.get("speaker_name") or self._last_speaker_name

        self._session_traces.append(
            {
                "user_input": user_input,
                "entity_response": final,
                "emotion": affect.get("emotion", "neutral"),
                "topic_tags": features.get("entities", []),
                "speaker_name": features.get("speaker_name", ""),
                # Personality-observation signals (rolled up at sleep time).
                "user_emotion": features.get("user_emotion", ""),
                "user_tone_toward_ai": features.get("user_tone_toward_ai", ""),
                "msg_length": features.get("msg_length", ""),
                "intent": features.get("intent", ""),
                "requires_action": bool(features.get("requires_action")),
                "register": features.get("register", ""),
                "user_register": features.get("user_register", ""),
                "prosody_tone": affect.get("vocal_tone") or "",
                "pace_label": affect.get("pace_label") or "",
                "hesitant_speech": bool(affect.get("hesitant_speech")),
                "response_chars": len(final or ""),
            }
        )

        # Backstop on buffer growth: a very long unconsolidated session (or a
        # sleep loop that's wedged) would otherwise accumulate traces without
        # bound. Force a mini-consolidation; consolidate_now is single-flight,
        # so re-triggering while one runs is a no-op.
        _trace_cap = int(settings.get("session_trace_cap", 300))
        if _trace_cap > 0 and len(self._session_traces) >= _trace_cap:
            logger.warning("[Sleep] Trace buffer hit cap (%d) — forcing consolidation", _trace_cap)
            asyncio.create_task(self.consolidate_now("trace_cap"))

        await self._emit("hippocampus", 0.45, "encoding episode", turn_id)
        encode_task = asyncio.create_task(
            self.hippocampus.encode(
                session_id=self.session_id,
                turn_id=turn_id,
                user_input=user_input,
                entity_response=final,
                features=features,
                affect=affect,
                neuromod_snap=nm_snap,
                surprise_score=features.get("surprise_score", 0.5),
                embedding_fn=self.router.embed,
            )
        )
        self._track_encode(encode_task)

        if self.dmn:
            self.dmn.note_last_response(final)
            # Close the loop (B8): a routed thread the response actually engaged is
            # marked resolved-by-use; ignored ones decay their routing weight (B9).
            _routed = getattr(self, "_routed_threads", None)
            if _routed:
                try:
                    await self.dmn.note_threads_used(_routed, final)
                except Exception as _nt_err:
                    logger.debug("[DMN] note_threads_used skipped: %s", _nt_err)
                self._routed_threads = []
            self.dmn.resume()

        self._last_turn_ts = time.time()

        logger.info(
            "Turn %s: %d LLM calls | %.2fs | emotion=%s",
            turn_id,
            llm_calls,
            turn_result.elapsed(),
            affect.get("emotion"),
        )

        with contextlib.suppress(Exception):
            reset_current_trace(_ctx_token)

        return raw_final, affect

    def _task_is_on_topic(self, task_goal: str) -> bool:
        """Return True if the task goal is topically related to the current conversation.

        Uses word overlap against recent parietal turns (topic summaries + user text).
        A low threshold (0.12) catches broad relevance — we only suppress when the
        task is clearly about a different project or subject entirely.
        """
        recent = self.parietal.recent_turns(n=4)
        if not recent:
            return True  # no conversation yet — always surface
        convo_words: set[str] = set()
        for turn in recent:
            for field in ("topic", "user", "intent"):
                text = turn.get(field) or ""
                convo_words.update(text.lower().split())
        task_words = set(task_goal.lower().split())
        if not task_words or not convo_words:
            return True
        # Strip ultra-common stop words so "the", "a", "I" don't inflate overlap
        _STOPS = {
            "the",
            "a",
            "an",
            "i",
            "to",
            "and",
            "or",
            "of",
            "in",
            "it",
            "is",
            "was",
            "for",
            "on",
            "with",
            "that",
            "this",
            "be",
            "are",
        }
        task_words -= _STOPS
        convo_words -= _STOPS
        if not task_words:
            return True
        overlap = len(task_words & convo_words) / len(task_words)
        return overlap >= 0.12

    def _partner_proactive_target(self) -> str:
        """The tenant a self-directed job's result should be delivered to. These jobs
        run on the owner lane (no bind_turn → no end-user on the turn context), so the
        proactive partner-webhook would otherwise drop the result. Fall back to the same
        single-tenant id the work tray uses (AGENT_WORK_DEFAULT_END_USER_ID); empty when
        unset, which keeps delivery owner-only (current behaviour)."""
        return os.environ.get("AGENT_WORK_DEFAULT_END_USER_ID", "").strip()

    async def _run_task(self, task) -> None:
        job_turn_id = f"task_{task.id}"
        job_id = f"job_{job_turn_id}"
        # Record the job_id on the task so the queue entry links to the store
        try:
            task.job_id = job_id
            self._task_queue._save()
        except Exception:
            pass
        is_self = getattr(task, "source", "") == "self"
        # Autonomy policy: anything not directly commanded by the user right now
        # (DMN self-tasks AND recovered jobs from a previous session) runs under
        # the tighter self-directed grants.
        is_autonomous = getattr(task, "source", "") != "user"
        if is_self:
            self.router.enter_background_mode()
        if is_autonomous:
            self.motor.enter_self_mode()
        try:
            summary = await self.motor.execute_internal_job(task.goal, job_turn_id)
        except Exception as _e:
            logger.warning("[TaskWorker] Task [%s] execution failed: %s", task.id, _e)
            self._task_queue.mark_done(task.id, success=False)
            if self.dmn and self.dmn.is_project_task(task.id):
                with contextlib.suppress(Exception):
                    await self.dmn.note_project_complete(task.id, False, "execution error")
            return
        finally:
            if is_autonomous:
                self.motor.exit_self_mode()
            if is_self:
                self.router.exit_background_mode()

        on_topic = self._task_is_on_topic(task.goal)

        if summary.get("clarification"):
            question = summary["clarification"]
            # A degenerate local planner sometimes returns a JSON blob / echoed
            # tool output in place of a real question. Never block on (or speak)
            # that — fail the task quietly so the queue doesn't fill with garbage
            # "clarifications" and nothing junk reaches the copilot.
            from brain.clusters.follow_through import looks_like_json_blob

            if looks_like_json_blob(question):
                logger.info(
                    "[TaskWorker] Task [%s] clarification was a JSON blob — dropping "
                    "(local model degenerated): %r",
                    task.id,
                    question[:120],
                )
                self._task_queue.mark_done(task.id, success=False)
                if self.dmn and self.dmn.is_project_task(task.id):
                    with contextlib.suppress(Exception):
                        await self.dmn.note_project_complete(
                            task.id, False, "planner returned an unusable clarification"
                        )
                return
            self._task_queue.mark_blocked(task.id, reason=question)
            if self.dmn and self.dmn.is_project_task(task.id):
                with contextlib.suppress(Exception):
                    await self.dmn.note_project_blocked(task.id, question)
            logger.info(
                "[TaskWorker] Task [%s] blocked on clarification: %s", task.id, question[:120]
            )
            if on_topic:
                if self._proactive_speech_allowed():
                    if self._emitter:
                        await self._emitter.emit_proactive_speech(
                            question, partner_target=self._partner_proactive_target()
                        )
                    await self.pns.emit(question, {"emotion": "curious"})
                else:
                    logger.debug(
                        "[TaskWorker] Task [%s] clarification held — no connected "
                        "listener (task stays blocked, surfaces in context later)",
                        task.id,
                    )
            else:
                logger.info(
                    "[TaskWorker] Task [%s] clarification held — off-topic (will surface "
                    "in context when relevant)",
                    task.id,
                )
            return

        self._task_queue.mark_done(task.id, success=bool(summary.get("success")))
        # Stage 6: accomplishment / mastery — satisfaction from overcoming difficulty, no
        # prediction needed (cleaning a big mess, solving a hard problem). Effort-overcome ×
        # expectation-gap curve × persona mastery valuation. A hard, successful job feels like a
        # real achievement; a tiny one barely registers.
        with contextlib.suppress(Exception):
            self._emit_accomplishment_reward(summary)
        spoken_summary = await self._result_reporter.report(summary, job_turn_id)
        # Persist the spoken summary and task linkage in the job record
        job_id = summary.get("job_id")
        if job_id and hasattr(self.motor, "job_store"):
            try:
                self.motor.job_store.link_task(job_id, task.id)
                if spoken_summary:
                    self.motor.job_store.update_summary(job_id, spoken_summary)
            except Exception as _je:
                logger.debug("[TaskWorker] job_store update failed: %s", _je)
        if not spoken_summary:
            spoken_summary = (
                "Done — but I don't have a clean summary to share."
                if summary.get("success")
                else "I couldn't finish that — something went wrong."
            )
        # Project check-in: if this was a background project step, update the
        # project's status so the next idle cycle advances to the next one.
        if self.dmn and self.dmn.is_project_task(task.id):
            try:
                await self.dmn.note_project_complete(
                    task.id, bool(summary.get("success")), spoken_summary
                )
            except Exception as _pe:
                logger.debug("[TaskWorker] project check-in failed: %s", _pe)
        # Always store in ring buffer — LLM context gets the result regardless of topic.
        self._recent_task_results.append(
            {
                "goal": task.goal,
                "summary": spoken_summary,
                "success": bool(summary.get("success")),
                "ts": time.time(),
            }
        )
        if len(self._recent_task_results) > 3:
            self._recent_task_results.pop(0)
        logger.info("[TaskWorker] Reporting result [%s]: %s", task.id, spoken_summary[:160])
        if self._emitter:
            await self._emitter.emit_event(
                {
                    "type": "task_summary",
                    "job_id": summary.get("job_id"),
                    "summary": spoken_summary,
                }
            )
            if on_topic:
                await self._emitter.emit_proactive_speech(
                    spoken_summary, partner_target=self._partner_proactive_target()
                )
            else:
                logger.info(
                    "[TaskWorker] Task [%s] result held from speech — off-topic "
                    "(will surface in LLM context on next turn)",
                    task.id,
                )
        if on_topic and self._proactive_speech_allowed():
            await self.pns.emit(
                spoken_summary, {"emotion": "lively" if summary.get("success") else "concerned"}
            )

    async def _synthesize_lobe_result(self, tool_name: str, output: str, turn_id: str) -> str:
        """Synthesize a natural spoken utterance from a lobe tool's raw context output.

        recall_memory and analyze_image return LLM-context-formatted text, not speech.
        This pass turns the full retrieved content into 1-2 conversational sentences that
        reference the memory/observation naturally in the context of the current exchange.
        """
        parietal_ctx = self.parietal.recent_turns_text(n=3)
        if tool_name == "recall_memory":
            directive = (
                "A memory just surfaced. Speak it naturally in 1–2 sentences — "
                "as if you just thought of it, not as a readout. Connect it to what's "
                "being discussed. Don't say 'I recall' or 'I found'. Just say the thing."
            )
        else:
            directive = (
                "You just looked at something. Describe what's relevant in 1–2 sentences, "
                "connecting it to the current conversation."
            )
        prompt = (
            f"{directive}\n\nCurrent conversation:\n{parietal_ctx}\n\nRetrieved content:\n{output}"
        )
        try:
            result = await self.router.call(
                "haiku",
                "You are speaking your own thought aloud — a memory or observation that "
                "surfaced naturally while talking. First person, conversational, brief.",
                [{"role": "user", "content": prompt}],
                cluster="hippocampus",
                cell="recall_synthesizer",
                turn_id=turn_id + "_rs",
                max_tokens=200,
            )
            return (result or "").strip()
        except Exception as e:
            logger.warning("[MotorCortex] Lobe synthesis failed (%s): %s", tool_name, e)
            return ""

    async def _synthesize_tool_result(
        self, goal: str, tool_name: str, output: str, turn_id: str
    ) -> str:
        """Synthesize raw tool/action output into a natural spoken update.

        Raw command output (file listings, JSON, logs) must never be spoken directly.
        This turns it into a brief first-person conversational update about what was found.
        """
        parietal_ctx = self.parietal.recent_turns_text(n=3)
        prompt = (
            "You just finished a background task. Summarize what you found or did in "
            "1–2 conversational sentences. Be concrete but don't recite raw output — "
            "translate it into plain speech. If the result is uninteresting or just "
            "a list of internal files/paths with no meaningful content, say nothing "
            "(respond with exactly: SILENT).\n\n"
            f"Original goal: {goal}\n\n"
            f"Tool used: {tool_name}\n\n"
            f"Raw output:\n{output[:800]}\n\n"
            f"Recent conversation:\n{parietal_ctx}"
        )
        try:
            result = await self.router.call(
                "haiku",
                "You are giving a brief spoken update about something you just did. "
                "First person, conversational, never read out raw paths or code.",
                [{"role": "user", "content": prompt}],
                cluster="motor_cortex",
                cell="result_synthesizer",
                turn_id=turn_id + "_ts",
                max_tokens=150,
            )
            text = (result or "").strip()
            if text.upper() == "SILENT" or not text:
                return ""
            return text
        except Exception as e:
            logger.warning("[MotorCortex] Tool result synthesis failed (%s): %s", tool_name, e)
            return ""

    async def _run_motor_reactive(self, features: dict, turn_id: str) -> None:
        """Run a reactive motor action in the background.

        The main turn already fired with a [task_queued] acknowledgment. This
        runs the planner + tool dispatch, then surfaces the result via proactive
        speech. Uses its own turn-id so a concurrent turn can't corrupt state.
        """
        bg_turn_id = f"bg_{turn_id}"
        self.motor.reset_turn(bg_turn_id)
        _timeout = float(os.environ.get("BRAIN_MOTOR_INTERACTIVE_TIMEOUT_S", "30"))
        goal = features.get("raw_text") or features.get("topic_summary", "")
        tool_result = None
        try:
            tool_result = await asyncio.wait_for(
                self.motor.execute(features, bg_turn_id),
                timeout=_timeout,
            )
        except Exception as _e:
            logger.error("[MotorCortex] Background reactive failed: %s", _e)
            self.bus.neuromod.add("GABA", 0.10)
            self.bus.neuromod.add("NE", 0.08)
            with contextlib.suppress(Exception):
                if self._emitter:
                    await self._emitter.emit_neuromod(self.bus.neuromod.snapshot())
            return

        if not tool_result:
            return

        output = tool_result.get("output", "")
        tool_name = tool_result.get("tool", "tool")
        success = tool_result.get("success")

        # Neuromod update (deferred from main turn into background completion). Completing a
        # task is an intrinsic correctness signal — reward/penalty scale by how much this
        # persona values being right, and failure also drains 5HT (the sting that lingers).
        from brain.neuron import loss_aversion as _loss_aversion
        from brain.neuron import reward_weight as _reward_weight

        _tpersona = str(settings.get("persona_name", ""))
        _tw = _reward_weight(_tpersona, "correctness")
        _ter = float(settings.get("emotional_reactivity_scale"))
        if success is False:
            # A failed task is a LOSS — scale the dip/drain by loss aversion (λ), one-sided
            # (the success branch below stays unweighted), independent of correctness valuation.
            _tla = _loss_aversion(_tpersona)
            self.bus.neuromod.add("GABA", 0.08)
            self.bus.neuromod.add("NE", 0.06)
            self.bus.neuromod.add(
                "DA", -float(settings.get("correctness_penalty_base")) * _tw * _ter * _tla
            )
            self.bus.hormonal.add(
                "5HT", -float(settings.get("correctness_5ht_drain")) * _tw * _ter * _tla
            )
        elif success:
            self.bus.neuromod.add("DA", float(settings.get("correctness_reward_base")) * _tw * _ter)
            self.bus.neuromod.add("Glu", 0.04)
        with contextlib.suppress(Exception):
            if self._emitter:
                await self._emitter.emit_neuromod(self.bus.neuromod.snapshot())

        # Lobe tools (recall_memory, analyze_image) return LLM-context strings, not
        # human-readable output. Raw recall text must be synthesized into natural
        # speech before surfacing — never dump it directly or store it as a task result.
        _LOBE_TOOLS = {"recall_memory", "analyze_image"}
        is_lobe_tool = tool_name in _LOBE_TOOLS

        if tool_result.get("pending"):
            # Confirmation needed — surface as a question via proactive speech
            desc = output.replace("CONFIRMATION_NEEDED:", "").strip()
            msg = f"I'm ready to: {desc}. Want me to go ahead?"
        elif is_lobe_tool:
            if not output or output.startswith("[error]") or output.startswith("[no"):
                return
            msg = await self._synthesize_lobe_result(tool_name, output, turn_id)
            if not msg:
                return
        else:
            self._recent_task_results.append(
                {"goal": goal, "summary": output[:500], "success": bool(success), "ts": time.time()}
            )
            if len(self._recent_task_results) > 3:
                self._recent_task_results.pop(0)
            msg = await self._synthesize_tool_result(goal, tool_name, output, turn_id)

        if not msg:
            return

        logger.info(
            "[MotorCortex] Background reactive done: %s → %d chars (success=%s)",
            tool_name,
            len(msg),
            success,
        )
        if self._proactive_speech_allowed():
            with contextlib.suppress(Exception):
                if self._emitter:
                    await self._emitter.emit_proactive_speech(msg)
            with contextlib.suppress(Exception):
                await self.pns.emit(msg, {"emotion": "lively" if success else "concerned"})


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
