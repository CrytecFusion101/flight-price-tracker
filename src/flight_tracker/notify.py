"""Push notifications: ntfy.sh (primary) and Telegram (optional). Never raises."""

from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)


def _post(url: str, payload: dict, timeout: int = 15) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status >= 300:
            raise RuntimeError(f"HTTP {r.status}")


def send(title: str, message: str, priority: int = 3, click: str | None = None, tags: list[str] | None = None) -> bool:
    """Send to every configured channel. Returns True if at least one succeeded (or none configured)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    tg_token, tg_chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if os.environ.get("NOTIFY_DISABLED") == "1" or not (topic or (tg_token and tg_chat)):
        log.info("[notify:console] %s\n%s", title, message)
        return True

    ok = False
    if topic:
        payload = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags or []}
        if click:
            payload["click"] = click
        ok |= _try("ntfy", lambda: _post(server, payload))
    if tg_token and tg_chat:
        text = f"{title}\n\n{message}" + (f"\n\n{click}" if click else "")
        ok |= _try("telegram", lambda: _post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            {"chat_id": tg_chat, "text": text, "disable_web_page_preview": True},
        ))
    return ok


def _try(name: str, fn) -> bool:
    for attempt in (1, 2):
        try:
            fn()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("%s notify attempt %d failed: %s", name, attempt, e)
    return False
