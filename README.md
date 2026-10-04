# Autonomous AI Employee Runtime

Prototype runtime for CentrAlign AI's Founding Engineer challenge: an autonomous worker that can understand a goal, plan, use tools, observe results, recover from failures, verify outcomes, and return evidence.

## Current status

**Phase 0 — foundation / setup only.**

Implemented:

- Project layout (`app/`, `tests/`, `workspace/`)
- Dependency management via `uv`
- Settings + `.env.example`
- LLM provider abstraction (`LLMClient`, `MockLLMClient`, xAI placeholder)
- FastAPI app with `GET /health`
- Minimal pytest smoke tests
- Docker / Compose scaffolding

**Not implemented yet:** agent loop, planning, tools, persistence, HITL, verification, mock company world.

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
  api/        # HTTP routes
  models/     # domain models (future)
  runtime/    # agent orchestrator (future)
  tools/      # tool abstractions (future)
  llm/        # provider-agnostic LLM clients
  store/      # persistence (future)
  policy/     # risk / HITL (future)
  verify/     # outcome verification (future)
  world/      # mock company environment (future)
tests/
workspace/    # sandboxed file tool output (future)
```
