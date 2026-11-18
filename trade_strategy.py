from __future__ import annotations

import numpy as np
import pandas as pd

TF = {"15m": "15min", "30m": "30min", "1h": "1h", "2h": "2h", "4h": "4h"}


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
    tr = pd.concat([out.high - out.low, (out.high - previous_close).abs(),
                    (out.low - previous_close).abs()], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    return out


def resample_closed(base: pd.DataFrame, timeframe: str, base_timeframe: str) -> pd.DataFrame:
    if timeframe == base_timeframe:
        return indicators(base)
    if timeframe not in TF:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    bars = base.resample(TF[timeframe], label="right", closed="right", origin="start_day").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    return indicators(bars)


def build_signals(base: pd.DataFrame, base_timeframe: str = "30m",
                  higher_timeframes: tuple[str, ...] | list[str] = ("1h", "2h", "4h")) -> pd.DataFrame:
    """Build signals on completed base bars, merging only completed higher-timeframe bars."""
    if base_timeframe not in TF:
        raise ValueError(f"Unsupported base timeframe: {base_timeframe}")
    higher_timeframes = tuple(higher_timeframes)
    if not higher_timeframes or any(tf not in TF for tf in higher_timeframes):
        raise ValueError("At least one supported higher timeframe is required")
    frames = {tf: resample_closed(base, tf, base_timeframe)
              for tf in (base_timeframe, *higher_timeframes)}
    result = frames[base_timeframe].copy()
    for tf in higher_timeframes:
        higher = frames[tf][["ema_fast", "ema_slow", "rsi"]].rename(columns=lambda c: f"{tf}_{c}")
        result = pd.merge_asof(result.sort_index(), higher.sort_index(), left_index=True,
                               right_index=True, direction="backward")

    def score(frame: pd.DataFrame, timeframe: str) -> pd.Series:
        prefix = "" if timeframe == base_timeframe else f"{timeframe}_"
        return (np.sign(frame[f"{prefix}ema_fast"] - frame[f"{prefix}ema_slow"]) * 0.65
                + ((frame[f"{prefix}rsi"] - 50) / 50).clip(-1, 1) * 0.35)

    result[f"score_{base_timeframe}"] = score(result, base_timeframe)
    for tf in higher_timeframes:
        result[f"score_{tf}"] = score(result, tf)

    # Keep the established 0.50/0.20/0.15/0.15 weighting where possible;
    # renormalize for strategies with fewer higher timeframes.
    preferred = [0.50, 0.20, 0.15, 0.15]
    raw_weights = [preferred[0], *preferred[1:1 + len(higher_timeframes)]]
    total_weight = sum(raw_weights)
    weights = [weight / total_weight for weight in raw_weights]
    all_tfs = [base_timeframe, *higher_timeframes]
    result["confidence"] = sum(
        weight * result[f"score_{tf}"].abs() for tf, weight in zip(all_tfs, weights)
    ).clip(0, 1)

    base_score = result[f"score_{base_timeframe}"]
    higher_positive = np.logical_and.reduce([result[f"score_{tf}"] > 0 for tf in higher_timeframes])
    higher_negative = np.logical_and.reduce([result[f"score_{tf}"] < 0 for tf in higher_timeframes])
    long_ok = higher_positive & (base_score > 0.15)
    short_ok = higher_negative & (base_score < -0.15)

    # Preserve the 15m futures strategy's more specific exits and entry thresholds.
    if base_timeframe == "15m" and higher_timeframes == ("30m", "1h", "2h"):
        long_ok &= result.score_30m > 0.15
        short_ok &= result.score_30m < -0.15
        exit_long = (result.score_15m < -0.15) | (result.score_30m < 0)
        exit_short = (result.score_15m > 0.15) | (result.score_30m > 0)
        result["signal"] = np.select([long_ok, short_ok, exit_long, exit_short],
                                     ["buy", "sell", "exit", "exit"], default="hold")
    else:
        result["signal"] = np.select(
            [long_ok, short_ok, (base_score < -0.15) | higher_negative],
            ["buy", "sell", "exit"], default="hold",
        )

    required = ["atr", "confidence", f"score_{base_timeframe}"]
    required.extend(f"score_{tf}" for tf in higher_timeframes)
    return result.dropna(subset=required)
