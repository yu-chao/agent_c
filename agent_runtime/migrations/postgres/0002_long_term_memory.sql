CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  root_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  content TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_platform TEXT NOT NULL,
  source_message_id TEXT NOT NULL,
  source_subject TEXT NOT NULL,
  source_session_id TEXT,
  confidence DOUBLE PRECISION NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  created_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ,
  visibility TEXT NOT NULL,
  conversation_id TEXT,
  tenant_id TEXT,
  deleted_at TIMESTAMPTZ,
  superseded_by_id TEXT REFERENCES memories(id),
  CHECK(visibility IN ('private', 'conversation', 'tenant')),
  CHECK(visibility <> 'conversation' OR conversation_id IS NOT NULL),
  CHECK(visibility <> 'tenant' OR tenant_id IS NOT NULL)
);
CREATE INDEX memories_subject_active_idx
  ON memories(subject, deleted_at, expires_at);
CREATE INDEX memories_visibility_idx
  ON memories(visibility, conversation_id, tenant_id);
CREATE INDEX memories_root_idx ON memories(root_id);
