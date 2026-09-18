"""Text normalisation shared by all provider templates."""
from __future__ import annotations
import re
import unicodedata

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⭐⬆⬇✅❌️‍⃣]+")
_SYMBOL_ALIASES = {
    "GOLD": "XAUUSD", "XAUUSD": "XAUUSD",
    "NAS100": "NAS100", "US100": "NAS100", "USTEC": "NAS100", "NASDAQ": "NAS100", "NAS": "NAS100",
    "GER40": "GER40", "GER30": "GER40", "DE40": "GER40", "DE30": "GER40", "DAX": "GER40",
    "EURGBP": "EURGBP", "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "GBPJPY": "GBPJPY", "EURJPY": "EURJPY",
    "BTCUSD": "BTCUSD", "ETHUSD": "ETHUSD",
}


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = _EMOJI.sub(" ", s)
    s = s.replace("–", "-").replace("—", "-").replace("’", "'")
    s = re.sub(r"(?i)take profit targets?\s*:?", "", s)
    s = re.sub(r"(?i)stop\s*-?\s*loss\s*:?", "SL:", s)
    s = re.sub(r"(?i)\bSL\s*:?\s*(?=[\d@])", "SL: ", s)
    s = re.sub(r"(?i)\bTP\s*([1-4])(?!\d)\s*:?\s*", lambda m: f"TP{m.group(1)}: ", s)   # TP1..TP4 only; "TP 4353" is not an index
    s = re.sub(r"(?i)\bTP\s*:\s*", "TP: ", s)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def canonical_symbol(s: str) -> str | None:
    return _SYMBOL_ALIASES.get(s.strip().upper())


def parse_price(s: str) -> float:
    return float(s.replace(",", "").strip())
