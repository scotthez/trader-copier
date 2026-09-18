"""SQLite persistence: inbox, runs, journal, classifications, kv."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .models import InboxMessage, SignalRun, RunState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (provider TEXT, msg_id INTEGER, chat_id INTEGER, reply_to INTEGER, text TEXT, ts TEXT, status TEXT, PRIMARY KEY(provider, msg_id));
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, provider TEXT, state TEXT, telegram_msg_id INTEGER, data TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, provider TEXT, kind TEXT, run_id TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS classifications (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, provider TEXT, msg_id INTEGER, text TEXT, action TEXT, price REAL, confidence REAL, reason TEXT, executed INTEGER);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), isolation_level=None)   # autocommit
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    # inbox
    def add_inbox(self, m: InboxMessage) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO inbox VALUES (?,?,?,?,?,?,?)",
                                (m.provider, m.msg_id, m.chat_id, m.reply_to, m.text, m.ts.isoformat(), m.status))
        return cur.rowcount == 1

    def _row_msg(self, r: sqlite3.Row) -> InboxMessage:
        return InboxMessage(provider=r["provider"], msg_id=r["msg_id"], chat_id=r["chat_id"], reply_to=r["reply_to"],
                            text=r["text"], ts=datetime.fromisoformat(r["ts"]), status=r["status"])

    def new_inbox(self, provider: str) -> list[InboxMessage]:
        rows = self.conn.execute("SELECT * FROM inbox WHERE provider=? AND status='new' ORDER BY msg_id", (provider,))
        return [self._row_msg(r) for r in rows]

    def get_inbox(self, provider: str, msg_id: int) -> InboxMessage | None:
        r = self.conn.execute("SELECT * FROM inbox WHERE provider=? AND msg_id=?", (provider, msg_id)).fetchone()
        return self._row_msg(r) if r else None

    def set_inbox_status(self, provider: str, msg_id: int, status: str) -> None:
        self.conn.execute("UPDATE inbox SET status=? WHERE provider=? AND msg_id=?", (status, provider, msg_id))

    def recent_inbox(self, provider: str, before_msg_id: int, n: int = 3) -> list[InboxMessage]:
        rows = self.conn.execute("SELECT * FROM inbox WHERE provider=? AND msg_id<? ORDER BY msg_id DESC LIMIT ?", (provider, before_msg_id, n))
        return [self._row_msg(r) for r in rows][::-1]

    # runs
    def save_run(self, run: SignalRun) -> None:
        run.updated_at = datetime.now(timezone.utc)
        self.conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                          (run.id, run.signal.provider, run.state.value, run.signal.telegram_msg_id, run.model_dump_json(), run.updated_at.isoformat()))

    def get_run(self, run_id: str) -> SignalRun | None:
        r = self.conn.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        return SignalRun.model_validate_json(r["data"]) if r else None

    def runs(self, provider: str, states: list[RunState] | None = None) -> list[SignalRun]:
        if states:
            q = f"SELECT data FROM runs WHERE provider=? AND state IN ({','.join('?' * len(states))}) ORDER BY telegram_msg_id DESC"
            rows = self.conn.execute(q, (provider, *[s.value for s in states]))
        else:
            rows = self.conn.execute("SELECT data FROM runs WHERE provider=? ORDER BY telegram_msg_id DESC", (provider,))
        return [SignalRun.model_validate_json(r["data"]) for r in rows]

    def run_by_msg_id(self, provider: str, msg_id: int) -> SignalRun | None:
        r = self.conn.execute("SELECT data FROM runs WHERE provider=? AND telegram_msg_id=?", (provider, msg_id)).fetchone()
        return SignalRun.model_validate_json(r["data"]) if r else None

    # journal / classifications / kv
    def journal(self, provider: str, kind: str, detail: dict, run_id: str | None = None) -> None:
        self.conn.execute("INSERT INTO journal (ts, provider, kind, run_id, detail) VALUES (?,?,?,?,?)",
                          (_now(), provider, kind, run_id, json.dumps(detail, default=str)))

    def journal_tail(self, n: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (n,))
        return [{"ts": r["ts"], "provider": r["provider"], "kind": r["kind"], "run_id": r["run_id"], "detail": json.loads(r["detail"])} for r in rows]

    def save_classification(self, provider: str, msg_id: int, text: str, action: str, price: float | None,
                            confidence: float, reason: str, executed: bool) -> None:
        self.conn.execute("INSERT INTO classifications (ts, provider, msg_id, text, action, price, confidence, reason, executed) VALUES (?,?,?,?,?,?,?,?,?)",
                          (_now(), provider, msg_id, text, action, price, confidence, reason, int(executed)))

    def kv_get(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))
