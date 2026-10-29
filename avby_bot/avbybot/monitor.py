"""Фоновый цикл мониторинга.

Как это работает:
  1. Раз в POLL_INTERVAL секунд берём «ленту» самых свежих объявлений av.by (1–MAX_PAGES страниц).
     Один запрос на всех пользователей, а не по запросу на каждый мониторинг — нагрузка на сайт не растёт
     с числом подписчиков.
  2. Объявления, которых нет в seen_adverts, считаются новыми и сразу в неё записываются.
     Это и есть «первый проход»: на самом первом запуске бот только запоминает ленту и никого не уведомляет.
  3. Каждое новое объявление проверяется по всем активным мониторингам (matcher.matches).
  4. Уведомление уходит один раз: пара (мониторинг, объявление) «занимается» в sent_adverts до отправки,
     а в рамках одного цикла пользователю не шлются дубли из пересекающихся мониторингов.
  5. Мониторинг реагирует только на объявления, опубликованные после его создания (с запасом GRACE секунд
     на расхождение часов), поэтому старые «поднятые» объявления спама не создают.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from .matcher import matches
from .models import Advert
from .parsing import MIN_DT, AvByError

if TYPE_CHECKING:  # только для подсказок типов, чтобы модуль импортировался без aiogram
    from aiogram import Bot

    from .context import App

log = logging.getLogger(__name__)

GRACE = 60  # сек: допуск на расхождение часов между av.by и сервером бота
Sender = Callable[[int, Advert], Awaitable[bool]]
_sort_warned = False


async def run_cycle(app: "App", send: Sender) -> int:
    """Один проход: забрать свежую ленту, найти новые объявления, разослать уведомления.

    Возвращает число отправленных уведомлений.
    """
    db, client, cfg = app.db, app.client, app.cfg
    bootstrap = not await db.has_seen()
    fresh: list[Advert] = []
    ids: set[int] = set()
    try:
        for page in range(1, cfg.max_pages + 1):
            try:
                ads = await client.fetch_page(page)
            except AvByError:
                if page == 1:
                    raise
                log.warning("Страница %s ленты не получена, работаем с тем, что есть", page, exc_info=True)
                break
            if not ads:
                break
            app.catalog.observe(ads)
            new = [a for a in ads if a.id not in ids]
            known = await db.known_ids(a.id for a in new)
            new = [a for a in new if a.id not in known]
            fresh.extend(new)
            ids.update(a.id for a in new)
            if not new and not bootstrap:
                break  # дошли до уже виденных объявлений — глубже смотреть незачем
            if page < cfg.max_pages:
                await asyncio.sleep(random.uniform(0.4, 1.0))
    finally:
        requests, errors = client.take_counters()
        await db.incr("parser_requests", requests)
        await db.incr("parser_errors", errors)

    global _sort_warned
    if client.sorted_ok is False and not _sort_warned:
        _sort_warned = True
        log.warning("Лента не похожа на «новые сверху»: проверьте AVBY_SORT (python -m avbybot.check_api)")
    await db.set_counter("last_cycle_ts", int(time.time()))
    if not fresh:
        return 0

    await db.mark_seen(a.id for a in fresh)
    await db.incr("adverts_seen", len(fresh))
    if bootstrap:
        log.info("Первый запуск: запомнил %s объявлений, уведомления начнутся со следующих", len(fresh))
        return 0

    monitors = await db.active_monitors()
    if not monitors:
        return 0

    fresh.sort(key=lambda a: a.published_at or MIN_DT)  # старые -> новые: уведомления идут по порядку
    delivered: set[tuple[int, int]] = set()
    sent = 0
    for ad in fresh:
        for m in monitors:
            if ad.published_at and ad.published_at.timestamp() < m["created_at"] - GRACE:
                continue  # объявление появилось раньше мониторинга
            if not matches(m["filters"], ad):
                continue
            key = (m["user_id"], ad.id)
            if key in delivered:
                continue  # у пользователя несколько подходящих мониторингов — шлём один раз
            if not await db.claim(m["id"], ad.id):
                continue
            delivered.add(key)
            if await send(m["user_id"], ad):
                sent += 1
            await asyncio.sleep(0.05)  # ~20 сообщений/сек — в пределах лимитов Telegram
    await db.incr("alerts_sent", sent)
    return sent


async def monitor_loop(bot: "Bot", app: "App") -> None:
    from .notify import send_advert  # ленивый импорт: тесты цикла не требуют aiogram

    async def send(chat_id: int, ad: Advert) -> bool:
        return await send_advert(bot, app.db, chat_id, ad)

    log.info("Мониторинг запущен: интервал %s с, страниц за цикл: %s", app.cfg.poll_interval, app.cfg.max_pages)
    fails = 0
    last_cleanup = time.time()
    while True:
        started = time.monotonic()
        try:
            await run_cycle(app, send)
            fails = 0
        except asyncio.CancelledError:
            raise
        except AvByError as e:
            fails += 1
            log.warning("av.by недоступен (подряд: %s): %s", fails, e)
        except Exception:
            fails += 1
            log.exception("Ошибка цикла мониторинга")
        if time.time() - last_cleanup > 86400:
            last_cleanup = time.time()
            try:
                await app.db.cleanup()
            except Exception:
                log.exception("Очистка старых записей не удалась")
        pause = max(5.0, app.cfg.poll_interval - (time.monotonic() - started)) + random.uniform(0, 5)
        pause += min(300.0, 15.0 * 2 ** min(fails, 5)) if fails else 0.0  # при сбоях не долбим сайт
        await asyncio.sleep(pause)
