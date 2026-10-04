# Autonomous AI Employee Runtime

Prototype runtime for CentrAlign AI's Founding Engineer challenge: an autonomous worker that can understand a goal, plan, use tools, observe results, recover from failures, verify outcomes, and return evidence.

## Architecture status

| Phase | Status | Scope |
|-------|--------|-------|
| **Phase 0** | Done | Project foundation (FastAPI, uv, LLM abstraction, Docker, `/health`) |
| **Phase 1** | Done | Domain contracts + `ExecutionState` + SQLite persistence + audit events |
| **Future** | Not started | Runtime/orchestrator, tools, recovery, verification, HITL, live LLM integration |

### Phase 1 implemented

- Pydantic v2 domain models (`Task`, `InterpretedGoal`, `PlanStep`, records, enums)
- Central `ExecutionState` contract (JSON-serializable, orchestrator-agnostic)
- `SQLiteStore`: `save_state` / `get_state` / `append_event` / `get_events`
- Append-only audit `events` table + `tasks` snapshot table
- Unit tests for models and persistence round-trips

Default DB path: `workspace/runtime.db` (gitignored via `*.db`).

## Setup (uv)

```bash
cd /home/naval/project/Autonomous-AI-Employee-Runtime
uv sync --group dev
cp .env.example .env
```

## Environment variables

See `.env.example`. Important keys:

| Variable | Description |
|----------|-------------|
| `LLM_PROVIDER` | `mock` (default) or `xai` |
| `XAI_API_KEY` | xAI API key (do not commit) |
| `XAI_MODEL` | Model id, e.g. `grok-2-latest` |
| `APP_HOST` / `APP_PORT` | Local server bind |
| `DATABASE_PATH` | SQLite file path (default `workspace/runtime.db`) |

Never commit `.env` or real secrets.

## Local development

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## Docker

```bash
docker compose up --build
```

Then: `curl http://127.0.0.1:8000/health`

## Tests

```bash
uv run pytest
```

## Package layout

```text
app/
  api/        # HTTP routes (health)
  models/     # domain contracts + ExecutionState
  runtime/    # agent orchestrator (future)
  tools/      # tool abstractions (future)
  llm/        # provider-agnostic LLM clients
  store/      # SQLite persistence
  policy/     # risk / HITL (future)
  verify/     # outcome verification (future)
  world/      # mock company environment (future)
tests/
workspace/    # sandbox + runtime.db (local)
```
