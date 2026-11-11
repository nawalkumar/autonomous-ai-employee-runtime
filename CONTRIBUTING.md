# Contributing

Thanks for your interest. This project is a **local prototype**: a deterministic
runtime with a mock planner, a CLI, and an HTTP API plus dashboard that wrap the same
runtime. Contributions
that improve correctness, tests, documentation and portability are welcome.

## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest -q
uv run python -m compileall -q app tests scripts
uv run aae --help
uv run playwright install chromium   # browser tests; skipped locally if unavailable
```

CI sets `AAE_REQUIRE_BROWSER_TESTS=1`, which turns a missing browser into a failure.

## Architecture

```text
app/cli.py              CLI commands, exit codes
app/main.py, app/api/   HTTP API v1, request guard, public-demo sessions
app/web/                live dashboard (static HTML/CSS/JS served by the API)
app/runtime/locking.py  per-task OS locks shared by the CLI and the API
app/llm/mock.py         strict mock interpreter/planner (two goal formats)
app/planning/           goal interpretation, planning, plan validation
app/runtime/executor.py task lifecycle, approval gate, bounded retry, crash resume
app/policy/             which tool operations need human approval
app/tools/              company API tool (idempotent writes), sandboxed file tool
app/verify/             independent outcome verification against real state
app/store/, app/world/  SQLite task state/audit events and the mock company data
demo-ui/index.html      scripted simulation (not connected to the runtime)
```

## Mock goals and the approval workflow

The CLI always uses the mock planner, which accepts only these goal formats and
rejects everything else before creating a task:

```text
Update employee E-17 title to Senior Engineer [and write a confirmation note | and confirm the change in a note file]
Create a support ticket for Acme Corp about a $240 billing discrepancy [and save a summary note]
```

`--provider xai` is disabled in the CLI. Typical flow:

```bash
uv run aae run 'Update employee E-17 title to Senior Engineer and confirm the change in a note file.'
uv run aae approve <task_id> --step step_1 --yes
uv run aae history <task_id>
```

The task pauses with exit code 4 before the employee write, and completes only after
the verifier has checked the database and workspace. See the README for all commands
and exit codes.

## Rules for changes

- Every behaviour change or bug fix needs a regression test that fails without it.
- Do not weaken the safeguards: task lifecycle guards, the per-step retry budget, the
  approval gate, idempotent company writes, database isolation, or independent
  verification. Changes to these areas need tests for the failure cases.
- Keep the mock grammar strict. New goal formats need positive and rejection tests.
- Keep the README examples and `demo-ui/index.html` consistent with real CLI output;
  `tests/test_readme.py` and `tests/test_demo_ui.py` check this.
- No new runtime dependencies without discussion.
- Never add an HTTP endpoint that runs arbitrary tools, plans, shell commands or file
  writes, or that accepts unrestricted filesystem paths.
- Dashboard code must render data with `textContent`/`createElement`, never
  `innerHTML`, and must not show values that did not come from the API.

## Runtime data and secrets

- Never commit `.env`, API keys, or anything under `data/` and `workspace/` except their
  `.gitkeep` files (databases, lock files and generated notes are gitignored).
- Use obviously fake values in tests, never real-looking credentials.
- Check `git status` before committing.

## Pull requests

- Keep pull requests small and focused, with a clear description of the problem.
- Include the commands you ran (`uv run pytest -q`, `compileall`) and their results.
- Mention any platform impact (Windows, Docker) and whether you could test it.

## Platform support

Linux/WSL2 and the Linux Docker engine are verified. Native Windows, macOS and Docker
Desktop shared folders are not yet verified; CI runs the suite on Windows, and reports
of results on other platforms are welcome.

## Reporting issues and security problems

For bugs, open an issue with the command you ran, its exit code, the `--json` output
(with any secrets removed), your OS and Python version.

Please do not report security vulnerabilities in public issues. Use GitHub's private
vulnerability reporting for this repository if it is enabled, or contact the
maintainer privately through GitHub.
