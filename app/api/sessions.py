"""Per-browser-session sandboxes for the public demo.

Each session gets its own SQLite database (runtime state, audit events and a
freshly seeded mock company) and its own file workspace under an ephemeral root.
Sessions never see each other's tasks, approvals or files. The session token is a
random 256-bit value held only in an HttpOnly cookie; the server keeps its
SHA-256 digest. Sessions live in memory: a restart wipes the storage root, so
every earlier token and task ID becomes invalid instead of being reported stale.
"""

from __future__ import annotations

import hashlib
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.api.errors import ApiError
from app.config import Settings

SESSION_COOKIE = "aae_demo_session"


@dataclass
class DemoSession:
    session_id: str
    root: Path
    created_at: float
    last_seen: float = field(default=0.0)

    @property
    def approver(self) -> str:
        return f"demo-session:{self.session_id[:12]}"


class SessionManager:
    def __init__(
        self,
        root: Path,
        *,
        max_sessions: int,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = root
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, DemoSession] = {}
        self._guard = threading.Lock()

    def reset_storage(self) -> None:
        """Erase all demo state; called once at startup."""
        with self._guard:
            self._sessions.clear()
            if self.root.exists():
                shutil.rmtree(self.root)
            self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, is_busy: Callable[[str], bool]) -> tuple[str, DemoSession]:
        self.purge_expired(is_busy)
        token = secrets.token_urlsafe(32)
        session_id = self._digest(token)[:32]
        now = self._clock()
        with self._guard:
            if len(self._sessions) >= self.max_sessions:
                raise ApiError(
                    503,
                    "demo_capacity",
                    "The public demo is at capacity. Please try again later.",
                    headers={"Retry-After": "60"},
                )
            session = DemoSession(session_id, self.root / session_id, now, now)
            self._sessions[session_id] = session
        (session.root / "data").mkdir(parents=True, exist_ok=True)
        (session.root / "workspace").mkdir(parents=True, exist_ok=True)
        return token, session

    def resolve(self, token: str | None) -> DemoSession | None:
        if not token or len(token) > 128:
            return None
        session_id = self._digest(token)[:32]
        now = self._clock()
        with self._guard:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if now - session.last_seen > self.ttl_seconds:
                return None
            session.last_seen = now
            return session

    def purge_expired(self, is_busy: Callable[[str], bool]) -> int:
        now = self._clock()
        with self._guard:
            expired = [
                s
                for s in self._sessions.values()
                if now - s.last_seen > self.ttl_seconds and not is_busy(s.session_id)
            ]
            for session in expired:
                del self._sessions[session.session_id]
        for session in expired:
            shutil.rmtree(session.root, ignore_errors=True)
        return len(expired)

    def count(self) -> int:
        with self._guard:
            return len(self._sessions)

    @staticmethod
    def settings_for(session: DemoSession, base: Settings) -> Settings:
        return base.model_copy(
            update={
                "database_path": str(session.root / "data" / "runtime.db"),
                "workspace_path": str(session.root / "workspace"),
            }
        )
