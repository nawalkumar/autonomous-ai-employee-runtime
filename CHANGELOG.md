# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for developer-preview packages (not a production stability guarantee).

## [0.3.0] — 2026-11-12

### Added

- Operation-level capability registry discoverable at `GET /api/v1/capabilities`.
- Company operations: `list_tickets`, `update_ticket`, `assign_ticket`,
  `create_customer`, `deactivate_employee`, with independent verification.
- Additive company schema migration (`employee.status`, `ticket.assignee_id`).
- Deterministic demo-company fixture (`python -m app.world.demo_fixture`) with
  reset safeguards that refuse non-demo databases.
- Typed step-output `$ref` bindings for explicit plans.
- Goal-scoped authorization with per-step constraint shapes and budgets.
- Optional dynamic LLM planning behind `AAE_DYNAMIC_PLANNING_ENABLED` (default off).
- Three trusted collection grammars (list / assign / batch status) planned
  deterministically when dynamic planning is enabled.
- Optional bounded replanning behind `AAE_REPLANNING_ENABLED` (default off);
  cannot widen the original authorization scope.
- Live dashboard collection progress, approval labels from audit state, and
  read-only replan event display.
- Local demo helper `scripts/demo_local.sh` and `docs/LOCAL_DEMO.md`.
- Demo video storyboard and presentation drafts under `docs/`.

### Changed

- Approval actions are fingerprint-bound; stale approvals are invalidated.
- Verification fails unsupported criteria; read-only tasks report `unverified`
  instead of `passed`.
- `update_employee` rejects unsupported or blank updates with `invalid_args`.
- Package version synchronized to **0.3.0** (`pyproject.toml`, `app.__version__`).

### Security

- Local mode remains unauthenticated and loopback-oriented by default.
- Documented operator hazard: binding local mode to a non-loopback address while
  still accepting `Host: 127.0.0.1` (Docker compose publishes on `127.0.0.1` only).
- Approval fingerprint remains optional on API/CLI for compatibility; the
  dashboard always sends the fingerprint it displayed.
- Scripted provider requires both `AAE_TEST_MODE=1` and `AAE_TEST_SCRIPTED_PLAN`.

### Known limitations

- Not production-ready; no public user-account system.
- Default planner accepts only two mock goal formats unless dynamic planning is on.
- Automatic replanning and live xAI planning are opt-in and narrowly scoped.
- See README Limitations and `docs/LOCAL_DEMO.md`.

## [0.2.0] — prior

- Live FastAPI dashboard and HTTP API wrapping the deterministic runtime.
- Shared OS locks between CLI and API.
- Local and public demo modes; Docker and Render Free blueprint.
- Human approval for sensitive employee updates; independent verification;
  bounded retry recovery.
