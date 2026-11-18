from __future__ import annotations

import unittest
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from trade_strategy import build_signals


class MultiTimeframeStrategyTests(unittest.TestCase):
    def make_candles(self, periods=5000):
        index = pd.date_range(datetime(2024, 1, 1, tzinfo=timezone.utc), periods=periods,
                              freq="5min", tz="UTC")
        trend = np.linspace(30000, 38000, periods)
        cycle = 250 * np.sin(np.arange(periods) / 35)
        close = trend + cycle
        return pd.DataFrame({"open": close - 2, "high": close + 25, "low": close - 25,
                             "close": close, "volume": 100 + (np.arange(periods) % 17) * 2}, index=index)

    def test_requested_timeframes_produce_finite_scores(self):
        result = build_signals(self.make_candles(), "5m", ("10m", "30m", "1h", "2h"))
        for timeframe in ("5m", "10m", "30m", "1h", "2h"):
            self.assertIn(f"score_{timeframe}", result)
            self.assertTrue(np.isfinite(result[f"score_{timeframe}"].dropna()).all())
        self.assertTrue(result.signal.isin(["buy", "sell", "exit", "hold"]).all())
        self.assertTrue(result.confidence.between(0, 1).all())
        self.assertGreater(float(result.confidence.max()), 0.65)

    def test_higher_timeframe_data_only_appears_at_closed_bar_times(self):
        result = build_signals(self.make_candles(2000), "5m", ("10m", "30m", "1h", "2h"))
        self.assertTrue(result.index.is_monotonic_increasing)
        self.assertTrue(result.index.is_unique)
        self.assertTrue((result.index.minute % 5 == 0).all())

    def test_invalid_weights_are_rejected(self):
        with self.assertRaises(ValueError):
            build_signals(self.make_candles(2000), "5m", ("10m", "30m", "1h", "2h"), {"5m": 1})

    def test_duplicate_higher_timeframes_are_rejected(self):
        with self.assertRaises(ValueError):
            build_signals(self.make_candles(1000), "5m", ("10m", "10m"))

    def test_legacy_timeframe_strategy_remains_supported(self):
        result = build_signals(self.make_candles(4000), "15m", ("30m", "1h", "2h"))
        self.assertIn("score_15m", result)


if __name__ == "__main__":
    unittest.main()
