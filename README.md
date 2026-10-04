# Autonomous AI Employee Runtime

Prototype runtime for CentrAlign AI's Founding Engineer challenge: an autonomous worker that can understand a goal, plan, use tools, observe results, recover from failures, verify outcomes, and return evidence.

## Architecture status

| Phase | Status | Scope |
|-------|--------|-------|
| **Phase 0** | Done | Project foundation (FastAPI, uv, LLM abstraction, Docker, `/health`) |
| **Phase 1** | Done | Domain contracts + `ExecutionState` + SQLite persistence + audit events |
| **Phase 2** | Done | Mock company world + tools + registry + deterministic failure injection |
| **Future** | Not started | Runtime/orchestrator, recovery, verification, HITL, live LLM integration |

### Phase 2 implemented

- Deterministic mock company world (`employees`, `customers`, `tickets`) in SQLite
- `CompanyRepository` separate from runtime `SQLiteStore` (shared DB file)
- Tool abstraction (`BaseTool`, `ToolResult`, `RiskLevel`, `ToolRegistry`)
- `CompanyAPITool` — real CRUD against the mock company DB
- `FileTool` — sandboxed read/write under `workspace/` with traversal rejection
- One-shot `FailureInjector` for demo/test transient failures
- Offline-capable: no LLM keys or external APIs required

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
  runtime/    # agent orchestrator (future)
  tools/      # BaseTool, registry, company/file tools
  llm/        # provider-agnostic LLM clients
  store/      # runtime SQLite persistence
  policy/     # risk / HITL (future)
  verify/     # outcome verification (future)
  world/      # mock company repository + seed data
tests/
workspace/    # FileTool sandbox + runtime.db (local)
```
