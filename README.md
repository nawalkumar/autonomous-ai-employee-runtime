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
| **Phase 7** | Done | Human-in-the-loop approval (pause / approve / reject / resume) |

### Phase 7 implemented

- Deterministic `ApprovalPolicy` gates sensitive tools **before** invocation
- `PendingAction` + `ApprovalStatus` persisted on `ExecutionState`
- `ExecutionRuntime.approve_task` / `reject_task` resume or terminate cleanly
- Audit events: `approval.requested`, `approval.approved`, `approval.rejected`

### Phase 6 implemented

- `OutcomeVerifier` inspects CompanyRepository + workspace filesystem
- Does **not** trust `ToolResult.ok` alone
- Success criteria / derived outcomes checked against actual world state
- Evidence recorded from observed facts only
- Task reaches `COMPLETED` only when verification passes

## Human-in-the-Loop Approval

Sensitive plan steps pause for human approval **before** the tool runs.

**Requires approval**
- `company_api` / `update_employee`
- Any tool operation classified as `destructive`

**Does not require approval**
- Read-only company operations (`get_employee`, `find_customer`, list/search)
- Demo ticket creation
- `file` writes (unless marked destructive)

When approval is required the runtime:

1. Creates a `PendingAction` (tool, arguments, risk, reason)
2. Sets `TaskStatus.NEEDS_APPROVAL` and `ApprovalStatus.PENDING`
3. Keeps the plan step `PENDING` (no mutation)
4. Emits `approval.requested` and returns control to the caller

Then:

- `runtime.approve_task(task_id)` → marks approved, resumes from the same step, continues recovery/verification
- `runtime.reject_task(task_id, reason)` → marks rejected, sets `ABORTED`, never invokes the tool

```text
Goal
  → Plan
  → Policy check
  → Approval required?
       ├── No  → Execute → Verify → Complete
       └── Yes → Pause (PendingAction)
                    ↓
               Human decision
                    ├── Approve → Execute → Verify → Complete
                    └── Reject  → Terminate (no mutation)
```

Pending approvals survive process restart via SQLite. Double-approve and approve-after-reject raise `ApprovalError("no pending approval")`.

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
  policy/     # ApprovalPolicy (HITL gate)
  verify/     # independent OutcomeVerifier + evidence
  world/      # mock company repository + seed data
tests/
workspace/    # FileTool sandbox + runtime.db (local)
```
