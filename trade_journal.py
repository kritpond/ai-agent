from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import yaml


class TradeJournal:
    """Append-only Obsidian trade notes and periodic review; never changes strategy."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def record(self, symbol: str, trade, metrics: dict | None = None) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.directory / f"{day} Trade Log.md"
        timestamp = datetime.now(timezone.utc).isoformat()
        lines = [
            f"## {symbol} — {trade.entry_time} to {trade.exit_time}",
            f"- Logged: {timestamp}", f"- Side: {trade.side}",
            f"- Entry / exit: {trade.entry:.8g} / {trade.exit:.8g}",
            f"- Quantity: {trade.quantity:.8g}", f"- Net PnL (fees included): {trade.net_pnl:.2f} USDT",
            f"- Exit reason: {trade.exit_reason}", "", "### Review prompts",
            "- Did the higher-timeframe trend agree with the entry?",
            "- Were fees and slippage assumptions realistic?",
            "- Was the stop/target sequence handled conservatively?",
            "- What evidence supports changing a rule? Validate any proposal out-of-sample.",
        ]
        if metrics:
            lines += ["", "### Backtest context", "```yaml", yaml.safe_dump(metrics, sort_keys=False).rstrip(), "```"]
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n\n")
        return path

    def review(self, metrics_by_symbol: dict) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.directory / f"{day} Research Review.md"
        content = "# Research review\n\n> Statistics are historical, not a forecast or a promise of profit. No automatic parameter changes or live orders.\n\n```yaml\n"
        content += yaml.safe_dump(metrics_by_symbol, sort_keys=False, allow_unicode=True)
        content += "```\n\n## Next checks\n- Compare against buy-and-hold and a no-trade baseline.\n- Re-run with higher fees/slippage and a separate untouched test period.\n- Reject parameter changes that do not improve out-of-sample results.\n"
        path.write_text(content, encoding="utf-8")
        return path
