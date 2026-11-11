"""Deterministic action fingerprints for binding approvals to exact actions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.models.records import PendingAction


def action_fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    """Deterministic identity of an exact action: tool, operation and arguments.

    Canonical JSON (sorted keys, no whitespace) so that equal actions always
    produce the same fingerprint, across processes and restarts.
    """
    canonical = json.dumps(
        {"tool": tool_name, "operation": arguments.get("operation"), "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def pending_fingerprint(pending: PendingAction) -> str:
    """The action a pending approval is bound to.

    Records persisted before fingerprints existed are bound to the arguments
    they recorded, which are the arguments shown to the approver.
    """
    return pending.fingerprint or action_fingerprint(pending.tool_name, pending.arguments)


def pending_matches(pending: PendingAction, tool_name: str, arguments: dict[str, Any]) -> bool:
    """True only if the approved/requested action is exactly the action about to run."""
    current = action_fingerprint(tool_name, arguments)
    recorded = action_fingerprint(pending.tool_name, pending.arguments)
    return recorded == current and pending.fingerprint in (None, current)
