"""Сборка и запуск бота: python -m avbybot"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .avby import AvByClient, Catalog
from .config import load_config
from .context import App
from .db import Database
from .handlers import admin, fallback, monitors, seller, start, wizard
from .monitor import monitor_loop
from .subscription import SubscriptionGate, SubscriptionMiddleware

log = logging.getLogger("avbybot")


async def main() -> None:
    cfg = load_config()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not cfg.admin_ids:
        log.warning("ADMIN_IDS пуст — админ-команды (/admin, /stats, /broadcast, /premium) недоступны")
    if cfg.channel and not cfg.channel_url:
        log.warning("CHANNEL_URL не задан — у кнопки «Подписаться» не будет ссылки")

    db = Database(cfg.db_path)
    await db.connect()
    client = AvByClient(sort=cfg.avby_sort)
    app = App(cfg=cfg, db=db, client=client, catalog=Catalog(client), gate=SubscriptionGate(cfg))

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage(), app=app)  # app попадает в хендлеры по имени параметра

    gate_mw = SubscriptionMiddleware(app.gate)
    dp.message.outer_middleware(gate_mw)
    dp.callback_query.outer_middleware(gate_mw)

    # Порядок важен: команды и меню раньше состояний мастера; fallback — последним.
    routers = [start.router, admin.router, wizard.router, monitors.router, seller.router, fallback.router]
    for r in routers:  # бот работает только в личных чатах
        r.message.filter(F.chat.type == "private")
        r.callback_query.filter(F.message.chat.type == "private")
    dp.include_routers(*routers)

    monitor_task = asyncio.create_task(monitor_loop(bot, app), name="monitor")
    try:
        await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
        me = await bot.get_me()
        log.info("Бот @%s запущен", me.username)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await monitor_task
        await client.close()
        await db.close()
        await bot.session.close()
