"""Claude-backed classification of free-form provider messages (management) and entry fallback."""
from __future__ import annotations
from typing import Literal, Protocol
import anthropic
from pydantic import BaseModel, Field

Action = Literal["none", "close_all", "cancel_pending", "move_sl", "break_even"]


class Classification(BaseModel):
    action: Action
    price: float | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str = ""


class EntryExtraction(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    entry_type: Literal["MARKET", "LIMIT"]
    entry_zone: list[float] = Field(default_factory=list)
    sl: float
    tps: list[float | None]
    confidence: float = Field(ge=0, le=1)


class RunContext(BaseModel):
    symbol: str; side: str; entry_type: str; entry_zone: list[float] = Field(default_factory=list)
    sl: float; open_legs: int; pending_legs: int; sl_current: float


NONE = Classification(action="none", confidence=0.0, reason="")

MANAGEMENT_SYSTEM = """You classify messages from a Telegram trading-signal provider into ONE management action
for a copy-trading robot. The robot already opened the trade described in the context. Be conservative:
if the message is commentary, hype, a profit screenshot caption, a question, or ambiguous, answer "none".
Actions:
- close_all: the provider closes/exits the whole trade now ("delete this", "out at BE", "closing", "secure 100%", "cancel and close").
- cancel_pending: cancel unfilled pending/limit orders only ("cancel this limit", "delete the pending", "price ran, cancel").
- move_sl: move the stop loss to an explicit price the message states (put it in "price").
- break_even: move the stop loss to the entry price ("BE", "break even", "risk free", "SL to entry").
Typos and slang are common ("delte", "im be now", "sl 4412 guys"). "TP hit", "running +40 pips", "secure some profits" are NOT actions → "none".
confidence is your probability that the action is what the provider means for THIS trade."""

ENTRY_SYSTEM = """Extract a trade signal from a Telegram message if, and only if, it clearly states a direction, a symbol,
a stop loss and at least three take-profit levels. Use MARKET unless the message says limit/zone/pending, in which case
entry_type is LIMIT and entry_zone holds the stated price(s). tps must list TP1..TP4 in order, using null for an "open" TP.
If any of side, symbol, sl or three TPs is missing, set confidence to 0."""


class Classifier(Protocol):
    def classify_management(self, text: str, provider: str, ctx: RunContext | None, recent: list[str]) -> Classification: ...
    def extract_entry(self, text: str, provider: str) -> EntryExtraction | None: ...


_SDK_ERRORS = (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.APIStatusError, ValueError)


class ClaudeClassifier:
    def __init__(self, model: str, timeout_sec: float, api_key: str | None = None, client=None):
        self.model, self.timeout = model, timeout_sec
        self.client = client or anthropic.Anthropic(api_key=api_key)

    def _parse(self, system: str, user: str, schema):
        return self.client.with_options(timeout=self.timeout, max_retries=1).messages.parse(
            model=self.model, max_tokens=1024, system=system,
            messages=[{"role": "user", "content": user}], output_format=schema).parsed_output

    def classify_management(self, text: str, provider: str, ctx: RunContext | None, recent: list[str]) -> Classification:
        context = ctx.model_dump_json() if ctx else "no open trade"
        user = (f"Provider: {provider}\nOpen trade context: {context}\nRecent provider messages (oldest first):\n"
                + "\n".join(f"- {r}" for r in recent) + f"\n\nMessage to classify:\n{text}")
        try:
            return self._parse(MANAGEMENT_SYSTEM, user, Classification)
        except _SDK_ERRORS as e:
            return Classification(action="none", confidence=0.0, reason=f"error: {type(e).__name__}")

    def extract_entry(self, text: str, provider: str) -> EntryExtraction | None:
        try:
            return self._parse(ENTRY_SYSTEM, f"Provider: {provider}\nMessage:\n{text}", EntryExtraction)
        except _SDK_ERRORS:
            return None


class MockClassifier:
    def __init__(self, management: dict[str, Classification] | None = None, entries: dict[str, EntryExtraction] | None = None):
        self.management = management or {}
        self.entries = entries or {}
        self.calls: list[tuple[str, str]] = []

    def classify_management(self, text: str, provider: str, ctx: RunContext | None, recent: list[str]) -> Classification:
        self.calls.append(("management", text))
        return self.management.get(text, NONE)

    def extract_entry(self, text: str, provider: str) -> EntryExtraction | None:
        self.calls.append(("entry", text))
        return self.entries.get(text)
