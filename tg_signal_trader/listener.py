"""Telethon user-session listener: every message from the configured channels → SQLite inbox."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from telethon import TelegramClient, events, errors
from .config import AppConfig, Secrets
from .models import InboxMessage
from .store import Store

log = logging.getLogger("tg-listener")

POLL_LIMIT = 10     # newest messages fetched per channel per poll


def _client(cfg: AppConfig, secrets: Secrets) -> TelegramClient:
    return TelegramClient(str(cfg.session_path), secrets.telegram_api_id, secrets.telegram_api_hash)


def store_message(store: Store, m, chat_id: int, provider: str, via: str) -> bool:
    """Inserts one Telegram message into the inbox (idempotent). Logs the delivery lag, the seconds from
    the post to its arrival here, so a late Telegram delivery can be told apart from a slow trader."""
    msg = InboxMessage(msg_id=m.id, chat_id=chat_id, provider=provider, reply_to=m.reply_to_msg_id,
                       text=m.message or "", ts=m.date)
    if not store.add_inbox(msg):
        return False
    lag = (datetime.now(timezone.utc) - m.date).total_seconds()
    log.info("%s #%d +%.1fs via %s %s", provider, m.id, lag, via, (m.message or "")[:80].replace("\n", " | "))
    return True


async def poll_once(client, chats: dict[int, str], store: Store, since: datetime) -> int:
    """Fetches each channel's newest messages and stores any the push feed has not delivered. Only
    messages posted after `since` (listener start) are taken, so a restart never replays old
    management messages ("close all", "move SL") into the inbox."""
    added = 0
    for chat_id, provider in chats.items():
        try:
            msgs = await client.get_messages(chat_id, limit=POLL_LIMIT)
        except errors.FloodWaitError as e:
            log.warning("poll: Telegram asks to wait %ss (flood control)", e.seconds)
            await asyncio.sleep(e.seconds)
            continue
        for m in reversed(msgs):
            if getattr(m, "action", None) is None and m.date >= since:
                added += store_message(store, m, chat_id, provider, "poll")
    return added


async def _poll_loop(client, chats: dict[int, str], store: Store, since: datetime, interval: float) -> None:
    # Live miss, 2026-09-24: Lewis #3854 (a MARKET entry) reached the listener 55s after it was posted,
    # together with the next message. The channel push update was lost, and Telethon only caught up when
    # the next update arrived. Polling bounds a lost update to about `interval` seconds.
    while True:
        await asyncio.sleep(interval)
        try:
            await poll_once(client, chats, store, since)
        except Exception:
            log.exception("poll failed; retrying next interval")


async def run_listener(cfg: AppConfig, secrets: Secrets, store: Store) -> None:
    chats = {p.telegram_chat: name for name, p in cfg.providers.items() if p.telegram_chat}
    if not chats:
        raise SystemExit("no provider has a telegram_chat id; run `tg-trader resolve-chats` first")
    client = _client(cfg, secrets)
    await client.start()          # first run: interactive phone/code/2FA prompt; then the session file is enough
    since = datetime.now(timezone.utc)

    @client.on(events.NewMessage(chats=list(chats)))
    async def on_message(event):
        provider = chats.get(event.chat_id)
        if provider is None:
            return
        store_message(store, event.message, event.chat_id, provider, "push")

    if cfg.listener_poll_sec > 0:
        asyncio.get_running_loop().create_task(_poll_loop(client, chats, store, since, cfg.listener_poll_sec))
    log.info("listening on %s (poll every %ss)", chats, cfg.listener_poll_sec)
    await client.run_until_disconnected()


async def resolve_chats(cfg: AppConfig, secrets: Secrets) -> list[tuple[str, int]]:
    client = _client(cfg, secrets)
    await client.start()
    out: list[tuple[str, int]] = []
    async for d in client.iter_dialogs():
        if d.is_channel or d.is_group:
            out.append((d.name, d.id))
    await client.disconnect()
    return out
