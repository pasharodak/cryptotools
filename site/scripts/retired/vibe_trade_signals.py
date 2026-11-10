"""vibe-trade DebateOrchestrator → Cursor50 book actions.

Uses installed ``vibe-trade`` (DebateOrchestrator + llm_client). Does not start
the vibe-trade web UI. Bars can come from our Bybit klines or vibe-trade fetch.
"""
from __future__ import annotations
import sys
from pathlib import Path as _BootstrapPath

_scripts_root = _BootstrapPath(__file__).resolve().parents[1]
if str(_scripts_root) not in sys.path:
    sys.path.insert(0, str(_scripts_root))

import asyncio
import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("vibe_trade_signals")

DEBATE_ROUNDS = int(os.environ.get("CT_VIBE_ROUNDS", "3") or "3")
DEBATE_SPEAKERS = int(os.environ.get("CT_VIBE_SPEAKERS", "3") or "3")
DEBATE_TIMEOUT_SEC = float(os.environ.get("CT_VIBE_TIMEOUT_SEC", "120") or "120")
MAX_NEW_PAIRS = int(os.environ.get("CT_VIBE_MAX_NEW_PAIRS", "3") or "3")
ROTATION_PATH_ENV = "CT_VIBE_ROTATION_PATH"

_IMPORT_ERROR: str | None = None
_ENV_READY = False
_CURSOR_PATCHED = False


def _cursor_key() -> str:
    return (os.environ.get("CURSOR") or os.environ.get("CURSOR_API_KEY") or "").strip()


def _patch_vibe_llm_to_cursor() -> None:
    """Route vibe-trade DebateOrchestrator LLM calls through Cursor SDK."""
    global _CURSOR_PATCHED
    if _CURSOR_PATCHED:
        return
    import retired.cursor_client as cursor_client
    from core.agents import llm_client as _llm  # type: ignore

    call_timeout = float(os.environ.get("CT_VIBE_CURSOR_CALL_SEC", "90") or "90")

    def chat_completion(
        system_prompt: str,
        user_message: str,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        del model, temperature, max_tokens
        return cursor_client.complete_messages(
            [
                {"role": "system", "content": str(system_prompt or "")},
                {"role": "user", "content": str(user_message or "")},
            ],
            timeout=call_timeout,
        )

    def chat_completion_json(
        system_prompt: str,
        user_message: str,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        import json
        import re

        text = chat_completion(
            system_prompt,
            user_message,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            data = json.loads(cleaned)
            return data if isinstance(data, dict) else {"raw": text}
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if not match:
                return {"raw": text}
            try:
                data = json.loads(match.group(0))
                return data if isinstance(data, dict) else {"raw": text}
            except json.JSONDecodeError:
                return {"raw": text}

    def is_available() -> bool:
        return bool(cursor_client.configured())

    def active_provider_info() -> dict[str, str]:
        return {"provider": "cursor", "model": cursor_client.model_name()}

    _llm.chat_completion = chat_completion  # type: ignore[assignment]
    _llm.chat_completion_json = chat_completion_json  # type: ignore[assignment]
    _llm.is_available = is_available  # type: ignore[assignment]
    _llm.active_provider_info = active_provider_info  # type: ignore[assignment]
    # simulation_agents binds names at import time — rebind those too.
    try:
        from core.agents import simulation_agents as _sim  # type: ignore

        _sim.chat_completion = chat_completion  # type: ignore[attr-defined]
        _sim.chat_completion_json = chat_completion_json  # type: ignore[attr-defined]
        _sim.llm_available = is_available  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        log.warning("could not rebind simulation_agents LLM: %s", exc)
    for mod_name in (
        "core.agents.processors",
        "core.agents.planner",
        "core.agents.strategy_agent",
        "core.agents.pattern_agent",
        "core.agents.vibe_trade_agent",
        "core.engine.dag_orchestrator",
    ):
        try:
            mod = __import__(mod_name, fromlist=["*"])
            if hasattr(mod, "chat_completion"):
                setattr(mod, "chat_completion", chat_completion)
            if hasattr(mod, "chat_completion_json"):
                setattr(mod, "chat_completion_json", chat_completion_json)
            if hasattr(mod, "llm_available"):
                setattr(mod, "llm_available", is_available)
            if hasattr(mod, "is_available"):
                # only replace if it was imported from llm_client
                pass
        except Exception:
            continue
    _CURSOR_PATCHED = True
    log.info("vibe-trade LLM routed to Cursor SDK model=%s", cursor_client.model_name())


def ensure_llm_env() -> None:
    """Prefer CURSOR key for vibe-trade debates (not DeepSeek / CT_LLM)."""
    global _ENV_READY
    if _ENV_READY:
        return
    if not _cursor_key():
        _ENV_READY = True
        return
    # Drop DeepSeek / sharellm mapping so DebateOrchestrator cannot fall back to it.
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "LLM_PROVIDER", "LLM_MODEL"):
        # Keep LLM_PROVIDER unset; Cursor patch bypasses provider selection.
        if key in {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"} and key in os.environ:
            # Do not delete real user keys from the process env permanently if they
            # were meant for other bots — only clear the CT_LLM-injected openai path
            # when it equals CT_LLM_API_KEY.
            ct_key = (os.environ.get("CT_LLM_API_KEY") or os.environ.get("CT_LLM_OPENCODE_KEY") or "").strip()
            if ct_key and os.environ.get(key, "").strip() == ct_key:
                os.environ.pop(key, None)
    try:
        _patch_vibe_llm_to_cursor()
    except Exception as exc:  # noqa: BLE001
        global _IMPORT_ERROR
        _IMPORT_ERROR = f"cursor llm patch failed: {exc}"[:200]
        log.warning("cursor llm patch failed: %s", exc)
    _ENV_READY = True


def _try_imports() -> tuple[Any, Any] | None:
    global _IMPORT_ERROR
    try:
        from core.agents.llm_client import is_available as llm_available  # type: ignore
        from core.engine.dag_orchestrator import DebateOrchestrator  # type: ignore

        return llm_available, DebateOrchestrator
    except Exception as exc:  # noqa: BLE001
        _IMPORT_ERROR = str(exc)[:200]
        return None


def configured() -> bool:
    ensure_llm_env()
    if _cursor_key():
        try:
            import retired.cursor_client as cursor_client

            return bool(cursor_client.configured()) and _CURSOR_PATCHED
        except Exception:  # noqa: BLE001
            return False
    mods = _try_imports()
    if mods is None:
        return False
    llm_available, _ = mods
    try:
        return bool(llm_available())
    except Exception:  # noqa: BLE001
        return False


def config_reason() -> str:
    ensure_llm_env()
    if _try_imports() is None:
        return _IMPORT_ERROR or "vibe-trade not installed"
    if not _cursor_key():
        return "CURSOR token missing"
    if not configured():
        return _IMPORT_ERROR or "Cursor LLM bridge not ready"
    return ""


def provider_label() -> str:
    ensure_llm_env()
    try:
        import retired.cursor_client as cursor_client

        if _cursor_key() and cursor_client.configured():
            return f"cursor:{cursor_client.model_name()}"
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.agents.llm_client import active_provider_info  # type: ignore

        info = active_provider_info() or {}
        provider = str(info.get("provider") or "vibe-trade")
        model = str(info.get("model") or "")
        return f"{provider}:{model}" if model else provider
    except Exception:  # noqa: BLE001
        return "vibe-trade"


def bars_from_frame(frame: Any, *, limit: int = 200) -> list[dict[str, Any]]:
    """Convert a pandas OHLC frame (Bybit klines) to vibe-trade bar dicts."""
    if frame is None or getattr(frame, "empty", True):
        return []
    tail = frame.tail(limit)
    out: list[dict[str, Any]] = []
    for _, row in tail.iterrows():
        try:
            ts = row["date"]
            if hasattr(ts, "timestamp"):
                t = int(ts.timestamp())
            else:
                t = int(float(row.get("date_ms") or 0) / 1000)
            out.append(
                {
                    "time": t,
                    "timestamp": t,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume") or 0),
                }
            )
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _asset_symbol(pair: str) -> str:
    text = str(pair or "").strip().upper()
    if ":" in text:
        text = text.split(":", 1)[0]
    if "/" in text:
        return text.split("/", 1)[0]
    if text.endswith("USDT") and len(text) > 4:
        return text[:-4]
    return text or "BTC"


def _run_debate(bars: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    ensure_llm_env()
    mods = _try_imports()
    if mods is None:
        raise RuntimeError(_IMPORT_ERROR or "vibe-trade import failed")
    _, DebateOrchestrator = mods
    orchestrator = DebateOrchestrator()
    orchestrator.MAX_ROUNDS = max(1, DEBATE_ROUNDS)
    orchestrator.SPEAKERS_PER_ROUND = max(1, DEBATE_SPEAKERS)

    holder: dict[str, Any] = {}

    def _call() -> None:
        try:
            holder["result"] = asyncio.run(orchestrator.run(bars, symbol, report_text=""))
        except Exception as exc:  # noqa: BLE001
            holder["error"] = exc

    worker = threading.Thread(target=_call, name=f"vibe-debate-{symbol}", daemon=True)
    worker.start()
    worker.join(DEBATE_TIMEOUT_SEC)
    if worker.is_alive():
        raise TimeoutError(f"vibe-trade debate timed out after {int(DEBATE_TIMEOUT_SEC)}s for {symbol}")
    if "error" in holder:
        raise RuntimeError(str(holder["error"]))
    result = holder.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("vibe-trade debate returned empty result")
    return result


def _conf(summary: dict[str, Any]) -> float:
    try:
        conf = float(summary.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf > 1.0:
        conf = conf / 100.0
    return min(1.0, max(0.0, conf))


def _price(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def map_consensus_to_action(
    pair: str,
    result: dict[str, Any],
    *,
    position: dict[str, Any] | None = None,
    free_margin: float = 0.0,
) -> dict[str, Any]:
    """Map DebateOrchestrator result to a cursor_book action dict."""
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    direction = str(summary.get("consensus_direction") or "NEUTRAL").strip().upper()
    rec = summary.get("recommendation") if isinstance(summary.get("recommendation"), dict) else {}
    rec_action = str(rec.get("action") or "").strip().upper()
    confidence = _conf(summary)
    reasoning_bits = []
    if direction:
        reasoning_bits.append(direction)
    for arg in (summary.get("key_arguments") or [])[:2]:
        reasoning_bits.append(str(arg))
    reasoning = " · ".join(reasoning_bits)[:400]

    held_side = ""
    if position:
        held_side = str(position.get("side") or "").lower()

    action = "HOLD"
    if held_side == "long" and direction == "BEARISH":
        action = "CLOSE"
    elif held_side == "short" and direction == "BULLISH":
        action = "CLOSE"
    elif not held_side:
        wants_long = direction == "BULLISH" or rec_action in {"BUY", "LONG", "BULLISH"}
        wants_short = direction == "BEARISH" or rec_action in {"SELL", "SHORT", "BEARISH"}
        if wants_long and not wants_short:
            action = "BUY"
        elif wants_short and not wants_long:
            action = "SELL"

    stake = 0.0
    size_pct = rec.get("position_size_pct")
    try:
        pct = float(size_pct) if size_pct is not None else 0.0
    except (TypeError, ValueError):
        pct = 0.0
    if pct > 1.0:
        pct = pct / 100.0
    if action in {"BUY", "SELL"} and free_margin > 0:
        if pct > 0:
            stake = round(max(0.0, free_margin * min(1.0, pct)), 2)
        else:
            stake = round(min(10.0, free_margin), 2)

    return {
        "pair": pair,
        "action": action,
        "stake_usdt": stake,
        "stop_loss": _price(rec.get("stop")),
        "take_profit": _price(rec.get("target")),
        "confidence": confidence,
        "reasoning": reasoning,
        "vibe_direction": direction,
        "vibe_rec": rec_action,
    }


def debate_action(
    pair: str,
    bars: list[dict[str, Any]],
    *,
    position: dict[str, Any] | None = None,
    free_margin: float = 0.0,
) -> dict[str, Any]:
    if len(bars) < 20:
        raise ValueError(f"not enough bars for {pair}")
    symbol = _asset_symbol(pair)
    started = time.monotonic()
    result = _run_debate(bars, symbol)
    action = map_consensus_to_action(pair, result, position=position, free_margin=free_margin)
    log.info(
        "vibe %s %s conf=%.2f dir=%s in %.1fs",
        action["action"],
        pair,
        float(action.get("confidence") or 0),
        action.get("vibe_direction"),
        time.monotonic() - started,
    )
    return action


def pick_candidates(
    pairs: list[str],
    open_pairs: set[str],
    *,
    rotation_path: Any,
    max_new: int = MAX_NEW_PAIRS,
) -> list[str]:
    """Open positions first, then up to max_new rotating universe pairs."""
    ordered = [p for p in pairs if p in open_pairs]
    rest = [p for p in pairs if p not in open_pairs]
    start = 0
    try:
        if rotation_path is not None and getattr(rotation_path, "is_file", lambda: False)():
            start = int(rotation_path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError, TypeError):
        start = 0
    if rest:
        start = start % len(rest)
        rotated = rest[start:] + rest[:start]
        ordered.extend(rotated[: max(0, int(max_new))])
        nxt = (start + max(0, int(max_new))) % len(rest)
        try:
            if rotation_path is not None:
                rotation_path.parent.mkdir(parents=True, exist_ok=True)
                rotation_path.write_text(str(nxt), encoding="utf-8")
        except OSError:
            pass
    return ordered
