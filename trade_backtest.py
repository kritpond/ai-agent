from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Trade:
    entry_time: str
    exit_time: str
    side: str
    entry: float
    exit: float
    quantity: float
    net_pnl: float
    exit_reason: str


def _metrics(trades: list[Trade], equity: list[float], initial: float) -> dict:
    pnls = [t.net_pnl for t in trades]
    gains = sum(x for x in pnls if x > 0)
    losses = -sum(x for x in pnls if x < 0)
    peak = initial
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        max_dd = max(max_dd, (peak - value) / peak if peak else 0.0)
    return {
        "initial_balance": initial,
        "final_balance": equity[-1] if equity else initial,
        "net_return_pct": ((equity[-1] / initial) - 1) * 100 if equity and initial else 0.0,
        "trades": len(trades),
        "win_rate_pct": (sum(x > 0 for x in pnls) / len(pnls) * 100) if pnls else 0.0,
        "profit_factor": gains / losses if losses else (float("inf") if gains else 0.0),
        "max_drawdown_pct": max_dd * 100,
    }


def run_backtest(signals, config: dict, initial_balance: float | None = None) -> tuple[dict, list[Trade]]:
    """Long-only spot simulation. Signal at candle close fills at next candle open.

    If stop and target are both touched within one candle, stop is assumed first.
    Equity curve is realized equity (not mark-to-market); fees/slippage apply on both sides.
    """
    initial = float(initial_balance if initial_balance is not None else config["initial_balance"])
    cash = initial
    fee = float(config["fees_bps"]) / 10_000
    slip = float(config["slippage_bps"]) / 10_000
    position = None
    trades: list[Trade] = []
    equity = [initial]
    pending = None

    def close_position(time, raw_price, reason):
        nonlocal cash, position
        fill = raw_price * (1 - slip)
        exit_fee = position["qty"] * fill * fee
        gross = position["qty"] * (fill - position["entry"])
        cash += gross - exit_fee
        net = gross - exit_fee - position["entry_fee"]
        trades.append(Trade(position["time"], time.isoformat(), "long", position["entry"], fill, position["qty"], net, reason))
        position = None
        equity.append(cash)

    for i in range(1, len(signals)):
        time, row = signals.index[i], signals.iloc[i]
        previous = signals.iloc[i - 1]
        open_price = float(row.open)

        # Execute decisions made only after the previous candle had closed.
        if position and pending == "exit":
            close_position(time, open_price, "signal")
        elif not position and pending == "buy" and previous.confidence >= config["min_confidence"]:
            entry = open_price * (1 + slip)
            stop = entry - float(previous.atr) * float(config["stop_atr_multiple"])
            target = entry + float(previous.atr) * float(config["take_profit_atr_multiple"])
            risk_cash = cash * float(config["risk_per_trade_pct"])
            qty_by_risk = risk_cash / (entry - stop) if stop < entry else 0.0
            qty_by_cap = cash * float(config["max_position_pct"]) / entry
            qty = max(0.0, min(qty_by_risk, qty_by_cap))
            entry_fee = qty * entry * fee
            if qty > 0 and entry_fee < cash:
                cash -= entry_fee
                position = {"time": time.isoformat(), "entry": entry, "stop": stop, "target": target, "qty": qty, "entry_fee": entry_fee}

        # Conservative intrabar execution; stop takes precedence if both levels are hit.
        if position:
            low, high = float(row.low), float(row.high)
            if low <= position["stop"]:
                close_position(time, position["stop"], "stop")
            elif high >= position["target"]:
                close_position(time, position["target"], "target")

        pending = str(row.signal) if row.signal in ("buy", "exit") else None

    if position and len(signals):
        last = signals.iloc[-1]
        close_position(signals.index[-1], float(last.close), "end_of_data")
    if not equity or equity[-1] != cash:
        equity.append(cash)
    return _metrics(trades, equity, initial), trades
