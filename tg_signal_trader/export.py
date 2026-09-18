"""Telegram Desktop HTML export → InboxMessage list. Used for fixtures and `--replay`."""
from __future__ import annotations
import html
import re
from datetime import datetime, timezone
from pathlib import Path
from .models import InboxMessage

_CHUNK = re.compile(r'(?=<div class="message (?:default|service))')
_ID = re.compile(r'id="message(\d+)"')
_DATE = re.compile(r'class="pull_right date details" title="([^"]+)"')
_TEXT = re.compile(r'<div class="text">(.*?)</div>\s*(?:<div|</div>)', re.S)
_REPLY = re.compile(r'In reply to <a href="#go_to_message(\d+)"')
_TAG = re.compile(r"<.*?>", re.S)


def _file_order(p: Path) -> int:
    m = re.fullmatch(r"messages(\d*)\.html", p.name)
    return int(m.group(1) or "1")


def parse_export_html(text: str, provider: str, chat_id: int) -> list[InboxMessage]:
    out: list[InboxMessage] = []
    for chunk in _CHUNK.split(text)[1:]:
        if chunk.startswith('<div class="message service'):
            continue
        mid = _ID.search(chunk)
        date = _DATE.search(chunk)
        if not mid or not date:
            continue
        ts = datetime.strptime(date.group(1)[:19], "%d.%m.%Y %H:%M:%S").replace(tzinfo=timezone.utc)
        t = _TEXT.search(chunk)
        body = ""
        if t:
            body = html.unescape(re.sub(r"<br\s*/?>", "\n", t.group(1)))
            body = _TAG.sub("", body).strip()
        r = _REPLY.search(chunk)
        out.append(InboxMessage(msg_id=int(mid.group(1)), chat_id=chat_id, provider=provider,
                                reply_to=int(r.group(1)) if r else None, text=body, ts=ts))
    return out


def read_export(directory: str | Path, provider: str, chat_id: int = 0) -> list[InboxMessage]:
    files = sorted(Path(directory).glob("messages*.html"), key=_file_order)
    msgs: list[InboxMessage] = []
    for f in files:
        msgs.extend(parse_export_html(f.read_text(encoding="utf-8"), provider, chat_id))
    return msgs


def write_fixture(msgs: list[InboxMessage], path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for m in msgs:
            f.write(m.model_dump_json() + "\n")


def read_fixture(path: str | Path) -> list[InboxMessage]:
    with open(path, "r", encoding="utf-8") as f:
        return [InboxMessage.model_validate_json(line) for line in f if line.strip()]
