ALTER TABLE sessions
  ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE sessions
  ADD CONSTRAINT sessions_tenant_id_nonempty CHECK (btrim(tenant_id) <> '');
ALTER TABLE sessions
  DROP CONSTRAINT sessions_platform_conversation_id_key;
ALTER TABLE sessions
  ADD CONSTRAINT sessions_tenant_platform_conversation_key
  UNIQUE (tenant_id, platform, conversation_id);

CREATE INDEX sessions_tenant_updated_idx
  ON sessions(tenant_id, updated_at, id);
CREATE INDEX runs_session_status_idx ON runs(session_id, status);
CREATE INDEX messages_session_created_idx
  ON messages(session_id, created_at, id);
CREATE INDEX checkpoints_run_created_idx
  ON checkpoints(run_id, created_at, id);

CREATE TABLE attachments (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  storage_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX attachments_tenant_created_idx
  ON attachments(tenant_id, created_at, id);

CREATE TABLE admin_operations (
  operation_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  result_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX admin_operations_tenant_idx
  ON admin_operations(tenant_id, created_at);

CREATE TABLE audit_events (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_id TEXT NOT NULL CHECK (btrim(actor_id) <> ''),
  action TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
  operation_id TEXT NOT NULL UNIQUE REFERENCES admin_operations(operation_id),
  outcome TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  details_json JSONB NOT NULL
);
CREATE INDEX audit_events_tenant_created_idx
  ON audit_events(tenant_id, created_at, id);
