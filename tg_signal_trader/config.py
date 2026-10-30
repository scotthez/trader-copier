"""config.yaml (no secrets) and environment secrets."""
from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
import yaml

ALLOWED_ACTIONS = ["close_all", "cancel_pending", "move_sl", "break_even"]


class ProviderConfig(BaseModel):
    telegram_chat: int
    bridge_dir: Path
    risk_pct_per_leg: float = 1.0
    size_from: str = "balance"                      # "balance" (closed funds) or "equity" (balance + floating P&L); lots risk risk_pct_per_leg of this
    symbols: dict[str, str]                         # canonical -> broker symbol
    sl_range: dict[str, tuple[float, float]]        # canonical -> (min, max) SL distance in price units
    min_volume: dict[str, float] = Field(default_factory=dict)   # canonical -> minimum lots per leg, applied even above risk_pct_per_leg
    market_entry_tolerance_pct: float = 0.3
    limit_max_distance_pct: float = 1.0
    max_signal_age_sec: int = 120
    pending_ttl_hours: int = 24
    management_actions: list[str] = Field(default_factory=lambda: list(ALLOWED_ACTIONS))
    max_open_signals: int = 2
    max_legs_open: int = 8
    min_margin_level_pct: float = 300.0     # no new entries while the account's margin level (equity / used margin) is below this (0 = off)
    daily_loss_stop_pct: float = 5.0                # stop new entries once the day is down this % of start-of-day equity (0 = off)
    ignore_patterns: list[str] = Field(default_factory=list)   # regexes; matching management messages are journaled, never classified
    expected_login: int = 0             # refuse to send anything unless state.json reports this login (0 = any)
    live: bool = False                  # must be true to send commands to a REAL account; requires expected_login

    @model_validator(mode="after")
    def _live_requires_login(self) -> "ProviderConfig":
        if self.live and not self.expected_login:
            raise ValueError("live: true requires expected_login to be set to the exact account number")
        return self

    @field_validator("size_from")
    @classmethod
    def _known_size_from(cls, v: str) -> str:
        if v not in ("balance", "equity"):
            raise ValueError("size_from must be 'balance' or 'equity'")
        return v

    @field_validator("ignore_patterns")
    @classmethod
    def _valid_regexes(cls, v: list[str]) -> list[str]:
        import re
        for pat in v:
            re.compile(pat)
        return v

    @field_validator("management_actions")
    @classmethod
    def _known_actions(cls, v: list[str]) -> list[str]:
        bad = [a for a in v if a not in ALLOWED_ACTIONS]
        if bad:
            raise ValueError(f"unknown management_actions {bad}; allowed: {ALLOWED_ACTIONS}")
        return v


class AccountConfig(BaseModel):
    """A terminal with SignalBridge attached that is not a Telegram provider's account: a mirror destination."""
    bridge_dir: Path
    expected_login: int = 0             # refuse to send anything unless state.json reports this login (0 = any)
    live: bool = False                  # must be true to send commands to a REAL account; requires expected_login

    @model_validator(mode="after")
    def _live_requires_login(self) -> "AccountConfig":
        if self.live and not self.expected_login:
            raise ValueError("live: true requires expected_login to be set to the exact account number")
        return self


class FixedPips(BaseModel):
    fixed_pips: float = Field(gt=0)     # SL/TP this many pips from the destination's price at placement


StopMode = Literal["copy", "none"] | FixedPips


# Magic numbers copy-trading platforms put on the positions they copy into an account (IconFX/IconTech
# copy trading: 2147483646). Those positions are someone else's trades and are never mirrored.
COPY_PLATFORM_MAGICS = frozenset({2147483646})


class MirrorSource(BaseModel):
    login: int = Field(gt=0)            # the source account; events written by any other login are ignored
    events_dir: Path                    # the source terminal's Common/Files folder (where CopierSource writes)
    events_base: str = "copytrader"     # CopierSource InpEventsFileBase
    include_magics: list[int] = Field(default_factory=lambda: [0])   # 0 = trades placed by hand; add your own EAs' magics
    symbols: dict[str, str] = Field(default_factory=dict)            # source symbol -> destination symbol; empty = every symbol, same name

    @field_validator("symbols", mode="before")
    @classmethod
    def _list_is_same_name(cls, v):
        return {x: x for x in v} if isinstance(v, list) else v

    @field_validator("include_magics")
    @classmethod
    def _some_magic(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("include_magics must list at least one magic number (0 = trades placed by hand)")
        bad = sorted(set(v) & COPY_PLATFORM_MAGICS)
        if bad:
            raise ValueError(f"include_magics {bad}: that magic marks copy-trading platform positions, which are never mirrored")
        return v


class MirrorSizing(BaseModel):
    mode: Literal["balance_ratio", "source_lots", "fixed_lots"] = "balance_ratio"
    multiplier: float = Field(default=1.0, gt=0)    # balance_ratio: source lots x destination balance / source balance x this; source_lots: source lots x this
    lots: float | None = Field(default=None, gt=0)  # fixed_lots: every trade this size

    @model_validator(mode="after")
    def _lots_for_fixed(self) -> "MirrorSizing":
        if self.mode == "fixed_lots" and self.lots is None:
            raise ValueError("sizing mode fixed_lots needs lots")
        return self


class MirrorConfig(BaseModel):
    source: MirrorSource
    destination: str                    # a name under accounts: (or providers:) - its bridge_dir, expected_login and live are used
    sizing: MirrorSizing = Field(default_factory=MirrorSizing)
    max_lots: float = Field(gt=0)       # no destination trade is larger than this
    stop_loss: StopMode = "copy"
    take_profit: StopMode = "copy"
    pip_size: dict[str, float] = Field(default_factory=dict)   # destination symbol -> price of one pip, for fixed_pips (3/5-digit symbols default to 10 points)
    mirror_closes: Literal["proportional"] = "proportional"    # a partial close on the source closes the same fraction on the destination
    max_open_age_sec: int = 300         # a source OPEN older than this when it is read (terminal down, catching up) is not mirrored


MIRROR_NAME = re.compile(r"^[a-z0-9_]{1,8}$")     # goes into the destination comment m:<name>:<position>, which MT5 caps at 31 chars


class LlmConfig(BaseModel):
    provider: str = "anthropic"          # anthropic | openrouter
    model: str = "claude-opus-5"
    confidence_threshold: float = 0.8
    timeout_sec: float = 8.0
    entry_crosscheck: bool = True        # template-parsed entries must agree with the model's reading before trading
    market_crosscheck_after: bool = True # MARKET entries: place first, crosscheck in the background, close the trade if the model disagrees

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        if v not in ("anthropic", "openrouter"):
            raise ValueError("llm.provider must be 'anthropic' or 'openrouter'")
        return v


class AppConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    accounts: dict[str, AccountConfig] = Field(default_factory=dict)    # mirror-only destination accounts
    mirrors: dict[str, MirrorConfig] = Field(default_factory=dict)      # account -> account trade mirrors
    llm: LlmConfig = Field(default_factory=LlmConfig)
    db_path: Path = Path("tg_signal_trader.sqlite")
    session_path: Path = Path("tg_listener.session")
    poll_interval_sec: float = 0.5
    listener_poll_sec: float = 5.0
    notify_trades: bool = True           # message every placement / fill / TP / SL / SL move / cancel / rejection via the alert bot
    terminal_alert_sec: float = 60.0     # alert (and journal) when a terminal's state.json is missing/older than this (0 = off)      # listener also fetches recent channel messages this often (0 = push updates only)

    @model_validator(mode="after")
    def _mirrors_resolve(self) -> "AppConfig":
        clash = set(self.accounts) & set(self.providers)
        if clash:
            raise ValueError(f"accounts and providers share the names {sorted(clash)}; a mirror destination must be unambiguous")
        seen: dict[tuple, str] = {}
        for name, m in self.mirrors.items():
            if not MIRROR_NAME.match(name):
                raise ValueError(f"mirror name {name!r}: use 1-8 characters a-z, 0-9 or _")
            dest = self.mirror_destination(name)    # raises for an unknown destination
            if dest.expected_login and dest.expected_login == m.source.login:
                raise ValueError(f"mirror {name}: destination {m.destination} is the source account {m.source.login}")
            key = (m.source.login, str(m.source.events_dir), m.source.events_base, m.destination)
            if key in seen:
                raise ValueError(f"mirrors {seen[key]} and {name} copy the same source events to {m.destination}: every trade would open twice")
            seen[key] = name
        return self

    def mirror_destination(self, mirror: str) -> AccountConfig:
        """The destination account of a mirror, from accounts: or else a provider's terminal."""
        dest = self.mirrors[mirror].destination
        if dest in self.accounts:
            return self.accounts[dest]
        if dest in self.providers:
            p = self.providers[dest]
            return AccountConfig(bridge_dir=p.bridge_dir, expected_login=p.expected_login, live=p.live)
        raise ValueError(f"mirror {mirror}: destination {dest!r} is not under accounts: or providers:")


def load_config(path: str | Path) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)


class Secrets(BaseModel):
    telegram_api_id: int | None = None       # required only by listen / resolve-chats
    telegram_api_hash: str | None = None
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    openrouter_model: str | None = None      # overrides llm.model when llm.provider is openrouter
    alert_bot_token: str | None = None       # TELEGRAM_ALERT_BOT_TOKEN: bot from @BotFather that sends phone alerts
    alert_chat_id: str | None = None         # TELEGRAM_ALERT_CHAT_ID: your chat with that bot (`tg-trader alert-test` finds it)

    @classmethod
    def from_env(cls) -> "Secrets":
        api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
        return cls(telegram_api_id=int(api_id) if api_id else None,
                   telegram_api_hash=os.environ.get("TELEGRAM_API_HASH", "").strip() or None,
                   anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
                   openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
                   openrouter_model=os.environ.get("OPENROUTER_MODEL") or None,
                   alert_bot_token=os.environ.get("TELEGRAM_ALERT_BOT_TOKEN", "").strip() or None,
                   alert_chat_id=os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip() or None)

    def require_telegram(self) -> "Secrets":
        if not self.telegram_api_id or not self.telegram_api_hash:
            raise SystemExit("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set (my.telegram.org → API development tools)")
        return self
