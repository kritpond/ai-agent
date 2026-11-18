from __future__ import annotations

import numpy as np
import pandas as pd

TF = {
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
}


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


def resample_closed(base: pd.DataFrame, timeframe: str, base_timeframe: str) -> pd.DataFrame:
    """Aggregate a candle frame and label only completed bars."""
    if timeframe == base_timeframe:
        return indicators(base)
    if timeframe not in TF:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    bars = base.resample(TF[timeframe], label="right", closed="right", origin="start_day").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    return indicators(bars)


def build_signals(
    base: pd.DataFrame,
    base_timeframe: str = "30m",
    higher_timeframes: tuple[str, ...] | list[str] = ("1h", "2h", "4h"),
) -> pd.DataFrame:
    """Create signals on the base timeframe using only already closed higher bars."""
    if base_timeframe not in TF:
        raise ValueError(f"Unsupported base timeframe: {base_timeframe}")

    supported_higher = tuple(higher_timeframes)
    if not supported_higher:
        raise ValueError("At least one higher timeframe is required")

    timeframes = (base_timeframe,) + supported_higher
    frames = {tf: resample_closed(base, tf, base_timeframe) for tf in timeframes}
    result = frames[base_timeframe].copy()

    for tf in supported_higher:
        higher = frames[tf][["ema_fast", "ema_slow", "rsi"]].rename(columns=lambda c: f"{tf}_{c}")
        result = pd.merge_asof(
            result.sort_index(), higher.sort_index(),
            left_index=True, right_index=True, direction="backward",
        )

    def score(frame: pd.DataFrame, tf: str) -> pd.Series:
        prefix = "" if tf == base_timeframe else f"{tf}_"
        ema_fast = frame[f"{prefix}ema_fast"]
        ema_slow = frame[f"{prefix}ema_slow"]
        rsi = frame[f"{prefix}rsi"]
        return np.sign(ema_fast - ema_slow) * 0.65 + ((rsi - 50) / 50).clip(-1, 1) * 0.35

    base_score = f"score_{base_timeframe}"
    result[base_score] = score(result, base_timeframe)
    for tf in supported_higher:
        result[f"score_{tf}"] = score(result, tf)

    weights = {base_timeframe: 0.50, supported_higher[0]: 0.20}
    if len(supported_higher) >= 2:
        weights[supported_higher[1]] = 0.15
    if len(supported_higher) >= 3:
        weights[supported_higher[2]] = 0.15

    confidence = sum(weight * result[f"score_{tf}"].abs() for tf, weight in weights.items())
    if len(supported_higher) == 2:
        confidence += 0.15 * result[f"score_{supported_higher[1]}"].abs()
    result["confidence"] = confidence.clip(0, 1)

    positive = [float(result[f"score_{tf}"]) if False else result[f"score_{tf}"] for tf in supported_higher]

    if base_timeframe == "15m" and supported_higher == ("30m", "1h", "2h"):
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
    else:
        all_higher_positive = np.ones(len(result), dtype=bool)
        all_higher_negative = np.ones(len(result), dtype=bool)
        for tf in supported_higher:
            all_higher_positive &= result[f"score_{tf}"] > 0
            all_higher_negative &= result[f"score_{tf}"] < 0
        long_ok = all_higher_positive & (result[base_score] > 0.15)
        short_ok = all_higher_negative & (result[base_score] < -0.15)
        result["signal"] = np.select(
            [long_ok, short_ok, (result[base_score] < -0.15) | all_higher_negative],
            ["buy", "sell", "exit"],
            default="hold",
        )

    required = ["atr", "confidence", base_score] + [f"score_{tf}" for tf in supported_higher]
    return result.dropna(subset=required)
