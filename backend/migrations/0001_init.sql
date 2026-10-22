-- Skill Router — initial schema (SQLite)
-- Conventions:
--   * TEXT ids are opaque (uuid4 hex, or prefixed: cap_, inv_, task_, req_)
--   * TEXT timestamps are ISO-8601 UTC with microseconds and a trailing 'Z'
--   * JSON columns are TEXT holding a JSON document
--   * foreign_keys=ON, journal_mode=WAL are set on every connection (see app/core/db.py)

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------- identity --
CREATE TABLE IF NOT EXISTS users (
    id                    TEXT PRIMARY KEY,
    username              TEXT NOT NULL UNIQUE,
    password_hash         TEXT NOT NULL,
    role                  TEXT NOT NULL DEFAULT 'admin',
    must_change_password  INTEGER NOT NULL DEFAULT 0,
    disabled              INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    password_changed_at   TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,          -- sha256 of the cookie token
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    ip            TEXT,
    user_agent    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS login_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL,
    ip          TEXT,
    success     INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_lookup ON login_attempts(username, ip, created_at);

-- --------------------------------------------------------------- providers --
CREATE TABLE IF NOT EXISTS providers (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    slug         TEXT NOT NULL UNIQUE,
    category     TEXT NOT NULL DEFAULT 'llm',   -- llm|image|video|speech|transcription|generic
    base_url     TEXT,
    docs_url     TEXT,
    tags         TEXT NOT NULL DEFAULT '[]',
    environment  TEXT NOT NULL DEFAULT 'production', -- production|staging|development
    notes        TEXT,
    status       TEXT NOT NULL DEFAULT 'active',     -- active|disabled
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    id                 TEXT PRIMARY KEY,
    provider_id        TEXT NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    name               TEXT NOT NULL,
    auth_type          TEXT NOT NULL,   -- api_key|bearer|basic|custom_header
    -- AES-256-GCM envelope: {"v":1,"key_id":"k1","nonce":"b64","data":"b64"}
    -- The cleartext field map (e.g. {"api_key":"..."}) never leaves the service.
    secret_ciphertext  TEXT NOT NULL,
    hint               TEXT NOT NULL DEFAULT '',  -- masked preview, never a secret
    field_names        TEXT NOT NULL DEFAULT '[]',
    config             TEXT NOT NULL DEFAULT '{}', -- header name, basic username, scheme, etc.
    key_id             TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active', -- active|disabled|expired
    expires_at         TEXT,
    last_verified_at   TEXT,
    last_verify_status TEXT,
    last_verify_message TEXT,
    last_rotated_at    TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credentials_provider ON credentials(provider_id);

-- ------------------------------------------------------------ capabilities --
CREATE TABLE IF NOT EXISTS capabilities (
    id                 TEXT PRIMARY KEY,
    slug               TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL,
    type               TEXT NOT NULL DEFAULT 'generic', -- text|image|video|speech|transcription|generic
    description        TEXT NOT NULL DEFAULT '',
    provider_id        TEXT REFERENCES providers(id) ON DELETE SET NULL,
    credential_id      TEXT REFERENCES credentials(id) ON DELETE SET NULL,
    status             TEXT NOT NULL DEFAULT 'draft',   -- draft|testing|published|disabled
    current_version_id TEXT,
    needs_live_agent   INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capabilities_status ON capabilities(status);
CREATE INDEX IF NOT EXISTS idx_capabilities_provider ON capabilities(provider_id);

CREATE TABLE IF NOT EXISTS capability_versions (
    id             TEXT PRIMARY KEY,
    capability_id  TEXT NOT NULL REFERENCES capabilities(id) ON DELETE CASCADE,
    version        INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft',   -- draft|testing|published|deprecated
    config         TEXT NOT NULL DEFAULT '{}',      -- full request/response contract, see docs/CONTRACT.md
    notes          TEXT,
    checksum       TEXT NOT NULL DEFAULT '',
    created_by     TEXT,
    created_at     TEXT NOT NULL,
    published_at   TEXT,
    UNIQUE(capability_id, version)
);
CREATE INDEX IF NOT EXISTS idx_cap_versions_capability ON capability_versions(capability_id, version DESC);

CREATE TABLE IF NOT EXISTS capability_tests (
    id             TEXT PRIMARY KEY,
    capability_id  TEXT NOT NULL REFERENCES capabilities(id) ON DELETE CASCADE,
    version_id     TEXT REFERENCES capability_versions(id) ON DELETE SET NULL,
    kind           TEXT NOT NULL,   -- connectivity|sample_call|schema_validation
    status         TEXT NOT NULL,   -- passed|failed|blocked
    request        TEXT NOT NULL DEFAULT '{}',  -- redacted
    response       TEXT NOT NULL DEFAULT '{}',  -- redacted, truncated
    latency_ms     INTEGER,
    http_status    INTEGER,
    error          TEXT,
    actor          TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capability_tests_lookup ON capability_tests(capability_id, created_at DESC);

-- Routing: several compatible offers per standard capability.
CREATE TABLE IF NOT EXISTS routes (
    id                    TEXT PRIMARY KEY,
    capability_id         TEXT NOT NULL REFERENCES capabilities(id) ON DELETE CASCADE,
    capability_version_id TEXT NOT NULL REFERENCES capability_versions(id) ON DELETE CASCADE,
    provider_id           TEXT REFERENCES providers(id) ON DELETE SET NULL,
    credential_id         TEXT REFERENCES credentials(id) ON DELETE SET NULL,
    strategy              TEXT NOT NULL DEFAULT 'priority', -- priority|fixed|health_first
    priority              INTEGER NOT NULL DEFAULT 100,
    weight                INTEGER NOT NULL DEFAULT 1,
    enabled               INTEGER NOT NULL DEFAULT 1,
    health_status         TEXT NOT NULL DEFAULT 'unknown',  -- unknown|healthy|degraded|unhealthy
    last_health_at        TEXT,
    conditions            TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routes_capability ON routes(capability_id, enabled, priority);

-- --------------------------------------------------------------- execution --
CREATE TABLE IF NOT EXISTS invocations (
    id                TEXT PRIMARY KEY,
    request_id        TEXT NOT NULL UNIQUE,
    caller_type       TEXT NOT NULL,        -- user|agent|system
    agent_id          TEXT REFERENCES agents(id) ON DELETE SET NULL,
    surface           TEXT NOT NULL DEFAULT 'admin', -- admin|http|mcp|cli|agent
    capability_id     TEXT REFERENCES capabilities(id) ON DELETE SET NULL,
    version_id        TEXT REFERENCES capability_versions(id) ON DELETE SET NULL,
    provider_id       TEXT REFERENCES providers(id) ON DELETE SET NULL,
    route_id          TEXT,
    route_reason      TEXT,
    mode              TEXT NOT NULL DEFAULT 'sync', -- sync|async|stream
    status            TEXT NOT NULL,        -- queued|running|succeeded|failed|cancelled|uncertain|rejected
    http_status       INTEGER,
    latency_ms        INTEGER,
    retries           INTEGER NOT NULL DEFAULT 0,
    task_id           TEXT,
    idempotency_key   TEXT,
    request_digest    TEXT,
    request_preview   TEXT,
    response_preview  TEXT,
    usage             TEXT NOT NULL DEFAULT '{}',
    cost_estimate_usd REAL,
    cost_actual_usd   REAL,
    error_code        TEXT,
    error_message     TEXT,
    created_at        TEXT NOT NULL,
    finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_invocations_created ON invocations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invocations_capability ON invocations(capability_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invocations_agent ON invocations(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invocations_status ON invocations(status);
CREATE INDEX IF NOT EXISTS idx_invocations_task ON invocations(task_id);

CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    request_id        TEXT NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'capability_async',
    capability_id     TEXT REFERENCES capabilities(id) ON DELETE SET NULL,
    version_id        TEXT REFERENCES capability_versions(id) ON DELETE SET NULL,
    provider_id       TEXT REFERENCES providers(id) ON DELETE SET NULL,
    route_id          TEXT,
    agent_id          TEXT REFERENCES agents(id) ON DELETE SET NULL,
    upstream_task_id  TEXT,
    status            TEXT NOT NULL DEFAULT 'queued',
    -- queued|running|succeeded|failed|cancel_requested|cancelled|uncertain
    progress          INTEGER NOT NULL DEFAULT 0,
    progress_note     TEXT,
    input             TEXT NOT NULL DEFAULT '{}',   -- redacted at rest
    result            TEXT NOT NULL DEFAULT '{}',
    error             TEXT,
    idempotency_key   TEXT,
    fingerprint       TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 3,
    budget_usd        REAL,
    cost_actual_usd   REAL,
    poll_after        TEXT,
    deadline          TEXT,
    lease_owner       TEXT,
    lease_until       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_idem ON tasks(idempotency_key);

-- Generic durable queue for the worker (agent runs, file parsing, imports).
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued|running|succeeded|failed|cancelled
    priority      INTEGER NOT NULL DEFAULT 100,
    run_at        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    lease_owner   TEXT,
    lease_until   TEXT,
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_pick ON jobs(status, run_at, priority);

-- ------------------------------------------------------------- agent access --
CREATE TABLE IF NOT EXISTS agents (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    slug         TEXT NOT NULL UNIQUE,
    description  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active', -- active|disabled
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    revoked_at   TEXT
);

CREATE TABLE IF NOT EXISTS agent_tokens (
    id                TEXT PRIMARY KEY,
    agent_id          TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    token_prefix      TEXT NOT NULL,          -- "sr_live_ab12…" shown in the UI
    token_hash        TEXT NOT NULL UNIQUE,   -- sha256 of the full token
    scopes            TEXT NOT NULL DEFAULT '[]',  -- capability slugs, or ["*"]
    expires_at        TEXT,
    rate_limit_per_min INTEGER,
    max_concurrency   INTEGER,
    budget_usd        REAL,
    status            TEXT NOT NULL DEFAULT 'active', -- active|revoked|expired
    created_at        TEXT NOT NULL,
    revoked_at        TEXT,
    last_used_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_agent ON agent_tokens(agent_id);

CREATE TABLE IF NOT EXISTS budget_policies (
    id              TEXT PRIMARY KEY,
    scope_type      TEXT NOT NULL,  -- global|provider|capability|agent
    scope_id        TEXT,
    window          TEXT NOT NULL DEFAULT 'day',  -- minute|day|month
    max_calls       INTEGER,
    max_cost_usd    REAL,
    rate_per_min    INTEGER,
    max_concurrency INTEGER,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_budget_scope ON budget_policies(scope_type, scope_id);

-- ---------------------------------------------------------------- SKILLs ----
CREATE TABLE IF NOT EXISTS skills (
    id                 TEXT PRIMARY KEY,
    slug               TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    category           TEXT NOT NULL DEFAULT 'general',
    status             TEXT NOT NULL DEFAULT 'draft', -- draft|published|disabled
    source_type        TEXT NOT NULL DEFAULT 'manual', -- manual|git|web|file
    source_ref         TEXT,
    current_version_id TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_versions (
    id           TEXT PRIMARY KEY,
    skill_id     TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    version      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',
    manifest     TEXT NOT NULL DEFAULT '{}', -- inputs/outputs/deps/plan/permissions/examples/instructions
    checksum     TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    published_at TEXT,
    UNIQUE(skill_id, version)
);
CREATE INDEX IF NOT EXISTS idx_skill_versions_skill ON skill_versions(skill_id, created_at DESC);

CREATE TABLE IF NOT EXISTS skill_imports (
    id                   TEXT PRIMARY KEY,
    skill_id             TEXT REFERENCES skills(id) ON DELETE SET NULL,
    source_type          TEXT NOT NULL,
    source_uri           TEXT NOT NULL,
    resolved_commit      TEXT,
    file_list            TEXT NOT NULL DEFAULT '[]',
    dependencies         TEXT NOT NULL DEFAULT '[]',
    permissions          TEXT NOT NULL DEFAULT '[]',
    status               TEXT NOT NULL,   -- pending|verified|rejected|failed|imported
    log                  TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL,
    finished_at          TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id           TEXT PRIMARY KEY,
    sha256       TEXT NOT NULL,
    filename     TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size         INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    purpose      TEXT NOT NULL DEFAULT 'upload', -- upload|artifact
    owner_type   TEXT NOT NULL DEFAULT 'user',
    owner_id     TEXT,
    task_id      TEXT,
    invocation_id TEXT,
    expires_at   TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_owner ON files(owner_type, owner_id);
CREATE INDEX IF NOT EXISTS idx_files_expiry ON files(expires_at);

-- --------------------------------------------------------- AragonAgent -------
CREATE TABLE IF NOT EXISTS agent_sessions (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL DEFAULT 'New session',
    status         TEXT NOT NULL DEFAULT 'idle', -- idle|running|stopped|error
    engine_release TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    archived_at    TEXT
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL,   -- user|assistant|system|tool
    content    TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages(session_id, seq);

CREATE TABLE IF NOT EXISTS agent_runs (
    id             TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    status         TEXT NOT NULL DEFAULT 'queued', -- queued|running|succeeded|failed|stopped
    prompt         TEXT NOT NULL DEFAULT '',
    stop_reason    TEXT,
    error          TEXT,
    engine_release TEXT,
    usage          TEXT NOT NULL DEFAULT '{}',
    queued_message TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id, seq);

CREATE TABLE IF NOT EXISTS engine_releases (
    id                     TEXT PRIMARY KEY,
    channel                TEXT NOT NULL,      -- stable|branch
    ref                    TEXT NOT NULL,      -- tag or branch
    version                TEXT NOT NULL,
    commit_sha             TEXT NOT NULL,
    npm_version            TEXT,
    status                 TEXT NOT NULL,      -- detected|validating|verified|failed|pending_approval|active|rolled_back|superseded
    adapter_version        TEXT NOT NULL DEFAULT '',
    dependency_fingerprint TEXT NOT NULL DEFAULT '{}',
    capabilities           TEXT NOT NULL DEFAULT '[]',
    checks                 TEXT NOT NULL DEFAULT '[]',
    previous_release_id    TEXT,
    activated_at           TEXT,
    verified_at            TEXT,
    created_at             TEXT NOT NULL,
    notes                  TEXT
);
CREATE INDEX IF NOT EXISTS idx_engine_releases_created ON engine_releases(created_at DESC);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_type  TEXT NOT NULL,           -- user|agent|system
    actor_id    TEXT,
    action      TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id   TEXT,
    summary     TEXT NOT NULL DEFAULT '',
    metadata    TEXT NOT NULL DEFAULT '{}',
    ip          TEXT,
    request_id  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_object ON audit_logs(object_type, object_id);
