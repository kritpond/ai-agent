from __future__ import annotations

import time
from pathlib import Path
import ccxt
import pandas as pd

TIMEFRAME_MS = {"30m": 30 * 60_000, "1h": 60 * 60_000, "2h": 2 * 60 * 60_000, "4h": 4 * 60 * 60_000}


def make_exchange(exchange_id: str = "okx"):
    if exchange_id not in {"okx", "bybit"}:
        raise ValueError("Supported public data exchanges: okx, bybit")
    exchange_class = getattr(ccxt, exchange_id)
    return exchange_class({"enableRateLimit": True, "options": {"defaultType": "spot"}})


def fetch_ohlcv(exchange, symbol: str, timeframe: str, since_ms: int, until_ms: int | None = None) -> pd.DataFrame:
    """Fetch public candles page-by-page. Index is candle OPEN time in UTC."""
    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    step = TIMEFRAME_MS[timeframe]
    until_ms = until_ms or exchange.milliseconds()
    cursor, rows, last_seen = since_ms, [], None
    while cursor < until_ms:
        page = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=300)
        if not page:
            break
        fresh = [row for row in page if row[0] >= cursor and row[0] < until_ms and row[0] != last_seen]
        if not fresh:
            break
        rows.extend(fresh)
        last_seen = fresh[-1][0]
        next_cursor = last_seen + step
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(exchange.rateLimit / 1000)
    if not rows:
        raise RuntimeError(f"No OHLCV returned for {symbol} {timeframe}")
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = frame.drop_duplicates("timestamp").sort_values("timestamp")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    return frame.set_index("timestamp").astype(float)


def cache_path(symbol: str, timeframe: str = "30m") -> Path:
    safe_symbol = symbol.replace("/", "-")
    path = Path("data") / "okx" / f"{safe_symbol}_{timeframe}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_or_fetch(exchange, symbol: str, timeframe: str, since_ms: int) -> pd.DataFrame:
    path = cache_path(symbol, timeframe)
    if path.exists():
        frame = pd.read_csv(path, index_col=0, parse_dates=True)
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        if len(frame) and frame.index[0].timestamp() * 1000 <= since_ms:
            return frame.loc[frame.index >= pd.to_datetime(since_ms, unit="ms", utc=True)]
    frame = fetch_ohlcv(exchange, symbol, timeframe, since_ms)
    frame.to_csv(path)
    return frame
