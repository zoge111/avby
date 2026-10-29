"""Отправка объявления пользователю (с фото, если получится)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from .db import Database
from .formatting import ALERT_HEADER, alert_text
from .models import Advert

log = logging.getLogger(__name__)


async def send_advert(bot: Bot, db: Database | None, chat_id: int, ad: Advert, header: str = ALERT_HEADER) -> bool:
    """True — доставлено. Если пользователь заблокировал бота, он помечается в БД и выпадает из рассылок."""
    text = alert_text(ad, header)
    for _ in range(2):
        try:
            if ad.photo and len(text) <= 1024:
                try:
                    await bot.send_photo(chat_id, ad.photo, caption=text)
                    return True
                except TelegramBadRequest:
                    pass  # фото недоступно по ссылке — отправим обычным сообщением
            await bot.send_message(chat_id, text)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            if db is not None:
                await db.mark_blocked(chat_id)
            return False
        except TelegramAPIError as e:
            log.warning("Не удалось отправить уведомление %s: %s", chat_id, e)
            return False
    return False
