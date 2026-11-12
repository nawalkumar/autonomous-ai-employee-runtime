#!/usr/bin/env bash
# Start a reproducible local-first demo of the Autonomous AI Employee Runtime.
#
# Safe by design:
#   - Uses an isolated demo database (default: data/demo.db)
#   - Never touches data/runtime.db
#   - Never resets a database unless --reset is passed
#   - Binds to 127.0.0.1 only
#   - Leaves external LLM providers and scripted test providers off
#   - Leaves automatic replanning off
#
# Usage:
#   ./scripts/demo_local.sh              # seed if needed, then start aae-api
#   ./scripts/demo_local.sh --reset      # recreate a marked demo DB, then start
#   ./scripts/demo_local.sh --seed-only  # seed/verify only; do not start the server
#   ./scripts/demo_local.sh --help

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DEMO_DB="${AAE_DEMO_DATABASE:-data/demo.db}"
DEMO_WORKSPACE="${AAE_DEMO_WORKSPACE:-workspace}"
RESET=0
SEED_ONLY=0

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset) RESET=1; shift ;;
    --seed-only) SEED_ONLY=1; shift ;;
    --database)
      DEMO_DB="${2:?--database requires a path}"; shift 2 ;;
    --workspace)
      DEMO_WORKSPACE="${2:?--workspace requires a path}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$DEMO_DB" == *"runtime.db"* ]]; then
  echo "Refusing to use a path that looks like the default runtime database: $DEMO_DB" >&2
  echo "Pass a disposable demo path such as data/demo.db." >&2
  exit 2
fi

mkdir -p "$(dirname "$DEMO_DB")" "$DEMO_WORKSPACE"

SEED_ARGS=(--database "$DEMO_DB")
if [[ "$RESET" -eq 1 ]]; then
  SEED_ARGS+=(--reset)
fi

echo "==> Preparing demo database: $DEMO_DB"
uv run python -m app.world.demo_fixture "${SEED_ARGS[@]}"

if [[ "$SEED_ONLY" -eq 1 ]]; then
  echo "==> Seed complete (--seed-only). Server not started."
  exit 0
fi

export DATABASE_PATH="$DEMO_DB"
export WORKSPACE_PATH="$DEMO_WORKSPACE"
export APP_MODE=local
export APP_HOST=127.0.0.1
export APP_PORT="${APP_PORT:-8000}"
export LLM_PROVIDER=mock
export AAE_DYNAMIC_PLANNING_ENABLED=true
export AAE_REPLANNING_ENABLED=false
unset AAE_TEST_MODE AAE_TEST_SCRIPTED_PLAN XAI_API_KEY || true

echo "==> Starting local demo API (loopback only)"
echo "    DATABASE_PATH=$DATABASE_PATH"
echo "    WORKSPACE_PATH=$WORKSPACE_PATH"
echo "    AAE_DYNAMIC_PLANNING_ENABLED=$AAE_DYNAMIC_PLANNING_ENABLED"
echo "    AAE_REPLANNING_ENABLED=$AAE_REPLANNING_ENABLED"
echo "    LLM_PROVIDER=$LLM_PROVIDER"
echo "    Open http://127.0.0.1:${APP_PORT}/"
echo "    Ctrl+C stops the server; the demo database is left intact."
exec uv run aae-api
