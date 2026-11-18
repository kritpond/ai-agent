# Binance USD-M Futures Testnet agent

This experimental agent trades only **BTC/USDT:USDT** on Binance USD-M Futures Testnet.

## Safety boundaries

- Uses only Binance Futures Testnet sandbox endpoints.
- Default mode is scan-only. Execution requires `--execute-testnet` and `BINANCE_TESTNET=1`.
- Testnet credentials must be supplied only through environment variables:
  - `BINANCE_FUTURES_TESTNET_API_KEY`
  - `BINANCE_FUTURES_TESTNET_API_SECRET`
- Isolated margin, 2x default leverage, one position, ATR stop/target sizing, and a 2% daily loss entry halt are enabled.
- The entry creates exchange-side reduce-only `STOP_MARKET` and `TAKE_PROFIT_MARKET` orders. The process also monitors them, but the exchange protections remain active if the process stops.
- State and trade events are stored under ignored `data/`.
- Testnet only; no profitability claim.

## Strategy

The 15-minute closed candle is the entry timeframe. Higher-timeframe confirmation is 30m, 1h, and 2h. The latest candle must be closed before any signal is used.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r trade_requirements.txt
python binance_futures_testnet_agent.py
```

Execution mode:

```bash
export BINANCE_FUTURES_TESTNET_API_KEY='your-key'
export BINANCE_FUTURES_TESTNET_API_SECRET='your-secret'
export BINANCE_TESTNET=1
python binance_futures_testnet_agent.py --execute-testnet
```

Never use production credentials or commit credentials in this repository.
