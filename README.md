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
| **Phase 5** | Done | Observe → classify failure → bounded recovery/retry → continue plan |
| **Phase 6** | Done | Independent verification + evidence before COMPLETED |
| **Future** | Not started | HITL |

### Phase 6 implemented

- `OutcomeVerifier` inspects CompanyRepository + workspace filesystem
- Does **not** trust `ToolResult.ok` alone
- Success criteria / derived outcomes checked against actual world state
- Evidence recorded from observed facts only
- Task reaches `COMPLETED` only when verification passes

## Independent Verification

The executor performs the requested actions, but completion is not
declared based solely on tool success. A deterministic verifier
independently inspects the resulting company/workspace state and
produces evidence before the task can reach COMPLETED.

```text
Execute
   ↓
Actual World State
   ↓
Independent Verifier
   ↓
Checks
   ↓
Evidence
   ↓
Verified?
 ├── YES → COMPLETED
 └── NO  → Verification Failed
```

### Why this exists

- **Action success:** a tool returned `ok=true`
- **Outcome success:** the intended company/workspace state actually exists

## Failure Recovery

The runtime can:

- observe tool failures
- classify failures
- distinguish transient vs non-retryable failures
- apply bounded recovery policies
- retry safe transient failures
- preserve execution history
- continue the plan after successful recovery
- persist recovery events

```text
Execute
  ↓
Observe
  ↓
Failure?
 ├── No → Continue
 └── Yes
       ↓
   Classify
       ↓
   Recovery Policy
       ↓
   Retry allowed?
    ├── Yes → Retry → Continue
    └── No  → Fail safely
```

This phase does **not** implement HITL or independent verification.

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
| `MAX_RECOVERY_ATTEMPTS` | Max recovery retries after first failure (default `2`) |

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
  runtime/    # deterministic ExecutionRuntime + recovery
  planning/   # GoalInterpreter + Planner + validation
  tools/      # BaseTool, registry, company/file tools
  llm/        # provider-agnostic LLM clients (mock/xai)
  store/      # runtime SQLite persistence
  policy/     # risk / HITL (future)
  verify/     # independent OutcomeVerifier + evidence
  world/      # mock company repository + seed data
tests/
workspace/    # FileTool sandbox + runtime.db (local)
```
