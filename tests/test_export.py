from datetime import datetime, timezone
from pathlib import Path
from tg_signal_trader.export import read_export, write_fixture, read_fixture

DATA = Path(__file__).parent / "data"


def test_read_export_parses_messages(tmp_path):
    (tmp_path / "messages.html").write_text((DATA / "export_sample.html").read_text())
    msgs = read_export(tmp_path, "lewis", chat_id=-100)
    assert [m.msg_id for m in msgs] == [8, 11, 12]
    m8 = msgs[0]
    assert m8.provider == "lewis" and m8.chat_id == -100
    assert m8.ts == datetime(2026, 3, 9, 14, 45, 6, tzinfo=timezone.utc)
    assert m8.text.startswith("🔵 BUY NAS100 NOW\n\nStop Loss: 24389")
    assert "🎯 TP4: 24949" in m8.text
    assert msgs[1].reply_to == 8 and msgs[1].text == "TP1 HIT! ✔️"
    assert msgs[2].text == "" and msgs[2].reply_to is None


def test_read_export_orders_files_numerically(tmp_path):
    body = (DATA / "export_sample.html").read_text()
    (tmp_path / "messages.html").write_text(body.replace('id="message8"', 'id="message1000"'))
    (tmp_path / "messages2.html").write_text(body.replace('id="message8"', 'id="message2000"'))
    (tmp_path / "messages10.html").write_text(body.replace('id="message8"', 'id="message3000"'))
    ids = [m.msg_id for m in read_export(tmp_path, "lewis")]
    assert ids.index(1000) < ids.index(2000) < ids.index(3000)


def test_fixture_round_trip(tmp_path):
    (tmp_path / "messages.html").write_text((DATA / "export_sample.html").read_text())
    msgs = read_export(tmp_path, "lewis")
    write_fixture(msgs, tmp_path / "f.jsonl")
    back = read_fixture(tmp_path / "f.jsonl")
    assert back == msgs


def test_secrets_are_redacted_from_export_text():
    from tg_signal_trader.export import redact_secrets
    assert redact_secrets("use key sk-ant[REDACTED] now") == "use key sk-ant[REDACTED] now"
    assert redact_secrets("bot 123456789:AAabcdefghijklmnopqrstuvwxyz0123456789abcd ok") == "bot 123456[REDACTED] ok"
    assert redact_secrets("BUY XAUUSD @4347 SL 4341") == "BUY XAUUSD @4347 SL 4341"
