"""Discord Webhook への投稿。レート制限(429)に対応する。"""
from __future__ import annotations

import logging
import re
import time

import requests

# サーバー独自の絵文字（<:name:id> / アニメーション絵文字 <a:name:id>）
CUSTOM_EMOJI_PATTERN = re.compile(r"^<a?:\w+:\d+>$")

logger = logging.getLogger("crypto_news_bot.discord")

REQUEST_TIMEOUT = 15

COLOR_GOOD = 0x2ECC71
COLOR_BAD = 0xE74C3C
COLOR_NEUTRAL = 0x95A5A6
COLOR_ALERT_GOOD = 0x1E8449
COLOR_ALERT_BAD = 0xA93226


def is_custom_emoji(value: str | None) -> bool:
    """サーバー独自の絵文字かどうか。"""
    return bool(value) and bool(CUSTOM_EMOJI_PATTERN.match(value.strip()))


def post_webhook(
    webhook_url: str,
    *,
    username: str,
    content: str | None = None,
    embeds: list[dict] | None = None,
    dry_run: bool = False,
    min_interval_seconds: float = 1.2,
) -> bool:
    payload = {"username": username}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    if dry_run:
        print("=" * 60)
        print(f"[DRY-RUN] webhook={webhook_url[:40]}... username={username}")
        if content:
            print(f"content: {content}")
        for embed in embeds or []:
            print(f"embed title: {embed.get('title')}")
            print(f"embed description:\n{embed.get('description')}")
            for f in embed.get("fields", []):
                print(f"  field: {f.get('name')} = {f.get('value')}")
        print("=" * 60)
        return True

    for attempt in range(5):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.warning("Discord投稿に失敗しました error=%s", e)
            return False

        if resp.status_code == 429:
            try:
                retry_after = resp.json().get("retry_after", 1.0)
            except ValueError:
                retry_after = 1.0
            logger.warning("Discordのレート制限に達しました retry_after=%s秒", retry_after)
            time.sleep(float(retry_after) + 0.1)
            continue

        if 200 <= resp.status_code < 300:
            time.sleep(min_interval_seconds)
            return True

        logger.warning("Discord投稿が失敗しました status=%s body=%s", resp.status_code, resp.text[:500])
        return False

    logger.error("Discord投稿がレート制限のリトライ上限に達しました")
    return False
