# Binance USD-M Futures Testnet agent

This adds an **experimental, long/short USDT-margined futures agent for Binance Futures Testnet**. It uses the repository's existing multi-timeframe indicators. It is not a profitability claim or a recommendation to trade. Futures can lose the entire margin quickly; leverage magnifies losses and may cause liquidation before a software-monitored stop runs.

## Safety boundaries

- This new agent unconditionally enables CCXT sandbox mode and has **no live endpoint mode**. Do not modify the sandbox routing or provide production credentials.
- It defaults to scan-only. Orders require `--execute-testnet` and `BINANCE_TESTNET=1`, plus Binance USD-M Futures **Testnet-only** API credentials.
- Isolated margin is requested; leverage is limited in code to 1–5x (default 3x). Configuration also caps estimated risk per trade and notional exposure. Do not raise these caps casually.
- One position at a time; long/short entries, reduce-only market exits, and a daily loss-entry halt are implemented.
- Stop and target levels are monitored by this process, **not submitted as exchange-side protective orders**. A crash, lag, disconnection, partial fill, or fast price move may leave a position open or cause losses beyond the estimate. A daily loss halt is not a guaranteed account-level loss cap.
- On startup, an untracked or mismatched exchange position stops execution for manual reconciliation. Ctrl+C does not close an open position. Check both the state file and Testnet account.
- Testnet fills, liquidity, fees, and liquidation behavior may differ from production. This agent is for engineering tests only; it is not safe for real funds.

## Setup

Install dependencies from the existing requirements file, then export **Testnet-only** credentials:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\\Scripts\\activate
pip install -r trade_requirements.txt
export BINANCE_API_KEY='your-futures-testnet-key'
export BINANCE_API_SECRET='your-futures-testnet-secret'
export BINANCE_TESTNET=1
```

Never commit API keys, use production credentials, or enable withdrawals.

## Run

Scan closed 30-minute candles without authenticating or placing orders:

```bash
python binance_futures_testnet_agent.py
```

Submit orders to USD-M Futures Testnet:

```bash
python binance_futures_testnet_agent.py --execute-testnet
```

Settings are in `binance_futures_testnet.yaml`. Local state and JSONL trade events are written under ignored `data/`.

## Strategy notes

A repository `buy` signal opens a long; aligned negative multi-timeframe scores may open a short. The loop polls every configured interval and sends market orders. Quantity is estimated from ATR stop distance, the risk fraction, a notional cap, leverage, and available margin. The stop/target are process-monitored only. Prices can gap through them, so sizing is not a guarantee of maximum loss. Review the code and verify testnet order handling and manual recovery before enabling execution.
