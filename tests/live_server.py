"""Run the real application in a separate uvicorn process for end-to-end tests."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
# Drop host-process overrides so each LiveServer starts from explicit ``extra`` only.
# Test-mode / scripted / replanning flags must not leak from earlier pytest cases
# that mutate ``os.environ`` or from the developer's shell.
_CLEARED = {
    "XAI_API_KEY", "LLM_PROVIDER", "DATABASE_PATH", "WORKSPACE_PATH", "APP_MODE", "PORT",
    "ALLOWED_HOSTS", "PUBLIC_DATA_ROOT", "PUBLIC_ACCESS_CODE", "RENDER_EXTERNAL_HOSTNAME",
    "AAE_DYNAMIC_PLANNING_ENABLED", "AAE_REPLANNING_ENABLED", "AAE_MAX_REPLAN_ROUNDS",
    "AAE_TEST_MODE", "AAE_TEST_SCRIPTED_PLAN",
}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def base_env(root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _CLEARED}
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "DATABASE_PATH": str(root / "data" / "runtime.db"),
            "WORKSPACE_PATH": str(root / "workspace"),
        }
    )
    env.update(extra or {})
    return env


class LiveServer:
    """One uvicorn process; ``restart`` starts a new process on the same storage."""

    def __init__(self, root: Path, extra_env: dict[str, str] | None = None) -> None:
        self.root = root
        self.env = base_env(root, extra_env)
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.log = root / "server.log"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 30.0) -> "LiveServer":
        self.root.mkdir(parents=True, exist_ok=True)
        log = self.log.open("ab")
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning",
            ],
            cwd=self.root,
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited: {self.log.read_text(errors='replace')}")
            try:
                if httpx.get(f"{self.url}/api/v1/health", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        self.stop()
        raise RuntimeError("server did not become healthy")

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=15)
        self.proc = None

    def restart(self) -> "LiveServer":
        """New process on the same storage (and a fresh port, avoiding TIME_WAIT reuse)."""
        self.stop()
        self.port = free_port()
        return self.start()
