"""Turning a model reply into usable text and JSON."""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def response_text(message: Any) -> str:
    """Flatten a response into plain text.

    Gemini 3.x can return content as a list of blocks rather than a string, so
    `.text` is preferred over `.content`. In current langchain-core `.text` is a
    property returning a str subclass that is *also* callable for backwards
    compatibility, so the isinstance check must come before the callable check
    or merely reading it emits a deprecation warning.
    """
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return str(text)
    if callable(text):  # pragma: no cover - legacy langchain-core
        return str(text())

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


def extract_json(text: str) -> Optional[Any]:
    """Best-effort parse of a model reply into Python data.

    With response_mime_type=application/json the first attempt normally
    succeeds. The fenced-block and brace-slice fallbacks cover replies that
    arrive wrapped in markdown or with a stray preamble.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None

    candidates = [stripped]

    fenced = _FENCE_RE.search(stripped)
    if fenced:
        candidates.append(fenced.group(1))

    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = stripped.find(opener), stripped.rfind(closer)
        if 0 <= start < end:
            candidates.append(stripped[start:end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None
