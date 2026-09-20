-- 044: one embedding space per row, and say which one.
--
-- Two embedding models have written into episodes.vector: Google's
-- gemini-embedding-001 (truncated to 768) until 2026-09-13, and Ollama's
-- nomic-embed-text since the CPU embed sidecar started working. Both are
-- 768-dimensional and nothing recorded which produced a row, so a nomic query
-- ranked Google rows by a distance that means nothing — memory search quietly
-- stopped finding the older half of itself.
--
-- The model is now stored per row and the search functions only compare rows
-- embedded by the model asking. Existing rows are labelled by vector norm,
-- which separates the two cleanly: gemini truncated to 768 lands at ~0.59,
-- raw nomic at ~18-22.
--
-- Failed embeds were written as a 768-wide zero vector, which pgvector can
-- never return from a cosine search (the distance is NaN) — those become a
-- NULL vector, the honest "not embedded", and the repair script can find them.

alter table episodes add column if not exists embed_model text;

update episodes
   set embed_model = case
     when vector is null then null
     when vector_norm(vector) < 0.5 then null              -- zero vector: never embedded
     when vector_norm(vector) < 1.5 then 'gemini-embedding-001'
     else 'nomic-embed-text'
   end
 where embed_model is null;

update episodes
   set vector = null
 where vector is not null
   and vector_norm(vector) < 0.5;

comment on column episodes.embed_model is
  'Embedding model that produced vector (nomic-embed-text | gemini-embedding-001). '
  'NULL = no vector: the embed failed and the row awaits repair. Search compares '
  'only rows whose model matches the querying model.';

-- Pruning idle thoughts by age (brain/sleep.py) scans org + ts, and the existing
-- index leads with persona, which the prune does not know.
create index if not exists episodes_org_ts_idx on episodes(org_id, ts);

-- The two search functions gain embed_model_param. Dropped first: an sql
-- function's signature cannot be changed by create or replace.
drop function if exists match_episodes(vector, uuid, text, int, text[], text);
drop function if exists match_episodes_by_tag(vector, uuid, text, text, int, text);

create or replace function match_episodes(
  query_vector vector(768),
  org_id_param uuid,
  persona_param text,
  match_count int,
  exclude_tags text[] default null,
  end_user_param text default null,
  embed_model_param text default null
)
returns table (
  id bigint, session_id text, turn_id text, ts float,
  user_input text, entity_response text,
  topic_tags text[], emotion_state text, user_emotion text,
  entities text[], neuromod_snapshot jsonb, surprise_score float,
  cog_signature jsonb, mandate_id text, end_user_id text,
  similarity float
)
language sql stable
as $$
  select
    id, session_id, turn_id, ts,
    user_input, entity_response,
    topic_tags, emotion_state, user_emotion,
    entities, neuromod_snapshot, surprise_score,
    cog_signature, mandate_id, end_user_id,
    1 - (vector <=> query_vector) as similarity
  from episodes
  where
    org_id = org_id_param
    and persona = persona_param
    and vector is not null
    and (embed_model_param is null or embed_model = embed_model_param)
    and (end_user_param is null or end_user_id = end_user_param)
    and (exclude_tags is null or not (topic_tags && exclude_tags))
  order by vector <=> query_vector
  limit match_count;
$$;

create or replace function match_episodes_by_tag(
  query_vector vector(768),
  org_id_param uuid,
  persona_param text,
  tag_param text,
  match_count int,
  end_user_param text default null,
  embed_model_param text default null
)
returns table (
  id bigint, session_id text, turn_id text, ts float,
  user_input text, entity_response text,
  topic_tags text[], emotion_state text, user_emotion text,
  entities text[], neuromod_snapshot jsonb, surprise_score float,
  cog_signature jsonb, mandate_id text, end_user_id text,
  similarity float
)
language sql stable
as $$
  select
    id, session_id, turn_id, ts,
    user_input, entity_response,
    topic_tags, emotion_state, user_emotion,
    entities, neuromod_snapshot, surprise_score,
    cog_signature, mandate_id, end_user_id,
    1 - (vector <=> query_vector) as similarity
  from episodes
  where
    org_id = org_id_param
    and persona = persona_param
    and vector is not null
    and (embed_model_param is null or embed_model = embed_model_param)
    and (end_user_param is null or end_user_id = end_user_param)
    and tag_param = any(topic_tags)
  order by vector <=> query_vector
  limit match_count;
$$;
