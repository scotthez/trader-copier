"""Phone alerts through a Telegram bot (a bot's messages notify; messages to your own Saved Messages don't)."""
from __future__ import annotations
import logging
import threading
from typing import Protocol
import httpx

log = logging.getLogger("tg-trader.alerts")
API = "https://api.telegram.org/bot{token}/{method}"


class Alerter(Protocol):
    def send(self, text: str) -> None: ...


class NullAlerter:
    """No bot configured: alerts are only journaled."""

    def send(self, text: str) -> None:
        pass


class TelegramBotAlerter:
    def __init__(self, token: str, chat_id: str, timeout_sec: float = 10.0):
        self.token, self.chat_id, self.timeout = token, chat_id, timeout_sec

    def send_now(self, text: str) -> None:
        """Sends synchronously and raises on failure (used by `tg-trader alert-test`)."""
        r = httpx.post(API.format(token=self.token, method="sendMessage"),
                       json={"chat_id": self.chat_id, "text": text}, timeout=self.timeout)
        r.raise_for_status()

    def send(self, text: str) -> None:
        """Sends on a background thread so a slow or unreachable Telegram never stalls the trader loop."""
        def run() -> None:
            try:
                self.send_now(text)
            except Exception as e:   # an alert failing must never take trading down
                log.warning("alert not sent: %s", type(e).__name__)
        threading.Thread(target=run, daemon=True).start()


def discover_chat_ids(token: str, timeout_sec: float = 10.0) -> list[tuple[str, str]]:
    """Chats that have messaged the bot recently: (chat id, name). Message the bot once, then call this."""
    r = httpx.get(API.format(token=token, method="getUpdates"), timeout=timeout_sec)
    r.raise_for_status()
    seen: dict[str, str] = {}
    for u in r.json().get("result", []):
        chat = (u.get("message") or u.get("my_chat_member") or {}).get("chat")
        if chat:
            seen[str(chat["id"])] = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username", "")
    return list(seen.items())
