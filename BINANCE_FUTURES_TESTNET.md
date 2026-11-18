# Binance USD-M Futures Testnet agent

This experimental agent targets **BTC/USDT:USDT on Binance Futures Testnet only**.

## Safety

- Scan-only is the default. Orders require `--execute-testnet`, `BINANCE_TESTNET=1`, and testnet-only API credentials.
- API keys are loaded from `BINANCE_FUTURES_TESTNET_API_KEY` and `BINANCE_FUTURES_TESTNET_API_SECRET` environment variables.
- The agent uses sandbox mode unconditionally; it has no production endpoint mode.
- One position at a time, isolated margin, 2x default leverage, capped notional/risk, and exchange-side reduce-only stop/target orders.
- The agent stores state immediately after an entry fill, reconciles it against the exchange, and preserves existing protective orders during polling.
- Testnet is for engineering checks, not a profitability claim. A network or exchange failure may still require manual account review.

## Strategy and data

Entry timeframe is 15m; confirmation timeframes are 30m, 1h, and 2h. OHLCV retrieval is paginated; unfinished candles are excluded.

## Setup / scan

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r trade_requirements.txt
python binance_futures_testnet_agent.py
```

## Test suite (mock exchange; no network orders)

```powershell
python -m unittest discover -s tests -v
```

## Testnet execution (will submit test orders)

Only after mock tests pass and you have verified the account is the Binance USD-M Futures Testnet account:

```powershell
$env:BINANCE_FUTURES_TESTNET_API_KEY='your-testnet-key'
$env:BINANCE_FUTURES_TESTNET_API_SECRET='your-testnet-secret'
$env:BINANCE_TESTNET='1'
python binance_futures_testnet_agent.py --execute-testnet
```

Never use production keys or commit credentials. Revoke any key previously exposed in chat. This agent can submit real orders to the test environment when explicitly enabled.
