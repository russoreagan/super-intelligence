"""Callback and background loop methods for BrainSession — imported as _LoopsMixin."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from brain.settings import settings as _brain_settings
from brain.utils import get_idle_seconds

logger = logging.getLogger("brain.run")


class _LoopsMixin:
    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_speaking_change(self, active: bool) -> None:
        from brain.voice_bridge import barge_in_mode

        if self._emitter:
            asyncio.ensure_future(self._emitter.emit_event({"type": "speaking", "active": active}))
        # voice/keyword modes run the mic full-duplex through TTS so the user
        # can interrupt; only "off" (half-duplex) mutes + pauses capture here.
        if barge_in_mode() == "off" and self._streaming_mic is not None:
            if active:
                if not self._streaming_mic.is_muted:
                    self._streaming_mic.mute()
                    self._tts_did_mute = True
                    if self._emitter:
                        asyncio.ensure_future(
                            self._emitter.emit_event(
                                {
                                    "type": "mic_state",
                                    "status": "muted",
                                }
                            )
                        )
                else:
                    self._tts_did_mute = False
                # Pause the physical input stream so the TTS output stream doesn't
                # collide with it on a shared audio device (Scarlett full-duplex →
                # CoreAudio err -10863, which silently kills mic capture for the
                # rest of the session). We're muted during TTS anyway.
                self._streaming_mic.pause_capture()
            else:
                # Always run the restore on speaking-end: capture was paused above
                # regardless of who muted, so it must always be resumed.
                asyncio.ensure_future(self._restore_mic_after_tts())

    async def _restore_mic_after_tts(self) -> None:
        """After the entity finishes speaking, re-open the (paused) input stream
        and restore the mic to whatever the user wants — live only if push-to-talk
        is still held, otherwise muted. (Hold-to-talk: we must NOT auto-reopen the
        mic just because TTS ended — that was the old always-on behaviour.)"""
        mic = self._streaming_mic
        if mic is None:
            return
        # Re-open the physical stream first (paused during TTS), then settle state.
        mic.resume_capture()
        await asyncio.sleep(self._mic_unmute_delay_s)
        if self._ptt_held:
            # Space is still held — re-open WITH push-to-talk semantics so Deepgram's
            # UtteranceEnd stays suppressed across mid-sentence pauses. Without
            # ptt_hold=True here, a held phrase spoken right after the entity finishes
            # talking gets finalized at the first pause and only its first half is
            # captured (the rest is lost when the brain responds and re-mutes).
            mic.unmute(ptt_hold=True)
        else:
            mic.mute()
        self._tts_did_mute = False
        self._emit_mic_state()

    def _mic_status(self) -> str:
        """Single source of truth for the mic status string the UI consumes:
        'off' | 'muted' | 'active'.

        'off'  → no server-side mic (browser should capture audio itself via
                 MediaRecorder). This is the hosted case: no audio input device,
                 so StreamingMicSession.start() failed and _streaming_mic is None.
        'muted'/'active' → a server-side mic exists; reflect its live state.

        While the mic is still starting up (voice requested but setup not yet
        complete) report 'muted' so the browser doesn't transiently self-capture
        before the server mic comes online.
        """
        mic = self._streaming_mic
        if mic is not None:
            return "muted" if mic.is_muted else "active"
        if self._voice_requested and not self._mic_setup_done:
            return "muted"
        return "off"

    def _emit_mic_state(self) -> None:
        """Broadcast the settled mic status so the button reflects reality."""
        if not self._emitter or self._streaming_mic is None:
            return
        status = "muted" if self._streaming_mic.is_muted else "active"
        asyncio.ensure_future(self._emitter.emit_event({"type": "mic_state", "status": status}))

    async def _on_browser_message(self, text: str) -> None:
        await self._ui_message_queue.put(text)

    def _on_live_speech(self, text: str) -> None:
        """Interim/final speech transcribed while the entity is speaking
        (fired from the streaming-mic read loop, mic live in full-duplex).
        In voice mode, cut TTS as soon as the speech passes the barge policy
        (keywords instantly; other speech needs ≥2 real words and a low
        overlap with the TTS text so open-speaker echo doesn't self-cancel).
        The user's sentence keeps accumulating and dispatches normally on
        UtteranceEnd — interrupting here only stops the playback."""
        from brain.voice_bridge import barge_in_mode, should_voice_interrupt

        if barge_in_mode() != "voice" or not self.pns.is_speaking:
            return
        barge_words = getattr(self, "_barge_in_words", None) or []
        if should_voice_interrupt(text, self.pns.speaking_text, barge_words=barge_words):
            logger.info("[I/O] voice barge-in — cutting TTS: %r", text[:60])
            self.pns.interrupt()

    def _on_eval_mode(self, intensive: bool) -> None:
        if self._baseline_runner:
            self._baseline_runner.set_intensive(intensive)

    def _set_mic_listening(self, want_live: bool) -> None:
        """Single source of truth for whether the user wants the mic live
        (push-to-talk held, or toggled on via the button). In half-duplex
        ("off" barge-in mode) never opens the mic while the entity is
        speaking — the post-TTS restore applies `want_live` then. In
        voice/keyword modes the mic may open mid-TTS (that's the point:
        speaking over the entity should work). On release, flush the held
        phrase and re-mute."""
        from brain.voice_bridge import barge_in_mode

        self._ptt_held = want_live
        mic = self._streaming_mic
        if mic is None:
            return
        if want_live:
            # Fresh hold — drop any chunks left over from a hold that never got a
            # clean release (e.g. window blur), so they don't prepend this phrase.
            self._ptt_chunks.clear()
            if barge_in_mode() != "off" or not self.pns.is_speaking:
                mic.unmute(ptt_hold=True)
            self._emit_mic_state()
        else:
            asyncio.ensure_future(self._release_mic())

    async def _release_mic(self) -> None:
        mic = self._streaming_mic
        if mic is None:
            return
        await mic.flush()  # finalize the held utterance, then mute
        self._emit_mic_state()

    def _on_mic_ptt(self, down: bool) -> None:
        """Push-to-talk hold: Space keydown -> live, keyup -> flush + mute."""
        self._set_mic_listening(down)

    def _on_mic_toggle(self) -> bool:
        """Mic button / fallback: toggle the desired-live state."""
        self._set_mic_listening(not self._ptt_held)
        return self._is_mic_muted()

    def _is_mic_muted(self) -> bool:
        if self._streaming_mic is not None:
            return self._streaming_mic.is_muted
        # streaming_mic not yet initialised (or voice mode off) — report as muted
        # so the UI button shows the safe default rather than "active".
        # The UI is set up before the mic (see brain_session.py run()), so a browser
        # that connects during the startup window would otherwise see 'active'.
        return True

    async def _emit(self, cluster: str, intensity: float, note: str, turn_id: str = "") -> None:
        if self._emitter:
            await self._emitter.emit(cluster, intensity, note, turn_id)
        if turn_id:
            self.obs.begin_cluster(turn_id, cluster, note)

    async def _emit_end(self, cluster: str, turn_id: str = "") -> None:
        if self._emitter:
            await self._emitter.emit(cluster, 0.0, "done", turn_id)
        if turn_id:
            self.obs.end_cluster(turn_id, cluster)

    async def _recall_memory(self, *, topic: str, entities: list, turn_id: str) -> str:
        result = await self.hippocampus.recall(topic, entities, turn_id, self.router.embed)
        parts: list[str] = []
        if result.get("episodes"):
            parts.append(f"Relevant episodes:\n{result['episodes']}")
        if result.get("schema"):
            parts.append(f"Known facts:\n{result['schema']}")
        return "\n\n".join(parts) or "(no relevant memories found)"

    async def _analyze_image(self, *, path: str, question: str, turn_id: str) -> str:
        result = await self.occipital.process(path, question, turn_id)
        if result is None:
            return "[error] Could not analyze image (occipital returned nothing)"
        parts: list[str] = []
        for key in ("description", "caption", "objects", "text_content", "scene"):
            val = result.get(key)
            if val:
                parts.append(f"{key}: {val}")
        return "\n".join(parts) or str(result)

    def _track_encode(self, task: asyncio.Task) -> None:
        self._pending_encodes.add(task)

        def _done(t: asyncio.Task) -> None:
            self._pending_encodes.discard(t)
            exc = t.exception() if not t.cancelled() else None
            if exc:
                # Count it so the loss is visible beyond a log line — a session
                # silently dropping episodes looks healthy from the outside.
                self._encode_failures = getattr(self, "_encode_failures", 0) + 1
                self._last_encode_error = str(exc)
                logger.error(
                    "Memory write failed for this turn — episode will not be saved to "
                    "long-term memory (%d failure(s) this session): %s",
                    self._encode_failures,
                    exc,
                )
                if self._emitter:
                    with contextlib.suppress(Exception):
                        asyncio.ensure_future(
                            self._emitter.emit(
                                "hippocampus",
                                0.0,
                                f"memory write failed ({self._encode_failures}x)",
                                "error",
                            )
                        )

        task.add_done_callback(_done)

    async def _dispatch_text(self, text: str) -> None:
        logger.info("[I/O] voice → turn: %r", text[:80])
        if self._emitter:
            with contextlib.suppress(Exception):
                await self._emitter.emit_event({"type": "transcript", "text": text, "final": True})
        await self._ui_message_queue.put(text)

    async def _dmn_tick_with_ui(self) -> None:
        if self._emitter:
            await self._emitter.emit("dmn", 0.25, "thinking...", "dmn")
            await self._emitter.emit("hippocampus", 0.15, "consolidating...", "dmn")
        await self._dmn_orig_tick()
        if self._emitter:
            await self._emitter.emit("dmn", 0.0, "done", "dmn")
            await self._emitter.emit("hippocampus", 0.0, "done", "dmn")

    # ── Background loop methods ───────────────────────────────────────────────

    async def _forward_thoughts(self) -> None:
        while True:
            msg = await self._thought_inbox.get()
            thought = msg.payload.get("thought", "") if not msg.expired else ""
            chem_delta = msg.payload.get("chem_delta", {}) if not msg.expired else {}
            proactive = bool(msg.payload.get("proactive", False)) if not msg.expired else False
            ts = msg.payload.get("ts") if not msg.expired else None
            # Ranking hints for the inner-thought panel (None when absent → emitter omits).
            salience = msg.payload.get("salience") if not msg.expired else None
            urgency = msg.payload.get("urgency") if not msg.expired else None
            from_job = msg.payload.get("from_job") if not msg.expired else None
            if thought:
                await self._emitter.emit_stream_thought(
                    thought,
                    chem_delta=chem_delta,
                    proactive=proactive,
                    ts=ts,
                    salience=salience,
                    urgency=urgency,
                    from_job=from_job,
                )

    async def _heartbeat_with_ui(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.brainstem.heartbeat_once(emitter=self._emitter)

    async def _runpod_heartbeat_loop(self) -> None:
        import json as _json
        import time as _time

        _path = os.path.join(
            os.path.dirname(__file__), "..", "second_brain", "runpod_heartbeat.json"
        )
        _path = os.path.realpath(_path)
        while True:
            await asyncio.sleep(300)  # write every 5 minutes
            try:
                with open(_path, "w") as _f:
                    _json.dump({"ts": _time.time()}, _f)
            except Exception:
                pass

    async def _usage_flush_loop(self) -> None:
        """Persist per-agent model usage (tokens, pod compute-seconds, cloud $) to the
        durable ledger every couple minutes so the Agents dashboard can sum cost +
        tokens over a date range — cumulative across every restart (migration 016)."""
        while True:
            await asyncio.sleep(120)
            try:
                if getattr(self, "router", None) is not None:
                    await asyncio.to_thread(self.router.flush_usage)
            except Exception:
                pass

    def _agent_usage_for_ui(
        self, since: str | None = None, until: str | None = None, scope: str = "org"
    ) -> dict:
        """Per-agent usage for the dashboard. Returns a discriminated payload:
          scope 'org'  → {"scope":"org", "usage": {agent_id: {...}}}
          scope 'all'  → {"scope":"all", "rows": [{org_id, org_name, agent_id, ...}]}
        Org scope with no range = the live in-memory meter (current session); with a
        range = this org's durable ledger. All scope = every org's ledger (the
        platform super-admin fleet view) — the caller must already have gated this to
        is_admin. Sync + blocking on the ledger paths; the UI server calls it
        off-thread."""
        try:
            from brain import agent_usage_store

            if scope == "all":
                return {"scope": "all", "rows": agent_usage_store.aggregate_all(since, until)}
            if since or until:
                return {"scope": "org", "usage": agent_usage_store.aggregate(since, until)}
        except Exception:
            return {"scope": scope, "usage": {}, "rows": []}
        _r = getattr(self, "router", None)
        try:
            _da = self.bus.da_source_tally()
        except Exception:
            _da = {"intrinsic": 0.0, "external": 0.0}
        return {
            "scope": "org",
            "usage": _r.agent_usage() if _r else {},
            # >0 = this process failed to meter some out-of-band cloud spend (CMA
            # usage reads errored) — the dashboard should distrust a clean tally.
            "unmetered_spend_suspected": getattr(_r, "unmetered_spend_suspected", 0) if _r else 0,
            # DA provenance this session: how much of the reward signal was
            # self-administered vs grounded in the user/world. intrinsic ≫ external
            # = the brain is mostly grading itself (premise-audit watch metric).
            "da_reward_sources": {
                "intrinsic": round(_da["intrinsic"], 4),
                "external": round(_da["external"], 4),
            },
        }

    async def _speak_gate_loop(self) -> None:
        SPEAK_GATE_INTERVAL = float(_brain_settings.get("speak_gate_poll_interval") or 5.0)
        SPEAK_CAND_MAX_AGE = float(_brain_settings.get("speak_candidate_max_age_s") or 60.0)
        SPEAK_CAND_MAX_ATTEMPTS = int(_brain_settings.get("speak_candidate_max_attempts") or 4)
        while True:
            try:
                await asyncio.sleep(SPEAK_GATE_INTERVAL)
                if self.dmn.candidate_count() == 0:
                    continue
                # Engine fan-out: a persona serving ≥2 distinct customers must not
                # voice unprompted thoughts into any one customer's channel. The
                # DMN keeps generating candidates (inner life), but we don't speak
                # them; they age out naturally. No-op in companion mode.
                _reg = getattr(self, "_client_chem", None)
                if _reg is not None and _reg.is_fanned_out():
                    continue
                now = time.time()
                since_last_spoke = now - self._last_brain_spoke_ts
                idle_s = get_idle_seconds()
                user_active = (
                    self._proactive_idle_threshold <= 0 or idle_s < self._proactive_idle_threshold
                )
                if not user_active:
                    while self.dmn.candidate_count() > 0:
                        c = self.dmn.take_oldest_candidate()
                        if c is None:
                            break
                        age = now - float(c.get("created_ts", now))
                        if age <= SPEAK_CAND_MAX_AGE:
                            self.dmn.return_candidate(c)
                            break
                    continue
                if self.pns.is_speaking or not self._ui_message_queue.empty():
                    continue
                if self._streaming_mic is not None and getattr(
                    self._streaming_mic, "is_user_speaking", False
                ):
                    continue
                if since_last_spoke < self._proactive_response_window:
                    continue
                while self.dmn.candidate_count() > 0:
                    c = self.dmn.take_oldest_candidate()
                    if c is None:
                        break
                    age = now - float(c.get("created_ts", now))
                    if age > SPEAK_CAND_MAX_AGE:
                        logger.info(
                            "[Speak gate] Dropping aged candidate (age=%.0fs > %.0fs): %r",
                            age,
                            SPEAK_CAND_MAX_AGE,
                            (c.get("spoken") or "")[:60],
                        )
                        continue
                    verdict, reason = await self.dmn.judge_candidate(c)
                    logger.info(
                        "[Speak gate] verdict=%s reason=%s candidate=%r",
                        verdict,
                        reason,
                        (c.get("spoken") or "")[:60],
                    )
                    if verdict == "yes":
                        try:
                            bridged = await self.dmn.bridge_if_needed(c)
                            if bridged and bridged != c.get("spoken"):
                                c["spoken"] = bridged
                        except Exception as _bridge_err:
                            logger.debug("[Speak gate] Bridge step failed: %s", _bridge_err)
                        self.dmn.commit_candidate_to_speech(c)
                    elif verdict == "wait":
                        # Belt-and-suspenders: drop a perpetually-deferred
                        # candidate so a stuck/erroring judge can't re-queue it
                        # forever. (Age-drop above is the primary guard.)
                        if int(c.get("attempts", 0)) >= SPEAK_CAND_MAX_ATTEMPTS:
                            logger.info(
                                "[Speak gate] Dropping candidate after %d attempts: %r",
                                int(c.get("attempts", 0)),
                                (c.get("spoken") or "")[:60],
                            )
                        else:
                            self.dmn.return_candidate(c)
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                raise

    async def _drain_pending_when_tts_ends(self) -> None:
        was_speaking = False
        while True:
            try:
                await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                return
            now_speaking = self.pns.is_speaking
            if was_speaking and not now_speaking:
                async with self._pending_lock:
                    if self._streaming_mic.is_muted:
                        if self._pending_during_tts:
                            logger.debug(
                                "[I/O] voice → discarded %d queued utterance(s) (mic muted)",
                                len(self._pending_during_tts),
                            )
                        self._pending_during_tts.clear()
                    else:
                        from brain.voice_bridge import pick_dispatch_from_queue

                        text, n = pick_dispatch_from_queue(self._pending_during_tts)
                        self._pending_during_tts.clear()
                        if text:
                            logger.info(
                                "[I/O] voice → flushing %d queued utterance(s): %r", n, text[:80]
                            )
                            await self._dispatch_text(text)
            was_speaking = now_speaking

    async def _voice_bridge(self) -> None:
        from brain.voice_bridge import classify_utterance

        while True:
            try:
                utt = await self._streaming_mic.next_utterance()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[I/O] voice bridge read failed: %s", e)
                await asyncio.sleep(0.5)
                continue

            # ── Push-to-talk incremental capture ──────────────────────────────
            # While Space is held, the mic dispatches each pause-delimited segment
            # as a chunk. Buffer them and withhold the response until the terminal
            # marker (Space release) so the brain reads the WHOLE held phrase as one
            # turn. Handled before the muted-discard check so release-time muting
            # can't drop the terminal.
            if utt.get("ptt_chunk"):
                seg = (utt.get("transcript") or "").strip()
                if seg:
                    self._ptt_chunks.append(seg)
                    logger.debug("[I/O] voice → PTT chunk buffered: %r", seg[:60])
                continue
            if utt.get("ptt_terminal"):
                n_chunks = len(self._ptt_chunks)
                combined = " ".join(self._ptt_chunks).strip()
                self._ptt_chunks.clear()
                if combined:
                    logger.info(
                        "[I/O] voice → PTT phrase complete (%d chunk(s)): %r",
                        n_chunks,
                        combined[:80],
                    )
                    await self._dispatch_text(combined)
                continue

            if self._streaming_mic.is_muted and not utt.get("from_ptt_flush"):
                # Discard utterances that arrived while muted (e.g. TTS bleed-through
                # in always-on mode). But PTT flush utterances are intentional — the
                # mic is muted *after* the utterance is queued, so don't discard them.
                logger.debug("[I/O] voice → discarded stale utterance (mic muted)")
                continue
            text = (utt.get("transcript") or "").strip()
            # Full-duplex echo guard: with the mic live through TTS (voice /
            # keyword modes on open speakers), the mic can transcribe the
            # entity's own playback. Drop utterances that are mostly the words
            # the entity was just saying.
            if text and self.pns.is_speaking:
                from brain.voice_bridge import echo_containment, echo_containment_max

                overlap = echo_containment(text, self.pns.speaking_text)
                if overlap >= echo_containment_max():
                    logger.debug(
                        "[I/O] voice → dropped TTS echo (containment %.2f): %r", overlap, text[:60]
                    )
                    continue
            decision, _ = classify_utterance(
                text,
                brain_is_speaking=self.pns.is_speaking,
                barge_words=self._barge_in_words,
            )
            if decision == "drop_empty":
                continue
            if decision == "barge_in":
                self.pns.interrupt()
                await self._dispatch_text(text)
                continue
            if decision == "queue":
                async with self._pending_lock:
                    self._pending_during_tts.append(text)
                logger.info("[I/O] voice → queued during TTS: %r", text[:60])
                continue
            await self._dispatch_text(text)

    # ── Periodic in-process sleep consolidation ──────────────────────────────

    def _consolidate_resting_mood(self) -> None:
        """Blend the cycle's weighted-average client mood into the persona resting
        mood and persist it (persona_chem `current`). Engine-mode only — a no-op
        unless this persona is serving ≥2 distinct customers."""
        reg = getattr(self, "_client_chem", None)
        if reg is None or not reg.is_fanned_out():
            return
        alpha = float(_brain_settings.get("resting_mood_consolidation_alpha") or 0.3)
        snap = reg.consolidate_into_resting(alpha)
        if snap and self.persona_name:
            from brain import persona_chem

            persona_chem.save_current(self.persona_name, snap["neuromod"], snap["hormonal"])

    async def consolidate_now(self, reason: str = "manual") -> dict:
        """Force a consolidation pass on the buffered traces. Safe to call
        anytime; single-flight protected. Returns a small status dict."""
        if self._sleep is None or self._consolidation_lock is None:
            return {"ran": False, "reason": "sleep_loop_disabled"}
        if self._consolidation_lock.locked():
            return {"ran": False, "reason": "already_running"}
        if not self._session_traces:
            return {"ran": False, "reason": "no_buffered_turns"}
        from brain.second_brain.store import active_persona, bind_persona

        async with self._consolidation_lock:
            # Persona-bind the consolidation so the Hebbian/wiring/memory writes land on the right
            # persona: the /consolidate route sets the SESSION persona; a trace-cap or UI/CLI call
            # has none bound → fall back to this brain's home persona (never a stray default).
            with bind_persona(active_persona() or getattr(self, "persona_name", "")):
                return await self._run_consolidation(reason)

    async def _run_consolidation(self, reason: str) -> dict:
        """Body of a consolidation pass. Snapshots & clears the trace buffers
        BEFORE running the LLM work so new turns during consolidation start a
        fresh batch. DMN is paused during the pass so it doesn't compete for
        the LLM. Caller is responsible for holding _consolidation_lock."""
        traces = list(self._session_traces)
        traces_full = list(self._session_traces_full)
        self._session_traces.clear()
        self._session_traces_full.clear()
        # Move the durable trace journal aside in lockstep with the in-memory
        # snapshot: a crash from here until the pass succeeds leaves this batch in
        # the inflight file for boot replay instead of losing it.
        with contextlib.suppress(Exception):
            from brain.observability import trace_journal

            trace_journal.rotate_inflight()
        dmn_thoughts = []
        if self.dmn:
            try:
                # Consolidation is AI-internal housekeeping, NOT user engagement — skip the
                # next tick but do NOT stamp the idle clock, or rumination never gets to fire.
                self.dmn.pause(stamp_activity=False)
                dmn_thoughts = self.dmn.session_thoughts() or []
                self.dmn._session_thought_buf.clear()
            except Exception:
                dmn_thoughts = []
        n_turns = len(traces)
        logger.info("[Sleep] In-process consolidation starting (%s, %d turns)", reason, n_turns)
        start = time.time()
        ok = True
        try:
            await self._sleep.consolidate(
                self.session_id,
                traces,
                full_traces=traces_full,
                session_thoughts=dmn_thoughts,
            )
            # Committed — drop the crash-safety copy immediately so the window in
            # which a crash could replay an already-consolidated batch stays
            # near-zero. (Anything raised above skips this and keeps the inflight
            # file, so the batch is retried on the next boot instead of lost.)
            with contextlib.suppress(Exception):
                from brain.observability import trace_journal

                trace_journal.clear_inflight()
            # Persist the user's learned style register so the next session
            # resumes warm rather than cold-starting (F3).
            try:
                _primary = self.hippocampus._schema.primary_user_name()
                await self.parietal.save_style_to_schema(self.hippocampus._schema, _primary or "")
            except Exception:
                pass
            # Refresh DMN's project context from the (possibly rewritten)
            # open_questions.md so any sleep-time edits land immediately.
            if self.dmn:
                try:
                    _oq = self.hippocampus._schema.read("open_questions.md")
                    if _oq:
                        self.dmn.set_projects_context(_oq)
                except Exception:
                    pass
        except Exception as exc:
            ok = False
            logger.warning("[Sleep] In-process consolidation failed (%s): %s", reason, exc)
        finally:
            if self.dmn:
                with contextlib.suppress(Exception):
                    self.dmn.resume()
            self._last_consolidation_ts = time.time()
        # Engine fan-out: fold the cycle's interaction-mass-weighted average client
        # mood into the persona resting mood (one-way valve — never seeds a client)
        # and persist it as the persona's current chemistry. No-op in companion mode.
        with contextlib.suppress(Exception):
            self._consolidate_resting_mood()
        elapsed = time.time() - start
        logger.info(
            "[Sleep] In-process consolidation done in %.1fs (ok=%s, %d turns)", elapsed, ok, n_turns
        )
        return {
            "ran": True,
            "ok": ok,
            "turns": n_turns,
            "elapsed_s": round(elapsed, 1),
            "reason": reason,
        }

    async def _periodic_sleep_loop(self) -> None:
        """Check periodically whether to fire an in-process consolidation pass.
        Fires when either the user has been idle long enough OR enough wall-clock
        has elapsed since the last pass, and a minimum number of turns have
        accumulated in the trace buffer.

        Reads from /settings (sleep_check_interval_s, sleep_idle_threshold_s,
        sleep_hard_cap_s, sleep_min_turns), with env vars taking precedence
        for one-off CLI overrides:
          BRAIN_SLEEP_CHECK_S, BRAIN_SLEEP_IDLE_S, BRAIN_SLEEP_HARD_S,
          BRAIN_SLEEP_MIN_TURNS.

        Settings are re-read each check, so changes saved in the UI take effect
        on the NEXT check tick (no restart needed for cadence tuning).
        """

        def _resolved() -> tuple[float, float, float, int]:
            check_s = float(
                os.environ.get("BRAIN_SLEEP_CHECK_S", _brain_settings.get("sleep_check_interval_s"))
            )
            idle_s = float(
                os.environ.get("BRAIN_SLEEP_IDLE_S", _brain_settings.get("sleep_idle_threshold_s"))
            )
            hard_s = float(
                os.environ.get("BRAIN_SLEEP_HARD_S", _brain_settings.get("sleep_hard_cap_s"))
            )
            min_turns = int(
                os.environ.get("BRAIN_SLEEP_MIN_TURNS", _brain_settings.get("sleep_min_turns"))
            )
            return check_s, idle_s, hard_s, min_turns

        # Stagger first check so it doesn't fire immediately at boot.
        initial_check, _, _, _ = _resolved()
        await asyncio.sleep(min(initial_check, 60.0))
        while True:
            try:
                check_s, idle_s, hard_s, min_turns = _resolved()
                await asyncio.sleep(check_s)
                if self._sleep is None or self._consolidation_lock is None:
                    return
                if self._consolidation_lock.locked():
                    continue
                buffered = len(self._session_traces)
                if buffered < min_turns:
                    continue
                now = time.time()
                idle_for = now - self._last_turn_ts
                since_last = now - self._last_consolidation_ts
                if idle_for >= idle_s:
                    reason = f"idle_{int(idle_for)}s"
                elif since_last >= hard_s:
                    reason = f"hard_cap_{int(since_last)}s"
                else:
                    continue
                # Don't fire while the brain is actively speaking — wait for
                # the next tick. Avoids competing for the LLM and audio path.
                if getattr(self.pns, "is_speaking", False):
                    continue
                # Same persona binding as consolidate_now: the loop runs in a bare
                # background task (nothing bound), so memory/self-model writes must
                # be pinned to the home persona explicitly, never a stray default.
                # (Hebbian attribution itself is per-trace inside the pass.)
                from brain.second_brain.store import active_persona, bind_persona

                async with self._consolidation_lock:
                    with bind_persona(active_persona() or getattr(self, "persona_name", "")):
                        await self._run_consolidation(reason)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("[Sleep] periodic loop error: %s", exc, exc_info=True)

    async def _task_worker_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(3.0)
                if not self._task_queue.has_pending():
                    if self.dmn and not self._self_work_saturated():
                        self_task = self.dmn.take_self_task()
                        if self_task:
                            self._task_queue.enqueue(
                                self_task["goal"],
                                source="self",
                                priority=2,
                                reflex_depth=int(self_task.get("reflex_depth", 0)),
                                origin_persona=str(self_task.get("persona", "")),
                            )
                        else:
                            # Clock-in: no ad-hoc self-task → start the next project
                            # step so a project is always making background progress
                            # while rumination runs in parallel. One at a time.
                            proj = self.dmn.next_project_goal()
                            if proj:
                                name, goal = proj
                                t = self._task_queue.enqueue(goal, source="self", priority=2)
                                if t:
                                    self.dmn.note_project_started(name, t.id)
                    continue
                if self.pns.is_speaking or not self._ui_message_queue.empty():
                    continue
                since_spoke = time.time() - self._last_brain_spoke_ts
                if since_spoke < self._proactive_response_window:
                    continue
                task = self._task_queue.take_next()
                if task:
                    source_label = {
                        "recovery": "📋 resuming",
                        "self": "💭 self-initiated",
                        "user": "▶ executing",
                    }.get(task.source, "▶")
                    logger.info(
                        "[TaskWorker] %s task [%s]: %s", source_label, task.id, task.goal[:80]
                    )
                    # Run as a child task so the UI kill switch can cancel the
                    # in-flight job without tearing down the worker loop itself.
                    exec_task = asyncio.create_task(self._run_task(task))
                    self._task_exec = exec_task
                    self._running_task_id = task.id
                    try:
                        await exec_task
                    except asyncio.CancelledError:
                        if getattr(self, "_tasks_kill_requested", False):
                            self._tasks_kill_requested = False
                            logger.info("[TaskWorker] Task [%s] killed by user", task.id)
                        else:
                            raise  # session shutdown — propagate
                    finally:
                        self._task_exec = None
                        self._running_task_id = None
            except asyncio.CancelledError:
                return
            except Exception as _e:
                logger.error("[TaskWorker] Unexpected error: %s", _e, exc_info=True)

    def _self_work_saturated(self) -> bool:
        """Generation-side gate for the DMN back-fill above. The motor's rate caps
        gate EXECUTION only, and the worker refills from the DMN whenever nothing in
        the queue is currently due — so once the daily cap was hit, parked tasks
        backing off read as "idle" and the worker kept minting fresh ideas that could
        only ever be rate-limit-deferred. The queue grew all day instead of the work
        (2026-08-23: 125 of 159 job records were deferrals). Saturated = tasks are
        already parked waiting out a backoff, or the rate caps have no free slot;
        while saturated, ideas stay in the DMN's small ring buffer and age out
        naturally — ideas are cheap, the backlog is not. Fails open: a probe error
        must never silence self-directed work entirely."""
        saturated = False
        try:
            if self._task_queue.deferred_count() > 0:
                saturated = True
            elif self.motor is not None and self.motor.autonomy_saturated():
                saturated = True
        except Exception as _e:
            logger.debug("[TaskWorker] saturation probe failed (treating as free): %s", _e)
        # Log edges only — this is polled every 3s.
        if saturated != getattr(self, "_self_work_was_saturated", False):
            self._self_work_was_saturated = saturated
            logger.info(
                "[TaskWorker] Self-task intake %s",
                "paused — lane saturated (parked backlog or rate caps)"
                if saturated
                else "resumed — lane has capacity again",
            )
        return saturated

    def kill_self_directed_work(self) -> dict:
        """UI kill switch: cancel the in-flight internal job (if any), fail all
        pending/blocked/running queue entries, and drain the DMN's un-enqueued
        self-task buffer so the work doesn't immediately respawn."""
        killed_running = False
        exec_task = getattr(self, "_task_exec", None)
        if exec_task is not None and not exec_task.done():
            self._tasks_kill_requested = True
            exec_task.cancel()
            killed_running = True
        cleared = self._task_queue.clear_all() if self._task_queue else 0
        drained = 0
        if self.dmn is not None:
            try:
                drained = len(self.dmn._self_task_q)
                self.dmn._self_task_q.clear()
            except Exception:
                pass
        logger.info(
            "[TaskWorker] Kill switch: running_killed=%s queue_cleared=%d dmn_drained=%d",
            killed_running,
            cleared,
            drained,
        )
        return {"killed_running": killed_running, "cleared": cleared, "drained": drained}

    def kill_task(self, job_id: str) -> dict:
        """UI per-job kill switch: stop a single job by its UI job_id.

        The UI sends ``job_task_<task_id>`` (see _run_task); accept the raw
        task id too. If the target is the in-flight job, cancel its asyncio
        task and settle the ledger; if it's still pending/blocked in the queue,
        just cancel the queue entry. Leaves every other job untouched."""
        # UI job_id is "job_task_<id>"; tolerate a bare task id as well.
        task_id = job_id
        for prefix in ("job_task_", "task_", "job_"):
            if task_id.startswith(prefix):
                task_id = task_id[len(prefix) :]
                break
        killed_running = False
        exec_task = getattr(self, "_task_exec", None)
        if (
            getattr(self, "_running_task_id", None) == task_id
            and exec_task is not None
            and not exec_task.done()
        ):
            self._tasks_kill_requested = True
            exec_task.cancel()
            killed_running = True
            # The cancelled coroutine never reaches mark_done — settle it here so
            # the queue ledger doesn't leave the job stuck "running".
            if self._task_queue:
                self._task_queue.mark_done(task_id, success=False)
        cancelled_pending = False
        if not killed_running and self._task_queue:
            cancelled_pending = self._task_queue.cancel(task_id)
        logger.info(
            "[TaskWorker] Kill job [%s]: running_killed=%s pending_cancelled=%s",
            task_id,
            killed_running,
            cancelled_pending,
        )
        return {
            "task_id": task_id,
            "killed_running": killed_running,
            "cancelled_pending": cancelled_pending,
        }

    # ── Action approvals ───────────────────────────────────────────────────────
    def _emit_approvals_resolved(self, ids: list[str]) -> None:
        """Tell every connected UI these approval cards are settled, so a resolve
        from ANY surface (owner UI, engine API / tenant app) clears them everywhere —
        the owner UI previously only removed a card on its own optimistic click."""
        ids = [i for i in ids if i]
        if not ids or not self._emitter:
            return
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().create_task(
                self._emitter.emit_event({"type": "task_approval_resolved", "ids": ids})
            )

    async def _gate_action(self, action: dict) -> str:
        """Cloud-executor approval hook. Returns 'allow' or 'deny'.

        Allow an action the user has already approved (the resume path);
        otherwise record it as pending, surface it to the UI, and deny (skip) for
        now so the job moves on without doing anything sensitive unattended."""
        approvals = getattr(self, "_approvals", None)
        if approvals is None:
            return "deny"
        tool = action.get("tool", "")
        tool_input = action.get("input")
        # Job-scope grant: the running job is the re-queue of an action the user
        # already approved — every ask it raises is covered, so one approval clears
        # the whole task instead of ping-ponging back per action.
        token = str(getattr(self, "_job_approval_token", "") or "")
        if token and approvals.token_valid(token):
            logger.info("[Approvals] allowed by job-scope grant: %s", tool)
            return "allow"
        if approvals.is_approved(tool, tool_input):
            return "allow"
        item = approvals.record(
            tool,
            tool_input,
            reason=action.get("reason", ""),
            turn_id=action.get("turn_id", ""),
            end_user_id=action.get("end_user_id", ""),
        )
        with contextlib.suppress(Exception):
            if self._emitter:
                # Explicit job linkage: job-originated approvals carry turn_id == job_id
                # (ids prefixed "job_"), so the UI can deep-link the approval to its job
                # without depending on the turn-id format staying that way forever.
                _tid = str(item.turn_id or "")
                await self._emitter.emit_event(
                    {
                        "type": "task_approval",
                        **item.to_dict(),
                        "job_id": _tid if _tid.startswith("job_") else "",
                    }
                )
        return "deny"

    def approve_action(
        self, approval_id: str, end_user_id: str | None = None, include_autonomous: bool = False
    ) -> dict:
        """UI/API: approve a pending action and re-queue it so the brain runs it,
        pre-authorized, on the next idle cycle. end_user_id (engine API) scopes the
        approval to that end-user's own items; None (owner/brain UI) = any.
        include_autonomous also admits the autonomous/owner lane ("")."""
        approvals = getattr(self, "_approvals", None)
        if approvals is None:
            return {"ok": False, "error": "approvals unavailable"}
        item = approvals.approve(
            approval_id, end_user_id=end_user_id, include_autonomous=include_autonomous
        )
        if item is None:
            return {"ok": False, "error": "no such pending approval"}
        # Soft-pause sentinel: approving "continue autonomous spending" lifts the pause
        # for the rest of the UTC day (up to the hard cap) rather than re-queuing a tool.
        from brain.autonomy import CONTINUE_SPEND_TOOL

        if item.tool == CONTINUE_SPEND_TOOL:
            gate = getattr(self, "_spend_gate", None)
            if gate is not None:
                with contextlib.suppress(Exception):
                    gate._budget.clear_soft_pause()
            logger.info("[Approvals] owner approved continue-spending — soft pause lifted")
            self._emit_approvals_resolved([item.id])
            return {"ok": True, "tool": item.tool, "continued": True}
        goal = f"The user approved this action — carry it out now: {item.tool}"
        if item.preview:
            goal += f" ({item.preview})"
        # One approval clears the whole task: mint a job-scope grant the re-run
        # carries, and settle the job's OTHER pending asks — the pre-authorized
        # re-run redoes them, so their cards are dead weight in every UI. The
        # per-item approval is superseded by the grant, so consume it here rather
        # than leave a stray one-time same-tool pass in the ledger.
        token = approvals.grant_for(item.turn_id)
        approvals.consume_item(item.id)
        superseded = approvals.resolve_siblings(item.turn_id, exclude_id=item.id)
        queued = None
        if self._task_queue:
            queued = self._task_queue.enqueue(goal, source="user", priority=1, approval_token=token)
        if queued is None:
            # Deduplicated against an already-queued twin (or no queue) — that twin
            # carries its own grant or re-asks; don't leave this one dangling live.
            approvals.revoke_token(token)
        self._emit_approvals_resolved([item.id, *superseded])
        logger.info("[Approvals] approved + re-queued [%s] %s (job grant)", item.id, item.tool)
        return {"ok": True, "tool": item.tool}

    def skip_action(
        self, approval_id: str, end_user_id: str | None = None, include_autonomous: bool = False
    ) -> dict:
        approvals = getattr(self, "_approvals", None)
        if approvals is None:
            return {"ok": False, "error": "approvals unavailable"}
        ok = approvals.skip(
            approval_id, end_user_id=end_user_id, include_autonomous=include_autonomous
        )
        if ok:
            self._emit_approvals_resolved([approval_id])
        return {"ok": ok}

    def list_approvals(
        self, end_user_id: str | None = None, include_autonomous: bool = False
    ) -> list[dict]:
        approvals = getattr(self, "_approvals", None)
        return (
            approvals.pending(end_user_id=end_user_id, include_autonomous=include_autonomous)
            if approvals
            else []
        )

    # ── Engine-API relay (tenant apps; scoped to the caller's end-user) ─────────
    # `include_autonomous` is set by the route for OWNER-key callers so a single-tenant
    # owner app (e.g. the trading copilot) also sees the actions the brain queued while
    # unattended (the "" autonomous lane), which otherwise surface only in the owner UI.
    # Non-owner partners never set it, so cross-tenant isolation is preserved.
    def api_list_approvals(self, end_user_id: str, include_autonomous: bool = False) -> list[dict]:
        return self.list_approvals(
            end_user_id=end_user_id or "", include_autonomous=include_autonomous
        )

    def api_resolve_approval(
        self, approval_id: str, end_user_id: str, approve: bool, include_autonomous: bool = False
    ) -> dict:
        """Resolve one approval on behalf of a tenant end-user. The end_user_id is
        enforced, so a partner can only resolve their own end-user's items (plus the
        autonomous/owner lane when include_autonomous is set, for owner-key callers)."""
        euid = end_user_id or ""
        if approve:
            return self.approve_action(
                approval_id, end_user_id=euid, include_autonomous=include_autonomous
            )
        return self.skip_action(
            approval_id, end_user_id=euid, include_autonomous=include_autonomous
        )

    # ── Autonomous job history (durable results surface) ────────────────────────
    def api_list_jobs(self, limit: int = 20, state: str | None = None) -> list[dict]:
        """Recent job outcomes. Prefers the durable agent_jobs table; falls back to the
        JSON JobStore (local/companion mode or before the table exists)."""
        try:
            from brain import agent_jobs_store

            rows = agent_jobs_store.list_recent(limit=limit, state=state)
            if rows:
                return rows
        except Exception:
            pass
        motor = getattr(self, "motor", None)
        store = getattr(motor, "job_store", None) if motor else None
        if store is None:
            return []
        out = store.list_recent(limit=limit)
        if state:
            out = [j for j in out if j.get("state") == state]
        return out

    def api_grade_turn(self, turn_id: str, grade, source: str = "user_thumbs") -> dict:
        """Record an EXTERNAL grade for a turn (thumbs press, validator verdict).

        Writes three places: the live TurnTrace when the turn hasn't consolidated
        yet (so the grade re-weights the Hebbian composite at the next sleep), the
        eval log via patch_turn (auditable even post-consolidation), and the
        decision stream (ledger + live UI). Nudges DA as a genuinely external
        signal — ON by default (external_grade_da_nudge=0.15); a tenant opts out
        by setting it to 0.

        Chemistry moves AT MOST once per turn_id: the first grade pays
        nudge*grade; a re-grade pays only the bounded difference from the
        previous grade (identical repeat = zero), so repeated posts can never
        saturate DA. And it moves only for a turn that is actually LIVE in the
        trace buffer — a fabricated or already-consolidated turn_id records to
        the eval log but touches no chemistry, and the response says so
        (applied_live=false, reason="turn_not_live") so an async grader can
        detect it missed the window."""
        from eval.external_grading import normalize_grade

        g = normalize_grade(grade)
        if not turn_id or g is None:
            return {"ok": False, "error": "missing turn_id or unusable grade"}
        applied_live = False
        prev_grade = None
        for trace in getattr(self, "_session_traces_full", []) or []:
            if getattr(trace, "turn_id", "") == turn_id:
                prev_grade = getattr(trace, "external_grade", None)
                trace.external_grade = g
                trace.external_grade_source = source
                applied_live = True
                break
        with contextlib.suppress(Exception):
            if self._eval_logger is not None:
                self._eval_logger.patch_turn(
                    turn_id, external_grade=g, external_grade_source=source
                )
        with contextlib.suppress(Exception):
            from brain.observability.decisions import decisions

            decisions.log("external_grade_recorded", turn_id=turn_id, grade=g, source=source)
        if applied_live:
            with contextlib.suppress(Exception):
                nudge = float(_brain_settings.get("external_grade_da_nudge", 0) or 0)
                if nudge > 0:
                    # Bound the felt-state move: clamp the grade to [-1, 1] (defends a
                    # future scale/caller that bypasses normalize_grade) and clamp the
                    # resulting delta to +/-nudge so a hostile or spammy grader cannot
                    # push more than the configured nudge per write. Level saturation
                    # in Neuromodulators.add ([0, 1]) is the spam ceiling on top.
                    # Idempotency: a re-grade of the same turn pays only the difference
                    # from the previous grade — the writes for one turn telescope to
                    # nudge*(last - first) instead of accumulating, so no sequence of
                    # posts can pump DA beyond one nudge from where it started.
                    g_clamped = max(-1.0, min(1.0, float(g)))
                    if prev_grade is not None:
                        g_clamped -= max(-1.0, min(1.0, float(prev_grade)))
                    delta = max(-nudge, min(nudge, nudge * g_clamped))
                    if delta:
                        self.bus.neuromod.add(
                            "DA",
                            delta,
                            source="external_grader",
                            reward_source="user_emotion",
                            reason="thumbs",
                        )
        out = {"ok": True, "grade": g, "applied_live": applied_live}
        if not applied_live:
            # The turn exists only in the eval log now (or never existed): the
            # learning half of the grade is gone. Say so instead of a silent ok.
            out["reason"] = "turn_not_live"
        return out

    def api_grade_turn_engine(
        self,
        turn_id: str,
        grade,
        end_user_id: str = "",
        persona: str = "",
        source: str = "api",
        api_session_id: str = "",
    ) -> dict:
        """Engine-mode external grade (partner API: POST /sessions/{id}/turns/{tid}/grade).

        The owner UI grades on the single resting chemistry, so api_grade_turn's DA
        nudge lands on the right bus with no binding. The engine is different: a turn
        runs with the end-user's (and persona's) chemistry BOUND for the scope of that
        turn, then reverts to the resting pair. An external grade arrives out of band,
        so unbound self.bus resolves to the resting pair — nudging the wrong mood. Here
        we re-resolve and bind the SAME pair the turn used (per-customer registry, or
        the persona chem pair under multi-persona Path B) so the grade moves THAT
        customer's dopamine, then persist it through the client-chem registry so it
        survives. Mirrors process_turn's binding exactly. The trace write + eval/decision
        log inside api_grade_turn happen regardless of binding — they key on turn_id.

        Isolation: ``api_session_id`` is the session the CALLER owns (the URL path the
        route already authorized). The trace buffer is process-wide across every
        partner, so a turn_id is resolved ONLY when its trace was stamped with this
        same session — a turn belonging to another partner's (or the owner's) session
        is refused outright: no trace write, no eval patch, no DA. When the trace is
        live, the chemistry binding comes from the TRACE's own stamps (the binding the
        turn actually ran under), not the caller-supplied persona/end_user — so the
        grade can't be steered onto a different pair than the turn used."""
        from brain.second_brain.store import bind_persona

        persona = (persona or "").strip()
        euid = (end_user_id or "").strip()
        api_session_id = (api_session_id or "").strip()
        trace = None
        for t in getattr(self, "_session_traces_full", []) or []:
            if getattr(t, "turn_id", "") == turn_id:
                trace = t
                break
        if (
            api_session_id
            and trace is not None
            and getattr(trace, "api_session_id", "") != api_session_id
        ):
            # Another session's turn (another partner, another session of the
            # same partner, or an owner turn). Deny before ANY write. The route
            # maps this to 404 so it's indistinguishable from a turn that never
            # existed — no cross-partner turn-id oracle.
            return {
                "ok": False,
                "denied": True,
                "applied_live": False,
                "error": "unknown turn_id for this session",
            }
        if trace is not None:
            # Bind what the turn bound. Stamps are authoritative; the caller's
            # session-derived values only fill in for pre-stamp traces (journal
            # replays from an older build).
            persona = (getattr(trace, "api_persona", "") or "").strip() or persona
            euid = (getattr(trace, "end_user_id", "") or "").strip() or euid
        registry = None
        if persona and euid:
            bind_cm = self.bus.bind(self._persona_chem_pair(persona, euid))
        elif euid:
            registry = self._client_chem_registry()
            bind_cm = self.bus.bind(registry.get_or_create(euid))
        else:
            bind_cm = contextlib.nullcontext()
        try:
            with bind_persona(persona), bind_cm:
                return self.api_grade_turn(turn_id, grade, source=source)
        finally:
            # Force-persist so the graded mood is durable now, not only at the next
            # turn or consolidation (the grade may be the last thing this customer does).
            if registry is not None and euid:
                with contextlib.suppress(Exception):
                    registry.persist(euid, force=True)

    def api_learning(self, view: str, persona: str = "", edge: str = "", limit: int = 50) -> dict:
        """Learning-surface views for the engine API (mirrors the owner UI's
        /learning/* routes). Live wiring/bus flow in so the active persona's
        unsaved in-session state is visible; other personas read their files."""
        from brain.observability import learning_reader

        if view == "stories":
            return learning_reader.stories(persona=persona, limit=limit, live_wiring=self.wiring)
        if view == "wiring":
            return learning_reader.wiring_view(persona=persona, edge=edge, live_wiring=self.wiring)
        return learning_reader.summary(persona=persona, live_wiring=self.wiring, live_bus=self.bus)

    def api_get_job(self, job_id: str) -> dict | None:
        """Full record for one job — agent_jobs table first, then the JSON JobStore."""
        try:
            from brain import agent_jobs_store

            rec = agent_jobs_store.get(job_id)
            if rec:
                return rec
        except Exception:
            pass
        motor = getattr(self, "motor", None)
        store = getattr(motor, "job_store", None) if motor else None
        return store.get(job_id) if store is not None else None
