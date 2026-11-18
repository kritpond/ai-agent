# Binance USD-M Futures Testnet multi-timeframe research agent

This experimental agent is **Testnet-only** and scans BTC/USDT, ETH/USDT, and SOL/USDT perpetual futures. It is not financial advice or a promise of profit. Leverage can lose test funds quickly; the same strategy may perform differently live.

## Strategy outline

- 5m closed-candle entries; confirmation from closed 10m, 30m, 1h, and 2h bars.
- Causal indicators: EMA trend structure, RSI, MACD histogram, ADX/directional movement, ATR, Bollinger-band standardized deviation, and relative volume.
- Multi-timeframe weighted agreement, minimum confidence, volume and volatility gates; ATR-sized stop/target levels.
- One position total across configured symbols, isolated margin, 2x default leverage (hard config maximum 5x), notional/risk limits, and daily loss entry halt.
- OHLCV history is paginated; unfinished bars are excluded. Exchange-side reduce-only stop/target orders are intended as protection; inspect actual open orders and positions.

## News and external data

This version does **not** consume news or social sentiment. There is no configured, licensed, timestamped provider or stale-data policy, so fabricating a news score would be unsafe. Add a provider only with explicit source, timestamps, failure behavior, and backtestable historical data. Funding/open-interest signals also require separate validation before they can affect orders.

## Install / test (Windows PowerShell)

Use stable 64-bit Python 3.11 or later; avoid prerelease/alpha Python versions.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r trade_requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe binance_futures_testnet_agent.py
```

The default command is scan-only and submits no orders.

## Futures Testnet execution

Only after tests pass, verify you are using Binance Futures Testnet credentials and understand the risk controls. Keep the key local; never paste it into chat or commit it.

```powershell
$env:BINANCE_FUTURES_TESTNET_API_KEY='your-testnet-key'
$env:BINANCE_FUTURES_TESTNET_API_SECRET='your-testnet-secret'
$env:BINANCE_TESTNET='1'
.\.venv\Scripts\python.exe binance_futures_testnet_agent.py --execute-testnet
```

The agent trades only after a new qualifying signal and may remain idle. `Ctrl+C` stops the process but does not guarantee a position is closed. Check Testnet positions and open orders directly. This code has not been validated for production use or profitability.
