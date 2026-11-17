# Trade Agent — OKX/Bybit research and paper-signal starter

This repository addition provides a reproducible **research/backtesting scaffold** for BTC/USDT, ETH/USDT, and SOL/USDT on 30m candles with completed 1h/2h/4h filters. It is spot-only. It does **not** place orders, and no strategy can guarantee profits.

## Install and run

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
python -m pip install -r trade_requirements.txt
python trade_agent.py backtest
```

Public OHLCV data is downloaded from OKX and cached locally under `data/`. The first run may take a while because exchange rate limits are respected. Change `exchange` to `bybit` in `trade_config.yaml` to use Bybit's public candles. Run a latest-signal scan (no order submission) with:

```bash
python trade_agent.py paper-scan
```

Backtest metrics are written to `data/trade_backtest_report.json`. Trade notes and a research review are appended/generated under `obsidian/Trade Journal` (open that folder as an Obsidian vault or move it into your existing vault).

## What the simulation does / limitations

- Uses historical 30m OHLCV and aggregates higher timeframes from completed base candles. Signals use only information available at the preceding candle close and enter at the following candle open.
- Long-only spot; position size is capped by both risk budget and configured maximum allocation.
- Includes configurable per-side fee and slippage assumptions. If stop and target are both touched in one candle, assumes the stop happened first.
- Exits on stop, target, opposite signal at the next open, or end of data. Reports realized-equity drawdown, not mark-to-market drawdown.
- Treat the result as an initial experiment, not evidence of future profitability. This simple single-symbol simulation does not model portfolio-level concurrent exposure, exchange outages, partial fills, spread dynamics, taxes, or market impact. It does not yet implement walk-forward optimization or a formal untouched holdout report.

Before considering real use, validate data quality, compare with buy-and-hold/no-trade baselines, run chronological train/validation/test periods, stress costs, inspect individual trades, and paper trade. Do not optimize and evaluate on the same period.

## Learning / Obsidian loop

The agent writes closed trade outcomes and backtest context to Markdown and creates a review with follow-up checks. This is an auditable **human-reviewed learning loop**, not autonomous self-modification: it never changes parameters or executes trades based on its notes. Only adopt a proposed change after separate out-of-sample evaluation.

## Safety

No API credentials are required; all data access is public. There is intentionally no live trading or order placement code. Historical results cannot guarantee profits, and crypto assets can lose substantial value.
