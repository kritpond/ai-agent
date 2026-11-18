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
MAX_ALLOWED_LEVERAGE = 5


def load_config(path: str) -> dict:
    with (ROOT / path).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("Config must be a YAML mapping")
    leverage = int(config["leverage"])
    if not 1 <= leverage <= MAX_ALLOWED_LEVERAGE:
        raise ValueError(f"leverage must be between 1 and {MAX_ALLOWED_LEVERAGE}x")
    return config


def make_testnet_exchange(authenticated: bool):
    if authenticated and os.getenv("BINANCE_TESTNET") != "1":
        raise RuntimeError("Refusing execution: set BINANCE_TESTNET=1 to confirm futures testnet mode")
    if authenticated and (not os.getenv("BINANCE_API_KEY") or not os.getenv("BINANCE_API_SECRET")):
        raise RuntimeError("Use Binance USD-M Futures Testnet credentials in BINANCE_API_KEY/SECRET")
    exchange = ccxt.binance({
        "apiKey": os.getenv("BINANCE_API_KEY", ""),
        "secret": os.getenv("BINANCE_API_SECRET", ""),
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })
    # Unconditional sandbox routing: this script has no production endpoint mode.
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
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=30)
    return frame.loc[frame.index + pd.Timedelta(minutes=30) <= cutoff].astype(float)


def read_state(path: Path) -> dict:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise RuntimeError("State file is invalid; reconcile testnet positions manually")
        return state
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


def account_equity(exchange) -> float:
    balance = exchange.fetch_balance()
    return float((balance.get("USDT") or {}).get("total", 0.0))


def free_margin(exchange) -> float:
    balance = exchange.fetch_balance()
    return float((balance.get("USDT") or {}).get("free", 0.0))


def signal_direction(row: pd.Series) -> str | None:
    # Existing strategy's buy signal opens long; strong agreement in the opposite
    # direction opens short. Its exit signal closes longs, not automatically shorts.
    long_ok = str(row.signal) == "buy"
    short_ok = all(float(row[f"score_{tf}"]) < 0 for tf in ("1h", "2h", "4h")) and float(row.score_30m) < -0.15
    if long_ok:
        return "long"
    if short_ok:
        return "short"
    return None


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


def market_order(exchange, symbol: str, side: str, amount: float, reduce_only: bool = False) -> dict:
    amount = float(exchange.amount_to_precision(symbol, amount))
    if amount <= 0:
        raise ValueError("Order amount rounded to zero")
    params = {"reduceOnly": True} if reduce_only else {}
    order = exchange.create_order(symbol, "market", side, amount, None, params)
    if not order.get("id"):
        raise RuntimeError("Exchange did not return an order ID; reconcile account before retrying")
    return order


def verify_no_untracked_positions(exchange, symbols: list[str], state: dict) -> None:
    position = state.get("position")
    if position:
        symbols_to_check = [position["symbol"]]
    else:
        symbols_to_check = symbols
    open_positions = []
    for symbol in symbols_to_check:
        for item in exchange.fetch_positions([symbol]):
            contracts = abs(float(item.get("contracts") or 0))
            if contracts > 0:
                open_positions.append((symbol, contracts, item.get("side")))
    if position:
        if not open_positions:
            raise RuntimeError("Saved position not found on exchange; reconcile state/account manually before continuing")
        if len(open_positions) != 1 or open_positions[0][0] != position["symbol"]:
            raise RuntimeError("Exchange position does not match saved state; reconcile manually")
    elif open_positions:
        raise RuntimeError(f"Untracked futures position(s) {open_positions}; reconcile before starting agent")


def enter_position(exchange, symbol: str, row: pd.Series, direction: str, config: dict,
                   state: dict, state_path: Path, log_path: Path) -> None:
    price = float(exchange.fetch_ticker(symbol)["last"])
    stop_distance = float(row.atr) * float(config["stop_atr_multiple"])
    if price <= 0 or stop_distance <= 0:
        return
    equity = account_equity(exchange)
    risk_budget = equity * float(config["risk_per_trade_pct"])
    qty_risk = risk_budget / stop_distance
    max_notional = equity * float(config["max_notional_equity_pct"]) * int(config["leverage"])
    qty_cap = max_notional / price
    qty = min(qty_risk, qty_cap)
    qty = float(exchange.amount_to_precision(symbol, qty))
    market = exchange.market(symbol)
    min_cost = float(((market.get("limits") or {}).get("cost") or {}).get("min") or 0)
    if qty <= 0 or qty * price < min_cost or qty * price > free_margin(exchange) * int(config["leverage"]) * 0.95:
        print(f"Skip {symbol}: size is below exchange minimum or exceeds available testnet margin")
        return
    exchange.set_margin_mode("isolated", symbol)
    exchange.set_leverage(int(config["leverage"]), symbol)
    side = "buy" if direction == "long" else "sell"
    order = market_order(exchange, symbol, side, qty)
    filled = float(order.get("filled") or 0)
    if filled <= 0:
        raise RuntimeError(f"Entry order {order['id']} has no confirmed fill; inspect account before retrying")
    entry = float(order.get("average") or price)
    stop = entry - stop_distance if direction == "long" else entry + stop_distance
    target_distance = float(row.atr) * float(config["take_profit_atr_multiple"])
    target = entry + target_distance if direction == "long" else entry - target_distance
    now = datetime.now(timezone.utc)
    position = {"symbol": symbol, "direction": direction, "qty": filled, "entry": entry,
                "stop": stop, "target": target, "entry_time": now.isoformat(),
                "entry_day": now.date().isoformat(), "entry_candle": row.name.isoformat()}
    state["position"] = position
    save_state(state_path, state)
    append_log(log_path, {"event": "open", **position, "order_id": order.get("id"),
                          "leverage": int(config["leverage"]), "testnet": True})
    print(f"TESTNET {direction.upper()} {symbol} qty={filled:g} avg={entry:g} leverage={config['leverage']}x stop={stop:g} target={target:g}")


def close_position(exchange, reason: str, state: dict, state_path: Path, log_path: Path) -> None:
    position = state.get("position")
    if not position:
        return
    symbol = position["symbol"]
    contracts = abs(float(position["qty"]))
    side = "sell" if position["direction"] == "long" else "buy"
    order = market_order(exchange, symbol, side, contracts, reduce_only=True)
    price = float(order.get("average") or exchange.fetch_ticker(symbol)["last"])
    signed_move = price - float(position["entry"])
    if position["direction"] == "short":
        signed_move = -signed_move
    pnl = signed_move * contracts
    fee_rate = float(config_value(state, "fees_bps", 10.0)) / 10_000
    pnl -= (price + float(position["entry"])) * contracts * fee_rate
    event = {"event": "close", "symbol": symbol, "direction": position["direction"], "qty": contracts,
             "entry": position["entry"], "exit": price, "estimated_net_pnl": pnl, "reason": reason,
             "exit_time": datetime.now(timezone.utc).isoformat(), "order_id": order.get("id"), "testnet": True}
    append_log(log_path, event)
    state["position"] = None
    save_state(state_path, state)
    print(f"TESTNET CLOSE {symbol} reason={reason} estimated_pnl={pnl:.4f} USDT")


def config_value(state: dict, key: str, default):
    return state.get("config", {}).get(key, default)


def run_loop(exchange, config: dict, state: dict, state_path: Path, log_path: Path) -> None:
    state.setdefault("config", {})["fees_bps"] = config.get("fees_bps", 10.0)
    verify_no_untracked_positions(exchange, config["symbols"], state)
    save_state(state_path, state)
    print(f"TESTNET futures execution enabled, isolated margin, {config['leverage']}x maximum configured leverage. No live endpoint mode exists.")
    while True:
        try:
            now = datetime.now(timezone.utc)
            today = now.date().isoformat()
            if state.get("position") and state["position"].get("entry_day") != today:
                close_position(exchange, "utc_day_end", state, state_path, log_path)
            results = scan(exchange, config)
            if state.get("position"):
                position = state["position"]
                ticker = float(exchange.fetch_ticker(position["symbol"])["last"])
                direction = position["direction"]
                stopped = ticker <= position["stop"] if direction == "long" else ticker >= position["stop"]
                targeted = ticker >= position["target"] if direction == "long" else ticker <= position["target"]
                if stopped:
                    close_position(exchange, "stop", state, state_path, log_path)
                elif targeted:
                    close_position(exchange, "target", state, state_path, log_path)
                elif position["symbol"] in results:
                    _, latest = results[position["symbol"]]
                    newer = latest.name.isoformat() > position["entry_candle"]
                    if newer and ((direction == "long" and str(latest.signal) == "exit") or
                                  (direction == "short" and signal_direction(latest) == "long")):
                        close_position(exchange, "strategy_exit", state, state_path, log_path)
            else:
                utc_day = datetime.now(timezone.utc).date().isoformat()
                if state.get("utc_day") != utc_day:
                    state["utc_day"] = utc_day
                    state["day_start_equity"] = account_equity(exchange)
                    save_state(state_path, state)
                equity = account_equity(exchange)
                day_start = float(state.get("day_start_equity") or equity)
                if day_start > 0 and equity <= day_start * (1 - float(config["max_daily_loss_pct"])):
                    print("Daily loss threshold reached; no new entries until next UTC day")
                else:
                    candidates = []
                    for symbol, (_, latest) in results.items():
                        candle_time = latest.name.isoformat()
                        if state.get("processed", {}).get(symbol) == candle_time:
                            continue
                        state.setdefault("processed", {})[symbol] = candle_time
                        direction = signal_direction(latest)
                        if direction and float(latest.confidence) >= float(config["min_confidence"]):
                            candidates.append((float(latest.confidence), symbol, latest, direction))
                    if candidates:
                        _, symbol, row, direction = max(candidates, key=lambda x: x[0])
                        enter_position(exchange, symbol, row, direction, config, state, state_path, log_path)
                    else:
                        save_state(state_path, state)
            print(f"Checked {datetime.now(timezone.utc).isoformat()} UTC; sleeping {config['poll_seconds']}s")
        except KeyboardInterrupt:
            print("Stopped. Any testnet position may remain open; inspect the testnet account and state file manually.")
            return
        except Exception as exc:
            print(f"Agent error (will retry; inspect account before continuing): {type(exc).__name__}: {exc}")
        time.sleep(int(config["poll_seconds"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated, long/short Binance USD-M Futures Testnet agent")
    parser.add_argument("--config", default="binance_futures_testnet.yaml")
    parser.add_argument("--execute-testnet", action="store_true", help="Submit orders on Binance USD-M Futures TESTNET only")
    args = parser.parse_args()
    config = load_config(args.config)
    exchange = make_testnet_exchange(authenticated=args.execute_testnet)
    state_path = ROOT / config["state_file"]
    log_path = ROOT / config["trade_log"]
    if args.execute_testnet:
        run_loop(exchange, config, read_state(state_path), state_path, log_path)
    else:
        for symbol, (_, latest) in scan(exchange, config).items():
            print(json.dumps({"symbol": symbol, "as_of": latest.name.isoformat(),
                              "signal": signal_direction(latest) or "hold",
                              "confidence": float(latest.confidence), "close": float(latest.close)}, allow_nan=False))
        print("Scan only: no orders submitted. Use --execute-testnet to trade on futures TESTNET.")


if __name__ == "__main__":
    main()
