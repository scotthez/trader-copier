"""tg-trader command line."""
from __future__ import annotations
import argparse, asyncio, logging, sys, time
from datetime import datetime
from pathlib import Path
from typing import Callable
from .bridge import Bridge, Command, FileBridge
from .classifier import ClaudeClassifier, Classifier, MockClassifier
from .config import AppConfig, Secrets, load_config
from .models import RunState
from .store import Store
from .trader import Trader, DryRunBridge

log = logging.getLogger("tg-trader")


def _load_dotenv(config_path: str) -> None:
    """Loads KEY=VALUE lines from a .env beside the config file (or the cwd) without overriding real env vars."""
    import os
    for candidate in (Path(config_path).resolve().parent / ".env", Path(".env")):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


def _bridges(cfg: AppConfig, store: Store) -> dict[str, Bridge]:
    out: dict[str, Bridge] = {}
    for name, p in cfg.providers.items():
        off = store.kv_get(f"results_offset:{name}")
        out[name] = FileBridge(p.bridge_dir, results_offset=int(off) if off else 0)
    return out


def _persist_offsets(bridges: dict[str, Bridge], store: Store) -> None:
    for name, b in bridges.items():
        if isinstance(b, FileBridge):
            store.kv_set(f"results_offset:{name}", str(b.results_offset))


def make_classifier(cfg: AppConfig, secrets: Secrets) -> Classifier:
    """The configured LLM classifier: llm.provider selects Anthropic or OpenRouter."""
    if cfg.llm.provider == "openrouter":
        from .classifier_openrouter import OpenRouterClassifier
        if not secrets.openrouter_api_key:
            raise SystemExit("llm.provider is openrouter but OPENROUTER_API_KEY is not set")
        return OpenRouterClassifier(secrets.openrouter_model or cfg.llm.model, cfg.llm.timeout_sec, api_key=secrets.openrouter_api_key)
    if not secrets.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY not set; the Anthropic SDK will use its own credential chain")
    return ClaudeClassifier(cfg.llm.model, cfg.llm.timeout_sec, api_key=secrets.anthropic_api_key)


def classify_eval(classifier: Classifier, provider: str, export_dir: Path, n: int, echo=print) -> dict[str, int]:
    """Runs the last n non-entry provider messages through the classifier and prints each verdict."""
    from .export import read_export
    from .parsers import get_parser, looks_like_entry
    msgs = read_export(export_dir, provider)
    parser = get_parser(provider)
    picked = [m for m in msgs if m.text.strip() and parser.parse(m, []).signal is None and not looks_like_entry(m.text)][-n:]
    counts: dict[str, int] = {}
    for m in picked:
        c = classifier.classify_management(m.text, provider, None, [])
        counts[c.action] = counts.get(c.action, 0) + 1
        echo(f"{c.action:<15} {c.confidence:.2f}  {m.text[:90].replace(chr(10), ' | ')}   [{c.reason[:60]}]")
    echo(f"--- {len(picked)} messages: {counts}")
    return counts


def bridge_ping(bridge: Bridge, timeout_sec: float, now_local: Callable[[], datetime] = datetime.now, sleep: Callable[[float], None] = time.sleep) -> float | None:
    cmd_id = f"ping:{now_local().strftime('%Y%m%d%H%M%S%f')}"
    t0 = time.perf_counter()
    bridge.send(Command(cmd_id=cmd_id, type="ping"))
    deadline = time.perf_counter() + timeout_sec
    while True:
        for r in bridge.read_results():
            if r.cmd_id == cmd_id:
                return time.perf_counter() - t0
        if time.perf_counter() >= deadline:
            return None
        sleep(0.1)


def status_lines(cfg: AppConfig, store: Store, bridges: dict[str, Bridge], now_local: Callable[[], datetime] = datetime.now) -> list[str]:
    lines: list[str] = []
    for name, p in cfg.providers.items():
        st = bridges[name].read_state()
        if st is None:
            lines.append(f"[{name}] NO STATE at {p.bridge_dir} — is SignalBridge attached?")
            continue
        age = st.age_sec(now_local())
        flag = "OK" if age <= 5 else "STALE"
        lines.append(f"[{name}] {flag} age {age:.1f}s | login {st.account.login} | balance {st.account.balance:g} equity {st.account.equity:g} | hedging {st.account.hedging}")
        for sym, s in st.symbols.items():
            lines.append(f"    {sym}: bid {s.bid} ask {s.ask} step {s.volume_step} min {s.volume_min} tick_value {s.tick_value} trade_allowed {s.trade_allowed}")
        lines.append(f"    positions {len(st.positions)} orders {len(st.orders)}")
        for run in store.runs(name, [RunState.PLACING, RunState.ACTIVE]):
            legs = " ".join(f"L{l.n}:{l.state.value}" for l in run.legs)
            lines.append(f"    run {run.id} {run.signal.symbol} {run.signal.side.value} {run.state.value} | {legs}")
        sod = store.kv_get(f"sod_equity:{name}:{now_local().date().isoformat()}")
        if sod:
            lines.append(f"    day P&L {(st.account.equity - float(sod)):+.2f} vs stop {p.daily_loss_stop_pct}% of {float(sod):g}")
    return lines


def bridge_test(bridge: Bridge, symbol: str, now_local: Callable[[], datetime] = datetime.now, sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Places a minimum-lot market BUY with SL/TP 1% away, moves the SL, closes it. Demo accounts only."""
    st = bridge.read_state()
    spec = st.symbols[symbol]
    stamp = now_local().strftime("%Y%m%d%H%M%S")
    out: list[str] = []

    def wait(cmd_id: str):
        deadline = time.perf_counter() + 15
        while time.perf_counter() < deadline:
            for r in bridge.read_results():
                if r.cmd_id == cmd_id:
                    return r
            sleep(0.2)
        return None

    def step(cmd: Command):
        bridge.send(cmd)
        r = wait(cmd.cmd_id)
        out.append(f"{cmd.type}: {'OK' if r and r.ok else 'FAILED'} {r.retcode_text if r else 'no result (EA not running?)'}")
        return r

    r = step(Command(cmd_id=f"test:{stamp}:1", type="open_market", symbol=symbol, side="BUY", volume=spec.volume_min,
                     sl=round(spec.ask * 0.99, spec.digits), tp=round(spec.ask * 1.01, spec.digits), comment="sig:bridge-test:L1"))
    if not (r and r.ok):
        return out
    step(Command(cmd_id=f"test:{stamp}:2", type="modify_sl", position=r.position, sl=round(spec.ask * 0.995, spec.digits)))
    step(Command(cmd_id=f"test:{stamp}:3", type="close", position=r.position))
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="tg-trader")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listen")
    r = sub.add_parser("run"); r.add_argument("--dry-run", action="store_true"); r.add_argument("--once", action="store_true")
    sub.add_parser("status")
    pp = sub.add_parser("bridge-ping"); pp.add_argument("provider"); pp.add_argument("--timeout", type=float, default=10)
    bt = sub.add_parser("bridge-test"); bt.add_argument("provider"); bt.add_argument("--confirm", action="store_true")
    rp = sub.add_parser("replay"); rp.add_argument("provider"); rp.add_argument("export_dir")
    sub.add_parser("resolve-chats")
    jn = sub.add_parser("journal"); jn.add_argument("--n", type=int, default=50)
    ce = sub.add_parser("classify-eval"); ce.add_argument("provider"); ce.add_argument("export_dir"); ce.add_argument("--n", type=int, default=40)
    a = ap.parse_args(argv)

    _load_dotenv(a.config)
    cfg = load_config(a.config)
    store = Store(cfg.db_path)

    if a.cmd == "listen":
        from .listener import run_listener
        asyncio.run(run_listener(cfg, Secrets.from_env().require_telegram(), store)); return 0
    if a.cmd == "resolve-chats":
        from .listener import resolve_chats
        for title, cid in asyncio.run(resolve_chats(cfg, Secrets.from_env().require_telegram())):
            print(f"{cid:>16}  {title}")
        return 0
    if a.cmd == "status":
        print("\n".join(status_lines(cfg, store, _bridges(cfg, store)))); return 0
    if a.cmd == "journal":
        for e in reversed(store.journal_tail(a.n)):
            print(e["ts"], e["provider"], e["kind"], e["run_id"] or "", e["detail"])
        return 0
    if a.cmd == "bridge-ping":
        rtt = bridge_ping(_bridges(cfg, store)[a.provider], a.timeout)
        print(f"{a.provider}: {'pong in %.3fs' % rtt if rtt is not None else 'NO RESPONSE — is SignalBridge attached and Algo Trading on?'}")
        return 0 if rtt is not None else 1
    if a.cmd == "bridge-test":
        b = _bridges(cfg, store)[a.provider]
        st = b.read_state()
        if st is None:
            print("no state.json — EA not running"); return 1
        print(f"This places a REAL minimum-lot BUY on account {st.account.login} (balance {st.account.balance:g}).")
        if not a.confirm:
            print("Re-run with --confirm on a DEMO account."); return 1
        sym = next(iter(cfg.providers[a.provider].symbols.values()))
        print("\n".join(bridge_test(b, sym))); return 0
    if a.cmd == "classify-eval":
        classify_eval(make_classifier(cfg, Secrets.from_env()), a.provider, Path(a.export_dir), a.n); return 0
    if a.cmd == "replay":
        from .replay import replay
        counts = replay(cfg, store, MockClassifier(), a.provider, Path(a.export_dir))
        print(counts); return 0
    if a.cmd == "run":
        classifier = make_classifier(cfg, Secrets.from_env())
        bridges = _bridges(cfg, store)
        real = bridges
        if a.dry_run:
            bridges = {n: DryRunBridge(b, store, n) for n, b in bridges.items()}
            log.warning("DRY RUN: commands are journaled, not sent")
        trader = Trader(cfg, store, bridges, classifier)
        trader.startup_reconcile()
        while True:
            trader.tick()
            _persist_offsets(real, store)
            if a.once:
                return 0
            time.sleep(cfg.poll_interval_sec)
    return 1


if __name__ == "__main__":
    sys.exit(main())
