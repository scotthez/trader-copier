from pathlib import Path
from tg_signal_trader.config import AppConfig, ProviderConfig
from tg_signal_trader.store import Store
from tg_signal_trader.classifier import MockClassifier
from tg_signal_trader.replay import replay

DATA = Path(__file__).parent / "data"


def test_replay_export_produces_counts(tmp_path):
    (tmp_path / "messages.html").write_text((DATA / "export_sample.html").read_text())
    cfg = AppConfig(providers={"lewis": ProviderConfig(telegram_chat=-1, bridge_dir="/tmp/l", symbols={"NAS100": "NAS100"}, sl_range={"NAS100": (5, 500)})})
    store = Store(":memory:")
    counts = replay(cfg, store, MockClassifier(), "lewis", tmp_path)
    assert counts["messages"] == 3 and counts["signals_accepted"] == 1
    run = store.get_run("lewis:8")
    assert run is not None and run.signal.symbol == "NAS100" and [l.volume for l in run.legs][0] > 0
