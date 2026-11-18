from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt
import pandas as pd
import yaml

from trade_strategy import build_signals

ROOT = Path(__file__).resolve().parent


def load_config(path: str) -> dict:
    with (ROOT / path).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("Config must be a YAML mapping")
    return config


def make_testnet_exchange(authenticated: bool):
    if authenticated and os.getenv("BINANCE_TESTNET") != "1":
        raise RuntimeError("Refusing execution: set BINANCE_TESTNET=1 to confirm testnet mode")
    if authenticated and (not os.getenv("BINANCE_API_KEY") or not os.getenv("BINANCE_API_SECRET")):
        raise RuntimeError("Set BINANCE_API_KEY and BINANCE_API_SECRET using Binance Spot Testnet credentials")
    exchange = ccxt.binance({
        "apiKey": os.getenv("BINANCE_API_KEY", ""),
        "secret": os.getenv("BINANCE_API_SECRET", ""),
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    # This is deliberately unconditional: this program has no live-trading mode.
    exchange.set_sandbox_mode(True)
    exchange.load_markets()
    return exchange


def closed_candles(exchange, symbol: str, days: int) -> pd.DataFrame:
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows = exchange.fetch_ohlcv(symbol, timeframe="30m", since=since, limit=1000)
    if not rows:
        raise RuntimeError(f"No candles returned for {symbol}")
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame = frame.drop_duplicates("timestamp").set_index("timestamp").sort_index()
    # Never make decisions using the still-forming 30-minute candle.
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=30)
    return frame.loc[frame.index + pd.Timedelta(minutes=30) <= cutoff].astype(float)


def read_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"position": None, "processed": {}, "utc_day": None, "day_start_equity": None}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp.replace(path)


def append_log(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, allow_nan=False) + "\n")


def free_usdt(exchange) -> float:
    balance = exchange.fetch_balance()
    return float((balance.get("USDT") or {}).get("free", 0.0))


def equity_usdt(exchange, position: dict | None) -> float:
    balance = exchange.fetch_balance()
    total = float((balance.get("USDT") or {}).get("total", 0.0))
    if position:
        base = position["symbol"].split("/")[0]
        total += float((balance.get(base) or {}).get("total", 0.0)) * float(exchange.fetch_ticker(position["symbol"])["last"])
    return total


def market_order(exchange, symbol: str, side: str, amount: float) -> dict:
    amount = float(exchange.amount_to_precision(symbol, amount))
    if amount <= 0:
        raise ValueError("Order amount rounded to zero")
    return exchange.create_order(symbol, "market", side, amount)


def execute_buy(exchange, symbol: str, signal: pd.Series, config: dict, state: dict, state_path: Path, log_path: Path) -> None:
    price = float(exchange.fetch_ticker(symbol)["last"])
    atr = float(signal.atr)
    stop_distance = atr * float(config["stop_atr_multiple"])
    if price <= 0 or stop_distance <= 0:
        return
    equity = equity_usdt(exchange, None)
    risk_budget = equity * float(config["risk_per_trade_pct"])
    qty = min(risk_budget / stop_distance, equity * float(config["max_position_pct"]) / price)
    qty = float(exchange.amount_to_precision(symbol, qty))
    market = exchange.market(symbol)
    min_cost = float(((market.get("limits") or {}).get("cost") or {}).get("min") or 0)
    if qty <= 0 or qty * price < min_cost or qty * price > free_usdt(exchange) * 0.98:
        print(f"Skip {symbol}: computed amount violates exchange minimum or available USDT")
        return
    order = market_order(exchange, symbol, "buy", qty)
    filled = float(order.get("filled") or qty)
    entry = float(order.get("average") or price)
    now = datetime.now(timezone.utc)
    position = {
        "symbol": symbol,
        "qty": filled,
        "entry": entry,
        "stop": entry - stop_distance,
        "target": entry + atr * float(config["take_profit_atr_multiple"]),
        "entry_time": now.isoformat(),
        "entry_day": now.date().isoformat(),
        "entry_candle": signal.name.isoformat(),
    }
    state["position"] = position
    save_state(state_path, state)
    append_log(log_path, {"event": "buy", **position, "order_id": order.get("id"), "testnet": True})
    print(f"TESTNET BUY {symbol} qty={filled:g} avg={entry:g} stop={position['stop']:g} target={position['target']:g}")


def execute_sell(exchange, reason: str, state: dict, state_path: Path, log_path: Path) -> None:
    position = state.get("position")
    if not position:
        return
    symbol = position["symbol"]
    base = symbol.split("/")[0]
    balance = exchange.fetch_balance()
    available = float((balance.get(base) or {}).get("free", 0.0))
    qty = min(float(position["qty"]), available)
    if qty <= 0:
        raise RuntimeError(f"No free {base} balance to close saved testnet position; reconcile manually")
    order = market_order(exchange, symbol, "sell", qty)
    price = float(order.get("average") or exchange.fetch_ticker(symbol)["last"])
    pnl = (price - float(position["entry"])) * qty
    fee_rate = float(config_value(state, "fees_bps", 10.0)) / 10_000
    pnl -= (price + float(position["entry"])) * qty * fee_rate
    event = {"event": "sell", "symbol": symbol, "qty": qty, "entry": position["entry"], "exit": price,
             "estimated_net_pnl": pnl, "reason": reason, "exit_time": datetime.now(timezone.utc).isoformat(),
             "order_id": order.get("id"), "testnet": True}
    append_log(log_path, event)
    print(f"TESTNET SELL {symbol} qty={qty:g} avg={price:g} reason={reason} estimated_pnl={pnl:.4f} USDT")
    state["position"] = None
    save_state(state_path, state)


def config_value(state: dict, key: str, default):
    # Kept separate so sell logging remains resilient if older state files are used.
    return state.get("config", {}).get(key, default)


def scan(exchange, config: dict) -> dict[str, tuple[pd.DataFrame, pd.Series]]:
    found = {}
    for symbol in config["symbols"]:
        frame = closed_candles(exchange, symbol, int(config["lookback_days"]))
        if len(frame) < 100:
            print(f"Skip {symbol}: insufficient closed candles")
            continue
        signals = build_signals(frame)
        if not signals.empty:
            found[symbol] = (signals, signals.iloc[-1])
    return found


def run_loop(exchange, config: dict, state: dict, state_path: Path, log_path: Path) -> None:
    state.setdefault("config", {})["fees_bps"] = config.get("fees_bps", 10.0)
    save_state(state_path, state)
    print("TESTNET execution enabled. Spot-only; this program cannot access live Binance endpoints.")
    while True:
        try:
            now = datetime.now(timezone.utc)
            today = now.date().isoformat()
            if state.get("position") and state["position"].get("entry_day") != today:
                execute_sell(exchange, "utc_day_end", state, state_path, log_path)
            results = scan(exchange, config)
            if state.get("position"):
                position = state["position"]
                ticker = float(exchange.fetch_ticker(position["symbol"])["last"])
                if ticker <= float(position["stop"]):
                    execute_sell(exchange, "stop", state, state_path, log_path)
                elif ticker >= float(position["target"]):
                    execute_sell(exchange, "target", state, state_path, log_path)
                elif position["symbol"] in results:
                    signals, latest = results[position["symbol"]]
                    if latest.name.isoformat() > position["entry_candle"] and str(latest.signal) == "exit":
                        execute_sell(exchange, "strategy_exit", state, state_path, log_path)
            else:
                utc_day = datetime.now(timezone.utc).date().isoformat()
                if state.get("utc_day") != utc_day:
                    state["utc_day"] = utc_day
                    state["day_start_equity"] = equity_usdt(exchange, None)
                    save_state(state_path, state)
                equity = equity_usdt(exchange, None)
                day_start = float(state.get("day_start_equity") or equity)
                loss_limit = float(config["max_daily_loss_pct"])
                if day_start > 0 and equity <= day_start * (1 - loss_limit):
                    print("Daily loss limit reached; no new entries until next UTC day")
                else:
                    candidates = []
                    for symbol, (signals, latest) in results.items():
                        candle_time = latest.name.isoformat()
                        if state.get("processed", {}).get(symbol) == candle_time:
                            continue
                        state.setdefault("processed", {})[symbol] = candle_time
                        if str(latest.signal) == "buy" and float(latest.confidence) >= float(config["min_confidence"]):
                            candidates.append((float(latest.confidence), symbol, latest))
                    if candidates:
                        _, symbol, signal = max(candidates, key=lambda x: x[0])
                        execute_buy(exchange, symbol, signal, config, state, state_path, log_path)
                    else:
                        save_state(state_path, state)
            print(f"Checked {datetime.now(timezone.utc).isoformat()} UTC; sleeping {config['poll_seconds']}s")
        except KeyboardInterrupt:
            print("Stopped by user; any testnet position remains open. Restart agent or close it manually on testnet.")
            return
        except Exception as exc:
            print(f"Agent error (will retry; inspect before continuing): {type(exc).__name__}: {exc}")
        time.sleep(int(config["poll_seconds"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Spot-only Binance Spot Testnet day-trading research agent")
    parser.add_argument("--config", default="binance_testnet.yaml")
    parser.add_argument("--execute-testnet", action="store_true", help="Place real orders on Binance Spot TESTNET only")
    args = parser.parse_args()
    config = load_config(args.config)
    exchange = make_testnet_exchange(authenticated=args.execute_testnet)
    state_path = ROOT / config["state_file"]
    log_path = ROOT / config["trade_log"]
    if args.execute_testnet:
        state = read_state(state_path)
        run_loop(exchange, config, state, state_path, log_path)
    else:
        for symbol, (_, latest) in scan(exchange, config).items():
            print(json.dumps({"symbol": symbol, "as_of": latest.name.isoformat(), "signal": str(latest.signal),
                              "confidence": float(latest.confidence), "close": float(latest.close)}, allow_nan=False))
        print("Scan only: no orders submitted. Add --execute-testnet to trade on Binance Spot Testnet.")


if __name__ == "__main__":
    main()
