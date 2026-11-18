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
BASE_TIMEFRAME = "15m"
BASE_TIMEFRAME_MS = 15 * 60_000
HIGHER_TIMEFRAMES = ("30m", "1h", "2h")


def load_config(path: str) -> dict:
    with (ROOT / path).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("Config must be a YAML mapping")
    leverage = int(config["leverage"])
    if not 1 <= leverage <= MAX_ALLOWED_LEVERAGE:
        raise ValueError(f"leverage must be between 1 and {MAX_ALLOWED_LEVERAGE}x")
    if config.get("symbols") != ["BTC/USDT:USDT"]:
        raise ValueError("This conservative version only allows BTC/USDT:USDT")
    return config


def make_testnet_exchange(authenticated: bool):
    if authenticated and os.getenv("BINANCE_TESTNET") != "1":
        raise RuntimeError("Refusing execution: set BINANCE_TESTNET=1")
    key = os.getenv("BINANCE_FUTURES_TESTNET_API_KEY")
    secret = os.getenv("BINANCE_FUTURES_TESTNET_API_SECRET")
    if authenticated and (not key or not secret):
        raise RuntimeError("Set BINANCE_FUTURES_TESTNET_API_KEY and BINANCE_FUTURES_TESTNET_API_SECRET")
    exchange = ccxt.binance({
        "apiKey": key or "", "secret": secret or "",
        "enableRateLimit": True, "options": {"defaultType": "future"},
    })
    exchange.set_sandbox_mode(True)
    exchange.load_markets()
    return exchange


def closed_candles(exchange, symbol: str, days: int) -> pd.DataFrame:
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    until = exchange.milliseconds()
    cursor = since
    rows = []
    last_seen = None

    while cursor < until:
        page = exchange.fetch_ohlcv(symbol, timeframe=BASE_TIMEFRAME, since=cursor, limit=1000)
        if not page:
            break
        fresh = [row for row in page if row[0] >= cursor and row[0] < until and row[0] != last_seen]
        if not fresh:
            break
        rows.extend(fresh)
        last_seen = fresh[-1][0]
        next_cursor = last_seen + BASE_TIMEFRAME_MS
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(exchange.rateLimit / 1000)

    if not rows:
        raise RuntimeError(f"No candles returned for {symbol}")
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame = frame.drop_duplicates("timestamp").set_index("timestamp").sort_index()
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=15)
    return frame.loc[frame.index + pd.Timedelta(minutes=15) <= cutoff].astype(float)


def read_state(path: Path) -> dict:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise RuntimeError("State file is invalid; reconcile manually")
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
    if str(row.signal) == "buy":
        return "long"
    if str(row.signal) == "sell":
        return "short"
    return None


def scan(exchange, config: dict) -> dict[str, tuple[pd.DataFrame, pd.Series]]:
    found = {}
    for symbol in config["symbols"]:
        frame = closed_candles(exchange, symbol, int(config["lookback_days"]))
        if len(frame) < 100:
            print(f"Skip {symbol}: insufficient closed candles")
            continue
        signals = build_signals(frame, BASE_TIMEFRAME, HIGHER_TIMEFRAMES)
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
        raise RuntimeError("Exchange did not return an order ID; reconcile account")
    return order


def cancel_protection(exchange, symbol: str) -> None:
    for order in exchange.fetch_open_orders(symbol):
        if order.get("reduceOnly"):
            exchange.cancel_order(order["id"], symbol)


def place_exchange_protection(exchange, symbol: str, position: dict) -> dict:
    qty = float(position["qty"])
    stop_side = "sell" if position["direction"] == "long" else "buy"
    protection = {}
    try:
        protection["stop"] = exchange.create_order(
            symbol, "stop_market", stop_side, qty, None,
            {"reduceOnly": True, "stopPrice": float(position["stop"])},
        )
        protection["target"] = exchange.create_order(
            symbol, "take_profit_market", stop_side, qty, None,
            {"reduceOnly": True, "stopPrice": float(position["target"])},
        )
    except Exception:
        cancel_protection(exchange, symbol)
        raise
    return protection


def exchange_position(exchange, symbol: str) -> dict | None:
    for item in exchange.fetch_positions([symbol]):
        contracts = float(item.get("contracts") or 0)
        if contracts:
            side = str(item.get("side") or "").lower()
            return {"contracts": abs(contracts), "direction": "long" if side in ("long", "buy") else "short"}
    return None


def reconcile_saved_position(exchange, state: dict, state_path: Path, log_path: Path) -> None:
    position = state.get("position")
    symbol = position["symbol"] if position else None

    if position:
        live = exchange_position(exchange, position["symbol"])
        if live is None:
            append_log(log_path, {
                "event": "close", "symbol": position["symbol"], "direction": position["direction"],
                "reason": "reconciled_exchange_position_closed", "exit_time": datetime.now(timezone.utc).isoformat(),
                "testnet": True,
            })
            state["position"] = None
            save_state(state_path, state)
            print("Reconciled: saved position is no longer open on exchange")
            return
        if live["direction"] != position["direction"]:
            raise RuntimeError("Saved direction differs from exchange position; reconcile manually")
        if abs(live["contracts"] - float(position["qty"])) / max(live["contracts"], 1.0) > 0.02:
            raise RuntimeError("Saved quantity differs from exchange position; reconcile manually")

        cancel_protection(exchange, position["symbol"])
        protection = place_exchange_protection(exchange, position["symbol"], position)
        position["protection_order_ids"] = {k: v.get("id") for k, v in protection.items() if v.get("id")}
        save_state(state_path, state)
    else:
        for configured_symbol in ("BTC/USDT:USDT",):
            live = exchange_position(exchange, configured_symbol)
            if live:
                raise RuntimeError(f"Untracked futures position found on {configured_symbol}; reconcile manually")


def enter_position(exchange, symbol: str, row: pd.Series, direction: str, config: dict,
                   state: dict, state_path: Path, log_path: Path) -> None:
    price = float(exchange.fetch_ticker(symbol)["last"])
    stop_distance = float(row.atr) * float(config["stop_atr_multiple"])
    if price <= 0 or stop_distance <= 0:
        return
    equity = account_equity(exchange)
    risk_budget = equity * float(config["risk_per_trade_pct"])
    qty_risk = risk_budget / stop_distance
    qty_cap = equity * float(config["max_notional_equity_pct"]) / price
    qty = float(exchange.amount_to_precision(symbol, min(qty_risk, qty_cap)))
    market = exchange.market(symbol)
    min_cost = float(((market.get("limits") or {}).get("cost") or {}).get("min") or 0)
    if qty <= 0 or qty * price < min_cost or qty * price > free_margin(exchange) * int(config["leverage"]):
        print(f"Skip {symbol}: computed size violates exchange limits")
        return

    exchange.set_margin_mode("isolated", symbol)
    exchange.set_leverage(int(config["leverage"]), symbol)
    side = "buy" if direction == "long" else "sell"
    order = market_order(exchange, symbol, side, qty)
    filled = float(order.get("filled") or 0)
    if filled <= 0:
        raise RuntimeError("Entry fill was not confirmed; reconcile account")

    entry = float(order.get("average") or price)
    stop = entry - stop_distance if direction == "long" else entry + stop_distance
    target_distance = float(row.atr) * float(config["take_profit_atr_multiple"])
    target = entry + target_distance if direction == "long" else entry - target_distance
    position = {
        "symbol": symbol, "direction": direction, "qty": filled, "entry": entry,
        "stop": stop, "target": target,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "entry_day": datetime.now(timezone.utc).date().isoformat(),
        "entry_candle": row.name.isoformat(),
    }
    protection = place_exchange_protection(exchange, symbol, position)
    position["protection_order_ids"] = {k: v.get("id") for k, v in protection.items() if v.get("id")}
    state["position"] = position
    save_state(state_path, state)
    append_log(log_path, {"event": "open", **position, "entry_order_id": order.get("id"), "leverage": int(config["leverage"]), "testnet": True})


def close_position(exchange, reason: str, state: dict, state_path: Path, log_path: Path) -> None:
    position = state.get("position")
    if not position:
        return
    cancel_protection(exchange, position["symbol"])
    contracts = abs(float(position["qty"]))
    side = "sell" if position["direction"] == "long" else "buy"
    order = market_order(exchange, position["symbol"], side, contracts, reduce_only=True)
    price = float(order.get("average") or exchange.fetch_ticker(position["symbol"])["last"])
    signed_move = price - float(position["entry"])
    if position["direction"] == "short":
        signed_move = -signed_move
    pnl = signed_move * contracts
    fee_rate = float(config_value(state, "fees_bps", 10.0)) / 10_000
    pnl -= (price + float(position["entry"])) * contracts * fee_rate
    append_log(log_path, {"event": "close", "symbol": position["symbol"], "direction": position["direction"], "qty": contracts, "entry": position["entry"], "exit": price, "estimated_net_pnl": pnl, "reason": reason, "exit_time": datetime.now(timezone.utc).isoformat(), "order_id": order.get("id"), "testnet": True})
    state["position"] = None
    save_state(state_path, state)


def config_value(state: dict, key: str, default):
    return state.get("config", {}).get(key, default)


def run_loop(exchange, config: dict, state: dict, state_path: Path, log_path: Path) -> None:
    state.setdefault("config", {})["fees_bps"] = config.get("fees_bps", 10.0)
    reconcile_saved_position(exchange, state, state_path, log_path)
    save_state(state_path, state)
    print("TESTNET execution enabled: BTC/USDT:USDT, 15m, isolated margin.")

    while True:
        try:
            reconcile_saved_position(exchange, state, state_path, log_path)
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
                if state.get("utc_day") != today:
                    state["utc_day"] = today
                    state["day_start_equity"] = account_equity(exchange)
                    save_state(state_path, state)
                equity = account_equity(exchange)
                day_start = float(state.get("day_start_equity") or equity)
                if day_start > 0 and equity <= day_start * (1 - float(config["max_daily_loss_pct"])):
                    print("Daily loss threshold reached; no new entries")
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
            print("Stopped. Inspect testnet account and state manually.")
            return
        except Exception as exc:
            print(f"Agent error (retry later): {type(exc).__name__}: {exc}")
        time.sleep(int(config["poll_seconds"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC/USDT Binance USD-M Futures Testnet agent")
    parser.add_argument("--config", default="binance_futures_testnet.yaml")
    parser.add_argument("--execute-testnet", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    exchange = make_testnet_exchange(authenticated=args.execute_testnet)
    state_path = ROOT / config["state_file"]
    log_path = ROOT / config["trade_log"]
    if args.execute_testnet:
        run_loop(exchange, config, read_state(state_path), state_path, log_path)
    else:
        for symbol, (_, latest) in scan(exchange, config).items():
            print(json.dumps({"symbol": symbol, "as_of": latest.name.isoformat(), "signal": signal_direction(latest) or "hold", "confidence": float(latest.confidence), "close": float(latest.close)}, allow_nan=False))
        print("Scan only: no orders submitted. Use --execute-testnet to trade on futures TESTNET.")


if __name__ == "__main__":
    main()
