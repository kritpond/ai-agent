from __future__ import annotations

from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "trade_config.yaml"


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict:
    with Path(path).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("Config must be a YAML mapping")
    return config
