# Binance USD-M Futures Testnet multi-timeframe research agent

This experimental agent scans BTC/USDT, ETH/USDT, and SOL/USDT perpetual futures on **Binance Futures Testnet only**. It is not financial advice or a promise of profit. Leverage can lose test funds quickly; the strategy has not been shown to be profitable.

## Strategy

- 5m closed-candle entries, with confirmation from completed 10m, 30m, 1h, and 2h bars.
- EMA structure, RSI, MACD histogram normalized by each timeframe's ATR, ADX/directional movement, ATR, Bollinger excursion, and relative volume.
- A weighted alignment/strength index, a consensus gate, volatility filter, ATR-sized stop/target, and one position total across configured symbols.
- Default isolated margin and 2x leverage; configuration caps leverage at 5x, risk per trade at 0.1% of equity, notional at 10% of equity, and daily loss halt at 1.5%.
- The displayed confidence is a heuristic alignment score, **not** a probability of profit. No strategy settings guarantee returns.

## Data/research limitations

No news, social sentiment, funding-rate, or open-interest data is integrated. These require timestamped historical datasets and point-in-time handling to avoid look-ahead and survivorship bias. This repository does not include a validated backtest engine; before any live trading, independently test out-of-sample with fees, slippage, funding, exchange constraints, and realistic order execution. Testnet validates software/API behavior only, not profitability.

## Install, test, and scan (Windows PowerShell)

Use stable 64-bit Python 3.11 or later (not an alpha/prerelease):

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r trade_requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe binance_futures_testnet_agent.py
```

The default command is scan-only and submits no orders.

## Futures Testnet execution

Only after tests pass, verify that credentials belong to Binance Futures Testnet. Do not paste credentials into chat or source files.

```powershell
$env:BINANCE_FUTURES_TESTNET_API_KEY='your-testnet-key'
$env:BINANCE_FUTURES_TESTNET_API_SECRET='your-testnet-secret'
$env:BINANCE_TESTNET='1'
.\.venv\Scripts\python.exe binance_futures_testnet_agent.py --execute-testnet
```

The agent may remain idle until a new qualifying signal appears; it does not force trades. `Ctrl+C` stops the process but does not guarantee that an open position is closed. Check Testnet positions and open orders directly. This code is not production validated.
