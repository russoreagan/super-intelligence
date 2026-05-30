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
        if self._emitter:
            asyncio.ensure_future(self._emitter.emit_event({"type": "speaking", "active": active}))
        _mute_enabled = os.environ.get("BRAIN_MIC_MUTE_DURING_TTS", "true").lower() != "false"
        if _mute_enabled and self._streaming_mic is not None:
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
            mic.unmute()
        else:
            mic.mute()
        self._tts_did_mute = False
        self._emit_mic_state()

    def _emit_mic_state(self) -> None:
        """Broadcast the settled mic status so the button reflects reality."""
        if not self._emitter or self._streaming_mic is None:
            return
        status = "muted" if self._streaming_mic.is_muted else "active"
        asyncio.ensure_future(self._emitter.emit_event({"type": "mic_state", "status": status}))

    async def _on_browser_message(self, text: str) -> None:
        await self._ui_message_queue.put(text)

    def _on_eval_mode(self, intensive: bool) -> None:
        if self._baseline_runner:
            self._baseline_runner.set_intensive(intensive)

    def _set_mic_listening(self, want_live: bool) -> None:
        """Single source of truth for whether the user wants the mic live
        (push-to-talk held, or toggled on via the button). Honours half-duplex:
        never opens the mic while the entity is speaking — the post-TTS restore
        applies `want_live` then. On release, flush the held phrase and re-mute."""
        self._ptt_held = want_live
        mic = self._streaming_mic
        if mic is None:
            return
        if want_live:
            if not self.pns.is_speaking:
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
                logger.error(
                    "Memory write failed for this turn — episode will not be saved to long-term memory: %s",
                    exc,
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
            if thought:
                await self._emitter.emit_stream_thought(
                    thought, chem_delta=chem_delta, proactive=proactive
                )

    async def _heartbeat_with_ui(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.brainstem.heartbeat_once(emitter=self._emitter)

    async def _speak_gate_loop(self) -> None:
        SPEAK_GATE_INTERVAL = float(_brain_settings.get("speak_gate_poll_interval") or 5.0)
        SPEAK_CAND_MAX_AGE = float(_brain_settings.get("speak_candidate_max_age_s") or 60.0)
        SPEAK_CAND_MAX_ATTEMPTS = int(_brain_settings.get("speak_candidate_max_attempts") or 4)
        while True:
            try:
                await asyncio.sleep(SPEAK_GATE_INTERVAL)
                if self.dmn.candidate_count() == 0:
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
            if self._streaming_mic.is_muted and not utt.get("from_ptt_flush"):
                # Discard utterances that arrived while muted (e.g. TTS bleed-through
                # in always-on mode). But PTT flush utterances are intentional — the
                # mic is muted *after* the utterance is queued, so don't discard them.
                logger.debug("[I/O] voice → discarded stale utterance (mic muted)")
                continue
            text = (utt.get("transcript") or "").strip()
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

    async def consolidate_now(self, reason: str = "manual") -> dict:
        """Force a consolidation pass on the buffered traces. Safe to call
        anytime; single-flight protected. Returns a small status dict."""
        if self._sleep is None or self._consolidation_lock is None:
            return {"ran": False, "reason": "sleep_loop_disabled"}
        if self._consolidation_lock.locked():
            return {"ran": False, "reason": "already_running"}
        if not self._session_traces:
            return {"ran": False, "reason": "no_buffered_turns"}
        async with self._consolidation_lock:
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
        dmn_thoughts = []
        if self.dmn:
            try:
                self.dmn.pause()
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
                async with self._consolidation_lock:
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
                    if self.dmn:
                        self_goal = self.dmn.take_self_task()
                        if self_goal:
                            self._task_queue.enqueue(self_goal, source="self", priority=2)
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
                    await self._run_task(task)
            except asyncio.CancelledError:
                return
            except Exception as _e:
                logger.error("[TaskWorker] Unexpected error: %s", _e, exc_info=True)
