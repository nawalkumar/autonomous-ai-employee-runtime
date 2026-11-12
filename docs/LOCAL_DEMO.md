# Local demo guide (v0.3.0 developer preview)

This guide runs the **real** FastAPI API, SQLite store, authorization, approval
policy and independent verifier against a disposable demo company database.

It is a **local-first developer preview**, not a production deployment.
Scripted-provider scenarios are labelled as controlled integration setups, not
live LLM reasoning.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- A browser on the same machine

## One-command startup

From the repository root:

```bash
chmod +x scripts/demo_local.sh   # once
./scripts/demo_local.sh
```

Open <http://127.0.0.1:8000/>.

Equivalent manual steps:

```bash
uv sync
uv run python -m app.world.demo_fixture --database data/demo.db
DATABASE_PATH=data/demo.db \
WORKSPACE_PATH=workspace \
APP_MODE=local \
APP_HOST=127.0.0.1 \
LLM_PROVIDER=mock \
AAE_DYNAMIC_PLANNING_ENABLED=true \
AAE_REPLANNING_ENABLED=false \
uv run aae-api
```

Safety rules enforced by the fixture and helper:

| Action | Behavior |
|--------|----------|
| Seed `data/demo.db` | Allowed when new, empty, or already marked as demo |
| Seed / reset `data/runtime.db` | **Refused** |
| `--reset` | Recreates only a **marked** demo database |
| Default bind | `127.0.0.1` (loopback) |
| External LLM | Not enabled (`LLM_PROVIDER=mock`) |
| Scripted test provider | Not enabled (`AAE_TEST_*` unset) |
| Automatic replanning | Off (`AAE_REPLANNING_ENABLED=false`) |

Copy `.env.example` to `.env` only if you need persistent overrides. Never commit
`.env` or real API keys.

## Shut down

Press `Ctrl+C` in the server terminal. The demo database and workspace files
remain on disk. To recreate the demo company:

```bash
./scripts/demo_local.sh --reset --seed-only
# or
uv run python -m app.world.demo_fixture --database data/demo.db --reset
```

## Demo acceptance scenarios

Run these against a freshly seeded `data/demo.db`. Prefer the dashboard; the
CLI commands below produce the same durable state.

Evidence for every scenario must come from the API (`/api/v1/tasks/{id}`,
`/history`, `/verification`) or from SQLite — not from narration alone.

### Demo A — Read-only task

**Goal:** `List up to 5 open high-priority tickets.`

Prefer one-shot environment prefixes (do not `export` these into your shell —
that can leak into later pytest runs):

```bash
DATABASE_PATH=data/demo.db AAE_DYNAMIC_PLANNING_ENABLED=true \
  uv run aae run 'List up to 5 open high-priority tickets.'
```

**Expect:**

- Task completes (or `unverified` verification status for a read-only plan).
- Tool result contains real ticket IDs from the fixture (open high-priority /
  urgent tickets such as `T-10001`–`T-10004` among others).
- No mutating company writes.

### Demo B — Bounded mutation

**Goal:** `Assign up to 2 open high-priority tickets to employee E-40.`

```bash
DATABASE_PATH=data/demo.db AAE_DYNAMIC_PLANNING_ENABLED=true \
  uv run aae run 'Assign up to 2 open high-priority tickets to employee E-40.'
```

**Expect:**

- Trusted deterministic collection plan (`list_tickets` then assignments).
- Collection progress with selected / processed counts.
- Assignments verified independently (`verified` items).
- At most two tickets assigned to `E-40`.

### Demo C — Human approval

**Goal:** `Mark tickets T-10009 and T-10010 as closed.`

```bash
DATABASE_PATH=data/demo.db AAE_DYNAMIC_PLANNING_ENABLED=true \
  uv run aae run 'Mark tickets T-10009 and T-10010 as closed.'
# exit code 4 → NEEDS_APPROVAL
uv run aae approve <task_id> --step <pending_step> --yes
# approve again for the second ticket when it pauses
```

On the dashboard: submit the goal, read the pending arguments, confirm Approve
twice (once per ticket).

**Expect:**

- Pause before each close mutation.
- Pending action shows operation, arguments and fingerprint.
- After approvals, both tickets are `closed` and verification passes.

### Demo D — Policy enforcement

**Goal:** `Assign ticket T-10018 to eligible Support employee E-45.`

```bash
DATABASE_PATH=data/demo.db AAE_DYNAMIC_PLANNING_ENABLED=true \
  uv run aae run 'Assign ticket T-10018 to eligible Support employee E-45.'
```

**Expect:**

- Task status `failed` (or equivalent failure path).
- Company rule blocks inactive / ineligible assignee; no fabricated success.
- Ticket `T-10018` remains unchanged.

### Demo E — Recovery and replanning (scripted provider)

**Label clearly:** this uses a **scripted** plan in a test process. It is not a
live LLM demonstration.

Automatic replanning stays off in normal demo mode. To inspect persisted
`replan.*` events, use the existing controlled browser / integration tests:

```bash
AAE_REQUIRE_BROWSER_TESTS=1 AAE_TEST_MODE=1 \
  uv run pytest -q tests/test_dashboard_operations.py -k replan
```

Those tests set `AAE_TEST_SCRIPTED_PLAN` only inside the test process. Do not
export those variables for the normal demo server.

### Demo F — Persistence

1. Complete Demo A or B and note the task ID.
2. Reload the browser, or stop and restart `aae-api` with the same
   `DATABASE_PATH`.
3. Open `#task=<task_id>` or select the task from History.

**Expect:**

- Same task status, plan, audit events and verification from the backend.
- No reconstruction from browser-only storage.

## Observability cheat sheet

```bash
curl -s http://127.0.0.1:8000/api/v1/health
curl -s http://127.0.0.1:8000/api/v1/tasks?limit=5
curl -s http://127.0.0.1:8000/api/v1/tasks/<task_id>
curl -s http://127.0.0.1:8000/api/v1/tasks/<task_id>/history
curl -s http://127.0.0.1:8000/api/v1/tasks/<task_id>/verification
```

CLI equivalents: `aae status`, `aae history`, `aae --json status <task_id>`.

## What not to claim

- Production readiness or multi-user authentication.
- Unrestricted natural-language autonomy.
- That scripted recovery is live model reasoning.
- That personal / commercial redistribution rights differ from the
  [Apache License 2.0](../LICENSE) — that license governs use, modification and
  redistribution under its terms.
