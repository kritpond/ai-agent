from __future__ import annotations

import numpy as np
import pandas as pd

TF = {"30m": "30min", "1h": "1h", "2h": "2h", "4h": "4h"}


def indicators(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["ema_fast"] = out.close.ewm(span=9, adjust=False, min_periods=9).mean()
    out["ema_slow"] = out.close.ewm(span=21, adjust=False, min_periods=21).mean()
    delta = out.close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi"] = 100 - 100 / (1 + rs)
    previous_close = out.close.shift(1)
    tr = pd.concat([out.high - out.low, (out.high - previous_close).abs(), (out.low - previous_close).abs()], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    return out


def resample_closed(base: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate 30m candles using their close timestamps; labels only completed bars."""
    if timeframe == "30m":
        return indicators(base)
    rule = TF[timeframe]
    bars = base.resample(rule, label="right", closed="right", origin="start_day").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    return indicators(bars)


def build_signals(base: pd.DataFrame) -> pd.DataFrame:
    """Create signals at each 30m close using only higher-TF bars already closed."""
    frames = {tf: resample_closed(base, tf) for tf in ("30m", "1h", "2h", "4h")}
    result = frames["30m"].copy()
    for tf in ("1h", "2h", "4h"):
        higher = frames[tf][["ema_fast", "ema_slow", "rsi"]].rename(columns=lambda c: f"{tf}_{c}")
        result = pd.merge_asof(result.sort_index(), higher.sort_index(), left_index=True, right_index=True, direction="backward")
    score = lambda f, tf: (np.sign(f[f"{tf}_ema_fast"] - f[f"{tf}_ema_slow"]) * 0.65 + ((f[f"{tf}_rsi"] - 50) / 50).clip(-1, 1) * 0.35)
    result["score_30m"] = score(result, "") if False else (np.sign(result.ema_fast - result.ema_slow) * 0.65 + ((result.rsi - 50) / 50).clip(-1, 1) * 0.35)
    for tf in ("1h", "2h", "4h"):
        result[f"score_{tf}"] = score(result, tf)
    result["confidence"] = (0.50 * result.score_30m.abs() + 0.20 * result.score_1h.abs() + 0.15 * result.score_2h.abs() + 0.15 * result.score_4h.abs()).clip(0, 1)
    long_ok = (result.score_4h > 0) & (result.score_2h > 0) & (result.score_1h > 0) & (result.score_30m > 0.15)
    exit_ok = (result.score_30m < -0.15) | (result.score_1h < 0)
    result["signal"] = np.select([long_ok, exit_ok], ["buy", "exit"], default="hold")
    return result.dropna(subset=["atr", "confidence", "score_1h", "score_2h", "score_4h"])
