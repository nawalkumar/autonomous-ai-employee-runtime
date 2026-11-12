# Presentation drafts — v0.3.0 (do not publish automatically)

Drafts only. No content in this file has been published.

## GitHub project description (≤350 characters)

```text
Local-first developer preview of an accountable AI employee runtime: trusted goal grammars, goal-scoped authorization, human approval, idempotent tools, independent verification, and durable SQLite audit history. Not production software.
```

## README introduction (visitor-facing)

Use as the opening of the repository README:

> Prototype runtime that turns a narrow natural-language goal into a bounded,
> authorized plan, executes tools against a fictional company system, pauses for
> human approval on sensitive writes, retries transient failures within a budget,
> and independently verifies outcomes before reporting success. **v0.3.0 is a
> local-first developer preview** — not a production autonomous employee.

## v0.3.0 release-note draft

**Title:** `v0.3.0 — Local-first developer preview (release candidate)`

**Body:**

```markdown
## Summary

v0.3.0 packages the Autonomous AI Employee Runtime as a reproducible
**local-first developer preview**. It demonstrates accountable execution —
not unrestricted autonomy.

## Highlights

- Goal-scoped authorization derived from trusted grammars (not from the model)
- Human-in-the-loop approval with action fingerprints
- Bounded collection workflows (list / assign / batch status) when dynamic planning is enabled
- Independent verification against SQLite + workspace
- Idempotent company writes and durable audit events
- Optional bounded replanning (off by default; cannot widen authorization)
- Live dashboard backed by the real API (not the offline `demo-ui` simulation)

## Quick start

```bash
uv sync
./scripts/demo_local.sh
# open http://127.0.0.1:8000/
```

See `docs/LOCAL_DEMO.md` and `CHANGELOG.md`.

## Explicit non-claims

- Not production-ready
- No public user-account system
- Local mode trusts whoever can reach loopback
- Scripted test providers are for controlled tests, not live LLM demos
```

## LinkedIn post draft — “Building the Background”

```text
Building the Background — shipping a local-first developer preview of an
accountable AI employee runtime (v0.3.0 RC).

The hard problem is not “can the model write a plan?” It’s whether the system
around the model can refuse unsafe work, pause for a human, prove what changed,
and recover without inventing success.

What’s in this preview:

• Goal-scoped authorization from trusted grammars (the model doesn’t grant permissions)
• Human-in-the-loop approval bound to the exact pending action
• Bounded retries and optional replanning that cannot widen the original scope
• Idempotent tool execution against a fictional company API
• Independent verification that re-reads SQLite before “completed”
• Durable audit history the dashboard reloads after a refresh

This is a developer preview on localhost — not a production autonomous employee,
and not a claim of unrestricted natural-language autonomy.

Repo + local demo notes in the comments. #BuildingTheBackground #AIEngineering
```

## Short demo-video caption

```text
AI agents can make plans. This runtime makes them accountable — trusted scopes,
human approval, real tool calls, independent verification, durable audit history.
v0.3.0 local-first developer preview (not production).
```
