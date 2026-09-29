"""Telegram-varsler. Stille no-op hvis TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID mangler."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("memebot.notify")


class Notifier:
    def __init__(self, enabled: bool = True):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat = os.environ.get("TELEGRAM_CHAT_ID")
        self.enabled = enabled and bool(self.token and self.chat)

    def send(self, text: str) -> None:
        log.info(text.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": True},
                timeout=10,
            )
        except requests.RequestException as e:
            log.warning("Telegram-varsel feilet: %s", e)
