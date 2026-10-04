"""Safe ID factories for domain objects."""

from uuid import uuid4


def new_id(prefix: str = "") -> str:
    """Return a unique ID, optionally prefixed for observability."""
    value = uuid4().hex
    return f"{prefix}{value}" if prefix else value
