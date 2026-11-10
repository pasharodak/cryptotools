"""Desired bot state → process adapter.

Linux and Windows process starters are two adapters. Enable and disable live
here; control_plane stays the desired/observed store.
"""

from __future__ import annotations

from collections.abc import Callable

EnsureFn = Callable[[str, str], tuple[bool, str]]


def ensure_with_adapter(
    user_id: str,
    bot: str,
    *,
    windows: bool,
    linux: EnsureFn,
    win: EnsureFn,
) -> tuple[bool, str]:
    adapter = win if windows else linux
    return adapter(user_id, bot)


def apply_desired(
    user_id: str,
    bot: str,
    desired_on: bool,
    *,
    enable: EnsureFn,
    disable: EnsureFn,
) -> tuple[bool, str]:
    if desired_on:
        return enable(user_id, bot)
    return disable(user_id, bot)


def apply(user_id: str, bot: str, desired_on: bool) -> tuple[bool, str]:
    if desired_on:
        return enable(user_id, bot)
    return disable(user_id, bot)


def enable(user_id: str, bot: str) -> tuple[bool, str]:
    import bot_reconcile as br

    if bot == "grid" and user_id == "admin":
        ok, err = br.prepare_admin_grid_demo()
        if not ok:
            br.cp.update_observed(user_id, bot, process_up=False, last_error=err)
            return False, err
    br.user_trading.save_trading_flags(user_id, {bot: True})
    ok, err = br.ensure_process(user_id, bot)
    if not ok:
        br.cp.update_observed(user_id, bot, process_up=False, last_error=err)
        return False, err

    if br._shared_strategy(bot):
        for _ in range(15):
            if br.ping_bot(8081):
                break
            br.time.sleep(0.4)
        # Skip /start if already running — avoids reload flap.
        if br.show_config_state(8081) not in br._RUNNING_LIKE_STATES:
            br.ctbot_start_stop(8081, "start")
    else:
        port = br.bot_port(user_id, bot)
        for _ in range(15):
            if br.port_open(port):
                break
            br.time.sleep(0.4)
        st = br.show_config_state(port) if br.port_open(port) else "unknown"
        # unknown = API timeout while the process is up. /start here reloaded forever.
        if st in br._RUNNING_LIKE_STATES or st == "unknown":
            pass
        elif st in ("stopped", "paused"):
            started, start_err = br.ctbot_start_stop(port, "start")
            if not started:
                br.cp.update_observed(
                    user_id, bot, process_up=True, trading_state="stopped", last_error=start_err
                )
                return False, start_err
        elif not br.port_open(port):
            pass
        else:
            started, start_err = br.ctbot_start_stop(port, "start")
            if not started:
                br.cp.update_observed(
                    user_id, bot, process_up=True, trading_state="stopped", last_error=start_err
                )
                return False, start_err

    snap = br.refresh_observed(user_id, bot)
    br.cp.append_event(user_id, "bot.status", snap, bot=bot)
    try:
        import telegram_links as tg_links

        tg_links.notify_user(
            user_id,
            tg_links.format_bot_event_ru(action="start", bot=bot, user_id=user_id),
            kind="bot_start",
        )
    except Exception:
        pass
    return True, ""


def disable(user_id: str, bot: str) -> tuple[bool, str]:
    import bot_reconcile as br

    br.user_trading.save_trading_flags(user_id, {bot: False})

    if br._shared_strategy(bot):
        snap = br.refresh_observed(user_id, bot)
        br.cp.append_event(user_id, "bot.status", snap, bot=bot)
        try:
            import telegram_links as tg_links

            tg_links.notify_user(
                user_id,
                tg_links.format_bot_event_ru(
                    action="stop",
                    bot=bot,
                    user_id=user_id,
                    detail="Автоторговля стратегиями выключена",
                ),
                kind="bot_stop",
            )
        except Exception:
            pass
        return True, ""

    port = br.bot_port(user_id, bot)
    if br.port_open(port):
        ok, err = br.ctbot_start_stop(port, "stop")
        if not ok:
            br.cp.update_observed(user_id, bot, process_up=True, last_error=err)
            snap = br.refresh_observed(user_id, bot)
            br.cp.append_event(user_id, "bot.status", snap, bot=bot)
            return False, err

    snap = br.refresh_observed(user_id, bot)
    br.cp.append_event(user_id, "bot.status", snap, bot=bot)
    try:
        import telegram_links as tg_links

        tg_links.notify_user(
            user_id,
            tg_links.format_bot_event_ru(action="stop", bot=bot, user_id=user_id),
            kind="bot_stop",
        )
    except Exception:
        pass
    return True, ""
