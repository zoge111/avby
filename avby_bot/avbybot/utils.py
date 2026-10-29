"""Мелкие хелперы для aiogram."""
from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message


async def safe_edit(msg: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Правит сообщение; если править нельзя (устарело/удалено) — отправляет новое."""
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            await msg.answer(text, reply_markup=markup)
