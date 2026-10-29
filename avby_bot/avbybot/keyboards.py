"""Клавиатуры."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def chunk(items: list, n: int) -> list[list]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def main_menu() -> InlineKeyboardMarkup:
    return kb(
        [btn("➕ Добавить мониторинг", "menu:add")],
        [btn("🚗 Поиск по параметрам", "menu:search")],
        [btn("👤 Отслеживать продавца/автохаус", "menu:seller")],
        [btn("📋 Мои мониторинги", "menu:list")],
        [btn("⏹ Стоп все", "menu:stop")],
    )


def to_menu() -> InlineKeyboardMarkup:
    return kb([btn("🏠 В меню", "menu:home")])


def monitor_kb(row: dict) -> InlineKeyboardMarkup:
    """Кнопки карточки мониторинга: пауза/запуск, удаление."""
    mid = row["id"]
    toggle = btn("⏸ Пауза", f"mon:toggle:{mid}") if row["is_active"] else btn("▶️ Запустить", f"mon:toggle:{mid}")
    return kb(
        [toggle, btn("🗑 Удалить", f"mon:del:{mid}")],
        [btn("📋 К списку", "menu:list"), btn("🏠 В меню", "menu:home")],
    )
