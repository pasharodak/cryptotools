"""One-shot Cursor agent calls for the trading bots.

Uses the user API key in ``CURSOR`` (or ``CURSOR_API_KEY``). Each call is a
cloud agent with no repository, so it cannot edit the trading tree. The
answer is the agent's final text, which the bots parse as JSON.
"""
from __future__ import annotations

import os
import threading
from typing import Any

DEFAULT_MODEL = "composer-2.5"
PAIR_TIMEOUT_SEC = 150.0
BOOK_TIMEOUT_SEC = 240.0


class CursorCallError(RuntimeError):
    """The agent did not return a usable answer."""


class CursorRateError(CursorCallError):
    """The key is rate-limited. The bot should pause before the next cycle."""

    def __init__(self, message: str, *, retry_after: float = 1800.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _rate_limited(message: str) -> bool:
    text = (message or "").lower()
    return "rate limit" in text or "too many visits" in text or "too many requests" in text


def api_key() -> str:
    return (os.environ.get("CURSOR") or os.environ.get("CURSOR_API_KEY") or "").strip()


def model_name() -> str:
    raw = (os.environ.get("CT_CURSOR_MODEL") or DEFAULT_MODEL).strip()
    return raw or DEFAULT_MODEL


def configured() -> bool:
    return bool(api_key())


def _options():
    from cursor_sdk import AgentOptions, CloudAgentOptions

    return AgentOptions(
        model=model_name(),
        api_key=api_key(),
        mode="agent",
        cloud=CloudAgentOptions(repos=[]),
    )


def complete(prompt: str, *, timeout: float = PAIR_TIMEOUT_SEC) -> str:
    """Run one cloud agent and return its final text."""
    key = api_key()
    if not key:
        raise CursorCallError("CURSOR token is not set")
    from cursor_sdk import Agent, CursorAgentError, RateLimitError

    holder: dict[str, Any] = {}

    def _call() -> None:
        try:
            holder["result"] = Agent.prompt(prompt, _options())
        except Exception as exc:  # noqa: BLE001
            holder["error"] = exc

    worker = threading.Thread(target=_call, name="cursor-agent", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise CursorCallError(f"cursor agent timed out after {int(timeout)}s")
    error = holder.get("error")
    if error is not None:
        message = str(error)[:300]
        retry_after = float(getattr(error, "retry_after", 0) or 0)
        if isinstance(error, RateLimitError) or _rate_limited(message) or (
            isinstance(error, CursorAgentError) and getattr(error, "is_retryable", False)
        ):
            raise CursorRateError(message, retry_after=max(retry_after, 1800.0)) from error
        raise CursorCallError(message) from error
    result = holder.get("result")
    if result is None:
        raise CursorCallError("cursor agent returned nothing")
    status = str(getattr(result, "status", "") or "")
    text = str(getattr(result, "result", "") or "").strip()
    if _rate_limited(text) or _rate_limited(status):
        raise CursorRateError(text or status)
    if status != "finished":
        raise CursorCallError(f"cursor run {status or 'unknown'}")
    if not text:
        raise CursorCallError("cursor agent finished with an empty answer")
    return text


def complete_messages(messages: list[dict[str, str]], *, timeout: float = PAIR_TIMEOUT_SEC) -> str:
    """Flatten a chat into one agent prompt that must answer with JSON only."""
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user").upper()
        parts.append(f"{role}:\n{message.get('content') or ''}")
    parts.append(
        "Reply with the JSON object only. "
        "Do not use tools, do not edit files, and do not add text outside the JSON."
    )
    return complete("\n\n".join(parts), timeout=timeout)
