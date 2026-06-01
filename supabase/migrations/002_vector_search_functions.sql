-- pgvector similarity search functions used by EpisodicStore._sb_recall

-- General episode search (cosine similarity, user+persona scoped)
create or replace function match_episodes(
  query_vector vector(768),
  user_id_param uuid,
  persona_param text,
  match_count int,
  exclude_tags text[] default null
)
returns table (
  id bigint,
  session_id text,
  turn_id text,
  ts float,
  user_input text,
  entity_response text,
  topic_tags text[],
  emotion_state text,
  user_emotion text,
  entities text[],
  neuromod_snapshot jsonb,
  surprise_score float,
  similarity float
)
language sql stable
as $$
  select
    id, session_id, turn_id, ts,
    user_input, entity_response,
    topic_tags, emotion_state, user_emotion,
    entities, neuromod_snapshot, surprise_score,
    1 - (vector <=> query_vector) as similarity
  from episodes
  where
    user_id = user_id_param
    and persona = persona_param
    and (exclude_tags is null or not (topic_tags && exclude_tags))
  order by vector <=> query_vector
  limit match_count;
$$;

-- Tag-scoped episode search
create or replace function match_episodes_by_tag(
  query_vector vector(768),
  user_id_param uuid,
  persona_param text,
  tag_param text,
  match_count int
)
returns table (
  id bigint,
  session_id text,
  turn_id text,
  ts float,
  user_input text,
  entity_response text,
  topic_tags text[],
  emotion_state text,
  user_emotion text,
  entities text[],
  neuromod_snapshot jsonb,
  surprise_score float,
  similarity float
)
language sql stable
as $$
  select
    id, session_id, turn_id, ts,
    user_input, entity_response,
    topic_tags, emotion_state, user_emotion,
    entities, neuromod_snapshot, surprise_score,
    1 - (vector <=> query_vector) as similarity
  from episodes
  where
    user_id = user_id_param
    and persona = persona_param
    and tag_param = any(topic_tags)
  order by vector <=> query_vector
  limit match_count;
$$;
