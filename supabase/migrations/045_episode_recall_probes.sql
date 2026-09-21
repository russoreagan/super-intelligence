-- 045 — give episode recall enough ivfflat probes to survive a filtered search.
--
-- WHY. 044 added `and (embed_model_param is null or embed_model = embed_model_param)`
-- to both search functions so a query never compares vectors from two different
-- models. Correct, but it is a POST-filter: pgvector's ivfflat index picks the
-- closest `probes` lists first and the WHERE clause is applied to what comes back.
--
-- The index (007_org_schema_reset.sql) is `ivfflat (vector vector_cosine_ops)
-- with (lists = 100)`, and Postgres defaults `ivfflat.probes` to 1 — so a scan
-- touches roughly 1% of the table. With production split ~9,100 gemini rows to
-- ~875 nomic, a same-model search probed ~113 rows and found ~9 candidates, then
-- filtered org, persona and end_user on top of that. Searches returned FEWER rows
-- than match_count, often zero, while matching rows sat in the table.
--
-- A partial index on embed_model would not help: the RPC compares against a
-- PARAMETER, so the planner cannot prove the query implies the index predicate and
-- will not use it. Raising probes is the fix that works for a parameterised filter.
--
-- probes = 10 is the usual sqrt(lists) starting point. On an 11k-row table the
-- extra scan cost is negligible next to silently missing memories. Set as a
-- FUNCTION attribute so it applies to these two queries only and needs no
-- per-session GUC from the client.
--
-- Running scripts/reembed_episodes.py remains the actual repair — this makes recall
-- robust during the transition, and keeps it robust the next time the corpus is
-- mid-migration.
--
-- `create or replace` (no drop): the signatures are unchanged from 044, and a
-- replace can add a SET clause.

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
set ivfflat.probes = 10
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
set ivfflat.probes = 10
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
