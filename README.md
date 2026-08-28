# Ops Project Registry

Durable project memory and bootstrap source for the Ops Orchestrator control plane.

This repository stores approved project records, task state snapshots, decisions,
and original intake requests. It does not store secrets or high-frequency logs.

The service runs on CT 126. Its live database and credentials are local-only.

## Operations

The API intentionally listens only on `127.0.0.1:8000`; preserve
`OPS_BIND_HOST=127.0.0.1` in `/etc/ops-orchestrator/ops.env`. Redis is likewise
local (`OPS_REDIS_URL=redis://127.0.0.1:6379/0`). The API only queues execution
jobs. `ops-orchestrator-worker.service` runs them using RQ, so a slow Codex run
never occupies an API worker.

Before first deploying this revision to an existing database, apply
`deploy/migrations/001_project_test_command.sql` with the PostgreSQL role used
by the service.

Install the supplied units, then use:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now ops-orchestrator-api ops-orchestrator-worker
sudo systemctl stop ops-orchestrator-worker ops-orchestrator-api
sudo systemctl start ops-orchestrator-api ops-orchestrator-worker
sudo systemctl status ops-orchestrator-api ops-orchestrator-worker
```

Each project can set `test_command` (for example `python -m pytest -q`) in the
database. It is executed in that task's isolated worktree without a shell. If it
is unset, a `tests/` directory uses `python -m pytest -q`; otherwise tests are
recorded as not configured.

Execution inspection is available through `GET /api/executions`,
`GET /api/executions/{id}`, and `GET /api/executions/{id}/log`, or `opsctl
executions`, `opsctl execution ID`, and `opsctl execution-log ID`. Logs are only
served from the configured local artifacts directory.

To enable Home Assistant actions, set `OPS_HOMEOPS_BRIDGE_URL` to the existing
HomeOps notification bridge and `OPS_MOBILE_BRIDGE_SECRET` to its shared secret.
Complete an intake at `/intakes/{id}/proposal`; it stores the complete proposed
record and sends Approve/Reject action IDs. The bridge posts those action IDs to
`/api/mobile-actions/{action}` with `X-Ops-Bridge-Secret`.

### Backup and restore

Stop both units before a consistent backup. Dump PostgreSQL and copy the project
registry (which contains approved checkpoints); keep execution artifacts only if
you need historical logs:

```sh
sudo systemctl stop ops-orchestrator-worker ops-orchestrator-api
sudo -u postgres pg_dump "$OPS_DATABASE_URL" > ops-orchestrator.sql
tar -C /opt/ops-orchestrator/repos -czf project-registry.tgz project-registry
tar -C /opt/ops-orchestrator -czf execution-artifacts.tgz artifacts
```

To restore, stop the units, restore into the intended empty PostgreSQL database
with `psql "$OPS_DATABASE_URL" < ops-orchestrator.sql`, extract the registry and
optional artifacts back under `/opt/ops-orchestrator`, confirm ownership is
`ops-orchestrator:ops-orchestrator`, then start API followed by worker. Do not
back up `/etc/ops-orchestrator/ops.env` with these archives; store its secrets in
the approved secrets backup instead.
