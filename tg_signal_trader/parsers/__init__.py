"""Provider parsers: each turns an InboxMessage (+ a little context) into a normalised Signal."""
from __future__ import annotations
import re
from typing import Protocol
from pydantic import BaseModel
from ..models import InboxMessage, Signal
from ..normalize import normalize_text


class ParseResult(BaseModel):
    signal: Signal | None = None
    template: str | None = None
    rejected_reason: str | None = None


class Parser(Protocol):
    def parse(self, msg: InboxMessage, prev: list[InboxMessage]) -> ParseResult: ...


_SL = re.compile(r"\bSL:\s*\d")
_TP = re.compile(r"\bTP\d?:\s*(\d|OPEN)", re.I)


def looks_like_entry(text: str) -> bool:
    n = normalize_text(text)
    return bool(_SL.search(n) and _TP.search(n))


def get_parser(provider: str) -> Parser:
    if provider == "lewis":
        from .lewis import LewisParser
        return LewisParser()
    if provider == "wolves":
        from .wolves import WolvesParser
        return WolvesParser()
    raise KeyError(provider)
