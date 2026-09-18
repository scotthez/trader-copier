"""Telethon user-session listener: every message from the configured channels → SQLite inbox."""
from __future__ import annotations
import logging
from telethon import TelegramClient, events
from .config import AppConfig, Secrets
from .models import InboxMessage
from .store import Store

log = logging.getLogger("tg-listener")


def _client(cfg: AppConfig, secrets: Secrets) -> TelegramClient:
    return TelegramClient(str(cfg.session_path), secrets.telegram_api_id, secrets.telegram_api_hash)


async def run_listener(cfg: AppConfig, secrets: Secrets, store: Store) -> None:
    chats = {p.telegram_chat: name for name, p in cfg.providers.items() if p.telegram_chat}
    if not chats:
        raise SystemExit("no provider has a telegram_chat id; run `tg-trader resolve-chats` first")
    client = _client(cfg, secrets)
    await client.start()          # first run: interactive phone/code/2FA prompt; then the session file is enough

    @client.on(events.NewMessage(chats=list(chats)))
    async def on_message(event):
        m = event.message
        provider = chats.get(event.chat_id)
        if provider is None:
            return
        msg = InboxMessage(msg_id=m.id, chat_id=event.chat_id, provider=provider, reply_to=m.reply_to_msg_id,
                           text=m.message or "", ts=m.date)
        if store.add_inbox(msg):
            log.info("%s #%d %s", provider, m.id, (m.message or "")[:80].replace("\n", " | "))

    log.info("listening on %s", chats)
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
