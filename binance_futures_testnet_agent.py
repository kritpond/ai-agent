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
BASE_TIMEFRAME = "5m"
BASE_TIMEFRAME_MS = 5 * 60_000
HIGHER_TIMEFRAMES = ("10m", "30m", "1h", "2h")
ALLOWED_SYMBOLS = {"BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"}


def load_config(path: str) -> dict:
    with (ROOT / path).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("Config must be a YAML mapping")
    leverage = int(config["leverage"])
    if not 1 <= leverage <= MAX_ALLOWED_LEVERAGE:
        raise ValueError(f"leverage must be between 1 and {MAX_ALLOWED_LEVERAGE}x")
    symbols = config.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("Configure at least one supported futures symbol")
    if len(symbols) != len(set(symbols)) or any(symbol not in ALLOWED_SYMBOLS for symbol in symbols):
        raise ValueError(f"symbols must be unique entries from {sorted(ALLOWED_SYMBOLS)}")
    if config.get("base_timeframe") != BASE_TIMEFRAME:
        raise ValueError(f"base_timeframe must be {BASE_TIMEFRAME}")
    if tuple(config.get("higher_timeframes", ())) != HIGHER_TIMEFRAMES:
        raise ValueError(f"higher_timeframes must be {list(HIGHER_TIMEFRAMES)}")
    if not 0 < float(config["risk_per_trade_pct"]) <= 0.01:
        raise ValueError("risk_per_trade_pct must be > 0 and <= 1%")
    if not 0 < float(config["max_notional_equity_pct"]) <= 0.25:
        raise ValueError("max_notional_equity_pct must be > 0 and <= 25%")
    if not 0 < float(config["max_daily_loss_pct"]) <= 0.05:
        raise ValueError("max_daily_loss_pct must be > 0 and <= 5%")
    return config


def make_testnet_exchange(authenticated: bool):
    if authenticated and os.getenv("BINANCE_TESTNET") != "1":
        raise RuntimeError("Refusing execution: set BINANCE_TESTNET=1")
    key, secret = os.getenv("BINANCE_FUTURES_TESTNET_API_KEY"), os.getenv("BINANCE_FUTURES_TESTNET_API_SECRET")
    if authenticated and (not key or not secret):
        raise RuntimeError("Set BINANCE_FUTURES_TESTNET_API_KEY and BINANCE_FUTURES_TESTNET_API_SECRET")
    exchange = ccxt.binance({"apiKey": key or "", "secret": secret or "",
                             "enableRateLimit": True, "options": {"defaultType": "future"}})
    exchange.set_sandbox_mode(True)
    exchange.load_markets()
    return exchange


def closed_candles(exchange, symbol: str, days: int) -> pd.DataFrame:
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    until = exchange.milliseconds() - 1_000
    cursor, rows, last_seen = since, [], None
    while cursor < until:
        page = exchange.fetch_ohlcv(symbol, timeframe=BASE_TIMEFRAME, since=cursor, limit=1000)
        if not page:
            break
        fresh = [row for row in page if cursor <= row[0] and row[0] + BASE_TIMEFRAME_MS <= until and row[0] != last_seen]
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
        raise RuntimeError(f"No closed candles returned for {symbol}")
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame = frame.drop_duplicates("timestamp").set_index("timestamp").sort_index()
    return frame.astype(float)


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
    return float((exchange.fetch_balance().get("USDT") or {}).get("total", 0.0))


def free_margin(exchange) -> float:
    return float((exchange.fetch_balance().get("USDT") or {}).get("free", 0.0))


def signal_direction(row: pd.Series) -> str | None:
    if str(row.signal) == "buy":
        return "long"
    if str(row.signal) == "sell":
        return "short"
    return None


def scan(exchange, config: dict) -> dict[str, tuple[pd.DataFrame, pd.Series]]:
    found = {}
    for symbol in config["symbols"]:
        candles = closed_candles(exchange, symbol, int(config["lookback_days"]))
        if len(candles) < 300:
            print(f"Skip {symbol}: insufficient closed 5m candles")
            continue
        signals = build_signals(candles, BASE_TIMEFRAME, HIGHER_TIMEFRAMES)
        if not signals.empty:
            found[symbol] = (signals, signals.iloc[-1])
    return found


def market_order(exchange, symbol: str, side: str, amount: float, reduce_only: bool = False) -> dict:
    amount = float(exchange.amount_to_precision(symbol, amount))
    if amount <= 0:
        raise ValueError("Order amount rounded to zero")
    order = exchange.create_order(symbol, "market", side, amount, None,
                                  {"reduceOnly": True} if reduce_only else {})
    if not order.get("id"):
        raise RuntimeError("Exchange did not return an order ID; reconcile account")
    return order


def cancel_protection(exchange, symbol: str) -> None:
    for order in exchange.fetch_open_orders(symbol):
        info = order.get("info") or {}
        if order.get("reduceOnly") or str(info.get("reduceOnly", "")).lower() == "true":
            exchange.cancel_order(order["id"], symbol)


def place_exchange_protection(exchange, symbol: str, position: dict) -> dict:
    qty = float(position["qty"])
    side = "sell" if position["direction"] == "long" else "buy"
    try:
        stop = exchange.create_order(symbol, "stop_market", side, qty, None,
                                     {"reduceOnly": True, "stopPrice": float(position["stop"])})
        target = exchange.create_order(symbol, "take_profit_market", side, qty, None,
                                       {"reduceOnly": True, "stopPrice": float(position["target"])})
        if not stop.get("id") or not target.get("id"):
            raise RuntimeError("Exchange did not confirm both protective order IDs")
        return {"stop": stop, "target": target}
    except Exception:
        cancel_protection(exchange, symbol)
        raise


def exchange_position(exchange, symbol: str) -> dict | None:
    for item in exchange.fetch_positions([symbol]):
        contracts = float(item.get("contracts") or 0)
        if contracts:
            side = str(item.get("side") or "").lower()
            if side not in ("long", "short", "buy", "sell"):
                raise RuntimeError("Cannot determine exchange position direction; reconcile manually")
            return {"contracts": abs(contracts), "direction": "long" if side in ("long", "buy") else "short"}
    return None


def reconcile_saved_position(exchange, state: dict, state_path: Path, log_path: Path,
                             symbols: list[str]) -> None:
    position = state.get("position")
    live_positions = {symbol: exchange_position(exchange, symbol) for symbol in symbols}
    untracked = {symbol: live for symbol, live in live_positions.items() if live and
                 (not position or symbol != position.get("symbol"))}
    if untracked:
        raise RuntimeError(f"Untracked futures position(s) {untracked}; manual reconciliation required")
    if not position:
        return
    symbol = position["symbol"]
    live = live_positions.get(symbol)
    if live is None:
        cancel_protection(exchange, symbol)
        append_log(log_path, {"event": "close", "symbol": symbol, "direction": position["direction"],
                              "reason": "reconciled_exchange_position_closed",
                              "exit_time": datetime.now(timezone.utc).isoformat(), "testnet": True})
        state["position"] = None
        save_state(state_path, state)
        return
    if live["direction"] != position["direction"] or abs(live["contracts"] - float(position["qty"])) / max(live["contracts"], 1e-12) > 0.02:
        raise RuntimeError("Saved position differs from exchange position; manual reconciliation required")
    protections = []
    for order in exchange.fetch_open_orders(symbol):
        info = order.get("info") or {}
        if order.get("reduceOnly") or str(info.get("reduceOnly", "")).lower() == "true":
            protections.append(order)
    if len(protections) < 2:
        cancel_protection(exchange, symbol)
        placed = place_exchange_protection(exchange, symbol, position)
        position["protection_order_ids"] = {key: value["id"] for key, value in placed.items()}
        save_state(state_path, state)


def enter_position(exchange, symbol: str, row: pd.Series, direction: str, config: dict,
                   state: dict, state_path: Path, log_path: Path) -> None:
    price = float(exchange.fetch_ticker(symbol)["last"])
    stop_distance = float(row.atr) * float(config["stop_atr_multiple"])
    if price <= 0 or stop_distance <= 0:
        return
    volatility = float(row.volatility_pct)
    if volatility > float(config["max_volatility_pct"]):
        print(f"Skip {symbol}: volatility filter {volatility:.2%} exceeds limit")
        return
    equity = account_equity(exchange)
    risk_budget = equity * float(config["risk_per_trade_pct"])
    qty_risk = risk_budget / stop_distance
    qty_cap = equity * float(config["max_notional_equity_pct"]) / price
    qty = float(exchange.amount_to_precision(symbol, min(qty_risk, qty_cap)))
    min_cost = float(((exchange.market(symbol).get("limits") or {}).get("cost") or {}).get("min") or 0)
    if qty <= 0 or qty * price < min_cost or qty * price > free_margin(exchange) * int(config["leverage"]):
        print(f"Skip {symbol}: computed size violates exchange limits")
        return
    exchange.set_margin_mode("isolated", symbol)
    exchange.set_leverage(int(config["leverage"]), symbol)
    order = market_order(exchange, symbol, "buy" if direction == "long" else "sell", qty)
    filled = float(order.get("filled") or 0)
    if filled <= 0:
        raise RuntimeError("Entry fill was not confirmed; reconcile account")
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
    try:
        protection = place_exchange_protection(exchange, symbol, position)
    except Exception:
        append_log(log_path, {"event": "protection_error", **position, "testnet": True})
        raise
    position["protection_order_ids"] = {key: value["id"] for key, value in protection.items()}
    save_state(state_path, state)
    append_log(log_path, {"event": "open", **position, "entry_order_id": order["id"],
                          "leverage": int(config["leverage"]), "testnet": True})
    print(f"TESTNET {direction.upper()} {symbol} qty={filled:g} leverage={config['leverage']}x")


def close_position(exchange, reason: str, state: dict, state_path: Path, log_path: Path) -> None:
    position = state.get("position")
    if not position:
        return
    cancel_protection(exchange, position["symbol"])
    order = market_order(exchange, position["symbol"], "sell" if position["direction"] == "long" else "buy",
                         float(position["qty"]), reduce_only=True)
    price = float(order.get("average") or exchange.fetch_ticker(position["symbol"])["last"])
    move = price - float(position["entry"])
    if position["direction"] == "short":
        move = -move
    qty = float(position["qty"])
    fee = float(config_value(state, "fees_bps", 10.0)) / 10_000
    pnl = move * qty - (price + float(position["entry"])) * qty * fee
    append_log(log_path, {"event": "close", "symbol": position["symbol"], "direction": position["direction"],
                          "qty": qty, "entry": position["entry"], "exit": price,
                          "estimated_net_pnl": pnl, "reason": reason,
                          "exit_time": datetime.now(timezone.utc).isoformat(), "order_id": order["id"], "testnet": True})
    state["position"] = None
    save_state(state_path, state)


def config_value(state: dict, key: str, default):
    return state.get("config", {}).get(key, default)


def run_loop(exchange, config: dict, state: dict, state_path: Path, log_path: Path) -> None:
    state.setdefault("config", {})["fees_bps"] = config.get("fees_bps", 10.0)
    reconcile_saved_position(exchange, state, state_path, log_path, config["symbols"])
    print(f"TESTNET only: {', '.join(config['symbols'])}; 5m entry; 10m/30m/1h/2h confirmation; one position total; isolated {config['leverage']}x.")
    while True:
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            if state.get("position"):
                reconcile_saved_position(exchange, state, state_path, log_path, config["symbols"])
            if state.get("position") and state["position"].get("entry_day") != today:
                close_position(exchange, "utc_day_end", state, state_path, log_path)
            results = scan(exchange, config)
            if state.get("position"):
                pos = state["position"]
                price = float(exchange.fetch_ticker(pos["symbol"])["last"])
                is_long = pos["direction"] == "long"
                if (price <= pos["stop"] if is_long else price >= pos["stop"]):
                    close_position(exchange, "stop", state, state_path, log_path)
                elif (price >= pos["target"] if is_long else price <= pos["target"]):
                    close_position(exchange, "target", state, state_path, log_path)
                elif pos["symbol"] in results and str(results[pos["symbol"]][1].signal) == "exit":
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
                        _, symbol, row, direction = max(candidates, key=lambda item: item[0])
                        enter_position(exchange, symbol, row, direction, config, state, state_path, log_path)
                    else:
                        save_state(state_path, state)
            print(f"Checked {datetime.now(timezone.utc).isoformat()} UTC; sleeping {config['poll_seconds']}s")
        except KeyboardInterrupt:
            print("Stopped. Inspect Testnet positions/orders manually.")
            return
        except Exception as exc:
            print(f"Agent error (retry later): {type(exc).__name__}: {exc}")
        time.sleep(int(config["poll_seconds"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance USD-M Futures Testnet multi-timeframe agent")
    parser.add_argument("--config", default="binance_futures_testnet.yaml")
    parser.add_argument("--execute-testnet", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    exchange = make_testnet_exchange(authenticated=args.execute_testnet)
    state_path, log_path = ROOT / config["state_file"], ROOT / config["trade_log"]
    if args.execute_testnet:
        run_loop(exchange, config, read_state(state_path), state_path, log_path)
    else:
        for symbol, (_, latest) in scan(exchange, config).items():
            print(json.dumps({"symbol": symbol, "as_of": latest.name.isoformat(),
                              "signal": signal_direction(latest) or "hold",
                              "confidence": float(latest.confidence), "close": float(latest.close),
                              "atr": float(latest.atr), "volatility_pct": float(latest.volatility_pct),
                              "htf_score": float(latest.htf_score)}, allow_nan=False))
        print("Scan only: no orders submitted. Use --execute-testnet for Futures Testnet orders.")


if __name__ == "__main__":
    main()
