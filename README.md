# Autonomous AI Employee Runtime

Prototype runtime for CentrAlign AI's Founding Engineer challenge: an AI worker that
interprets a goal, plans tool calls, executes them against a mock company system,
recovers from transient failures, pauses for human approval before sensitive writes,
and independently verifies the outcome before reporting success.

**Status:** working local prototype, not production software. The planner is a
deterministic mock that accepts exactly two goal formats (below). The CLI is the only
way to run tasks; the HTTP API exposes `/health` only.

## Platform support

| Platform | Status |
|----------|--------|
| Linux / WSL2, Python 3.10, `uv` | Verified: full test suite and CLI workflows |
| Docker (Linux engine) | Verified: CLI in container, shared data with the host CLI |
| Docker Desktop (Windows / macOS) | Not verified (file ownership and locking on shared folders may differ) |
| Native Windows | Not verified (the CLI has a `msvcrt` lock implementation that has not been run on Windows) |
| macOS | Not verified |

## Quick start

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repository-url>
cd Autonomous-AI-Employee-Runtime
uv sync
uv run aae --help
```

`uv sync` installs the project and the `aae` command. `python -m app.cli` is
equivalent to `aae`. Optionally copy `.env.example` to `.env` to change paths.

By default the CLI stores its SQLite database in `./data/runtime.db` and its file
workspace in `./workspace`, relative to the directory you run it from. Every command
prints the resolved paths.

## Supported goals (mock planner)

The CLI always uses the deterministic mock planner. It accepts **only** these goal
formats (case, extra spaces and a final period are ignored) and rejects everything
else before any task is created:

```text
Update employee E-17 title to Senior Engineer [and write a confirmation note | and confirm the change in a note file]
Create a support ticket for Acme Corp about a $240 billing discrepancy [and save a summary note]
```

Use **single quotes** in the shell so `$240` is not expanded.

## CLI walkthrough

### 1. Create a ticket (and optionally a summary note)

```bash
uv run aae run 'Create a support ticket for Acme Corp about a $240 billing discrepancy.'
uv run aae run 'Create a support ticket for Acme Corp about a billing discrepancy of $240 and save a summary note.'
```

Plan: `find_customer` → `create_ticket` (→ `file.write acme_billing_summary.txt`).
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

State is persisted in SQLite, so any later process (another terminal, after a reboot,
or a container) can continue:

```bash
uv run aae status <task_id>
uv run aae history <task_id>
uv run aae resume <task_id>
uv run aae --json status <task_id>
```

- `status` shows steps, retries, any pending approval, and verification results.
- `history` lists the stored audit events in order (for example `approval.requested`,
  `approval.approved`, `verification.passed`, `TASK_COMPLETED`).
- `resume` continues a queued or interrupted task. Paused and finished tasks are left
  unchanged.
- `--json` (before the command) prints machine-readable output.

### 4. Simulated transient failure and bounded retry

```bash
uv run aae run 'Create a support ticket for Acme Corp about a billing discrepancy of $240 and save a summary note.' --inject-transient create_ticket
```

`--inject-transient create_ticket` makes the **first** `create_ticket` call in this
process fail with `503 Service Unavailable (simulated by --inject-transient)`. It is a
deterministic demo switch, not a real outage: the mock company API never fails on its
own. The runtime classifies the failure as transient, retries once (`retry_count` 1),
and then verifies the outcome. The retry budget (`MAX_RECOVERY_ATTEMPTS`, default 2) is
persisted per step, so restarts cannot reset it.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Task completed, or a read command succeeded |
| 1 | Task failed or aborted |
| 2 | Usage, configuration or file-permission error |
| 3 | Goal rejected by the mock planner (no task created) |
| 4 | Task is waiting for approval |
| 5 | Unknown task, or approval refused (wrong step, no `--yes`, declined, nothing pending) |
| 6 | Task is locked by another CLI process (nothing changed) |
| 70 | Unexpected internal error |

### Limits of the CLI

- Mock planner only. `--provider xai` is refused (exit 2): goal grounding with the
  live xAI provider has not been validated. `LLM_PROVIDER` in the environment does not
  change the CLI.
- Mutating commands take an exclusive per-task lock (`data/locks/`), so two CLI
  processes cannot run or approve the same task at once. This protects cooperating CLI
  processes only, not code that calls `ExecutionRuntime` directly.
- Approvals are recorded as made by a human, without an approver identity.
- There is no `list` command; keep the task IDs printed by `run`.

## Docker

The image runs as a non-root user whose UID/GID default to `1000:1000`. Compose mounts
`./data` and `./workspace` from the repository (both exist in a fresh clone), so files
created by the container stay owned by your user and the host CLI can use them.

```bash
APP_UID=$(id -u) APP_GID=$(id -g) docker compose build
docker compose run --rm api aae run 'Update employee E-17 title to Senior Engineer and confirm the change in a note file.'
uv run aae status <task_id>
docker compose run --rm api aae approve <task_id> --step step_1 --yes
```

Pass the same `APP_UID`/`APP_GID` to `docker compose run` if they differ from
`1000:1000`. `docker compose up` starts the API, which only serves `/health`.

On Docker Desktop for Windows or macOS, shared folders are translated by the VM; file
ownership and lock behaviour across host and container have not been verified there.

## HTTP API

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```

The API returns `{"status": "ok"}` and has no task submission, execution or approval
endpoints yet.

## Demo UI

`demo-ui/index.html` is a **scripted simulation** for presentations. It replays
recorded results of real CLI runs of the two supported goals (ticket `T-10001`,
`acme_billing_summary.txt`, `employee_update.txt`, the real pending-action arguments and
event names). It is not connected to the runtime and does not execute anything; open it
directly in a browser.

## How it works

```text
Goal → mock interpreter (strict grammar) → plan validated against the tool registry
     → policy gate (approval for employee writes) → tool calls with bounded retry
     → independent verification of database + workspace → COMPLETED / FAILED
```

- **Human-in-the-loop:** `company_api.update_employee` (and any destructive tool)
  pauses the task before the tool runs. The pending action is persisted, so approval
  works across restarts. Approving twice, or after a rejection, is refused.
- **Independent verification:** completion is never based on tool responses alone. The
  verifier checks the requested and planned outcomes against the actual database and
  files, and a task cannot complete while any step is unfinished.
- **Recovery:** failures are classified; only transient failures of retry-safe
  operations are retried, within the per-step budget.
- **Idempotency:** company writes are recorded with a `task_id:step_id` key in the same
  SQLite transaction, so retries and crash recovery never duplicate tickets or updates.
- **Isolation:** the database must live outside the file workspace, and the file tool
  refuses database files.

## Configuration

See `.env.example`.

| Variable | Description |
|----------|-------------|
| `DATABASE_PATH` | SQLite file (default `data/runtime.db`; must be outside `WORKSPACE_PATH`) |
| `WORKSPACE_PATH` | File tool sandbox (default `workspace`) |
| `MAX_RECOVERY_ATTEMPTS` | Retries allowed after a step's first attempt (default `2`) |
| `LLM_PROVIDER`, `XAI_API_KEY`, `XAI_MODEL`, `XAI_BASE_URL` | Provider settings for library use; ignored by the CLI |
| `APP_HOST` / `APP_PORT` | API bind address |

Never commit `.env` or real secrets.

## Tests

```bash
uv sync
uv run pytest
```

CI runs the suite on Linux and Windows (`.github/workflows/tests.yml`). Windows support
is not claimed until that workflow has passed on a Windows runner.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Licensed under the [Apache License 2.0](LICENSE). Third-party dependencies are licensed
under their own terms.

## Package layout

```text
app/
  cli.py      # local CLI (aae / python -m app.cli)
  main.py     # FastAPI app (/health only)
  api/        # HTTP routes
  llm/        # LLM clients; mock.py holds the strict two-goal grammar
  planning/   # goal interpreter, planner, plan validation
  runtime/    # ExecutionRuntime, recovery, audit event types
  policy/     # approval policy
  tools/      # company API tool, file tool, registry, failure injection
  verify/     # independent outcome verifier
  store/      # SQLite task state and audit events
  world/      # mock company database and seed data
  models/     # domain models
demo-ui/      # scripted simulation page (not connected to the runtime)
tests/
data/         # runtime database and locks (local, gitignored)
workspace/    # file tool sandbox (local, gitignored)
```
