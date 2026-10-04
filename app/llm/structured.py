"""Provider-agnostic structured JSON helpers."""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.base import LLMClient, LLMRequest, LLMResponse

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(ValueError):
    """Raised when an LLM response cannot be parsed into the expected schema."""


def extract_json_text(content: str) -> str:
    """Extract a JSON object/array from model content (raw or fenced)."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue
    raise StructuredOutputError("LLM response did not contain valid JSON")


async def complete_structured(
    client: LLMClient,
    request: LLMRequest,
    schema: type[T],
) -> T:
    """Ask the LLM for a completion and validate it as `schema`."""
    response = await client.complete(request)
    return parse_structured(response, schema)


def parse_structured(response: LLMResponse, schema: type[T]) -> T:
    try:
        payload = extract_json_text(response.content)
        data = json.loads(payload)
    except StructuredOutputError:
        raise
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"Invalid JSON from LLM: {exc}") from exc

    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise StructuredOutputError(
            f"LLM JSON failed schema validation for {schema.__name__}: {exc}"
        ) from exc
