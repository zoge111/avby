"""Настройки из переменных окружения / файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv необязателен: можно задать переменные в окружении
        return
    load_dotenv()


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    channel: str          # @username или -100... ; пусто = подписка не проверяется
    channel_url: str      # ссылка для кнопки «Подписаться»
    db_path: str
    poll_interval: int    # пауза между проверками av.by, сек
    max_pages: int        # сколько страниц ленты просматривать за цикл
    free_limit: int       # активных мониторингов на бесплатном тарифе
    avby_sort: str        # значение параметра sort для «новые сверху»
    support: str          # контакт для покупки Premium


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.getenv(name, "").split("#")[0].strip() or default)
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def _clean(name: str, default: str = "") -> str:
    """Значение переменной без комментария после «#» (на случай .env с пояснениями в строке)."""
    return os.getenv(name, default).split("#")[0].strip()


def load_config() -> Config:
    load_env()
    token = _clean("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN не задан: скопируйте .env.example в .env и заполните его.")

    admins = frozenset(
        int(x) for x in _clean("ADMIN_IDS").replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()
    )
    channel = _clean("CHANNEL_ID")
    url = _clean("CHANNEL_URL")
    if channel.startswith("@") and not url:
        url = f"https://t.me/{channel[1:]}"

    return Config(
        bot_token=token,
        admin_ids=admins,
        channel=channel,
        channel_url=url,
        db_path=_clean("DB_PATH", "avby_bot.sqlite3"),
        poll_interval=_int("POLL_INTERVAL", 45, 15, 600),
        max_pages=_int("MAX_PAGES", 3, 1, 10),
        free_limit=_int("FREE_LIMIT", 2, 0, 1000),
        avby_sort=_clean("AVBY_SORT", "4"),
        support=_clean("SUPPORT_CONTACT"),
    )
