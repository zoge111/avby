"""Обязательная подписка на канал: проверка + «ворота» с кнопками + middleware."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject

from .config import Config
from .keyboards import btn, kb, url_btn

log = logging.getLogger(__name__)

_MEMBER = {"creator", "administrator", "member"}
GATE_TEXT = "🔒 Чтобы пользоваться ботом, подпишитесь на наш канал, затем нажмите «✅ Я подписался»."


class SubscriptionGate:
    def __init__(self, cfg: Config, ttl: int = 300) -> None:
        self.cfg = cfg
        self.ttl = ttl
        self._ok: dict[int, float] = {}  # user_id -> до какого момента (monotonic) подписка считается подтверждённой

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.channel)

    async def check(self, bot: Bot, user_id: int, fresh: bool = False) -> bool:
        """True, если пользователь подписан (или проверка отключена). fresh=True — минуя кэш."""
        if not self.enabled or user_id in self.cfg.admin_ids:
            return True
        now = time.monotonic()
        if not fresh and self._ok.get(user_id, 0.0) > now:
            return True
        try:
            member = await bot.get_chat_member(self.cfg.channel, user_id)
        except TelegramAPIError as e:
            # Обычно бот не админ канала или неверный CHANNEL_ID. Не блокируем всех, но громко пишем в лог.
            log.error("Не удалось проверить подписку (бот должен быть администратором канала %s): %s",
                      self.cfg.channel, e)
            return True
        status = str(getattr(member.status, "value", member.status))
        ok = status in _MEMBER or (status == "restricted" and bool(getattr(member, "is_member", False)))
        if ok:
            self._ok[user_id] = now + self.ttl
        else:
            self._ok.pop(user_id, None)
        return ok

    def gate_markup(self):
        rows = []
        if self.cfg.channel_url:
            rows.append([url_btn("📢 Подписаться", self.cfg.channel_url)])
        rows.append([btn("✅ Я подписался", "sub:check")])
        return kb(*rows)

    async def send_gate(self, event: Message | CallbackQuery) -> None:
        msg = event.message if isinstance(event, CallbackQuery) else event
        if isinstance(event, CallbackQuery):
            await event.answer()
        if msg is not None:
            await msg.answer(GATE_TEXT, reply_markup=self.gate_markup())


class SubscriptionMiddleware(BaseMiddleware):
    """Пускает дальше только подписанных. /start и «Я подписался» проверяют подписку сами."""

    def __init__(self, gate: SubscriptionGate) -> None:
        self.gate = gate

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject, data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or not self.gate.enabled:
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "sub:check":
            return await handler(event, data)
        if await self.gate.check(data["bot"], user.id):
            return await handler(event, data)
        await self.gate.send_gate(event)  # type: ignore[arg-type]
        return None
