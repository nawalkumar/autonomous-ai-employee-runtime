# Autonomous AI Employee Runtime

Prototype runtime for CentrAlign AI's Founding Engineer challenge: an autonomous worker that can understand a goal, plan, use tools, observe results, recover from failures, verify outcomes, and return evidence.

## Architecture status

| Phase | Status | Scope |
|-------|--------|-------|
| **Phase 0** | Done | Project foundation (FastAPI, uv, LLM abstraction, Docker, `/health`) |
| **Phase 1** | Done | Domain contracts + `ExecutionState` + SQLite persistence + audit events |
| **Phase 2** | Done | Mock company world + tools + registry + deterministic failure injection |
| **Phase 3** | Done | Deterministic execution runtime (predefined plans, no LLM) |
| **Phase 4** | Done | Goal interpreter + planner (LLM understands/plans; runtime executes) |
| **Future** | Not started | Recovery/retry, verification, HITL |

### Phase 4 implemented

- `GoalInterpreter`: natural language → `InterpretedGoal` (structured JSON + Pydantic)
- `Planner`: `InterpretedGoal` → validated `Plan` using `ToolRegistry` metadata
- `PlanningPipeline.interpret_and_plan()` / `build_plan()`
- Default `LLM_PROVIDER=mock` with deterministic demos (E-17 title update, Acme ticket)
- Optional `LLM_PROVIDER=xai` via existing OpenAI-compatible client (opt-in, not required for tests)
- Plan validation: tool existence, args schema, unique step IDs, non-empty plan
- LLM never executes tools or mutates SQLite/files — only `ExecutionRuntime` does

Default DB path: `workspace/runtime.db` (gitignored).

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
| `WORKSPACE_PATH` | FileTool sandbox root (default `workspace`) |

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
  runtime/    # deterministic ExecutionRuntime
  planning/   # GoalInterpreter + Planner + validation
  tools/      # BaseTool, registry, company/file tools
  llm/        # provider-agnostic LLM clients (mock/xai)
  store/      # runtime SQLite persistence
  policy/     # risk / HITL (future)
  verify/     # outcome verification (future)
  world/      # mock company repository + seed data
tests/
workspace/    # FileTool sandbox + runtime.db (local)
```
