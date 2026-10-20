-- Skill Router 初始 schema 快照（与 Alembic 0001_init 对齐，便于人工审阅）
-- user_version=3；alembic_version=['0004_llm_config']
-- 权威来源：apps/api/migrations/versions/0001_init.py。本文件只读，不参与迁移。

CREATE TABLE admin_sessions (
	id VARCHAR(64) NOT NULL, 
	user_id VARCHAR(64) NOT NULL, 
	token_hash VARCHAR(128) NOT NULL, 
	csrf_hash VARCHAR(128) NOT NULL, 
	scope VARCHAR(32) NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	last_seen_at VARCHAR(32) NOT NULL, 
	expires_at VARCHAR(32) NOT NULL, 
	revoked_at VARCHAR(32), 
	ip VARCHAR(64), 
	user_agent VARCHAR(256), 
	PRIMARY KEY (id), 
	UNIQUE (token_hash)
);

CREATE TABLE admin_users (
	username VARCHAR(64) NOT NULL, 
	password_hash TEXT NOT NULL, 
	must_change_password INTEGER NOT NULL, 
	password_changed_at VARCHAR(32), 
	last_login_at VARCHAR(32), 
	disabled INTEGER NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (username)
);

CREATE TABLE agent_approvals (
	flow_id VARCHAR(64), 
	turn_id VARCHAR(64), 
	tool_call_id VARCHAR(64), 
	action VARCHAR(64) NOT NULL, 
	target VARCHAR(256), 
	impact JSON NOT NULL, 
	requested_scope JSON NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	decided_by VARCHAR(64), 
	decided_at VARCHAR(32), 
	expires_at VARCHAR(32) NOT NULL, 
	note TEXT, 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE agent_artifacts (
	turn_id VARCHAR(64), 
	kind VARCHAR(32) NOT NULL, 
	payload JSON NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	reviewed_by VARCHAR(64), 
	reviewed_at VARCHAR(32), 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE agent_events (
	id INTEGER NOT NULL, 
	turn_id VARCHAR(64) NOT NULL, 
	session_id VARCHAR(64) NOT NULL, 
	seq INTEGER NOT NULL, 
	type VARCHAR(48) NOT NULL, 
	payload JSON NOT NULL, 
	at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE agent_flow_steps (
	flow_id VARCHAR(64) NOT NULL, 
	step VARCHAR(24) NOT NULL, 
	ordinal INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	detail TEXT, 
	evidence JSON NOT NULL, 
	error_code VARCHAR(64), 
	started_at VARCHAR(32), 
	finished_at VARCHAR(32), 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE agent_flows (
	title VARCHAR(200) NOT NULL, 
	capability_id VARCHAR(64), 
	skill_id VARCHAR(64), 
	credential_id VARCHAR(64), 
	status VARCHAR(24) NOT NULL, 
	auto_publish INTEGER NOT NULL, 
	budget JSON NOT NULL, 
	spent JSON NOT NULL, 
	current_step VARCHAR(24) NOT NULL, 
	created_by VARCHAR(64), 
	created_at VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	finished_at VARCHAR(32), 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE agent_sessions (
	title VARCHAR(200) NOT NULL, 
	model_profile JSON NOT NULL, 
	permission_profile JSON NOT NULL, 
	engine_version VARCHAR(32), 
	status VARCHAR(16) NOT NULL, 
	turn_count INTEGER NOT NULL, 
	created_by VARCHAR(64), 
	created_at VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE agent_tool_calls (
	turn_id VARCHAR(64) NOT NULL, 
	tool_name VARCHAR(64) NOT NULL, 
	args_redacted JSON NOT NULL, 
	result_summary TEXT, 
	ok INTEGER NOT NULL, 
	duration_ms INTEGER, 
	upstream_task_id VARCHAR(128), 
	at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE agent_turns (
	session_id VARCHAR(64) NOT NULL, 
	ordinal INTEGER NOT NULL, 
	user_text TEXT NOT NULL, 
	attachments JSON NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	queue_position INTEGER, 
	stop_report JSON, 
	usage JSON NOT NULL, 
	error_code VARCHAR(64), 
	error_detail_redacted TEXT, 
	retry_of VARCHAR(64), 
	started_at VARCHAR(32), 
	finished_at VARCHAR(32), 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL, 
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

CREATE TABLE audit_logs (
	at VARCHAR(32) NOT NULL, 
	actor_type VARCHAR(16) NOT NULL, 
	actor_id VARCHAR(64), 
	action VARCHAR(64) NOT NULL, 
	object_type VARCHAR(48), 
	object_id VARCHAR(64), 
	summary JSON NOT NULL, 
	request_id VARCHAR(64), 
	ip VARCHAR(64), 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE call_logs (
	request_id VARCHAR(64) NOT NULL, 
	caller_type VARCHAR(16) NOT NULL, 
	caller_id VARCHAR(64), 
	capability_key VARCHAR(128) NOT NULL, 
	capability_version_id VARCHAR(64), 
	provider_id VARCHAR(64), 
	credential_id VARCHAR(64), 
	mode VARCHAR(16) NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	needs_reconcile INTEGER NOT NULL, 
	http_status INTEGER, 
	error_code VARCHAR(64), 
	latency_ms INTEGER, 
	queue_ms INTEGER, 
	upstream_ms INTEGER, 
	retries INTEGER NOT NULL, 
	idempotency_key VARCHAR(128), 
	request_fingerprint VARCHAR(64), 
	payload_policy VARCHAR(24) NOT NULL, 
	request_preview TEXT, 
	response_preview TEXT, 
	agent_session_id VARCHAR(64), 
	agent_turn_id VARCHAR(64), 
	task_id VARCHAR(64), 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (request_id)
);

CREATE TABLE callback_events (
	provider_slug VARCHAR(64) NOT NULL, 
	event_id VARCHAR(128) NOT NULL, 
	signature_ok INTEGER NOT NULL, 
	payload_redacted JSON NOT NULL, 
	at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE capabilities (
	"key" VARCHAR(128) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	kind VARCHAR(48) NOT NULL, 
	modes JSON NOT NULL, 
	file_io JSON NOT NULL, 
	compatibility_group VARCHAR(96), 
	failover JSON NOT NULL, 
	enabled INTEGER NOT NULL, 
	draft_definition JSON NOT NULL, 
	draft_revision INTEGER NOT NULL, 
	published_version_id VARCHAR(64), 
	latest_version VARCHAR(32), 
	status VARCHAR(16) NOT NULL, 
	requires_agent_reasoning INTEGER NOT NULL, 
	tags JSON NOT NULL, 
	deleted_at VARCHAR(32), 
	id VARCHAR(64) NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE ("key")
);

CREATE TABLE capability_routes (
	capability_id VARCHAR(64) NOT NULL, 
	provider_id VARCHAR(64) NOT NULL, 
	credential_id VARCHAR(64) NOT NULL, 
	route_key VARCHAR(64) NOT NULL, 
	priority INTEGER NOT NULL, 
	enabled INTEGER NOT NULL, 
	model_hint VARCHAR(128), 
	request JSON NOT NULL, 
	param_map JSON NOT NULL, 
	response_map JSON NOT NULL, 
	stream JSON, 
	async JSON, 
	limits JSON NOT NULL, 
	errors JSON NOT NULL, 
	timeout_ms INTEGER NOT NULL, 
	retry JSON NOT NULL, 
	health JSON NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE capability_test_runs (
	capability_id VARCHAR(64) NOT NULL, 
	capability_version_id VARCHAR(64), 
	route_id VARCHAR(64), 
	version VARCHAR(32), 
	mode VARCHAR(16) NOT NULL, 
	ok INTEGER NOT NULL, 
	http_status INTEGER, 
	latency_ms INTEGER, 
	request_preview_redacted TEXT, 
	response_preview_redacted TEXT, 
	error_code VARCHAR(64), 
	error_detail_redacted TEXT, 
	credential_id VARCHAR(64), 
	route JSON NOT NULL, 
	at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE capability_versions (
	capability_id VARCHAR(64) NOT NULL, 
	version VARCHAR(32) NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	definition JSON NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	published_at VARCHAR(32), 
	published_by VARCHAR(64), 
	changelog TEXT, 
	test_run_ids JSON NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE compatibility_groups (
	slug VARCHAR(96) NOT NULL, 
	kind VARCHAR(48) NOT NULL, 
	notes TEXT, 
	confirmed_by VARCHAR(64), 
	confirmed_at VARCHAR(32), 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (slug)
);

CREATE TABLE credential_verifications (
	credential_id VARCHAR(64) NOT NULL, 
	ok INTEGER NOT NULL, 
	status_code INTEGER, 
	latency_ms INTEGER, 
	error_code VARCHAR(64), 
	detail_redacted TEXT, 
	at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE credentials (
	provider_id VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	auth_type VARCHAR(16) NOT NULL, 
	header_name VARCHAR(64), 
	secret_ciphertext TEXT NOT NULL, 
	secret_mask VARCHAR(64) NOT NULL, 
	secret_fingerprint VARCHAR(32) NOT NULL, 
	meta JSON NOT NULL, 
	meta_sealed TEXT, 
	status VARCHAR(16) NOT NULL, 
	expires_at VARCHAR(32), 
	last_verified_at VARCHAR(32), 
	last_verify_ok INTEGER, 
	rotation_of VARCHAR(64), 
	deleted_at VARCHAR(32), 
	id VARCHAR(64) NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE engine_candidates (
	version VARCHAR(32) NOT NULL, 
	commit_sha VARCHAR(64), 
	provenance VARCHAR(24) NOT NULL, 
	published_at VARCHAR(32), 
	channel VARCHAR(32) NOT NULL, 
	notes TEXT, 
	detected_at VARCHAR(32) NOT NULL, 
	verification_status VARCHAR(16) NOT NULL, 
	verification_report JSON NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE engine_capability_status (
	capability_key VARCHAR(128) NOT NULL, 
	state VARCHAR(24) NOT NULL, 
	detail TEXT, 
	since_version VARCHAR(32), 
	updated_at VARCHAR(32) NOT NULL, 
	ordinal INTEGER NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (capability_key)
);

CREATE TABLE engine_deployments (
	version VARCHAR(32) NOT NULL, 
	commit_sha VARCHAR(64), 
	provenance VARCHAR(24) NOT NULL, 
	source_url VARCHAR(512), 
	integrity VARCHAR(256), 
	adapter_version VARCHAR(32), 
	status VARCHAR(16) NOT NULL, 
	deployed_at VARCHAR(32), 
	health JSON NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE engine_upgrade_runs (
	from_version VARCHAR(32) NOT NULL, 
	to_version VARCHAR(32) NOT NULL, 
	mode VARCHAR(16) NOT NULL, 
	policy VARCHAR(16) NOT NULL, 
	started_at VARCHAR(32) NOT NULL, 
	finished_at VARCHAR(32), 
	result VARCHAR(16), 
	verify_status VARCHAR(16), 
	blocked_reasons JSON NOT NULL, 
	steps JSON NOT NULL, 
	error TEXT, 
	backup_file_id VARCHAR(64), 
	rollback_of VARCHAR(64), 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE external_agents (
	name VARCHAR(96) NOT NULL, 
	description TEXT, 
	contact VARCHAR(128), 
	status VARCHAR(16) NOT NULL, 
	last_seen_at VARCHAR(32), 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE file_objects (
	sha256 VARCHAR(64) NOT NULL, 
	mime VARCHAR(96) NOT NULL, 
	size INTEGER NOT NULL, 
	storage_path VARCHAR(512) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	owner_type VARCHAR(24), 
	owner_id VARCHAR(64), 
	filename VARCHAR(256), 
	expires_at VARCHAR(32), 
	ref_count INTEGER NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	deleted_at VARCHAR(32), 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE idempotency_keys (
	scope VARCHAR(64) NOT NULL, 
	idem_key VARCHAR(128) NOT NULL, 
	fingerprint VARCHAR(64) NOT NULL, 
	request_id VARCHAR(64) NOT NULL, 
	capability_key VARCHAR(128), 
	state VARCHAR(16) NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	expires_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE llm_connection_probes (
          id              TEXT PRIMARY KEY,
          credential_id   TEXT NULL,
          provider_id     TEXT NULL,
          protocol        TEXT NOT NULL,
          wire            TEXT NOT NULL,
          base_url        TEXT NOT NULL,
          model_id        TEXT NULL,
          mode            TEXT NOT NULL,
          ok              INTEGER NOT NULL DEFAULT 0,
          stages          TEXT NOT NULL DEFAULT '[]',
          http_status     INTEGER NULL,
          latency_ms      INTEGER NULL,
          error_code      TEXT NULL,
          detail_redacted TEXT NULL,
          actor_id        TEXT NULL,
          request_id      TEXT NULL,
          at              TEXT NOT NULL
        );

CREATE TABLE llm_model_catalog (
          id                  TEXT PRIMARY KEY,
          dedupe_key          TEXT NOT NULL UNIQUE,
          credential_id       TEXT NULL,
          provider_id         TEXT NULL,
          protocol            TEXT NOT NULL,
          wire                TEXT NOT NULL,
          base_url            TEXT NOT NULL,
          models              TEXT NOT NULL DEFAULT '[]',
          model_count         INTEGER NOT NULL DEFAULT 0,
          truncated           INTEGER NOT NULL DEFAULT 0,
          source              TEXT NOT NULL DEFAULT 'upstream',
          fetched_at          TEXT NOT NULL,
          expires_at          TEXT NOT NULL,
          last_error_redacted TEXT NULL,
          created_at          TEXT NOT NULL,
          updated_at          TEXT NOT NULL
        );

CREATE TABLE login_attempts (
	id VARCHAR(64) NOT NULL, 
	username VARCHAR(64) NOT NULL, 
	ip VARCHAR(64), 
	at VARCHAR(32) NOT NULL, 
	success INTEGER NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE master_key_rotations (
	from_key_id VARCHAR(16) NOT NULL, 
	to_key_id VARCHAR(16) NOT NULL, 
	rows_total INTEGER NOT NULL, 
	rows_done INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	started_at VARCHAR(32) NOT NULL, 
	finished_at VARCHAR(32), 
	error TEXT, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE platform_tokens (
	agent_id VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	token_hash VARCHAR(128) NOT NULL, 
	prefix VARCHAR(24) NOT NULL, 
	scopes JSON NOT NULL, 
	allowed_modes JSON NOT NULL, 
	expires_at VARCHAR(32), 
	rpm INTEGER, 
	concurrency INTEGER, 
	daily_calls INTEGER, 
	budget_usd FLOAT, 
	revoked_at VARCHAR(32), 
	last_used_at VARCHAR(32), 
	created_by VARCHAR(64), 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (token_hash)
);

CREATE TABLE price_book (
	provider_id VARCHAR(64) NOT NULL, 
	capability_key VARCHAR(128), 
	unit VARCHAR(16) NOT NULL, 
	unit_price_usd FLOAT NOT NULL, 
	currency VARCHAR(8) NOT NULL, 
	effective_from VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE providers (
	slug VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	category VARCHAR(32) NOT NULL, 
	base_url VARCHAR(512), 
	docs_url VARCHAR(512), 
	tags JSON NOT NULL, 
	environment VARCHAR(16) NOT NULL, 
	notes TEXT, 
	status VARCHAR(16) NOT NULL, 
	deleted_at VARCHAR(32), 
	id VARCHAR(64) NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (slug)
);

CREATE TABLE quota_counters (
	scope_type VARCHAR(16) NOT NULL, 
	scope_id VARCHAR(64) NOT NULL, 
	period VARCHAR(16) NOT NULL, 
	calls INTEGER NOT NULL, 
	cost_usd FLOAT NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE quota_limits (
	scope_type VARCHAR(16) NOT NULL, 
	scope_id VARCHAR(64) NOT NULL, 
	rpm INTEGER, 
	concurrency INTEGER, 
	daily_calls INTEGER, 
	monthly_budget_usd FLOAT, 
	updated_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE rate_counters (
	id VARCHAR(64) NOT NULL, 
	scope_type VARCHAR(16) NOT NULL, 
	scope_id VARCHAR(64) NOT NULL, 
	window_start VARCHAR(32) NOT NULL, 
	window_seconds INTEGER NOT NULL, 
	calls INTEGER NOT NULL, 
	concurrent INTEGER NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE route_decisions (
	call_id VARCHAR(64), 
	candidates JSON NOT NULL, 
	dropped JSON NOT NULL, 
	chosen_route_id VARCHAR(64), 
	chosen_capability_version_id VARCHAR(64), 
	chosen_provider_id VARCHAR(64), 
	reason TEXT, 
	failover_from VARCHAR(64), 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE settings (
	"key" VARCHAR(64) NOT NULL, 
	value JSON NOT NULL, 
	scope VARCHAR(16) NOT NULL, 
	updated_by VARCHAR(64), 
	updated_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY ("key")
);

CREATE TABLE skill_exports (
	skill_id VARCHAR(64) NOT NULL, 
	version VARCHAR(32), 
	format VARCHAR(16) NOT NULL, 
	file_id VARCHAR(64), 
	sha256 VARCHAR(64), 
	created_by VARCHAR(64), 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE skill_files (
	skill_version_id VARCHAR(64) NOT NULL, 
	path VARCHAR(512) NOT NULL, 
	size INTEGER NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	mime VARCHAR(96), 
	executable INTEGER NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE skill_imports (
	skill_id VARCHAR(64), 
	kind VARCHAR(16) NOT NULL, 
	source_url VARCHAR(1024), 
	source_ref VARCHAR(128), 
	resolved_commit VARCHAR(64), 
	preview_token VARCHAR(64), 
	files JSON NOT NULL, 
	warnings JSON NOT NULL, 
	requires_sandbox_review INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	error_code VARCHAR(64), 
	error_detail TEXT, 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE skill_versions (
	skill_id VARCHAR(64) NOT NULL, 
	version VARCHAR(32) NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	skill_md TEXT NOT NULL, 
	frontmatter JSON NOT NULL, 
	execution_plan JSON NOT NULL, 
	dependencies JSON NOT NULL, 
	permissions JSON NOT NULL, 
	examples JSON NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	published_at VARCHAR(32), 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE skills (
	name VARCHAR(64) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	description TEXT NOT NULL, 
	category VARCHAR(64), 
	tags JSON NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	enabled INTEGER NOT NULL, 
	draft_definition JSON NOT NULL, 
	published_version_id VARCHAR(64), 
	latest_version VARCHAR(32), 
	source_type VARCHAR(16) NOT NULL, 
	source_ref VARCHAR(128), 
	source_commit VARCHAR(64), 
	source_url VARCHAR(512), 
	deleted_at VARCHAR(32), 
	id VARCHAR(64) NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE sse_events (
	id INTEGER NOT NULL, 
	stream_key VARCHAR(64) NOT NULL, 
	type VARCHAR(48) NOT NULL, 
	payload JSON NOT NULL, 
	at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE system_state (
	"key" VARCHAR(64) NOT NULL, 
	value TEXT NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY ("key")
);

CREATE TABLE task_queue (
	kind VARCHAR(48) NOT NULL, 
	payload JSON NOT NULL, 
	priority INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	available_at VARCHAR(32) NOT NULL, 
	attempts INTEGER NOT NULL, 
	max_attempts INTEGER NOT NULL, 
	lease_owner VARCHAR(64), 
	lease_expires_at VARCHAR(32), 
	last_error TEXT, 
	created_at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE tasks (
	request_id VARCHAR(64) NOT NULL, 
	capability_key VARCHAR(128) NOT NULL, 
	capability_version_id VARCHAR(64), 
	mode VARCHAR(16) NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	progress_percent INTEGER NOT NULL, 
	progress_stage VARCHAR(64), 
	upstream_task_id VARCHAR(128), 
	upstream_payload_redacted JSON NOT NULL, 
	attempts INTEGER NOT NULL, 
	max_attempts INTEGER NOT NULL, 
	failure_class VARCHAR(32), 
	error_code VARCHAR(64), 
	error_detail_redacted TEXT, 
	cancel_capability VARCHAR(16) NOT NULL, 
	cancel_requested_at VARCHAR(32), 
	result JSON NOT NULL, 
	output_files JSON NOT NULL, 
	token_id VARCHAR(64), 
	agent_session_id VARCHAR(64), 
	resume_policy VARCHAR(16) NOT NULL, 
	input_snapshot JSON NOT NULL, 
	route_id VARCHAR(64), 
	created_at VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	finished_at VARCHAR(32), 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE usage_records (
	call_id VARCHAR(64), 
	provider_id VARCHAR(64), 
	capability_key VARCHAR(128), 
	token_id VARCHAR(64), 
	unit VARCHAR(16) NOT NULL, 
	qty FLOAT NOT NULL, 
	cost_usd FLOAT NOT NULL, 
	source VARCHAR(16) NOT NULL, 
	at VARCHAR(32) NOT NULL, 
	id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE worker_heartbeats (
	worker_id VARCHAR(64) NOT NULL, 
	started_at VARCHAR(32) NOT NULL, 
	last_seen_at VARCHAR(32) NOT NULL, 
	leases_held INTEGER NOT NULL, 
	engine_version VARCHAR(32), 
	PRIMARY KEY (worker_id)
);

CREATE INDEX ix_admin_sessions_expires ON admin_sessions (expires_at);
CREATE INDEX ix_admin_sessions_user ON admin_sessions (user_id, revoked_at);
CREATE INDEX ix_agent_approvals_status ON agent_approvals (status, expires_at);
CREATE INDEX ix_agent_artifacts_turn ON agent_artifacts (turn_id);
CREATE INDEX ix_agent_events_session ON agent_events (session_id, id);
CREATE INDEX ix_agent_flows_status ON agent_flows (status, updated_at);
CREATE INDEX ix_agent_tool_calls_turn ON agent_tool_calls (turn_id);
CREATE INDEX ix_agent_turns_session ON agent_turns (session_id, ordinal);
CREATE INDEX ix_audit_action ON audit_logs (action, at);
CREATE INDEX ix_audit_at ON audit_logs (at);
CREATE INDEX ix_audit_object ON audit_logs (object_type, object_id);
CREATE INDEX ix_call_logs_caller ON call_logs (caller_id, created_at);
CREATE INDEX ix_call_logs_cap ON call_logs (capability_key, created_at);
CREATE INDEX ix_call_logs_created ON call_logs (created_at);
CREATE INDEX ix_call_logs_status ON call_logs (status);
CREATE INDEX ix_call_logs_task ON call_logs (task_id);
CREATE INDEX ix_cap_routes_cap ON capability_routes (capability_id, enabled, priority);
CREATE INDEX ix_cap_routes_cred ON capability_routes (credential_id);
CREATE INDEX ix_cap_routes_provider ON capability_routes (provider_id);
CREATE INDEX ix_cap_tests_cap ON capability_test_runs (capability_id, at);
CREATE INDEX ix_capabilities_status ON capabilities (status, enabled);
CREATE INDEX ix_cred_verif_cred ON credential_verifications (credential_id, at);
CREATE INDEX ix_credentials_provider_status ON credentials (provider_id, status);
CREATE INDEX ix_engine_runs_started ON engine_upgrade_runs (started_at);
CREATE INDEX ix_files_expires ON file_objects (expires_at);
CREATE INDEX ix_files_owner ON file_objects (owner_type, owner_id);
CREATE INDEX ix_files_sha ON file_objects (sha256);
CREATE INDEX ix_idem_expires ON idempotency_keys (expires_at);
CREATE INDEX ix_idem_state ON idempotency_keys (state, expires_at);
CREATE INDEX ix_llm_catalog_cred ON llm_model_catalog(credential_id);
CREATE INDEX ix_llm_catalog_exp ON llm_model_catalog(expires_at);
CREATE INDEX ix_llm_probe_at ON llm_connection_probes(at);
CREATE INDEX ix_llm_probe_cred ON llm_connection_probes(credential_id, at);
CREATE INDEX ix_login_attempts_ip ON login_attempts (ip, at);
CREATE INDEX ix_login_attempts_user ON login_attempts (username, at);
CREATE INDEX ix_skills_status ON skills (status, enabled);
CREATE INDEX ix_sse_stream ON sse_events (stream_key, id);
CREATE INDEX ix_task_queue_pick ON task_queue (status, available_at);
CREATE INDEX ix_tasks_cancel_req ON tasks(status, cancel_requested_at);
CREATE INDEX ix_tasks_request ON tasks (request_id);
CREATE INDEX ix_tasks_status ON tasks (status, updated_at);
CREATE INDEX ix_tasks_upstream ON tasks (upstream_task_id);
CREATE INDEX ix_tokens_agent ON platform_tokens (agent_id, revoked_at);
CREATE INDEX ix_usage_cap ON usage_records (capability_key, at);
CREATE INDEX ix_usage_provider ON usage_records (provider_id, at);
CREATE UNIQUE INDEX uq_agent_events_seq ON agent_events (turn_id, seq);
CREATE UNIQUE INDEX uq_agent_flow_steps ON agent_flow_steps (flow_id, step);
CREATE UNIQUE INDEX uq_callback_event ON callback_events (provider_slug, event_id);
CREATE UNIQUE INDEX uq_cap_routes_key ON capability_routes (capability_id, route_key);
CREATE UNIQUE INDEX uq_cap_versions_hash ON capability_versions (capability_id, content_hash);
CREATE UNIQUE INDEX uq_cap_versions_ver ON capability_versions (capability_id, version);
CREATE UNIQUE INDEX uq_engine_candidates ON engine_candidates (version, provenance);
CREATE UNIQUE INDEX uq_idem_scope_key ON idempotency_keys (scope, idem_key);
CREATE UNIQUE INDEX uq_price_book ON price_book (provider_id, capability_key, unit, effective_from);
CREATE UNIQUE INDEX uq_quota_counters ON quota_counters (scope_type, scope_id, period);
CREATE UNIQUE INDEX uq_quota_limits ON quota_limits (scope_type, scope_id);
CREATE UNIQUE INDEX uq_rate_counters ON rate_counters (scope_type, scope_id, window_start, window_seconds);
CREATE UNIQUE INDEX uq_skill_files_path ON skill_files (skill_version_id, path);
CREATE UNIQUE INDEX uq_skill_versions_hash ON skill_versions (skill_id, content_hash);
CREATE UNIQUE INDEX uq_skill_versions_ver ON skill_versions (skill_id, version);

CREATE TRIGGER capability_versions_immutable
    BEFORE UPDATE ON capability_versions
    WHEN OLD.status = 'published' AND NEW.status = 'published'
         AND OLD.definition != NEW.definition
    BEGIN
        SELECT RAISE(ABORT, 'published_version_immutable');
    END;

CREATE TRIGGER skill_versions_immutable
    BEFORE UPDATE ON skill_versions
    WHEN OLD.status = 'published' AND NEW.status = 'published'
         AND OLD.skill_md != NEW.skill_md
    BEGIN
        SELECT RAISE(ABORT, 'published_version_immutable');
    END;
