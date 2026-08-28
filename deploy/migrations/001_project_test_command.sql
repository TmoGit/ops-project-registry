-- Apply once to existing Phase 1 databases before deploying this release.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS test_command VARCHAR(1024);
