"""The installed `aae` console script runs the real CLI and propagates exit codes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(sys.executable).parent / ("aae.exe" if os.name == "nt" else "aae")
TICKET_GOAL = "Create a support ticket for Acme Corp about a $240 billing discrepancy."

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists(), reason="aae console script not installed; run `uv sync`"
)


def _aae(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DATABASE_PATH", "WORKSPACE_PATH", "LLM_PROVIDER", "XAI_API_KEY"}
    }
    env["DATABASE_PATH"] = str(tmp_path / "data" / "runtime.db")
    env["WORKSPACE_PATH"] = str(tmp_path / "workspace")
    return subprocess.run(
        [str(SCRIPT), *argv],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )


def test_aae_help_lists_all_commands(tmp_path: Path) -> None:
    proc = _aae(tmp_path, "--help")
    assert proc.returncode == 0
    for command in ("run", "status", "approve", "reject", "resume", "history"):
        assert command in proc.stdout


def test_aae_run_completes_and_exit_codes_propagate(tmp_path: Path) -> None:
    run = _aae(tmp_path, "--json", "run", TICKET_GOAL)
    assert run.returncode == 0, run.stdout + run.stderr
    task = json.loads(run.stdout)["task"]
    assert task["status"] == "completed"
    assert task["verification_status"] == "passed"

    assert _aae(tmp_path, "run", "Refund $240 to Acme Corp.").returncode == 3
    assert _aae(tmp_path, "status", "task_" + "0" * 32).returncode == 5
    assert _aae(tmp_path, "--provider", "xai", "run", TICKET_GOAL).returncode == 2
