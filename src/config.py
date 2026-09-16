"""config.yaml と .env の読み込み。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else BASE_DIR / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env(path: str | Path | None = None) -> None:
    env_path = Path(path) if path else BASE_DIR / ".env"
    load_dotenv(dotenv_path=env_path)


def get_webhook_url(coin_cfg: dict[str, Any]) -> str | None:
    env_name = coin_cfg["webhook_env"]
    value = os.environ.get(env_name)
    if not value:
        return None
    return value.strip() or None
