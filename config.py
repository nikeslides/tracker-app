"""Configuration loader for Tracker."""

import os
from pathlib import Path

try:
    import tomllib
except ImportError:
    # Python < 3.11 fallback
    import tomli as tomllib

CONFIG_PATH = Path(__file__).parent / "config.toml"

_config = None


def load_config() -> dict:
    """Load configuration from config.toml, with env var overrides."""
    global _config
    if _config is not None:
        return _config

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Config file not found: {CONFIG_PATH}\n"
            "Copy config.example.toml to config.toml and configure it."
        )

    with open(CONFIG_PATH, "rb") as f:
        config = tomllib.load(f)

    # Environment variables override config file
    if os.environ.get("OUTPUT_PATH"):
        config["output_path"] = os.environ["OUTPUT_PATH"]
    if os.environ.get("SHEET_LINK"):
        config["sheet_link"] = os.environ["SHEET_LINK"]
    if os.environ.get("REFRESH_INTERVAL"):
        config["refresh_interval"] = int(os.environ["REFRESH_INTERVAL"])
    if os.environ.get("SHEET_GID") is not None:
        config["sheet_gid"] = str(os.environ["SHEET_GID"]).strip()

    # Resolve relative paths
    config["output_path"] = str(Path(config["output_path"]).resolve())

    _config = config
    return config


def get(key: str, default=None):
    """Get a config value."""
    return load_config().get(key, default)


# Convenience accessors
def output_path() -> str:
    return get("output_path")


def sheet_link() -> str:
    return get("sheet_link")


def refresh_interval() -> int:
    return get("refresh_interval", 3600)


def sheet_gid() -> str:
    """Sheet tab ID (gid) for htmlview/sheet scraping. Default "0"."""
    return get("sheet_gid", "0")
