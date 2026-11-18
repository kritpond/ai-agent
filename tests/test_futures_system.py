from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

import binance_futures_testnet_agent as agent
from trade_strategy import build_signals


class FakeExchange:
    rateLimit = 0

    def __init__(self):
        self.created = []
        self.open_orders = []
        self.position = None
        self.pages = 0

    def milliseconds(self):
        return int(datetime.now(timezone.utc).timestamp() * 1000)

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        self.pages += 1
        start = int((datetime.now(timezone.utc) - timedelta(days=14)).timestamp() * 1000)
        end = self.milliseconds() - 20 * 60_000
        timestamps = list(range(start, end, 15 * 60_000))
        selected = [stamp for stamp in timestamps if stamp >= since][:limit]
        return [[stamp, 40000, 40100, 39900, 40050, 1] for stamp in selected]

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.4f}"

    def market(self, symbol):
        return {"limits": {"cost": {"min": 5}}}

    def set_margin_mode(self, *args):
        return None

    def set_leverage(self, *args):
        return None

    def fetch_balance(self):
        return {"USDT": {"total": 10000.0, "free": 10000.0}}

    def fetch_ticker(self, symbol):
        return {"last": 40000.0}

    def create_order(self, symbol, kind, side, amount, price=None, params=None):
        order = {"id": f"order-{len(self.created) + 1}", "symbol": symbol,
                 "type": kind, "side": side, "amount": amount,
                 "filled": amount if kind == "market" else 0,
                 "average": 40000.0 if kind == "market" else None,
                 "reduceOnly": bool((params or {}).get("reduceOnly")),
                 "info": {"type": kind}}
        self.created.append(order)
        if kind in ("stop_market", "take_profit_market"):
            self.open_orders.append(order)
        elif kind == "market" and not (params or {}).get("reduceOnly"):
            self.position = {"contracts": amount, "side": "long" if side == "buy" else "short"}
        return order

    def fetch_open_orders(self, symbol):
        return list(self.open_orders)

    def cancel_order(self, order_id, symbol):
        self.open_orders = [order for order in self.open_orders if order["id"] != order_id]

    def fetch_positions(self, symbols):
        if not self.position:
            return [{"contracts": 0}]
        return [{"contracts": self.position["contracts"], "side": self.position["side"]}]


class FuturesSystemTests(unittest.TestCase):
    def test_strategy_timeframes_and_legacy_compatibility(self):
        periods = 1800
        index = pd.date_range(datetime.now(timezone.utc) - timedelta(minutes=15 * periods),
                              periods=periods, freq="15min", tz="UTC")
        values = [40000 + i * 0.8 + (i % 12) * 12 for i in range(periods)]
        candles = pd.DataFrame({"open": values, "high": [v + 20 for v in values],
                                "low": [v - 20 for v in values], "close": values,
                                "volume": [100] * periods}, index=index)
        futures = build_signals(candles, "15m", ("30m", "1h", "2h"))
        self.assertTrue({"score_15m", "score_30m", "score_1h", "score_2h"}.issubset(futures.columns))
        legacy_input = candles.resample("30min").agg({"open": "first", "high": "max", "low": "min",
                                                      "close": "last", "volume": "sum"}).dropna()
        legacy = build_signals(legacy_input, "30m", ("1h", "2h", "4h"))
        self.assertIn("score_4h", legacy.columns)

    def test_ohlcv_fetch_paginates_beyond_exchange_page_limit(self):
        exchange = FakeExchange()
        candles = agent.closed_candles(exchange, "BTC/USDT:USDT", 14)
        self.assertGreater(len(candles), 1000)
        self.assertGreater(exchange.pages, 1)
        self.assertEqual(candles.index.tz, timezone.utc)

    def test_entry_creates_stop_and_target_and_saves_position(self):
        exchange = FakeExchange()
        row = pd.Series({"atr": 100.0}, name=pd.Timestamp.now(tz="UTC"))
        config = {"risk_per_trade_pct": 0.0025, "max_notional_equity_pct": 0.15,
                  "stop_atr_multiple": 1.5, "take_profit_atr_multiple": 2.0,
                  "leverage": 2, "fees_bps": 5.0}
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            log_path = Path(directory) / "trades.jsonl"
            state = {"position": None, "config": {"fees_bps": 5}}
            agent.enter_position(exchange, "BTC/USDT:USDT", row, "long", config,
                                 state, state_path, log_path)
            self.assertEqual(state["position"]["direction"], "long")
            self.assertEqual(len(exchange.open_orders), 2)
            self.assertTrue(state_path.exists())

    def test_reconciliation_does_not_replace_valid_protection(self):
        exchange = FakeExchange()
        exchange.position = {"contracts": 1.0, "side": "long"}
        exchange.open_orders = [
            {"id": "stop", "reduceOnly": True},
            {"id": "target", "reduceOnly": True},
        ]
        state = {"position": {"symbol": "BTC/USDT:USDT", "direction": "long", "qty": 1.0,
                              "entry": 40000, "stop": 39900, "target": 40200}}
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            log_path = Path(directory) / "trades.jsonl"
            agent.reconcile_saved_position(exchange, state, state_path, log_path)
        self.assertEqual(len(exchange.open_orders), 2)
        self.assertEqual(exchange.created, [])


if __name__ == "__main__":
    unittest.main()
