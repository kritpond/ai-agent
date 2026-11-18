from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd

from trade_strategy import build_signals


class StrategyTests(unittest.TestCase):
    def _frame(self, periods: int = 1600) -> pd.DataFrame:
        start = datetime.now(timezone.utc) - timedelta(minutes=15 * periods)
        index = pd.date_range(start=start, periods=periods, freq="15min", tz="UTC")
        values = [40000.0 + i * 0.8 + (i % 12) * 12 for i in range(periods)]
        frame = pd.DataFrame({
            "open": values,
            "high": [value + 20 for value in values],
            "low": [value - 20 for value in values],
            "close": values,
            "volume": [100.0] * periods,
        }, index=index)
        return frame

    def test_15m_30m_1h_2h_columns_exist(self):
        frame = build_signals(self._frame(), "15m", ("30m", "1h", "2h"))
        self.assertIn("score_15m", frame.columns)
        self.assertIn("score_30m", frame.columns)
        self.assertIn("score_1h", frame.columns)
        self.assertIn("score_2h", frame.columns)

    def test_legacy_30m_strategy_still_works(self):
        frame = build_signals(self._frame().resample("30min").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna(), "30m", ("1h", "2h", "4h"))
        self.assertIn("score_30m", frame.columns)
        self.assertIn("score_1h", frame.columns)


if __name__ == "__main__":
    unittest.main()
