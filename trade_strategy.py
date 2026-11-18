from __future__ import annotations

import numpy as np
import pandas as pd

TF = {"5m": "5min", "10m": "10min", "15m": "15min", "30m": "30min", "1h": "1h", "2h": "2h", "4h": "4h"}
DURATION = {"5m": pd.Timedelta(minutes=5), "10m": pd.Timedelta(minutes=10),
            "15m": pd.Timedelta(minutes=15), "30m": pd.Timedelta(minutes=30),
            "1h": pd.Timedelta(hours=1), "2h": pd.Timedelta(hours=2),
            "4h": pd.Timedelta(hours=4)}


def indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Calculate causal trend, momentum, volatility and volume indicators."""
    out = frame.copy()
    close = out["close"]
    out["ema_fast"] = close.ewm(span=9, adjust=False, min_periods=9).mean()
    out["ema_mid"] = close.ewm(span=21, adjust=False, min_periods=21).mean()
    out["ema_slow"] = close.ewm(span=50, adjust=False, min_periods=50).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi"] = 100 - 100 / (1 + rs)

    previous_close = close.shift(1)
    tr = pd.concat([(out.high - out.low), (out.high - previous_close).abs(),
                    (out.low - previous_close).abs()], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    plus_dm = out.high.diff().where(out.high.diff() > -out.low.diff(), 0.0).clip(lower=0)
    minus_dm = (-out.low.diff()).where(-out.low.diff() > out.high.diff(), 0.0).clip(lower=0)
    atr_safe = out.atr.replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / atr_safe
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / atr_safe
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    out["adx"] = dx.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    macd = close.ewm(span=12, adjust=False, min_periods=12).mean() - close.ewm(
        span=26, adjust=False, min_periods=26).mean()
    out["macd_hist"] = macd - macd.ewm(span=9, adjust=False, min_periods=9).mean()
    out["bb_mid"] = close.rolling(20, min_periods=20).mean()
    bb_std = close.rolling(20, min_periods=20).std(ddof=0)
    out["bb_z"] = ((close - out.bb_mid) / (2 * bb_std.replace(0, np.nan))).clip(-1, 1)
    vol_mean = out.volume.rolling(20, min_periods=20).mean()
    out["volume_ratio"] = out.volume / vol_mean.replace(0, np.nan)
    return out


def resample_closed(base: pd.DataFrame, timeframe: str, base_timeframe: str) -> pd.DataFrame:
    """Resample open-timestamped candles; labels are the actual close times."""
    if timeframe not in TF or base_timeframe not in DURATION:
        raise ValueError(f"Unsupported timeframe: {timeframe} or base {base_timeframe}")
    close_indexed = base.copy()
    close_indexed.index = close_indexed.index + DURATION[base_timeframe]
    if timeframe == base_timeframe:
        return indicators(close_indexed)
    bars = close_indexed.resample(TF[timeframe], label="right", closed="right", origin="start_day").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    # Reject partial higher-timeframe bars. Require exact expected constituent count.
    expected = int(DURATION[timeframe] / DURATION[base_timeframe])
    counts = close_indexed["close"].resample(TF[timeframe], label="right", closed="right", origin="start_day").count()
    bars = bars.loc[counts.reindex(bars.index).fillna(0).astype(int) == expected]
    return indicators(bars)


def build_signals(base: pd.DataFrame, base_timeframe: str = "30m",
                  higher_timeframes: tuple[str, ...] | list[str] = ("1h", "2h", "4h"),
                  weights: dict[str, float] | None = None) -> pd.DataFrame:
    """Generate causal multi-timeframe signals; no unfinished HTF bars are merged."""
    if base_timeframe not in TF:
        raise ValueError(f"Unsupported base timeframe: {base_timeframe}")
    higher_timeframes = tuple(higher_timeframes)
    if not higher_timeframes or any(tf not in TF for tf in higher_timeframes):
        raise ValueError("At least one supported higher timeframe is required")
    if any(DURATION[tf] <= DURATION[base_timeframe] for tf in higher_timeframes):
        raise ValueError("Higher timeframes must be longer than the base timeframe")
    all_tfs = (base_timeframe, *higher_timeframes)
    frames = {tf: resample_closed(base, tf, base_timeframe) for tf in all_tfs}
    result = frames[base_timeframe].copy()
    for tf in higher_timeframes:
        higher = frames[tf][["ema_fast", "ema_mid", "ema_slow", "rsi", "adx", "macd_hist"]].rename(
            columns=lambda col: f"{tf}_{col}")
        result = pd.merge_asof(result.sort_index(), higher.sort_index(), left_index=True,
                               right_index=True, direction="backward")

    def score(frame: pd.DataFrame, tf: str) -> pd.Series:
        prefix = "" if tf == base_timeframe else f"{tf}_"
        trend = (np.sign(frame[f"{prefix}ema_fast"] - frame[f"{prefix}ema_mid"])
                 + np.sign(frame[f"{prefix}ema_mid"] - frame[f"{prefix}ema_slow"])) / 2
        momentum = ((frame[f"{prefix}rsi"] - 50) / 25).clip(-1, 1)
        macd = np.tanh((frame[f"{prefix}macd_hist"] / frame["atr"].replace(0, np.nan)) * 8)
        adx = frame[f"{prefix}adx"].clip(0, 50) / 50
        # Trend/momentum signal is moderated in low-trend regimes.
        directional = 0.50 * trend + 0.30 * momentum + 0.20 * macd
        return directional * (0.55 + 0.45 * adx)

    for tf in all_tfs:
        result[f"score_{tf}"] = score(result, tf)

    default_weights = {base_timeframe: 0.28}
    higher_defaults = [0.22, 0.22, 0.16, 0.12]
    for i, tf in enumerate(higher_timeframes):
        default_weights[tf] = higher_defaults[min(i, len(higher_defaults) - 1)]
    selected_weights = weights or default_weights
    if set(selected_weights) != set(all_tfs) or any(value < 0 for value in selected_weights.values()):
        raise ValueError("Weights must be non-negative and defined for every selected timeframe")
    total_weight = sum(selected_weights.values())
    if total_weight <= 0:
        raise ValueError("At least one timeframe weight must be positive")
    result["confidence"] = sum(
        selected_weights[tf] / total_weight * result[f"score_{tf}"].abs() for tf in all_tfs
    ).clip(0, 1)

    base_score = result[f"score_{base_timeframe}"]
    higher_scores = [result[f"score_{tf}"] for tf in higher_timeframes]
    weighted_confirmation = sum(selected_weights[tf] * result[f"score_{tf}"] for tf in higher_timeframes)
    # Require the bulk of HTF evidence to agree; avoid trading a lone fast-TF spike.
    higher_long = (weighted_confirmation > 0.15) & (sum((s > 0).astype(int) for s in higher_scores) >= max(1, len(higher_scores) - 1))
    higher_short = (weighted_confirmation < -0.15) & (sum((s < 0).astype(int) for s in higher_scores) >= max(1, len(higher_scores) - 1))
    volume_ok = result.volume_ratio >= 0.6
    long_ok = higher_long & (base_score > 0.18) & volume_ok
    short_ok = higher_short & (base_score < -0.18) & volume_ok
    result["signal"] = np.select(
        [long_ok, short_ok, (base_score < -0.12) & (weighted_confirmation < 0),
         (base_score > 0.12) & (weighted_confirmation > 0)],
        ["buy", "sell", "exit", "exit"], default="hold")
    result["volatility_pct"] = result.atr / result.close.replace(0, np.nan)
    required = ["atr", "confidence", "volume_ratio", "volatility_pct"] + [f"score_{tf}" for tf in all_tfs]
    return result.dropna(subset=required)
