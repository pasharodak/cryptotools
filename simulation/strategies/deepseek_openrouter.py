"""LLM client for the DeepSeek bot.

Decision loop follows yohanatubike/Bitcoin-Deepseek-Trader: a compact market
snapshot goes to the model, which must answer with JSON
``{action, confidence, stop_loss, take_profit, reasoning}``.
Trades fire only when confidence is at least the configured threshold (default 0.65).

Default route is ShareLLM's OpenAI-compatible gateway:
``https://sharellm.net/v1/chat/completions``, model ``deepseek-v4-flash``.
Override with ``CT_LLM_BASE_URL``, ``CT_LLM_MODEL`` and ``CT_LLM_API_KEY``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ShareLLM free DeepSeek route. A local OpenSeek URL can still be set via CT_LLM_BASE_URL.
DEFAULT_URL = "https://sharellm.net/v1/chat/completions"
OPENSEEK_URL = DEFAULT_URL
DEFAULT_MODEL = "deepseek-v4.1-flash"
OPENCODE_URL = "https://opencode.ai/inference/openai/v1/chat/completions"
OPENCODE_MODELS_URL = "https://opencode.ai/inference/v1/models"
OPENCODE_MODEL = "deepseek-v4.1-flash"
_active_route = ""
DEFAULT_MIN_CONFIDENCE = 0.65
DEFAULT_TIMEOUT_SEC = 90.0


class LlmQuotaError(RuntimeError):
    """The gateway refused the call because the free quota is spent."""
# Example bot waits 180s between cycles. A 5m candle is the live cadence here.
LIVE_CANDLE_MAX_AGE = timedelta(minutes=20)

SYSTEM_PROMPT = """You are a premier trading professional experienced in crypto USDT perpetual futures (Bybit).
Leveraging real-time market analytics, you excel at precision swing trading and rapid scalping,
strategically capturing profitable movements. You identify entry and exit opportunities and manage risk
in volatile futures markets.

Your goal is to analyze the provided market data (raw candles and calculated indicators) and generate a precise trading signal.

KEY DIRECTIVES:
1. Analyze the raw price data FIRST to form your own opinion about market direction
2. Compare your assessment with the provided indicators
3. TAKE ACTION when you see potential opportunities - don't wait for perfect setups
4. Assume the user is looking to make regular trades and capture market moves
5. Provide either BUY or SELL recommendations whenever possible
6. Only use HOLD when market conditions are truly unclear or extremely risky

Trade signal guidelines:
1. For BUY/SELL signals:
   - Set tight stop losses (1-2% from the current price)
   - Set reasonable take profits (2-4% minimum)
   - Size positions according to conviction (0.1-0.8)
2. When setting confidence, use 0.5-0.7 for moderate conviction and 0.7-0.9 for high conviction
3. Only generate HOLD signals when ALL of these are true:
   - No clear trend in either timeframe
   - Extreme volatility with no direction
   - Multiple conflicting signals across timeframes
   - Price is exactly at a major support/resistance level

BUY means open a long. SELL means open a short. HOLD means do nothing.
Order book, funding rate and open interest are not provided — do not invent them.
Return only valid JSON. Do not include text outside the JSON.
"""


def endpoint_url() -> str:
    raw = (os.environ.get("CT_LLM_BASE_URL") or DEFAULT_URL).strip() or DEFAULT_URL
    raw = raw.rstrip("/")
    if raw.endswith("/chat/completions"):
        return raw
    if raw.endswith("/v1"):
        return raw + "/chat/completions"
    return raw


def models_url() -> str:
    url = endpoint_url()
    suffix = "/chat/completions"
    if url.endswith(suffix):
        return url[: -len(suffix)] + "/models"
    return url.rstrip("/") + "/models"


def model_name() -> str:
    if _active_route:
        for route in routes():
            if route["name"] == _active_route:
                return route["model"]
    return (os.environ.get("CT_LLM_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def min_confidence() -> float:
    raw = os.environ.get("CT_LLM_MIN_CONFIDENCE")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_MIN_CONFIDENCE
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_CONFIDENCE
    return min(1.0, max(0.0, value))


def routes() -> list[dict[str, str]]:
    """Primary gateway first, then OpenCode when its key is set."""
    primary = {
        "name": "sharellm",
        "url": endpoint_url(),
        "model": (os.environ.get("CT_LLM_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        "key": api_key(),
    }
    found = [primary]
    oc_key = (os.environ.get("CT_LLM_OPENCODE_KEY") or "").strip()
    if oc_key:
        found.append(
            {
                "name": "opencode",
                "url": (os.environ.get("CT_LLM_OPENCODE_URL") or OPENCODE_URL).strip() or OPENCODE_URL,
                "model": (os.environ.get("CT_LLM_OPENCODE_MODEL") or OPENCODE_MODEL).strip() or OPENCODE_MODEL,
                "key": oc_key,
            }
        )
    return found


def api_key() -> str:
    """ShareLLM and other hosted gateways require CT_LLM_API_KEY. Local OpenSeek accepts any bearer."""
    return (
        os.environ.get("CT_LLM_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or "sk-local"
    ).strip()


def _direct_opener() -> urllib.request.OpenerDirector:
    # Ignore the Windows SOCKS registry proxy. ShareLLM is reached directly.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _route_ready(route: dict[str, str], *, timeout: float) -> bool:
    if route["name"] == "opencode":
        url = OPENCODE_MODELS_URL
    else:
        url = models_url()
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {route['key']}",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with _direct_opener().open(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except Exception:
        return False


def endpoint_ready(*, timeout: float = 12.0) -> bool:
    """True when any configured gateway accepts its key."""
    return any(_route_ready(route, timeout=timeout) for route in routes())


def candle_is_live(ts: Any, *, now: datetime | None = None, max_age: timedelta = LIVE_CANDLE_MAX_AGE) -> bool:
    """True when the candle is recent enough to spend an API call on it."""
    if ts is None:
        return False
    try:
        import pandas as pd

        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        else:
            stamp = stamp.tz_convert("UTC")
        candle = stamp.to_pydatetime()
    except Exception:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = current - candle
    return timedelta(0) - timedelta(minutes=2) <= age <= max_age


def build_user_prompt(symbol: str, snapshot: dict[str, Any]) -> str:
    price = snapshot.get("price")
    return f"""Please analyze the following market data for {symbol} and provide trading signals:

CURRENT MARKET PRICE: {price}

RAW PRICE DATA (5-minute timeframe — last candles):
{json.dumps(snapshot.get("candles_5m") or [], ensure_ascii=False)}

RAW PRICE DATA (1-hour timeframe — resampled from 5m):
{json.dumps(snapshot.get("candles_1h") or [], ensure_ascii=False)}

5-minute indicators:
{json.dumps(snapshot.get("indicators_5m") or {}, ensure_ascii=False)}

1-hour indicators:
{json.dumps(snapshot.get("indicators_1h") or {}, ensure_ascii=False)}

Given the raw price data and calculated indicators above, please provide:
1. Your own assessment of key indicators and price action
2. Trading action (BUY/SELL/HOLD)
3. Confidence level (0.0-1.0)
4. Suggested stop loss price
5. Suggested take profit price
6. Detailed reasoning for the recommendation
7. Position sizing recommendation (0.0-1.0)

Your response must be a JSON object with the following structure:
{{
    "action": "BUY",
    "confidence": 0.0,
    "stop_loss": 0.0,
    "take_profit": 0.0,
    "reasoning": "Your detailed analysis here",
    "position_size": 0.0,
    "risk_factors": [],
    "key_levels": {{"support": 0.0, "resistance": 0.0}}
}}
Use action BUY, SELL, or HOLD.
"""


def build_messages(symbol: str, snapshot: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(symbol, snapshot)},
    ]


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("model response is not a JSON object")
    nested = data.get("prediction")
    if isinstance(nested, dict) and "action" not in data:
        return nested
    return data


def _as_price(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return price


def parse_prediction(content: str) -> dict[str, Any]:
    """Normalize the example response shape. Invalid input becomes HOLD."""
    try:
        data = _extract_json_object(content)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {
            "action": "HOLD",
            "confidence": 0.0,
            "stop_loss": None,
            "take_profit": None,
            "reasoning": "",
            "position_size": None,
            "error": "unparseable",
        }
    action = str(data.get("action") or "HOLD").strip().upper()
    if action not in {"BUY", "SELL", "HOLD"}:
        action = "HOLD"
    try:
        confidence = float(data.get("confidence") if data.get("confidence") is not None else 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))
    try:
        size = float(data.get("position_size")) if data.get("position_size") is not None else None
    except (TypeError, ValueError):
        size = None
    reasoning = str(data.get("reasoning") or "").strip()
    return {
        "action": action,
        "confidence": confidence,
        "stop_loss": _as_price(data.get("stop_loss")),
        "take_profit": _as_price(data.get("take_profit")),
        "reasoning": reasoning[:500],
        "position_size": size,
        "error": "",
    }


def entry_sides(prediction: dict[str, Any], *, min_conf: float | None = None) -> tuple[int, int]:
    """Return (enter_long, enter_short). HOLD and a weak confidence emit nothing."""
    threshold = DEFAULT_MIN_CONFIDENCE if min_conf is None else float(min_conf)
    action = str(prediction.get("action") or "HOLD").upper()
    try:
        confidence = float(prediction.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < threshold:
        return 0, 0
    if action == "BUY":
        return 1, 0
    if action == "SELL":
        return 0, 1
    return 0, 0


def _ordered_routes() -> list[dict[str, str]]:
    found = routes()
    if not _active_route:
        return found
    return sorted(found, key=lambda route: 0 if route["name"] == _active_route else 1)


def _post_route(
    route: dict[str, str],
    messages: list[dict[str, str]],
    *,
    timeout: float,
    max_tokens: int,
) -> str:
    body: dict[str, Any] = {
        "model": route["model"],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if os.environ.get("CT_LLM_JSON_MODE", "").strip() in {"1", "true", "TRUE", "yes"}:
        body["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        route["url"],
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {route['key']}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with _direct_opener().open(request, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        if (
            "insufficient_user_quota" in detail
            or "额度已用尽" in detail
            or "Insufficient account funds" in detail
        ):
            raise LlmQuotaError(f"LLM HTTP {exc.code}: insufficient_user_quota") from exc
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM endpoint unreachable ({route['url']}): {exc.reason}") from exc
    choices = raw.get("choices") if isinstance(raw, dict) else None
    if not choices:
        raise RuntimeError("LLM response has no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LLM response has empty content")
    return content


def chat_completion(
    messages: list[dict[str, str]],
    *,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Call the active gateway. If it fails, try the next configured one."""
    global _active_route
    limit = DEFAULT_TIMEOUT_SEC if timeout is None else float(timeout)
    env_timeout = os.environ.get("CT_LLM_TIMEOUT")
    if timeout is None and env_timeout:
        try:
            limit = float(env_timeout)
        except (TypeError, ValueError):
            limit = DEFAULT_TIMEOUT_SEC
    cap = 1200 if max_tokens is None else int(max_tokens)
    errors: list[Exception] = []
    for route in _ordered_routes():
        try:
            text = _post_route(route, messages, timeout=limit, max_tokens=cap)
        except Exception as exc:
            errors.append(exc)
            logger.warning("LLM route %s failed: %s", route["name"], exc)
            continue
        if _active_route != route["name"]:
            logger.info("LLM route switched to %s model=%s", route["name"], route["model"])
        _active_route = route["name"]
        return text
    if errors and all(isinstance(exc, LlmQuotaError) for exc in errors):
        raise LlmQuotaError(str(errors[-1]))
    if errors:
        raise errors[-1]
    raise RuntimeError("LLM route is not configured")
