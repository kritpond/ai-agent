# Binance USD-M Futures Testnet agent

This experimental agent scans and trades **BTC/USDT, ETH/USDT, and SOL/USDT perpetual futures on Binance Futures Testnet only**.

## Safety

- Scan-only is the default. Orders require `--execute-testnet`, `BINANCE_TESTNET=1`, and Testnet-only API credentials.
- API keys are loaded from `BINANCE_FUTURES_TESTNET_API_KEY` and `BINANCE_FUTURES_TESTNET_API_SECRET` environment variables.
- CCXT sandbox mode is enabled unconditionally; the application has no production endpoint mode.
- Leverage is configured to 2x (maximum allowed configuration is 5x); isolated margin, risk/notional caps, daily loss halt, and exchange-side reduce-only stop/target orders are enabled.
- The agent scans all configured symbols and can hold **one position total at a time**, selecting the highest-confidence new signal. It does not open three simultaneous positions.
- State is persisted after fills and reconciled with exchange positions across all configured symbols. A position not represented in local state halts execution for manual reconciliation.
- A Testnet key and balance are required for order execution. Network/API failures may require manual account review.
- Testnet results are not a profitability claim. Never use production keys or commit credentials; revoke any key previously exposed.

## Setup and scan-only mode (no orders)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r trade_requirements.txt
python -m unittest discover -s tests -v
python binance_futures_testnet_agent.py
```

## Execute on Futures Testnet

Only after tests pass, verify the Binance Futures Testnet account, create a Testnet key, and confirm the configured 2x leverage/risk limits. Never paste keys into chat or source files.

```powershell
$env:BINANCE_FUTURES_TESTNET_API_KEY='your-testnet-key'
$env:BINANCE_FUTURES_TESTNET_API_SECRET='your-testnet-secret'
$env:BINANCE_TESTNET='1'
python binance_futures_testnet_agent.py --execute-testnet
```

The agent trades only when a new strategy signal meets the confidence threshold. It may remain idle if there is no signal. Press `Ctrl+C` to stop the process, then verify open positions/orders in the Testnet account; stopping the agent does not itself close an open position.
