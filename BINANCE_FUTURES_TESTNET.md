# Binance USD-M Futures Testnet agent

This experimental agent trades only **BTC/USDT:USDT** on Binance USD-M Futures Testnet.

## Safety boundaries

- Uses only Binance Futures Testnet sandbox endpoints.
- Default mode is scan-only. Execution requires `--execute-testnet` and `BINANCE_TESTNET=1`.
- Credentials are read only from `BINANCE_FUTURES_TESTNET_API_KEY` and `BINANCE_FUTURES_TESTNET_API_SECRET`.
- Isolated margin, 2x default leverage, one position, ATR stop/target sizing, and a 2% daily loss entry halt are enabled.
- Entry and exchange-side reduce-only stop/target orders are reconciled on startup and after each loop.
- If a saved position is no longer open, the agent records reconciliation and clears local state.
- If an exchange position exists without matching local state, the agent stops and requires manual reconciliation.
- Local state and trade events are under ignored `data/`.
- Testnet only; no profitability claim.

## Strategy

The 15-minute closed candle is the entry timeframe. Higher-timeframe confirmation is 30m, 1h, and 2h. The latest candle must be closed before a signal is used.

## Data handling

OHLCV requests are paginated until the requested lookback is exhausted. No candle that is still forming is used.

## Run

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r trade_requirements.txt
python binance_futures_testnet_agent.py
```

Execution mode:

```powershell
$env:BINANCE_FUTURES_TESTNET_API_KEY='your-testnet-key'
$env:BINANCE_FUTURES_TESTNET_API_SECRET='your-testnet-secret'
$env:BINANCE_TESTNET='1'
python binance_futures_testnet_agent.py --execute-testnet
```

Never use production credentials or commit credentials. If a key was previously exposed, revoke and rotate it before testing.

## Tests

```powershell
python -m unittest discover -s tests -v
```
