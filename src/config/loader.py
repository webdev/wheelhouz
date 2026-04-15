"""Hot-reloadable configuration manager.

Reads YAML config files and watches for changes.
All config access goes through this module.
"""

from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def load_yaml(filename: str) -> dict[str, Any]:
    """Load a YAML config file from the config directory."""
    path = CONFIG_DIR / filename
    with open(path) as f:
        return yaml.safe_load(f)  # type: ignore[no-any-return]


def load_trading_params() -> dict[str, Any]:
    """Load trading parameters."""
    return load_yaml("trading_params.yaml")


def load_watchlist() -> list[str]:
    """Load the watchlist symbols."""
    data = load_yaml("watchlist.yaml")
    return data.get("watchlist", [])  # type: ignore[no-any-return]


def load_scanner_universe() -> list[str]:
    """Load the scanner universe symbols (broader screening pool)."""
    data = load_yaml("watchlist.yaml")
    return data.get("scanner_universe", [])  # type: ignore[no-any-return]


def load_accounts_config() -> dict[str, Any]:
    """Load account routing configuration."""
    return load_yaml("accounts.yaml")
