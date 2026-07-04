-- 022: answer-only sessions.
--
-- An answer_only session is a caller-declared contract: every turn of it is
-- synchronous Q&A — the brain drafts an answer and does NOTHING else. No motor
-- dispatch (and therefore no muscle-memory open-loop execution), no FollowThrough
-- task enqueue. Built for orchestrated multi-agent flows (e.g. the trading-debate
-- seats) whose prompts merely SOUND like work requests; the 2026-07-03 AAPL debate
-- showed prompt-level prohibition cannot reach muscle memory or FollowThrough,
-- which never read prompt text. Opt-in and per-session: unflagged sessions are
-- byte-for-byte unchanged.
--
-- APPLY BEFORE deploying the code that writes this column: ApiSessionRegistry
-- persistence upserts the full row, and an unknown column fails the (best-effort)
-- upsert — sessions would silently stop persisting across restarts until applied.

alter table api_sessions
  add column if not exists answer_only boolean not null default false;
