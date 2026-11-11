# Autonomous AI Employee Runtime

Prototype runtime for CentrAlign AI's Founding Engineer challenge: an AI worker that
interprets a goal, plans tool calls, executes them against a mock company system,
recovers from transient failures, pauses for human approval before sensitive writes,
and independently verifies the outcome before reporting success.

**Status (v0.2.0):** working prototype, not production software. The planner is a
deterministic mock that accepts exactly two goal formats (below). Tasks can be run from
the CLI or from a live web dashboard backed by an HTTP API; both use the same runtime,
SQLite database and per-task locks. All company data is fictional.

![Dashboard after a ticket task with one simulated 503, a bounded retry and 6/6 verification checks](docs/images/dashboard-recovery.png)

## Contents

- [What v0.2.0 adds](#what-v020-adds)
- [Platform support](#platform-support)
- [Quick start](#quick-start)
- [Supported goals](#supported-goals-mock-planner)
- [Dashboard](#dashboard)
- [CLI walkthrough](#cli-walkthrough)
- [HTTP API](#http-api)
- [Local mode and public demo mode](#local-mode-and-public-demo-mode)
- [Security model](#security-model)
- [Configuration](#configuration)
- [Docker](#docker)
- [Deploying the public demo on Render Free](#deploying-the-public-demo-on-render-free)
- [Limitations](#limitations)
- [How it works](#how-it-works)
- [Tests](#tests)

## What v0.2.0 adds

- A **live dashboard** served by FastAPI. It submits real goals, shows the real task ID,
  plan, tool calls, retries, audit events, approval requests and verification evidence,
  and lets you approve, reject or resume tasks. Nothing in it is scripted.
- An **HTTP API** (`/api/v1`) that wraps the existing planner, executor, approval policy,
  verifier and SQLite store. There is no endpoint for arbitrary tools, plans, shell
  commands or file writes.
- **Shared OS locks** between CLI and API processes, so the same task can never be
  executed, approved or rejected by two processes at once.
- Two run modes: **local** (persistent, localhost only) and **public demo** (per-browser
  sandboxes of fictional data on ephemeral storage), plus a Render Free Blueprint.
- Approval events record the approver (`local-dashboard`, `demo-session:<id>`, or
  `cli:<user>`).

## Platform support

| Platform | Status |
|----------|--------|
| Linux / WSL2, Python 3.10, `uv` | Verified: full test suite (API, real-server and Chromium browser tests), CLI and dashboard |
| Docker (Linux engine) | Verified: local mode with bind mounts; public mode under Render Free limits (512 MB, 0.1 CPU, read-only root) |
| Render Free | Configuration provided (`render.yaml`); not yet deployed or tested on Render |
| Native Windows | CI runs the full suite on a Windows runner; not yet confirmed passing |
| Docker Desktop (Windows / macOS), macOS | Not verified |

## Quick start

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repository-url>
cd Autonomous-AI-Employee-Runtime
uv sync
uv run aae --help
uv run aae-api
```

`uv run aae-api` starts the API and dashboard in **local mode** on
<http://127.0.0.1:8000>. Open that address in a browser. The server listens on the
loopback interface only and answers only requests addressed to `localhost`,
`127.0.0.1` or `[::1]`.

By default the database is `./data/runtime.db` and the file workspace is `./workspace`,
relative to the directory you start from. The CLI and the dashboard share them, so a
task started in one can be inspected, approved or resumed in the other. Optionally copy
`.env.example` to `.env` to change paths or settings.

## Supported goals (mock planner)

The runtime always uses the deterministic mock planner. It accepts **only** these goal
formats (case, extra spaces and a final period are ignored) and rejects everything else
before any task is created:

```text
Update employee E-17 title to Senior Engineer [and write a confirmation note | and confirm the change in a note file]
Create a support ticket for Acme Corp about a $240 billing discrepancy [and save a summary note]
```

Use **single quotes** in the shell so `$240` is not expanded.

| Scenario | Plan | Human approval |
|----------|------|----------------|
| Ticket | `find_customer` → `create_ticket` (→ `file.write acme_billing_summary.txt`) | No |
| Employee | `update_employee` (→ `file.write employee_update.txt`) | Yes, before `update_employee` |

## Dashboard

The dashboard keeps the dark design and eight-stage pipeline of the original demo, but
every value comes from the API:

- **Goal:** one button per supported goal; free text is allowed and sent to the server,
  which rejects unsupported goals (`422`, no task created). For the ticket scenario an
  explicitly labelled control injects **one simulated 503** into `create_ticket`.
- **Execution:** real task ID and status, interpreted goal, plan steps with attempt
  counts, tool arguments (secret-looking values redacted), retries, errors, and the
  persisted audit events with their stored timestamps. Pipeline stages are derived from
  those events.
- **Approval:** when the policy gate pauses a task, the card shows the pending step,
  operation, arguments, risk and reason. Approve and Reject each require confirmation in
  a dialog and send an API request; the server enforces the decision and the card shows
  the resulting status.
- **Verification:** result, each independent check (expected vs actual), failure reasons
  and evidence.
- **History:** persisted tasks (including ones started from the CLI); select one to
  reload it, or resume a queued or interrupted task. `#task=<id>` in the URL reopens it.
- **Company and workspace:** read-only employees, tickets, customers, and the files
  written to the workspace.
- While a task executes the page polls every second (at most 120 times), stops when the
  task is settled, and shows a banner if the backend is unreachable or has restarted.

![Approval card for update_employee on step_1](docs/images/dashboard-approval.png)

`demo-ui/index.html` is the earlier **scripted simulation** for offline presentations.
It is clearly labelled, is not served by the API, and does not execute anything.

## CLI walkthrough

`python -m app.cli` is equivalent to `aae`. Every command prints the resolved database
and workspace paths.

### 1. Create a ticket (and optionally a summary note)

```bash
uv run aae run 'Create a support ticket for Acme Corp about a $240 billing discrepancy.'
uv run aae run 'Create a support ticket for Acme Corp about a billing discrepancy of $240 and save a summary note.'
```

On a freshly seeded database the first ticket is `T-10001`. After execution the
verifier checks the ticket and file in the database and workspace; the task is
`COMPLETED` only if every check passes.

### 2. Update an employee with human approval

```bash
uv run aae run 'Update employee E-17 title to Senior Engineer and confirm the change in a note file.'
```

The task stops with status `NEEDS_APPROVAL` (exit code 4) before anything is written,
and prints the pending action: step `step_1`, tool `company_api`, the exact arguments,
risk `write`, and the reason. Copy the printed task ID, then either approve:

```bash
uv run aae approve <task_id> --step step_1
```

`approve` shows the pending action again and asks for confirmation. In scripts, add
`--yes`; without a terminal and without `--yes` the CLI refuses to approve. `--step`
makes the approval fail if a different step is pending.

```bash
uv run aae approve <task_id> --step step_1 --yes
```

…or reject it (the task ends `ABORTED`, the write never runs, the reason is stored):

```bash
uv run aae reject <task_id> --reason 'Not approved this quarter'
```

### 3. Inspect, restart and resume

```bash
uv run aae status <task_id>
uv run aae history <task_id>
uv run aae resume <task_id>
uv run aae --json status <task_id>
```

- `status` shows steps, retries, any pending approval, and verification results.
- `history` lists the stored audit events in order.
- `resume` continues a queued or interrupted task. Paused and finished tasks are left
  unchanged.
- `--json` (before the command) prints machine-readable output.

### 4. Simulated transient failure and bounded retry

```bash
uv run aae run 'Create a support ticket for Acme Corp about a billing discrepancy of $240 and save a summary note.' --inject-transient create_ticket
```

The first `create_ticket` call in this process fails with a **simulated** `503 Service
Unavailable`. The mock company API never fails on its own. The runtime classifies the
failure as transient, retries once (`retry_count` 1) and then verifies the outcome. The
retry budget (`MAX_RECOVERY_ATTEMPTS`, default 2) is persisted per step, so restarts
cannot reset it. The dashboard's injection control does the same for one task.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Task completed, or a read command succeeded |
| 1 | Task failed or aborted |
| 2 | Usage, configuration or file-permission error |
| 3 | Goal rejected by the mock planner (no task created) |
| 4 | Task is waiting for approval |
| 5 | Unknown task, or approval refused (wrong step, no `--yes`, declined, nothing pending) |
| 6 | Task is locked by another CLI process or API request (nothing changed) |
| 70 | Unexpected internal error |

## HTTP API

All endpoints are under `/api/v1` and return JSON. Errors have the form
`{"error": {"code": "...", "message": "...", "request_id": "..."}}` without stack traces
or filesystem paths. In local mode interactive docs are at `/docs`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health`, `/api/v1/health` | Liveness; v1 also returns mode, version, persistence and a per-process `boot_id` |
| GET | `/api/v1/scenarios` | The supported goals |
| GET / POST | `/api/v1/session` | Session status; start a public demo session |
| POST | `/api/v1/tasks` | Plan and start a goal: `{"goal": "...", "inject_failure": "create_ticket"}` (injection optional) |
| GET | `/api/v1/tasks` | Persisted tasks, newest first (`limit`, `offset`) |
| GET | `/api/v1/tasks/{task_id}` | Task state: plan, tool calls, retries, failures, pending action, verification summary |
| GET | `/api/v1/tasks/{task_id}/history` | Audit events in order |
| GET | `/api/v1/tasks/{task_id}/verification` | Checks, failure reasons and evidence |
| POST | `/api/v1/tasks/{task_id}/approve` | `{"step_id": "step_1", "confirm": true}` |
| POST | `/api/v1/tasks/{task_id}/reject` | `{"step_id": "step_1", "reason": "...", "confirm": true}` |
| POST | `/api/v1/tasks/{task_id}/resume` | `{"confirm": true}` for a queued or interrupted task |
| GET | `/api/v1/company/employees`, `/customers`, `/tickets` | Mock company data (read-only) |
| GET | `/api/v1/workspace/files`, `/api/v1/workspace/file?path=...` | Workspace files (read-only; only listed files can be read) |

Behaviour worth knowing:

- `POST /tasks` returns `202` with the real task ID and status `queued`; execution runs
  in a bounded background pool. Poll the task until `executing` is `false`. An
  unsupported goal returns `422` and creates nothing.
- Send an `Idempotency-Key` header (8–128 of `A-Z a-z 0-9 _ -`) to make retries safe:
  repeating it returns the same task (`200`); reusing it for a different body is `409`.
- Approve, reject and resume take the task's lock (waiting up to 2 s). Concurrent or
  repeated approvals execute the write once; the others get `409`.
- Mutating requests must be `application/json`, at most 8 KiB, and same-origin.

```bash
curl -s http://127.0.0.1:8000/api/v1/health
curl -s -X POST http://127.0.0.1:8000/api/v1/tasks -H 'Content-Type: application/json' -H 'Idempotency-Key: example-0001' -d '{"goal": "Update employee E-17 title to Senior Engineer."}'
```

## Local mode and public demo mode

| | Local mode (`APP_MODE=local`, default) | Public demo mode (`APP_MODE=public`) |
|---|---|---|
| Intended for | One developer on their own machine | An internet-facing demonstration |
| Storage | `DATABASE_PATH` and `WORKSPACE_PATH`, shared with the CLI | One SQLite database and workspace per browser session under `PUBLIC_DATA_ROOT` (default: system temp dir) |
| Persistence | Survives restarts; history and approvals can be resumed later | **Ephemeral.** Erased on every start, restart or free-host sleep, and after `PUBLIC_SESSION_TTL_SECONDS` (default 1 hour) of inactivity |
| Who can act | Anyone who can reach localhost on that machine | Only the session that created a task; other sessions get `404` |
| Approver recorded | `local-dashboard` (or `cli:<user>`) | `demo-session:<first 12 hex chars>` |
| Network | Loopback bind; localhost host names only | `0.0.0.0` in the container; only `ALLOWED_HOSTS` / `RENDER_EXTERNAL_HOSTNAME` |

After a public-mode restart, old cookies and task IDs return `401`/`404` instead of a
stale result, and an open dashboard detects the new `boot_id` and says the demo state was
erased. Public demo data is fictional and may be reset at any time.

![Public demo: a session must be started; the notice explains that state is temporary](docs/images/dashboard-public-session.png)

## Security model

Enforced on the server, in both modes:

- Tasks come only from goals accepted by the strict mock grammar. There is no endpoint
  that runs a tool, plan, shell command or file write directly.
- The approval policy pauses `company_api.update_employee` (and any destructive tool)
  before it runs. Approval requires `confirm: true` and the exact pending `step_id`.
- Workspace reads accept only plain relative names that the sandboxed file tool lists;
  traversal, absolute paths, escaping symlinks and database files are refused.
- The `Host` header must be on the allowlist (blocks DNS rebinding of the localhost
  API). Mutations must be same-origin JSON (blocks cross-site request forgery); there is
  no CORS. Bodies are limited to 8 KiB.
- Responses carry a strict Content-Security-Policy (no inline scripts), `nosniff`,
  `frame-ancestors 'none'` and `no-store` for API responses. The dashboard renders data
  with `textContent` only.
- Values under secret-looking keys (`password`, `token`, `api_key`, ...) and configured
  secrets are redacted from API responses.

Additionally in public mode:

- Every data and task endpoint requires a session (random 256-bit token in an
  `HttpOnly; Secure; SameSite=Strict` cookie; the server stores only its hash).
- Each session has its own database and workspace, so it cannot see or approve another
  session's tasks.
- Optional `PUBLIC_ACCESS_CODE`: checked on the server, never sent to the browser. Share
  it out of band; without it anyone can start a session.
- Limits: per-client rate limits (reads, mutations, session creation), tasks per session,
  total sessions, idle expiry. `/docs` and `/openapi.json` are disabled; HSTS is sent.

Not provided: user accounts, roles or separation of duties. In the public demo the person
who starts a task also approves it, in their own sandbox.

## Configuration

See `.env.example`. Settings are read from the environment and an optional `.env`.

| Variable | Description |
|----------|-------------|
| `APP_MODE` | `local` (default) or `public` |
| `APP_HOST` / `APP_PORT` | Bind address (default `127.0.0.1:8000`) |
| `PORT` | Set by hosting providers; overrides `APP_PORT` |
| `ALLOWED_HOSTS` | Extra accepted host names, comma-separated (required in public mode unless Render sets `RENDER_EXTERNAL_HOSTNAME`) |
| `TRUST_PROXY_HEADERS` | Use the last `X-Forwarded-For` hop as the client address (behind a proxy only) |
| `ALLOW_FAILURE_INJECTION` | Allow the dashboard's simulated-503 control (default `true`) |
| `PUBLIC_DATA_ROOT`, `PUBLIC_ACCESS_CODE` | Public mode storage root; optional access code |
| `PUBLIC_MAX_SESSIONS`, `PUBLIC_SESSION_TTL_SECONDS`, `PUBLIC_MAX_TASKS_PER_SESSION` | Public mode quotas (defaults 200, 3600, 20) |
| `RATE_LIMIT_PER_MINUTE`, `MUTATION_RATE_LIMIT_PER_MINUTE`, `SESSION_RATE_LIMIT_PER_MINUTE` | Public mode rate limits (defaults 240, 30, 6) |
| `SESSION_COOKIE_SECURE` | `Secure` cookie flag (default `true`; set `false` only for plain-HTTP testing) |
| `DATABASE_PATH` | SQLite file (default `data/runtime.db`; must be outside `WORKSPACE_PATH`) |
| `WORKSPACE_PATH` | File tool sandbox (default `workspace`) |
| `MAX_RECOVERY_ATTEMPTS` | Retries allowed after a step's first attempt (default `2`) |
| `LLM_PROVIDER`, `XAI_API_KEY`, `XAI_MODEL`, `XAI_BASE_URL` | Provider settings for library use; ignored by the CLI and the API |

Never commit `.env` or real secrets.

## Docker

The image runs as a non-root user (UID/GID default `1000:1000`), contains no dev
dependencies, keeps the application code read-only, and starts `aae-api`. Compose runs
**local mode**, publishes port 8000 on `127.0.0.1` only, and mounts `./data` and
`./workspace` from the repository so the host CLI sees the same tasks.

```bash
APP_UID=$(id -u) APP_GID=$(id -g) docker compose build
docker compose up -d
docker compose run --rm api aae run 'Update employee E-17 title to Senior Engineer and confirm the change in a note file.'
uv run aae status <task_id>
docker compose run --rm api aae approve <task_id> --step step_1 --yes
```

The dashboard is then at <http://127.0.0.1:8000>. Pass the same `APP_UID`/`APP_GID` to
`docker compose run` if they differ from `1000:1000`. On Docker Desktop for Windows or
macOS, file ownership and lock behaviour on shared folders have not been verified.

To try public mode locally the way Render runs it:

```bash
docker build -t aae-runtime:0.2.0 .
docker run --rm -p 127.0.0.1:10000:10000 --memory 512m --cpus 0.1 --read-only --tmpfs /tmp:rw,size=64m --cap-drop ALL --security-opt no-new-privileges -e APP_MODE=public -e PORT=10000 -e ALLOWED_HOSTS=127.0.0.1,localhost -e SESSION_COOKIE_SECURE=false aae-runtime:0.2.0
```

## Deploying the public demo on Render Free

`render.yaml` is a Render Blueprint for one free Docker web service in public mode. It
sets `plan: free` explicitly (the Blueprint default is a paid plan), the health check
path `/api/v1/health`, `PORT=10000`, and turns automatic deploys off.

1. Push the repository to GitHub or GitLab.
2. In the Render Dashboard choose **New → Blueprint**, select the repository, and
   confirm the `autonomous-ai-employee-demo` service on the **Free** plan.
3. When prompted for `PUBLIC_ACCESS_CODE`, enter a code to restrict who can start a
   session, or leave it empty for an open demo.
4. Deploy. Render builds the `Dockerfile`; the start command is the image's `aae-api`.
   `RENDER_EXTERNAL_HOSTNAME` is set by Render and becomes the allowed host name.
5. Verify the deployment from your machine:

```bash
uv run python scripts/smoke_demo.py https://autonomous-ai-employee-demo.onrender.com
```

Use your service's actual URL, and add `--access-code <code>` if you set one. The script
waits up to three minutes for a cold start, then runs every scenario through the API
(recovery, approval, rejection, unsupported goal, path traversal, cross-origin
refusal) and exits non-zero on any failure.

Render Free limits that matter here (from Render's documentation): 0.1 CPU and 512 MB
RAM; the service spins down after 15 minutes without traffic and takes about a minute
to start again; the local filesystem is lost on every spin-down, restart or deploy, and
there are no persistent disks; Render may restart free services at any time; 750 free
instance hours per workspace per month. The demo therefore makes no persistence claims
in public mode. Sessions live in one process, so the service must not be scaled beyond
one instance.

## Limitations

- **Mock planner only.** Exactly two goal formats; no live LLM planning. `--provider xai`
  is refused by the CLI and `LLM_PROVIDER` does not affect the CLI or the API.
- **Fictional world.** The company API, employees, customers and tickets are a seeded
  SQLite mock; there are no real integrations.
- **Locks protect cooperating processes only** (the CLI and the API). Code that calls
  `ExecutionRuntime` directly is not locked.
- **`executing` is per process.** The API only knows about tasks it is running itself. A
  task that the CLI is executing shows `executing: false`; resuming it returns `409` while
  the CLI holds the lock.
- **Public demo state is ephemeral and single-instance** (see above). Rate limits are in
  memory; behind Render's proxy all clients may share one rate-limit key if the last
  forwarded hop is a proxy address, which makes limits stricter, not looser.
- **No accounts or roles.** Local mode trusts whoever can reach localhost. In public mode
  the session that creates a task is the one that approves it.
- **Approver identity** is a label (`local-dashboard`, `demo-session:<id>`, `cli:<user>`),
  not an authenticated person.
- **Interrupted writes** are re-invoked with the same idempotency key within the retry
  budget; with `MAX_RECOVERY_ATTEMPTS=0` an interrupted write is failed, not reconciled.
- `langgraph` and `langchain-core` are declared dependencies but are not used by the
  runtime.

## How it works

```text
Goal → mock interpreter (strict grammar) → plan validated against the tool registry
     → policy gate (approval for employee writes) → tool calls with bounded retry
     → independent verification of database + workspace → COMPLETED / FAILED
```

- **Human-in-the-loop:** the pending action is persisted, so approval works across
  restarts and processes. Approving twice, or after a rejection, is refused.
- **Independent verification:** completion is never based on tool responses alone. The
  verifier checks the requested and planned outcomes against the actual database and
  files, and a task cannot complete while any step is unfinished.
- **Recovery:** failures are classified; only transient failures of retry-safe
  operations are retried, within the per-step budget.
- **Idempotency:** company writes are recorded with a `task_id:step_id` key in the same
  SQLite transaction, so retries and crash recovery never duplicate tickets or updates.
- **Isolation:** the database must live outside the file workspace, and the file tool
  refuses database files.

## Tests

```bash
uv sync
uv run playwright install chromium
uv run pytest
```

The suite covers the runtime and CLI, the HTTP API in both modes, a real `uvicorn`
process over HTTP (restart, concurrency, CLI interoperability), and the dashboard in
headless Chromium, where each test compares what the page shows with the API's
responses. On Linux, Chromium also needs system libraries:
`uv run playwright install --with-deps chromium` installs them (requires sudo). Browser
tests are skipped when Chromium cannot start, unless `AAE_REQUIRE_BROWSER_TESTS=1`.

CI (`.github/workflows/tests.yml`) runs the full suite with browser tests required on
Linux and Windows, and builds the Docker image and runs the smoke test against it under
Render Free limits. Windows support is not claimed until that workflow has passed.

## Package layout

```text
app/
  cli.py      # local CLI (aae / python -m app.cli)
  main.py     # FastAPI app factory, dashboard routes, aae-api entry point
  api/        # v1 routes, service layer, sessions, request guard, errors, redaction
  web/        # live dashboard (index.html, app.css, app.js)
  llm/        # LLM clients; mock.py holds the strict two-goal grammar
  planning/   # goal interpreter, planner, plan validation
  runtime/    # ExecutionRuntime, recovery, audit event types, shared task locks
  policy/     # approval policy
  tools/      # company API tool, file tool, registry, failure injection
  verify/     # independent outcome verifier
  store/      # SQLite task state and audit events
  world/      # mock company database and seed data
  models/     # domain models
demo-ui/      # scripted simulation page (offline presentations only)
docs/images/  # dashboard screenshots
scripts/      # smoke_demo.py deployment check
tests/
render.yaml   # Render Free Blueprint (public demo)
data/         # runtime database and locks (local, gitignored)
workspace/    # file tool sandbox (local, gitignored)
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Licensed under the [Apache License 2.0](LICENSE). Third-party dependencies are licensed
under their own terms.
