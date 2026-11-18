from __future__ import annotations

import numpy as np
import pandas as pd

BASE_TIMEFRAME = "15m"
TF = {"15m": "15min", "30m": "30min", "1h": "1h", "2h": "2h"}


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
    tr = pd.concat([
        out.high - out.low,
        (out.high - previous_close).abs(),
        (out.low - previous_close).abs(),
    ], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    return out


def resample_closed(base: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate base candles and label only completed bars."""
    if timeframe == BASE_TIMEFRAME:
        return indicators(base)
    rule = TF[timeframe]
    bars = base.resample(rule, label="right", closed="right", origin="start_day").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    return indicators(bars)


def build_signals(base: pd.DataFrame) -> pd.DataFrame:
    """Create signals on closed 15m candles using only already closed higher bars."""
    frames = {tf: resample_closed(base, tf) for tf in TF}
    result = frames["15m"].copy()
    for tf in ("30m", "1h", "2h"):
        higher = frames[tf][["ema_fast", "ema_slow", "rsi"]].rename(columns=lambda c: f"{tf}_{c}")
        result = pd.merge_asof(
            result.sort_index(), higher.sort_index(),
            left_index=True, right_index=True, direction="backward",
        )

    def score(frame: pd.DataFrame, tf: str) -> pd.Series:
        prefix = "" if tf == "15m" else f"{tf}_"
        ema_fast = frame[f"{prefix}ema_fast"]
        ema_slow = frame[f"{prefix}ema_slow"]
        rsi = frame[f"{prefix}rsi"]
        return (
            np.sign(ema_fast - ema_slow) * 0.65
            + ((rsi - 50) / 50).clip(-1, 1) * 0.35
        )

    result["score_15m"] = score(result, "15m")
    for tf in ("30m", "1h", "2h"):
        result[f"score_{tf}"] = score(result, tf)

    result["confidence"] = (
        0.50 * result.score_15m.abs()
        + 0.20 * result.score_30m.abs()
        + 0.15 * result.score_1h.abs()
        + 0.15 * result.score_2h.abs()
    ).clip(0, 1)

    long_ok = (
        (result.score_2h > 0)
        & (result.score_1h > 0)
        & (result.score_30m > 0.15)
        & (result.score_15m > 0.15)
    )
    short_ok = (
        (result.score_2h < 0)
        & (result.score_1h < 0)
        & (result.score_30m < -0.15)
        & (result.score_15m < -0.15)
    )
    exit_long = (result.score_15m < -0.15) | (result.score_30m < 0)
    exit_short = (result.score_15m > 0.15) | (result.score_30m > 0)

    result["signal"] = np.select(
        [long_ok, short_ok, exit_long, exit_short],
        ["buy", "sell", "exit", "exit"],
        default="hold",
    )
    return result.dropna(subset=["atr", "confidence", "score_30m", "score_1h", "score_2h"])
