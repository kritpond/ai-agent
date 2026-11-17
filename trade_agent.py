from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trade_config import load_config
from trade_data import load_or_fetch, make_exchange
from trade_strategy import build_signals
from trade_backtest import run_backtest
from trade_journal import TradeJournal


def main():
    parser = argparse.ArgumentParser(description="Research/paper-only multi-timeframe crypto agent")
    parser.add_argument("command", choices=["backtest", "paper-scan"])
    parser.add_argument("--config", default="trade_config.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    exchange = make_exchange(config["exchange"])
    since = int((datetime.now(timezone.utc) - timedelta(days=int(config["lookback_days"]))).timestamp() * 1000)
    journal = TradeJournal(config["journal_dir"])
    all_metrics = {}
    for symbol in config["symbols"]:
        print(f"Fetching {symbol} 30m OHLCV from {config['exchange']}...")
        candles = load_or_fetch(exchange, symbol, config["base_timeframe"], since)
        signals = build_signals(candles)
        if args.command == "backtest":
            metrics, trades = run_backtest(signals, config)
            all_metrics[symbol] = metrics
            for trade in trades:
                journal.record(symbol, trade, metrics)
            print(json.dumps({symbol: metrics}, indent=2, allow_nan=False))
        else:
            latest = signals.iloc[-1]
            # This reports a research signal only; it never submits an order.
            print(json.dumps({symbol: {"as_of": signals.index[-1].isoformat(), "signal": str(latest.signal), "confidence": float(latest.confidence), "close": float(latest.close)}}, indent=2))
    if args.command == "backtest":
        report = Path("data") / "trade_backtest_report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(all_metrics, indent=2, allow_nan=False), encoding="utf-8")
        journal.review(all_metrics)
        print(f"Saved report: {report}; Obsidian notes: {config['journal_dir']}")


if __name__ == "__main__":
    main()
