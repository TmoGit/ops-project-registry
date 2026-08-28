DO $$ BEGIN
  CREATE TYPE integration_provider AS ENUM ('OPENAI', 'GITHUB', 'GITEA', 'AWS');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE TYPE integration_status AS ENUM ('NOT_CONFIGURED', 'CONFIGURED', 'CONNECTED', 'ERROR', 'DISABLED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE TABLE IF NOT EXISTS integration_configurations (
  id SERIAL PRIMARY KEY, provider integration_provider NOT NULL UNIQUE, display_name VARCHAR(128) NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE, status integration_status NOT NULL DEFAULT 'NOT_CONFIGURED',
  configuration_json JSON, credential_source VARCHAR(32), credential_reference VARCHAR(255),
  last_tested_at TIMESTAMPTZ, last_test_status VARCHAR(32), last_test_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), created_by VARCHAR(128) NOT NULL DEFAULT 'local-admin',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_by VARCHAR(128) NOT NULL DEFAULT 'local-admin'
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE integration_configurations TO ops_orchestrator;
GRANT USAGE, SELECT ON SEQUENCE integration_configurations_id_seq TO ops_orchestrator;
