# Demo video storyboard (v0.3.0) — 90–120 seconds

Ready-to-record technical demonstration. **No video has been recorded.**

**Narrative:** “AI agents can make plans. I built a runtime that makes them accountable.”

**Setup before recording** (terminal, leave running):

```bash
./scripts/demo_local.sh --reset
# Open http://127.0.0.1:8000/ in Chromium/Chrome
```

Optional second terminal for a one-line API peek near the end:

```bash
curl -s http://127.0.0.1:8000/api/v1/health
```

---

## Scene sequence

| # | Duration | On screen | Terminal / dashboard actions | Expected result | Narration |
|---|----------|-----------|------------------------------|-----------------|-----------|
| 1 | 0:00–0:08 | Editor or terminal + title card text | Show repo name / `aae-api` already running on `127.0.0.1:8000` | Health / dashboard loads | “AI agents can make plans. I built a runtime that makes them accountable.” |
| 2 | 0:08–0:22 | Dashboard Goal panel | Click **List up to 5 open high-priority tickets.** → Run | Real task ID appears; status moves through execution; ticket IDs visible in tool result / collection | “Natural-language goals go through a trusted grammar. This is a bounded read-only list — real ticket IDs from SQLite.” |
| 3 | 0:22–0:40 | Dashboard Execution + Collection | Click **Assign up to 2 open high-priority tickets to employee E-40.** → Run | Plan shows `list_tickets` then assignments; collection counters update; items reach `verified` | “Trusted bounded planning expands a sealed collection. Mutations run through the real company API and independent verification.” |
| 4 | 0:40–1:00 | Approval card | Click **Mark tickets T-10009 and T-10010 as closed.** → wait for pause → open Approve → confirm | Pending args visible; task stays paused until approval; after approve, second ticket may pause | “Sensitive writes stop for human approval. The fingerprint binds the exact action — no silent mutation.” |
| 5 | 1:00–1:12 | Verification panel | After second approval, scroll Verification | Checks show expected vs actual; task `completed` / verification `passed` | “Success is not a model claim. An independent verifier re-reads the database.” |
| 6 | 1:12–1:28 | Browser reload or History | Reload page; open prior task from History / `#task=` | Same plan, audit timeline, verification restored | “Everything is durable in SQLite — reload the browser and the audit history is still there.” |
| 7 | 1:28–1:45 | Optional: policy failure | Run **Assign ticket T-10018 to eligible Support employee E-45.** | Status `failed`; no success banner | “Business rules fail closed. The runtime does not fabricate success.” |
| 8 | 1:45–2:00 | Closing frame | Show Limitations / local-first badge or health JSON `version` | `version` 0.3.0, mode local | “Local-first developer preview — goal-scoped authz, HITL, idempotent execution, independent verification. Not a production autonomous employee.” |

Total target: **~100 seconds** (trim scene 7 if under time pressure).

---

## Exact commands (pre-roll / cutaways)

```bash
# Prepare (before camera)
./scripts/demo_local.sh --reset

# Optional CLI twin of scene 2 (cutaway)
DATABASE_PATH=data/demo.db AAE_DYNAMIC_PLANNING_ENABLED=true \
  uv run aae run 'List up to 5 open high-priority tickets.'

# Inspect evidence (cutaway)
uv run aae history <task_id>
curl -s http://127.0.0.1:8000/api/v1/tasks/<task_id>/verification | head
```

Do **not** set `AAE_TEST_MODE`, `AAE_TEST_SCRIPTED_PLAN`, or `LLM_PROVIDER=xai` during this recording.

---

## What to highlight on screen

- Real task UUID / ID from the API
- Plan steps with tool + operation names
- Collection item IDs and `verified` vs merely `completed`
- Approval dialog with arguments
- Verification checks (expected vs actual)
- Audit timeline timestamps
- Loopback URL `127.0.0.1:8000`

## What must not be claimed

- Production-ready autonomous employee
- Multi-user accounts / enterprise SSO
- Unrestricted natural language → arbitrary tools
- Live LLM replanning (unless you separately show a labelled scripted test)
- Public internet deployment as the default trust model
- That the offline `demo-ui/` page is the live runtime

## Recording checklist

- [ ] Fresh `--reset` demo DB
- [ ] Dynamic planning on; replanning off; mock provider
- [ ] Browser zoom readable; hide personal bookmarks
- [ ] Mic check; 90–120 s single take preferred
- [ ] Export caption from `docs/RELEASE_PRESENTATION_DRAFTS.md`
