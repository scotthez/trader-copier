import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from telethon import errors
from tg_signal_trader.listener import poll_once
from tg_signal_trader.store import Store

NOW = datetime.now(timezone.utc)


def msg(mid, text="x", age=5.0, action=None, reply_to=None):
    return SimpleNamespace(id=mid, message=text, date=NOW - timedelta(seconds=age), reply_to_msg_id=reply_to, action=action)


class FakeClient:
    def __init__(self, by_chat):
        self.by_chat, self.calls = by_chat, []

    async def get_messages(self, chat_id, limit):
        self.calls.append((chat_id, limit))
        out = self.by_chat[chat_id]
        if isinstance(out, Exception):
            raise out
        return list(out)          # Telethon returns newest first


def test_poll_stores_messages_the_push_feed_missed():
    store = Store(":memory:")
    client = FakeClient({-100: [msg(3855, "analysis", 1), msg(3854, "TRADE SETUP: SELL NAS100", 55, reply_to=None)]})
    added = asyncio.run(poll_once(client, {-100: "lewis"}, store, since=NOW - timedelta(minutes=5)))
    assert added == 2 and [m.msg_id for m in store.new_inbox("lewis")] == [3854, 3855]
    # a second poll (or the late push) finds them already stored
    assert asyncio.run(poll_once(client, {-100: "lewis"}, store, since=NOW - timedelta(minutes=5))) == 0


def test_poll_never_replays_messages_from_before_listener_start():
    store = Store(":memory:")
    client = FakeClient({-100: [msg(3860, "new", 2), msg(3850, "close all", 3600)]})
    asyncio.run(poll_once(client, {-100: "lewis"}, store, since=NOW - timedelta(seconds=60)))
    assert [m.msg_id for m in store.new_inbox("lewis")] == [3860]


def test_poll_skips_service_messages():
    store = Store(":memory:")
    client = FakeClient({-100: [msg(3861, "", 2, action=object())]})
    assert asyncio.run(poll_once(client, {-100: "lewis"}, store, since=NOW - timedelta(minutes=5))) == 0


def test_flood_wait_on_one_channel_does_not_stop_the_other(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr("tg_signal_trader.listener.asyncio.sleep", fake_sleep)
    store = Store(":memory:")
    client = FakeClient({-100: errors.FloodWaitError(request=None, capture=7), -200: [msg(29900, "wolves", 2)]})
    added = asyncio.run(poll_once(client, {-100: "lewis", -200: "wolves"}, store, since=NOW - timedelta(minutes=5)))
    assert slept == [7] and added == 1 and store.new_inbox("wolves")[0].msg_id == 29900
