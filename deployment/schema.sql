CREATE SCHEMA IF NOT EXISTS fnol;

CREATE TABLE IF NOT EXISTS fnol.reviews (
    id text PRIMARY KEY,
    case_id text NOT NULL,
    draft jsonb NOT NULL,
    draft_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewer text,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at timestamptz
);

CREATE TABLE IF NOT EXISTS fnol.records (
    id text PRIMARY KEY,
    case_id text NOT NULL UNIQUE,
    draft jsonb NOT NULL,
    review_id text NOT NULL REFERENCES fnol.reviews(id),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fnol.idempotency_keys (
    idempotency_key text PRIMARY KEY,
    request_hash text NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fnol.tool_calls (
    case_id text NOT NULL,
    tool_name text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    PRIMARY KEY (case_id, tool_name)
);

CREATE TABLE IF NOT EXISTS fnol.audit (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event text NOT NULL,
    case_id text,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS fnol.workbench_state (
    case_id text PRIMARY KEY,
    entry jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

