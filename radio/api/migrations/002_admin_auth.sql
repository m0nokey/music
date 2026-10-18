CREATE TABLE IF NOT EXISTS admin_login_attempts (
    client_key char(64) PRIMARY KEY,
    failed_attempts integer NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    blocked_until timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash char(64) PRIMARY KEY,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS admin_sessions_expiry_idx ON admin_sessions (expires_at);
