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
                        asyncio.ensure_future(self._emitter.emit_event({
                            "type": "voice_mode", "active": True, "muted": True,
                        }))
                else:
                    self._tts_did_mute = False
            else:
                if self._tts_did_mute:
                    self._tts_did_mute = False
                    asyncio.ensure_future(self._unmute_after_drain())

    async def _unmute_after_drain(self) -> None:
        await asyncio.sleep(self._mic_unmute_delay_s)
        if self._streaming_mic is not None:
            self._streaming_mic.unmute()
            if self._emitter:
                await self._emitter.emit_event({
                    "type": "voice_mode", "active": True, "muted": False,
                })

    async def _on_browser_message(self, text: str) -> None:
        await self._ui_message_queue.put(text)

    def _on_eval_mode(self, intensive: bool) -> None:
        if self._baseline_runner:
            self._baseline_runner.set_intensive(intensive)

    def _on_mic_toggle(self) -> bool:
        if self._streaming_mic is not None:
            return self._streaming_mic.toggle_mute()
        return False

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
            if thought:
                await self._emitter.emit_stream_thought(thought, chem_delta=chem_delta)

    async def _heartbeat_with_ui(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.brainstem.heartbeat_once(emitter=self._emitter)

    async def _speak_gate_loop(self) -> None:
        SPEAK_GATE_INTERVAL = float(_brain_settings.get("speak_gate_poll_interval") or 5.0)
        SPEAK_CAND_MAX_AGE = float(_brain_settings.get("speak_candidate_max_age_s") or 60.0)
        while True:
            try:
                await asyncio.sleep(SPEAK_GATE_INTERVAL)
                if self.dmn.candidate_count() == 0:
                    continue
                now = time.time()
                since_last_spoke = now - self._last_brain_spoke_ts
                idle_s = get_idle_seconds()
                user_active = (self._proactive_idle_threshold <= 0
                               or idle_s < self._proactive_idle_threshold)
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
                        self._streaming_mic, "is_user_speaking", False):
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
                            age, SPEAK_CAND_MAX_AGE, (c.get("spoken") or "")[:60],
                        )
                        continue
                    verdict, reason = await self.dmn.judge_candidate(c)
                    logger.info("[Speak gate] verdict=%s reason=%s candidate=%r",
                                verdict, reason, (c.get("spoken") or "")[:60])
                    if verdict == "yes":
                        try:
                            bridged = await self.dmn.bridge_if_needed(c)
                            if bridged and bridged != c.get("spoken"):
                                c["spoken"] = bridged
                        except Exception as _bridge_err:
                            logger.debug("[Speak gate] Bridge step failed: %s", _bridge_err)
                        self.dmn.commit_candidate_to_speech(c)
                    elif verdict == "wait":
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
                            logger.debug("[I/O] voice → discarded %d queued utterance(s) (mic muted)",
                                         len(self._pending_during_tts))
                        self._pending_during_tts.clear()
                    else:
                        from brain.voice_bridge import pick_dispatch_from_queue
                        text, n = pick_dispatch_from_queue(self._pending_during_tts)
                        self._pending_during_tts.clear()
                        if text:
                            logger.info("[I/O] voice → flushing %d queued utterance(s): %r",
                                        n, text[:80])
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
            if self._streaming_mic.is_muted:
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
                    source_label = {"recovery": "📋 resuming", "self": "💭 self-initiated",
                                    "user": "▶ executing"}.get(task.source, "▶")
                    logger.info("[TaskWorker] %s task [%s]: %s",
                                source_label, task.id, task.goal[:80])
                    await self._run_task(task)
            except asyncio.CancelledError:
                return
            except Exception as _e:
                logger.error("[TaskWorker] Unexpected error: %s", _e, exc_info=True)
