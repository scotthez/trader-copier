"""config.yaml (no secrets) and environment secrets."""
from __future__ import annotations
import os
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
import yaml

ALLOWED_ACTIONS = ["close_all", "cancel_pending", "move_sl", "break_even"]


class ProviderConfig(BaseModel):
    telegram_chat: int
    bridge_dir: Path
    risk_pct_per_leg: float = 1.0
    symbols: dict[str, str]                         # canonical -> broker symbol
    sl_range: dict[str, tuple[float, float]]        # canonical -> (min, max) SL distance in price units
    market_entry_tolerance_pct: float = 0.3
    limit_max_distance_pct: float = 1.0
    max_signal_age_sec: int = 120
    pending_ttl_hours: int = 24
    management_actions: list[str] = Field(default_factory=lambda: list(ALLOWED_ACTIONS))
    max_open_signals: int = 2
    max_legs_open: int = 8
    daily_loss_stop_pct: float = 5.0

    @field_validator("management_actions")
    @classmethod
    def _known_actions(cls, v: list[str]) -> list[str]:
        bad = [a for a in v if a not in ALLOWED_ACTIONS]
        if bad:
            raise ValueError(f"unknown management_actions {bad}; allowed: {ALLOWED_ACTIONS}")
        return v


class LlmConfig(BaseModel):
    model: str = "claude-opus-5"
    confidence_threshold: float = 0.8
    timeout_sec: float = 8.0


class AppConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    llm: LlmConfig = Field(default_factory=LlmConfig)
    db_path: Path = Path("tg_signal_trader.sqlite")
    session_path: Path = Path("tg_listener.session")
    poll_interval_sec: float = 0.5


def load_config(path: str | Path) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)


class Secrets(BaseModel):
    telegram_api_id: int
    telegram_api_hash: str
    anthropic_api_key: str | None = None

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(telegram_api_id=int(os.environ["TELEGRAM_API_ID"]),
                   telegram_api_hash=os.environ["TELEGRAM_API_HASH"],
                   anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None)
