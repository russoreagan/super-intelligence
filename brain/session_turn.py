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

# Per-customer chemistry write throttle, matching the persona chemistry's own
# per-turn save cadence below (both write one small file, overwritten in place).
_CLIENT_CHEM_PERSIST_INTERVAL_S = 5.0

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


def _effective_answer_only(features) -> bool:
    """Is the CURRENT turn answer-only (pure Q&A — no motor dispatch, no
    muscle-memory open-loop, no FollowThrough enqueue)? True when the bound turn
    context declares it (API session/turn option → turn_ctx.bind_turn) OR the
    turn's agent carries the answer_only permission. Fails open to False so a
    store hiccup can never silence a normal agent's tools."""
    from brain.turn_ctx import current_turn

    if bool(current_turn().get("answer_only")):
        return True
    agent_id = features.get("agent_id") if isinstance(features, dict) else None
    if not agent_id:
        return False
    try:
        from brain import agents

        return agents.answer_only(agent_id)
    except Exception:
        return False


class _TurnMixin:
    def _judge_grade_lookup(self, turn_id: str) -> float | None:
        """The EXTERNAL grade recorded for `turn_id`, or None.

        Handed to the judge-attachment grader so a genuine partner/owner verdict
        (§4.4) can ground a judge's self-read — the grade lands on the trace after
        the turn ends, so it is only ever readable one turn later, which is exactly
        when this is called. Reads the live in-session traces; a consolidated turn is
        simply ungraded (None), never guessed.
        """
        if not turn_id:
            return None
        try:
            for t in getattr(self, "_session_traces_full", []) or []:
                if getattr(t, "turn_id", None) == turn_id:
                    g = getattr(t, "external_grade", None)
                    return None if g is None else float(g)
        except Exception:
            pass
        return None

    # ── Turn processing ───────────────────────────────────────────────────────

    async def api_turn(
        self,
        message: str,
        end_user_id: str,
        mandate_id: str | None = None,
        persona: str | None = None,
        inline_tools: bool = True,
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
        process persona, unchanged.

        `inline_tools` is the transport's async capability: a request/response
        transport (POST /turns, /turns/stream) has no channel for a later result, so
        it runs reactive tools INLINE (default True). A long-lived WS transport CAN
        receive an out-of-band proactive_speech after turn_end, so it passes False to
        keep the non-blocking defer→proactive loop (the trading-job path)."""
        lock = getattr(self, "_api_turn_lock", None)
        if lock is None:
            lock = self._api_turn_lock = asyncio.Lock()
        async with lock:
            text, affect = await self.process_turn(
                message,
                end_user_id=end_user_id,
                mandate_id=mandate_id,
                persona=persona,
                inline_tools=inline_tools,
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

    async def api_extract(
        self,
        input_text: str,
        schema: dict,
        instructions: str = "",
        tool_name: str = "extract",
    ) -> dict:
        """Engine entry point for SESSIONLESS structured extraction. Forces a single
        cheap model call to return JSON matching `schema`, with NO session, persona,
        memory, motor, or DMN — none of the conversational-turn machinery. Built for
        high-volume utility classification (a partner pulling structured fields out of
        text, e.g. a tradeable signal from an article) that must not pay for a full
        turn and needs reliable JSON, not free-form prose.

        Bounded by the daily USD ceiling like every cloud call: on a lite brain over
        budget, call_structured raises CloudBudgetExceeded → the API maps it to 402.
        Not turn-locked: extraction is stateless and read-only, so it can run
        concurrently with (and without blocking) live conversational turns."""
        system = instructions.strip() or "Extract structured data from the user's text."
        return await self.router.call_structured(
            "haiku",
            system,
            [{"role": "user", "content": input_text}],
            tool_name or "extract",
            "Return the extracted fields as a single structured object.",
            schema,
            cluster="api",
            cell="extract",
            max_tokens=1024,
        )

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

    # Supabase tables keyed directly by end_user_id. Adding a table here is the whole
    # fix for it; tests/test_api_purge_end_user.py asserts on this exact tuple so a
    # new per-end-user table cannot be introduced without a purge decision.
    _PURGE_TABLES = (
        "episodes",
        "tasks",
        "dmn_state",
        "speaker_profiles",
        "brain_schemas",
        "api_sessions",
        # Verbatim prompt/response text per turn — the highest-value personal data in
        # the system, and previously never erased.
        "agent_turns",
    )

    async def api_purge_end_user(self, end_user_id: str) -> dict:
        """Erase one end-user's footprint (ops / GDPR): their durable rows across
        every per-end-user table and store, plus this process's in-memory caches.
        Returns a per-step deleted summary. Serialized on the turn lock so a purge
        can't race a turn for the same customer.

        `ok` is False if ANY step failed. It used to be unconditionally True, which is
        how the documented promise to erase "every per-user table" went years without
        anyone noticing it covered six of about a dozen stores."""
        end_user_id = (end_user_id or "").strip()
        if not end_user_id:
            return {"ok": False, "error": "end_user_id required"}
        lock = getattr(self, "_api_turn_lock", None)
        if lock is None:
            lock = self._api_turn_lock = asyncio.Lock()
        async with lock:
            deleted: dict = {}

            # 1. In-process caches first, so a concurrent reload can't repopulate from
            #    a row we are about to delete.
            reg = getattr(self, "_client_chem", None)
            if reg is not None:
                with contextlib.suppress(Exception):
                    reg.forget(end_user_id, durable=True)
            um = getattr(self, "_engine_um_cache", None)
            if isinstance(um, dict):
                um.pop(end_user_id, None)

            # 2. Durable chemistry snapshots for EVERY persona in the org. The store
            #    key is "<persona>:<end_user_id>", so purging only the current persona
            #    left the customer's mood behind on every other one they had spoken to.
            deleted["chem_snapshots"] = self._purge_chem_snapshots(end_user_id)

            # 3. Pending approvals carry end_user_id plus a tool_input blob.
            deleted["approvals"] = self._purge_approvals(end_user_id)

            try:
                from brain.second_brain import supabase_client

                if supabase_client.is_enabled():
                    client = supabase_client.get_client()
                    org = supabase_client.get_org_id()
                    for table in self._PURGE_TABLES:
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

                    # The per-speaker profile is written with end_user_id='' (a
                    # companion-mode default the engine path never threaded through),
                    # so the filter above never reaches it and the customer's personal
                    # facts survived erasure. It is addressable by FILENAME instead —
                    # across all personas, since each keeps its own row.
                    deleted["speaker_schema"] = self._purge_speaker_schema(client, org, end_user_id)

                    # Connector tokens: live third-party OAuth credentials, and the
                    # row only points at a Vault secret, so this needs the RPC from
                    # migration 030 rather than a row delete (which would orphan the
                    # ciphertext).
                    try:
                        resp = client.rpc(
                            "purge_end_user_mcp_tokens",
                            {"p_end_user_id": end_user_id, "p_org_id": org},
                        ).execute()
                        deleted["end_user_mcp_tokens"] = resp.data
                    except Exception as e:
                        deleted["end_user_mcp_tokens"] = f"error: {e}"
                else:
                    # Local backend: the loop above is a no-op, but the on-disk stores
                    # still hold the customer. Reporting ok:True here without doing
                    # this was a silent non-erasure.
                    deleted["local_schema"] = self._purge_local_speaker_files(end_user_id)
            except Exception as e:
                return {"ok": False, "error": str(e)}

            failed = [k for k, v in deleted.items() if isinstance(v, str) and v.startswith("error")]
            return {
                "ok": not failed,
                "end_user_id": end_user_id,
                "deleted": deleted,
                **({"failed": failed} if failed else {}),
            }

    # ── purge helpers ────────────────────────────────────────────────────────
    # Separate methods so each store's failure is recorded independently: one
    # unreachable store must not abandon the rest of an erasure.

    def _all_persona_slugs(self) -> list[str]:
        """Every persona this org runs — built-ins plus custom specs."""
        try:
            from brain import personas

            slugs = [str(p.get("slug") or "") for p in (personas.list_all() or [])]
        except Exception:
            slugs = []
        current = str(getattr(self, "persona_name", "") or "")
        if current and current not in slugs:
            slugs.append(current)
        return [s for s in slugs if s]

    def _purge_chem_snapshots(self, end_user_id: str) -> int | str:
        try:
            from brain import client_chem
            from brain.persona_key import persona_slug

            n = 0
            for slug in self._all_persona_slugs():
                store = client_chem.default_store(slug)
                # Both key shapes: persona-qualified, and the bare id used when a
                # registry was built without a persona.
                for key in (f"{persona_slug(slug)}:{end_user_id}", end_user_id):
                    with contextlib.suppress(Exception):
                        store.delete(key)
                        n += 1
            return n
        except Exception as e:
            return f"error: {e}"

    def _purge_approvals(self, end_user_id: str) -> int | str:
        approvals = getattr(self, "_approvals", None)
        if approvals is None:
            return 0
        try:
            return approvals.forget_end_user(end_user_id)
        except Exception as e:
            return f"error: {e}"

    def _purge_speaker_schema(self, client, org: str, end_user_id: str) -> int | str:
        try:
            from brain.second_brain.store import SchemaStore

            filename = SchemaStore(persona="").speaker_filename(end_user_id)
            res = (
                client.table("brain_schemas")
                .delete()
                .eq("org_id", org)
                .eq("filename", filename)
                .execute()
            )
            return len(res.data or [])
        except Exception as e:
            return f"error: {e}"

    def _purge_local_speaker_files(self, end_user_id: str) -> int | str:
        try:
            from brain.persona_key import persona_state_root
            from brain.second_brain.store import SchemaStore

            filename = SchemaStore(persona="").speaker_filename(end_user_id)
            n = 0
            for slug in self._all_persona_slugs():
                path = persona_state_root(slug) / "schema" / filename
                with contextlib.suppress(OSError):
                    if path.exists():
                        path.unlink()
                        n += 1
            return n
        except Exception as e:
            return f"error: {e}"

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
        companion turns never call this — the registry is never even constructed,
        so that path stays byte-for-byte the single-resting-chemistry brain.

        Durably backed: ``default_store`` routes onto this tenant's volume under
        THIS persona's state root, so the emotional relationship a persona has with
        each customer survives a restart instead of resetting to the temperament
        baseline on every deploy. Writes are throttled per customer (the turn path
        persists every turn); the session force-flushes on graceful shutdown. An
        unwritable volume degrades to in-memory — never into the turn."""
        reg = getattr(self, "_client_chem", None)
        if reg is None:
            from brain.client_chem import ClientChemRegistry, default_store

            # Same persona for the store root and the key: one is the path fence,
            # the other the key fence, and they must agree.
            reg = ClientChemRegistry(
                self.bus,
                default_store(self.persona_name),
                persona=self.persona_name,
                min_persist_interval_s=_CLIENT_CHEM_PERSIST_INTERVAL_S,
            )
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
        inline_tools: bool = False,
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
                return await self._run_turn_guarded(
                    user_input, image_path, end_user_id, mandate_id, inline_tools=inline_tools
                )
        finally:
            if registry is not None:
                registry.persist(end_user_id)

    async def _run_turn_guarded(
        self,
        user_input: str,
        image_path: str | None = None,
        end_user_id: str | None = None,
        mandate_id: str | None = None,
        inline_tools: bool = False,
    ) -> tuple[str, dict]:
        from brain.brainstem import TURN_TIMEOUT

        try:
            return await asyncio.wait_for(
                self._process_turn_body(
                    user_input, image_path, end_user_id, mandate_id, inline_tools=inline_tools
                ),
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
        from brain.persona_key import active_or_home_persona

        persona = active_or_home_persona()
        w = reward_weight(persona, "mastery")
        er = float(settings.get("emotional_reactivity_scale"))
        base = float(settings.get("accomplishment_base"))
        if bool(summary.get("success")):
            # Per-job intrinsic ceiling: the job may already have paid itself
            # story-criteria DA mid-run (summary.intrinsic_da_spent). One success
            # must not stack unbounded self-reward across emission points — the
            # premise audit measured ~0.34 DA/job vs ~0.10 for real user praise.
            cap = float(settings.get("job_intrinsic_da_cap"))
            already = float(summary.get("intrinsic_da_spent", 0.0) or 0.0)
            room = max(0.0, cap - already) if cap > 0 else float("inf")
            delta = min(base * difficulty * modifier * w * er, room)
            if delta > 0:
                self.bus.neuromod.add("DA", delta, reward_source="mastery", reason="job_success")
        else:
            # Failing a braced-for hard task is a loss — weight the dip by this persona's loss
            # aversion (λ), independently of the symmetric mastery reward weight (w). The reward
            # branch above is never λ-scaled; the asymmetry IS loss aversion.
            la = loss_aversion(persona)
            fail_ratio = float(settings.get("accomplishment_fail_ratio"))
            self.bus.neuromod.add(
                "DA",
                -base * difficulty * fail_ratio * w * er * la,
                reward_source="mastery",
                reason="job_failure",
            )
            self.bus.hormonal.add(
                "5HT", -float(settings.get("correctness_5ht_drain")) * w * er * la
            )

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
            from brain.persona_key import active_or_home_persona

            persona = active_or_home_persona()
            delta = (
                pr
                * float(settings.get("prediction_reward_base"))
                * reward_weight(persona, "correctness")
                * float(settings.get("emotional_reactivity_scale"))
            )
            cap = float(settings.get("prediction_reward_turn_cap"))
            self.bus.neuromod.add(
                "DA",
                max(-cap, min(cap, delta)),
                reward_source="correctness",
                reason="world_prediction",
            )
        except Exception:
            pass

    async def _process_turn_body(
        self,
        user_input: str,
        image_path: str | None = None,
        end_user_id: str | None = None,
        mandate_id: str | None = None,
        inline_tools: bool = False,
    ) -> tuple[str, dict]:
        from brain.observability.firing_path import (
            record_node_active,
            reset_current_trace,
            set_current_trace,
        )
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

        # Stamp the trace with the TURN-BOUND persona (agent lanes bind per turn),
        # not the boot home persona — the Hebbian pass groups traces by this stamp
        # to credit each persona's own wiring, and eval rows tag by it.
        from brain.persona_key import active_or_home_persona
        from brain.turn_ctx import current_turn

        # Engine turns also stamp their API session + chemistry binding, so an
        # out-of-band grade can prove the turn belongs to the grading session and
        # re-bind exactly the pair this turn ran under. api_persona mirrors
        # _session_persona: the persona half of the session's agent_id ("" = the
        # process persona / per-customer registry path). Owner turns stamp "".
        _tctx = current_turn()
        _aid = _tctx.get("agent_id") or ""
        trace = TurnTrace(
            turn_id=turn_id,
            session_id=self.session_id,
            persona_name=active_or_home_persona() or self.persona_name,
            user_input=user_input,
            api_session_id=_tctx.get("session_id") or "",
            api_persona=_aid.split(".", 1)[0] if "." in _aid else "",
            end_user_id=(end_user_id or "") or (_tctx.get("end_user_id") or ""),
        )
        trace.prior_neuromod = self.bus.neuromod.snapshot()
        _ctx_token = set_current_trace(trace)
        # sensory.text is a bus channel with no neuron object, so it never reaches
        # fired_path — yet six wired edges start there. Record it as co-active as soon
        # as the trace is bound (NOT at the publish site in pns.py: the trace is not
        # bound yet there, so the record would be silently dropped).
        record_node_active("sensory.text", 1.0)

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

        # ── Answer-only enforcement ────────────────────────────────────────────
        # A turn declared answer_only (API session/turn option, carried on the
        # turn context) or run by an agent whose permissions mark it answer_only
        # is pure Q&A: draft an answer, do NOTHING else. Neutralizing
        # requires_action HERE — before the frontal task subsystem and the motor
        # branch read it — means no goal deposit, no "[task_queued]" ack the brain
        # never honors, no motor planning, and no muscle-memory open-loop (its
        # matching only runs inside motor execution). FollowThrough is gated
        # separately below because it fires on every turn, action or not.
        answer_only = _effective_answer_only(features)
        if answer_only:
            features = dict(features) if isinstance(features, dict) else {}
            features["answer_only"] = True
            if features.get("requires_action"):
                features["requires_action"] = False
                logger.info("[AnswerOnly] requires_action suppressed (answer-only turn)")

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

        # ── Hypothalamus: affect ──────────────────────────────────────────────
        await self._emit("hypothalamus", 0.6, "updating affect", turn_id)
        try:
            affect = await self.hypothalamus.process(features)
        except Exception as e:  # noqa: BLE001
            logger.warning("Emotion analysis failed — using neutral defaults: %s", e)
            affect = {"emotion": "neutral", "user_emotion": "unknown"}
        await self._emit_end("hypothalamus", turn_id)

        # ── Thalamus: the global-workspace spotlight (GWT) ────────────────────
        # Runs after affect, not in parallel with it: the spotlight ignites on
        # affective coalitions (threat), so it must see the settled affect. Cost is
        # a sub-microsecond read of the bus concentration layer. The verdict rides
        # on affect/features for the same-turn consumers (recall gate, frontal) and
        # is published on attention.focus for the DMN.
        await self._emit("thalamus", 0.55, "routing attention", turn_id)
        try:
            spotlight = await self.thalamus.route(features, affect)
        except Exception as e:  # noqa: BLE001
            logger.warning("Workspace routing failed — spotlight inactive: %s", e)
            from brain.clusters.thalamus import _neutral_verdict

            spotlight = _neutral_verdict()
        affect["spotlight"] = spotlight
        features["spotlight"] = spotlight
        await self._emit_end("thalamus", turn_id)

        # Tier-2 alternative recruitment signal: persist a content-free tally of workspace
        # ignitions for the sleep-time recruiter (hebbian._maybe_recruit_nodes). Flag-gated
        # at the call site so the killed path does no work at all — not even an import.
        if spotlight.get("ignited") and settings.get("node_recruit_from_ignition", 1):
            try:
                from brain.ignition_tally import record

                record(str(spotlight.get("coalition") or ""))
            except Exception:  # noqa: BLE001
                pass

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

        # ── Embed-first ──────────────────────────────────────────────────────
        # One embed of the input serves three consumers: the approach stage's
        # stance draw (which must see the CURRENT turn's vector — the frontal
        # cache is only written later, inside skill selection), hippocampus
        # recall (via query_vec), and later skill selection (via
        # memory["_query_vec"]). Net embeds per turn go DOWN.
        _query_vec: list | None = None
        try:
            _query_vec = await self.router.embed(user_input)
        except Exception:
            _query_vec = None

        # ── Approach outcome verification (a turn late, grounded) ────────────
        # Grade the PRIOR turn's committed approach against THIS turn's read:
        # tool outcome, post-suppression tool-request, re-ask, confusion, tone.
        # Patches the prior trace (external-grade pattern) and the pair ledger's
        # `confirmed` column. Writes no chemistry, ever.
        _pending_ap = getattr(self, "_pending_approach", None)
        if _pending_ap is not None:
            self._pending_approach = None
            try:
                from brain.approach_outcome import verify as _verify_approach
                from brain.stance_pairs import record_verdict as _pair_verdict

                _averdict = _verify_approach(_pending_ap, features, _query_vec)
                if _averdict:
                    for _t in getattr(self, "_session_traces_full", []) or []:
                        if getattr(_t, "turn_id", "") == _pending_ap.turn_id:
                            _t.approach_outcome = _averdict
                            break
                    if _averdict.get("confirmed"):
                        _pair_verdict(
                            getattr(self, "_stance_pairs", {}),
                            _pending_ap.info_id,
                            _pending_ap.method_id,
                            column="confirmed",
                        )
                    from brain.observability.decisions import decisions as _decisions

                    _decisions.log(
                        "approach_outcome_verified",
                        turn_id=_pending_ap.turn_id,
                        cluster="frontal",
                        info=round(_averdict["info"], 2),
                        method=round(_averdict["method"], 2),
                        confirmed=_averdict["confirmed"],
                        signals=_averdict["signals"],
                    )
            except Exception as _ve:
                logger.debug("[Approach] outcome verification skipped: %s", _ve)

        _needs_recall = bool(
            features.get("requires_memory") or features.get("epistemic_action") or novelty
        )
        if _needs_recall:
            await self._emit("hippocampus", 0.75, "recalling memory", turn_id)
            recall_task = asyncio.create_task(
                self.hippocampus.recall(
                    query=user_input,
                    entities=features.get("entities", []),
                    turn_id=turn_id,
                    embedding_fn=self.router.embed,
                    novelty=novelty,
                    features=features,
                    query_vec=_query_vec,
                )
            )
        else:

            async def _core_only() -> dict:
                return {
                    "core": self.hippocampus._active_core_context(),
                    "schema": "",
                    "episodes": "",
                }

            recall_task = asyncio.create_task(_core_only())

        # ── Approach stage: LAUNCHED CONCURRENT WITH RECALL ─────────────────
        # Generators fire now (they propose priors and don't need the evidence);
        # only the critic, inside compete_approach, awaits recall_task —
        # hypotheses form in parallel with retrieval, adjudication waits for
        # evidence. Hard skips: trivial/switch-only turns, answer-only turns
        # (nothing to decide — requires_action was already forced off), tiny
        # inputs, and a turn already at its LLM-call budget.
        approach_task: asyncio.Task | None = None
        if (
            self.frontal is not None
            and settings.get("approach_competition", 1)
            and not features.get("switch_only")
            and not features.get("answer_only")
            and features.get("intent") not in ("greeting",)
            and features.get("msg_length") != "tiny"
            and self.brainstem.check_budget()
        ):
            if _query_vec:
                self.frontal._current_query_vec = _query_vec
            approach_task = asyncio.create_task(
                self.frontal.compete_approach(
                    features,
                    affect,
                    recall_task,
                    self.parietal.recent_turns_text(),
                    turn_id,
                )
            )

        if _needs_recall:
            memory = await recall_task
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
            memory = await recall_task
        if _query_vec:
            memory["_query_vec"] = _query_vec

        if vision_features:
            memory["vision"] = (
                f"Image: {vision_features.get('description', '')}\n"
                f"Text in image: {vision_features.get('text_in_image', '')}\n"
                f"Context: {vision_features.get('context_for_response', '')}"
            )

        # ── Recent background task results ────────────────────────────────────
        # Inject recent background job outcomes so the LLM knows what actually happened
        # (prevents confabulation) AND stays aware of paused/stopped work it should not
        # re-plan. The in-memory ring buffer is primary (it also holds reactive inline
        # results); when it's empty (e.g. just after a restart) fall back to the durable
        # agent_jobs table so awareness of finished work survives the restart.
        _job_rows: list[dict] = [
            {
                "goal": r.get("goal", ""),
                "summary": r.get("summary", ""),
                "state": r.get("state") or ("completed" if r.get("success") else "failed"),
            }
            for r in getattr(self, "_recent_task_results", []) or []
        ]
        if not _job_rows:
            with contextlib.suppress(Exception):
                _job_rows = self.api_list_jobs(limit=5) or []
        if _job_rows:
            lines = []
            for r in _job_rows:
                state = r.get("state") or ("completed" if r.get("success") else "failed")
                summ = r.get("summary") or r.get("reason_human") or ""
                lines.append(f"- [{state}] {str(r.get('goal', ''))[:80]}: {summ}")
            lines.append(
                "(Older/full results are in the jobs store — call recall_jobs to retrieve "
                "them instead of re-running work.)"
            )
            memory["recent_task_results"] = "\n".join(lines)

        # ── Approach commit: strategy decided BEFORE the motor gate ──────────
        # The stage ran concurrently with recall; its verdict must land before
        # the requires_action branch below reads the flag. Reuses the answer-only
        # mechanism (copy features, mutate the flag, no new branch). Clamps:
        # advisory candidates (n==1, unscored) never get authority; a parked
        # cloud write awaiting user confirmation is a USER decision, untouched;
        # low-confidence winners shape framing but leave temporal's verdict alone.
        approach: dict | None = None
        if approach_task is not None:
            try:
                approach = await approach_task
            except Exception as _ae:
                logger.warning("[Approach] stage failed — falling through: %s", _ae)
                approach = None
        if approach:
            from brain.clusters.approach_schema import wants_action

            features = dict(features) if isinstance(features, dict) else {}
            features["_approach"] = approach
            trace.approach_scores = list(approach.get("scores", []))
            trace.selected_approach_id = str(approach.get("cell", ""))
            trace.approach_stance = str(approach.get("stance", ""))
            trace.approach_information_need = str(approach.get("information_need", ""))
            trace.approach_permutation = list(approach.get("permutation", []))
            trace.approach_wall_ms = int(approach.get("wall_ms", 0))
            trace.approach_chem_effort = float(approach.get("chem_effort", 0.0))
            if approach.get("advisory"):
                trace.approach_override = "advisory"
            elif settings.get("approach_authority", 1) and float(
                approach.get("confidence", 0.0)
            ) >= float(settings.get("approach_authority_confidence_floor", 0.55)):
                _cloud_exec = getattr(self.motor, "_cloud", None) if self.motor else None
                if not (_cloud_exec and _cloud_exec.has_pending):
                    want = wants_action(approach)
                    prior = bool(features.get("requires_action"))
                    if want != prior:
                        from brain.observability.decisions import decisions

                        features["requires_action"] = want
                        trace.approach_override = "added_action" if want else "suppressed_action"
                        decisions.log(
                            "approach_override_requires_action",
                            turn_id=turn_id,
                            cluster="frontal",
                            from_value=prior,
                            to_value=want,
                            information_need=approach.get("information_need", ""),
                            stance=str(approach.get("stance", ""))[:120],
                            confidence=round(float(approach.get("confidence", 0.0)), 2),
                        )
                        logger.info(
                            "[Approach] requires_action %s → %s (%s)",
                            prior,
                            want,
                            approach.get("information_need"),
                        )
            # Stash for next-turn grounded verification + record every candidate
            # pair (plays for all, wins for the selected — counts, never a rate).
            with contextlib.suppress(Exception):
                from brain.approach_outcome import PendingApproach
                from brain.stance_pairs import record_candidate

                if not hasattr(self, "_stance_pairs"):
                    self._stance_pairs = {}
                for _c in approach.get("scores", []) or []:
                    record_candidate(
                        self._stance_pairs,
                        str(_c.get("info_id", "")),
                        str(_c.get("method_id", "")),
                        won=bool(_c.get("selected")),
                    )
                self._pending_approach = PendingApproach(
                    turn_id=turn_id,
                    information_need=str(approach.get("information_need", "")),
                    info_id=str(approach.get("info_id", "")),
                    method_id=str(approach.get("method_id", "")),
                    override=trace.approach_override,
                    query_vec=list(_query_vec or []),
                    topic=str(features.get("topic_summary", "") or ""),
                )
        else:
            trace.approach_bypassed = True

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
                elif inline_tools:
                    # Request/response transport (POST /turns, /turns/stream): the caller
                    # gets one synchronous (text, affect) and has no channel for a later
                    # result — a deferred one would be lost (the caller would only ever see
                    # "I'm working on this"). Run the reactive tool INLINE and fold its
                    # output into the drafter context, like a confirmed cloud_action above.
                    memory["tool_result"] = await self._run_motor_inline(features, turn_id)
                    logger.info("[MotorCortex] Reactive — inline (request/response transport)")
                else:
                    # Deferred: don't block the turn on motor planning/execution. Frontal
                    # produces an acknowledgment; the result surfaces out-of-band via
                    # proactive speech — the owner UI, and the WS engine transport, both
                    # forward it (ws.py forwards proactive_speech after turn_end).
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
                # Engine lane (a partner customer turn) gates the agenda to the
                # mandate's domain, so the persona's introspective off-time threads
                # never surface into a customer's conversation; a thread opened
                # while working the mandate's domain still can, when relevant. The
                # companion owner lane (no end_user_id) stays ungated.
                _domain_tags = None
                if features.get("end_user_id"):
                    _mid = features.get("mandate_id") or ""
                    if _mid:
                        from brain.mandates import catalog as _mandate_catalog
                        from brain.persona_context import mandate_domain_tags

                        _domain_tags = mandate_domain_tags(_mid, _mandate_catalog())
                    else:
                        _domain_tags = set()  # customer turn, no mandate → surface nothing
                _routed = self.dmn.route_threads_for_turn(
                    _activity, budget=_budget, domain_tags=_domain_tags
                )
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
                from brain.persona_key import active_or_home_persona

                _persona = active_or_home_persona()
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
        # Avoidance gate (shadow unless avoidance_gate=1): accumulate per-entity
        # avoidance evidence off parietal's freshly-updated entity map, and learn from
        # any re-engagement. No-op when evidence_gates is off; never breaks the turn.
        if settings.get("evidence_gates", 0) and self.meta is not None:
            with contextlib.suppress(Exception):
                self.meta._avoidance.observe_turn(
                    current_entities=set(features.get("entities", []) or []),
                    stale_entities=self.parietal.entity_last_seen(),
                    turn_count=self.parietal.turn_count,
                    user_emotion=(features.get("user_emotion") or features.get("emotion") or ""),
                    bus=self.bus,
                    agent_text=final,  # so a flagged topic the reply surfaces can be confirmed
                    store=self.bus.evidence,
                )
        # Judge-host attachments (brain/judge_attachment.py): grade the PREVIOUS turn's
        # judge claims against what actually happened this turn — the cross-turn signal
        # that replaces the within-turn drafting competition a judge does not have — and
        # establish any shadow candidate whose evidence gate has committed. Runs here,
        # after parietal's update, so the observed affect is this turn's. No-op when the
        # feature is off or BRAIN_WIRING_FROZEN; never breaks the turn.
        with contextlib.suppress(Exception):
            self.frontal._judge_attach.observe_turn(
                getattr(self.bus, "evidence", None),
                self.bus,
                user_emotion=(features.get("user_emotion") or features.get("emotion") or ""),
                sentiment=float(features.get("sentiment", 0.0) or 0.0),
                grade_lookup=self._judge_grade_lookup,
                turn_count=self.parietal.turn_count,
                wiring=self.frontal._wiring,
            )
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
            # Skip while a NON-resting pair is bound (engine turn in a client's or
            # bound persona's mood): snapshotting the bus then would persist that
            # transient state as the HOME persona's evolved chemistry — a client's
            # mood leaking into the persona's durable profile. Client pairs persist
            # through their own registry; the resting pair is what this save owns.
            if _persona and not self.bus.is_bound:
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

        if self._follow_through and answer_only:
            # Answer-only turn: never mint background work off it — neither the
            # task-mode deposit (requires_action was neutralized, so none exists)
            # nor a commitment extracted from the drafted answer's phrasing.
            logger.debug("[AnswerOnly] FollowThrough suppressed (answer-only turn)")
        elif self._follow_through:

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
                        # source="commitment", NOT "user": the executive did NOT
                        # classify this turn as a task (no deposit above) — the
                        # assistant volunteered the work in its own phrasing. Such
                        # jobs must respect the autonomy rate caps + spend gate;
                        # stamping them "user" let a chatty session mint uncapped
                        # background jobs (the 2026-07-03 debate cascade).
                        self._task_queue.enqueue(goal, source="commitment", priority=1)
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

        # Crash-safety: durably journal this turn's trace so an ungraceful exit
        # (OOM/SIGKILL) before the next consolidation can't drop its learning. A
        # graceful exit consolidates the buffer; this covers the rest. Boot replay
        # re-stages anything a crash left behind. Guarded — never breaks the turn.
        with contextlib.suppress(Exception):
            from brain.observability import trace_journal

            trace_journal.append(trace, self._session_traces[-1])

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

        # Surface the turn id so a request/response transport can hand it back to the
        # partner (POST /turns returns it as resp["turn_id"]). It's the handle a later
        # external grade needs — POST /turns/{turn_id}/grade. Not sensitive: the same
        # id already flows through the SSE events and the eval log. The curated
        # affect/mood views (_affect.py) read only emotion/user_emotion, so this extra
        # key never crosses the chemistry-not-exposed boundary.
        if isinstance(affect, dict):
            affect["turn_id"] = turn_id

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

    def _genuine_mood(self) -> dict:
        """The brain's genuine current emotion, re-derived from live state at this
        moment — the mood OUTPUT only ({"emotion": <label>}), never the underlying
        chemistry. Proactive results voice the entity's real feeling (already shaped
        by the reward appraisal that just ran), not a hardcoded label; and only the
        label crosses the boundary, so the chemical model stays un-reverse-engineerable.
        Falls back to neutral if the hypothalamus is unavailable."""
        emotion = "neutral"
        with contextlib.suppress(Exception):
            emotion = self.hypothalamus.refresh_emotion()[0] or "neutral"
        return {"emotion": emotion}

    async def _deliver_proactive(self, text: str, mood: dict, *, partner_target: str = "") -> None:
        """Deliver an unprompted message (a finished job's result, a clarification, a
        failure) on the correct two channels — the single place the proactive lane
        gate lives, so callers can't drift back to a weaker check.

          • Partner / observed delivery (``emit_proactive_speech``) ALWAYS fires when
            an emitter is present: the owning tenant may be away, which is exactly when
            a completed job's result matters most. Best-effort.
          • Local TTS (``pns.emit``) fires ONLY when ``_proactive_voice_allowed()`` —
            a connected listener AND the owner lane. On the agent lane (a third-party
            app driving this agent through the engine API) the brain stays silent;
            anyone in that app is observing the run, not conversing with it. Best-effort.

        Owner-lane behaviour is unchanged (there the lane check reduces to the existing
        listener gate); only the agent lane is held silent, which is the contract
        locked by tests/test_proactive_voice_lane_gate.py.
        """
        if self._emitter:
            with contextlib.suppress(Exception):
                await self._emitter.emit_proactive_speech(
                    text, affect=mood, partner_target=partner_target
                )
        # Durable outbound copy. emit_proactive_speech reaches whoever is CONNECTED
        # right now; a subscriber that is asleep, restarting, or simply a server gets
        # nothing from it. The signed webhook outbox survives that — it retries, backs
        # off and dead-letters — which is what makes "the brain told you something
        # while you were away" actually arrive. Best-effort by construction.
        with contextlib.suppress(Exception):
            from brain.api import webhooks

            webhooks.enqueue_for_current_lane(
                "message.proactive",
                {"text": text, "affect": mood or {}},
            )
        if self._proactive_voice_allowed():
            with contextlib.suppress(Exception):
                await self.pns.emit(text, mood)

    def _push_task_result(self, goal: str, state: str, summary_text: str) -> None:
        """Append one entry to the agent-awareness ring buffer for ANY terminal state,
        with a state token the frontal `completed_tasks` fence renders — so a deferred /
        stopped / awaiting job stays visible to the agent (never re-planned or forgotten),
        not just completed/failed ones."""
        self._recent_task_results.append(
            {
                "goal": goal,
                "summary": summary_text,
                "state": state,
                "success": state == "completed",
                "ts": time.time(),
            }
        )
        while len(self._recent_task_results) > 5:
            self._recent_task_results.pop(0)

    async def _run_task(self, task) -> None:
        """Run a queued job under its originating lane.

        Two buckets (see task_queue.Task.origin_*): a job that descends from an
        agent-lane turn runs bound to that agent, so every event it emits — the
        work tray, the spoken result, the reflection it seeds — is tagged with
        that agent and kept out of the owner's main feed (observable instead as
        that agent's self-directed work). The brain's OWN idle/self-directed jobs
        carry no agent origin and run on the owner lane, exactly as before — its
        private inner life, visible only in its own UI."""
        from brain.turn_ctx import bind_turn

        if getattr(task, "origin_channel", "owner") == "agent" and getattr(
            task, "origin_session_id", ""
        ):
            # Bind the originating agent's PERSONA too (agent_id = "persona.mandate"),
            # so the job's completion rewards scale by that persona's temperament and
            # its memory writes stay in that persona's stores — not the home persona's
            # (the turn-lane half of the 2026-07 persona-misattribution fix).
            _agent_id = getattr(task, "origin_agent_id", "") or ""
            _task_persona = _agent_id.split(".", 1)[0] if "." in _agent_id else ""
            from brain.second_brain.store import bind_persona

            with (
                bind_persona(_task_persona),
                bind_turn(
                    "agent",
                    session_id=task.origin_session_id,
                    agent_id=_agent_id or None,
                    end_user_id=getattr(task, "origin_end_user_id", ""),
                    partner_id=getattr(task, "origin_partner_id", ""),
                ),
            ):
                await self._run_task_body(task)
            return
        # Owner lane. A DMN self-task carries the persona whose tick produced it: its
        # queue is shared across the roster, so without this the job would run unbound
        # (i.e. as home) and every persona's self-directed learning would be attributed
        # to home. Same binding the agent lane above does, for the same reason.
        _origin_persona = getattr(task, "origin_persona", "") or ""
        if _origin_persona:
            from brain.second_brain.store import bind_persona

            with bind_persona(_origin_persona):
                await self._run_task_body(task)
        else:
            await self._run_task_body(task)

    async def _run_task_body(self, task) -> None:
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
        # A user-awaited job: the user asked for this (the agent deferred a tool/research
        # step needed to answer) and is WAITING. Its result must always come back — the
        # on_topic surfacing heuristic governs only the brain's discretionary autonomous
        # work, never an answer someone is waiting on.
        is_user = getattr(task, "source", "") == "user"
        if is_self:
            self.router.enter_background_mode()
        if is_autonomous:
            self.motor.enter_self_mode()
        # Job-scope approval grant: this task is the re-queue of a user-approved
        # action, so every ask the job raises is pre-authorized (_gate_action).
        # Held for exactly this job's run; revoked below so it dies with the job.
        self._job_approval_token = str(getattr(task, "approval_token", "") or "")
        try:
            summary = await self.motor.execute_internal_job(
                task.goal, job_turn_id, source=getattr(task, "source", "self")
            )
        except Exception as _e:
            logger.warning("[TaskWorker] Task [%s] execution failed: %s", task.id, _e)
            self._task_queue.mark_done(task.id, success=False)
            if self.dmn and self.dmn.is_project_task(task.id):
                with contextlib.suppress(Exception):
                    await self.dmn.note_project_complete(task.id, False, "execution error")
            # The user is waiting on this — never leave them with silence. Tell them it
            # failed (partner delivery isn't gated on a local listener; the tenant may
            # be away). Autonomous work fails quietly and only feeds reflection below.
            if is_user:
                _fail_msg = "I ran into an error working on that and couldn't finish it."
                await self._deliver_proactive(
                    _fail_msg,
                    {"emotion": "concerned"},
                    partner_target=self._partner_proactive_target(),
                )
            # Feed the crash back into reflection so the entity can react to / act on
            # the failure (result→reasoning loop; the DMN decides what, if anything).
            if self.dmn is not None:
                with contextlib.suppress(Exception):
                    self.dmn.note_job_result(
                        task.goal,
                        f"crashed: {_e}",
                        False,
                        depth=getattr(task, "reflex_depth", 0),
                        already_reported=is_user,
                    )
            return
        finally:
            _grant, self._job_approval_token = self._job_approval_token, ""
            if _grant and getattr(self, "_approvals", None) is not None:
                with contextlib.suppress(Exception):
                    self._approvals.revoke_token(_grant)
            if is_autonomous:
                self.motor.exit_self_mode()
            if is_self:
                self.router.exit_background_mode()

        on_topic = self._task_is_on_topic(task.goal)
        # Always surface a user-awaited answer; on_topic only gates autonomous work.
        should_report = on_topic or is_user

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
            if should_report:
                # Partner delivery isn't gated on a local listener (the user may be
                # waiting remotely in the copilot); local TTS is gated by lane+listener.
                await self._deliver_proactive(
                    question,
                    self._genuine_mood(),
                    partner_target=self._partner_proactive_target(),
                )
            else:
                logger.info(
                    "[TaskWorker] Task [%s] clarification held — off-topic autonomous work "
                    "(will surface in context when relevant)",
                    task.id,
                )
            return

        # ── Terminal-state routing (brain.autonomy JobState) ──────────────────────
        # A job that DEFERRED (cloud unavailable / soft-budget pause) or was STOPPED
        # (hard budget) or is AWAITING_APPROVAL is not "done" — route it so it resumes
        # or waits, feed the agent's awareness (so it doesn't re-plan or forget it), and
        # never fire the failure/partner "ran into an error" path.
        _state = summary.get("state") or ("completed" if summary.get("success") else "failed")
        if _state in ("deferred", "stopped_budget", "awaiting_approval"):
            reason = summary.get("reason_human") or _state.replace("_", " ")
            if _state == "deferred":
                backoff = float(summary.get("backoff_s") or 30.0)
                self._task_queue.mark_deferred(task.id, backoff_s=backoff, reason=reason)
            else:
                self._task_queue.mark_blocked(task.id, reason=reason)
            self._push_task_result(task.goal, _state, reason)
            # Feed awareness to reflection WITHOUT spawning a follow-up: a paused job is
            # not finished, so it must not churn the bounded reflect→act loop.
            if self.dmn is not None:
                with contextlib.suppress(Exception):
                    self.dmn.note_job_result(
                        task.goal,
                        reason,
                        False,
                        depth=getattr(task, "reflex_depth", 0),
                        already_reported=True,
                    )
            # Surface stop/approval to the user (deferred is low-urgency: buffer only).
            if _state != "deferred" and should_report:
                with contextlib.suppress(Exception):
                    await self._deliver_proactive(
                        reason,
                        self._genuine_mood(),
                        partner_target=self._partner_proactive_target(),
                    )
            logger.info("[TaskWorker] Task [%s] → %s: %s", task.id, _state, reason[:80])
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
                # Re-mirror to the durable agent_jobs table so the spoken summary (and
                # task linkage) land there too, not just in the JSON store.
                if hasattr(self.motor, "_mirror_job_to_table"):
                    self.motor._mirror_job_to_table(job_id)
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
        self._push_task_result(
            task.goal, "completed" if summary.get("success") else "failed", spoken_summary
        )
        # Feed the outcome back into reflection (result→reasoning loop). The DMN reasons
        # over what just finished and decides — via the existing speak-gate / self-task /
        # deferred pathways — whether to act further, and (for autonomous work) whether to
        # surface it. already_reported=should_report tells it the answer has already gone
        # out for a user-awaited job, so it won't repeat it — only consider a follow-up.
        if self.dmn is not None:
            with contextlib.suppress(Exception):
                self.dmn.note_job_result(
                    task.goal,
                    spoken_summary,
                    bool(summary.get("success")),
                    depth=getattr(task, "reflex_depth", 0),
                    already_reported=should_report,
                )
        logger.info("[TaskWorker] Reporting result [%s]: %s", task.id, spoken_summary[:160])
        # The reward appraisal above already moved the chemistry, so the genuine mood now
        # reflects how this result actually landed — voice it, don't hardcode a label.
        mood = self._genuine_mood()
        if self._emitter:
            await self._emitter.emit_event(
                {
                    "type": "task_summary",
                    "job_id": summary.get("job_id"),
                    "summary": spoken_summary,
                }
            )
        if should_report:
            # Partner delivery isn't gated on a local listener (a user awaiting in the
            # copilot may be away); local TTS is gated by lane+listener in the helper.
            await self._deliver_proactive(
                spoken_summary,
                mood,
                partner_target=self._partner_proactive_target(),
            )
        else:
            logger.info(
                "[TaskWorker] Task [%s] result held from speech — off-topic autonomous "
                "work (will surface in LLM context on next turn)",
                task.id,
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

    async def _run_motor_inline(self, features: dict, turn_id: str) -> str:
        """Run a reactive motor action INLINE and return a tool-result string for the
        drafter (the value stored in memory["tool_result"]).

        A request/response engine transport (POST /turns, /turns/stream) returns one
        synchronous response and has no channel for a later result, so the deferred
        model (_run_motor_reactive → proactive_speech) would silently drop it. Running
        the tool before the response is drafted, and folding its output into the
        drafter context, is the only way a pasted URL (or any reactive tool) reaches
        such a caller. The WS transport keeps the deferred loop (inline_tools=False).
        Mirrors the confirmed cloud_action path: execute → set memory["tool_result"]
        → drafter synthesizes.

        Best-effort: any failure returns a short marker rather than raising into the
        turn, so the response still goes out. A cloud WRITE that needs confirmation is
        left parked on the executor's pending slot for api_turn to harvest.
        """
        _timeout = float(os.environ.get("BRAIN_MOTOR_INTERACTIVE_TIMEOUT_S", "30"))
        # How many tools a reactive turn runs INLINE before any remainder is handed to a
        # background job. 1 = answer the first part now, continue the rest out of band
        # (lowest synchronous latency). Raise to run more parts inline.
        _inline_cap = int(os.environ.get("BRAIN_MOTOR_INLINE_STEP_CAP", "1") or 1)
        self.motor.reset_turn(turn_id)
        goal = features.get("raw_text") or features.get("topic_summary", "")
        try:
            tool_result = await asyncio.wait_for(
                self.motor.execute(features, turn_id, inline_step_cap=_inline_cap),
                timeout=_timeout,
            )
        except Exception as _e:
            logger.error("[MotorCortex] Inline reactive failed: %s", _e)
            return "[tool_error] The action could not be completed."

        if not tool_result:
            return ""

        output = tool_result.get("output", "")
        tool_name = tool_result.get("tool", "tool")
        success = tool_result.get("success")

        # Stamp the tool outcome onto the pending approach so next turn's verifier
        # can grade the `external` call with ground truth (tri-state `success` plus
        # output length — empty-but-successful is a weak negative, not a win).
        _pap = getattr(self, "_pending_approach", None)
        if _pap is not None and success is not None:
            _pap.tool_success = bool(success)
            _pap.tool_output_len = len(str(output or ""))

        # Same intrinsic-correctness neuromod the deferred path applies on completion
        # (kept in sync with _run_motor_reactive): completing a task is a correctness
        # signal scaled by how much this persona values being right; failure drains 5HT.
        from brain.neuron import loss_aversion as _loss_aversion
        from brain.neuron import reward_weight as _reward_weight
        from brain.persona_key import active_or_home_persona as _aohp

        _tpersona = _aohp()
        _tw = _reward_weight(_tpersona, "correctness")
        _ter = float(settings.get("emotional_reactivity_scale"))
        if success is False:
            _tla = _loss_aversion(_tpersona)
            self.bus.neuromod.add("GABA", 0.08)
            self.bus.neuromod.add("NE", 0.06)
            self.bus.neuromod.add(
                "DA",
                -float(settings.get("correctness_penalty_base")) * _tw * _ter * _tla,
                reward_source="correctness",
                reason="tool_failure",
            )
            self.bus.hormonal.add(
                "5HT", -float(settings.get("correctness_5ht_drain")) * _tw * _ter * _tla
            )
        elif success:
            self.bus.neuromod.add(
                "DA",
                float(settings.get("correctness_reward_base")) * _tw * _ter,
                reward_source="correctness",
                reason="tool_success",
            )
            self.bus.neuromod.add("Glu", 0.04)
        with contextlib.suppress(Exception):
            if self._emitter:
                await self._emitter.emit_neuromod(self.bus.neuromod.snapshot())

        if tool_result.get("pending"):
            # A write needs confirmation. The executor has parked it on its pending
            # slot; api_turn moves it onto the durable session and surfaces it. Tell
            # the drafter to ask, and DON'T clear the slot here.
            desc = output.replace("CONFIRMATION_NEEDED:", "").strip()
            return f"[action_pending] Ready to: {desc}. Ask the user to confirm before proceeding."

        # Record for "what did you just do" follow-ups, same as the deferred path.
        self._recent_task_results.append(
            {"goal": goal, "summary": output[:500], "success": bool(success), "ts": time.time()}
        )
        if len(self._recent_task_results) > 3:
            self._recent_task_results.pop(0)

        # Multi-part request: the inline cap was hit with the planner still wanting to
        # act. Hand the remainder to a background job the user is awaiting (source=user
        # → guaranteed delivery + bypasses the autonomy rate caps) and tell the drafter
        # the first part is done and the rest is continuing out of band.
        more = ""
        if tool_result.get("more_pending"):
            remaining = tool_result.get("remaining_goal") or goal
            q = getattr(self, "_task_queue", None)
            if q is not None:
                with contextlib.suppress(Exception):
                    q.enqueue(remaining, source="user", priority=1)
                    logger.info(
                        "[MotorCortex] Inline remainder handed to a background user job: %s",
                        remaining[:120],
                    )
                    more = (
                        "\n[continuing] The first part is handled above. The rest is a separate "
                        "task now running in the background — tell the user you're on it and the "
                        "result will follow; don't repeat the first part."
                    )

        if not output:
            return more.strip()
        # Raw tool output as drafter context (NOT shown verbatim to the user — the
        # drafter synthesizes it into the reply). fetch_url output already carries its
        # own UNTRUSTED-EXTERNAL-CONTENT markers from the dispatcher.
        return f"[{tool_name}]\n{output}{more}"

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
        from brain.persona_key import active_or_home_persona as _aohp

        _tpersona = _aohp()
        _tw = _reward_weight(_tpersona, "correctness")
        _ter = float(settings.get("emotional_reactivity_scale"))
        if success is False:
            # A failed task is a LOSS — scale the dip/drain by loss aversion (λ), one-sided
            # (the success branch below stays unweighted), independent of correctness valuation.
            _tla = _loss_aversion(_tpersona)
            self.bus.neuromod.add("GABA", 0.08)
            self.bus.neuromod.add("NE", 0.06)
            self.bus.neuromod.add(
                "DA",
                -float(settings.get("correctness_penalty_base")) * _tw * _ter * _tla,
                reward_source="correctness",
                reason="tool_failure",
            )
            self.bus.hormonal.add(
                "5HT", -float(settings.get("correctness_5ht_drain")) * _tw * _ter * _tla
            )
        elif success:
            self.bus.neuromod.add(
                "DA",
                float(settings.get("correctness_reward_base")) * _tw * _ter,
                reward_source="correctness",
                reason="tool_success",
            )
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
            # Deliver to the owning partner + the observed lane regardless of who drove
            # the turn; _deliver_proactive only VOICES it aloud on the owner lane (the
            # agent lane stays silent in the partner app — observing, not conversing).
            # The tool's reward/outcome already moved the chemistry, so the genuine mood
            # reflects how it landed — voice that, not a hardcoded success/fail label.
            await self._deliver_proactive(msg, self._genuine_mood())


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
