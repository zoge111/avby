"""Диагностика парсера: python -m avbybot.check_api

Запустите ДО первого старта бота (нужен доступ в интернет). Скрипт покажет:
  * какой источник данных av.by сработал (api / html);
  * сколько объявлений пришло и какие поля удалось разобрать;
  * лента отсортирована «новые сверху» или нужно поправить AVBY_SORT;
  * доступен ли каталог марок и моделей для кнопок мастера.
"""
from __future__ import annotations

import asyncio
import os

from .avby import AvByClient, Catalog
from .config import load_env
from .parsing import AvByError

FIELDS = ["brand", "model", "year", "price_usd", "price_byn", "location", "mileage_km", "engine_capacity",
          "fuel", "gearbox", "body", "description", "seller_name", "seller_id", "photo", "published_at"]


async def main() -> None:
    load_env()
    client = AvByClient(sort=os.getenv("AVBY_SORT", "4").split("#")[0].strip() or "4")
    try:
        try:
            ads = await client.fetch_page(1)
        except AvByError as e:
            print(f"❌ Не удалось получить ленту: {e}")
            print("   Проверьте доступ к api.av.by и cars.av.by с этого сервера (прокси, блокировки, защита от ботов).")
            return
        print(f"✅ Источник: {client.source} · объявлений на странице: {len(ads)}")
        if not ads:
            print("⚠️  Лента пуста — скорее всего, изменился формат ответа.")
            return

        print("\nЗаполненность полей (сколько объявлений из страницы содержат поле):")
        for name in FIELDS:
            filled = sum(1 for a in ads if getattr(a, name) not in (None, "", 0))
            mark = "✅" if filled >= len(ads) * 0.8 else ("⚠️ " if filled else "❌")
            print(f"  {mark} {name:16} {filled}/{len(ads)}")
        print("  (seller_id, mileage_km, engine_capacity, photo — могут быть пустыми у части объявлений)")

        verdict = {True: "✅ да, новые сверху", False: "❌ НЕТ — подберите AVBY_SORT", None: "не удалось определить"}
        print(f"\nСортировка по дате: {verdict[client.sorted_ok]}")

        a = ads[0]
        print("\nПервое объявление:")
        print(f"  {a.title} | {a.price_usd} USD / {a.price_byn} BYN | {a.location}")
        print(f"  {a.url}\n  опубликовано: {a.published_at}")

        catalog = Catalog(client)
        brands = await catalog.brands()
        models = await catalog.models(a.brand) if a.brand else []
        print(f"\nКаталог марок: {len(brands)} (из них с id: {sum(1 for b in brands if b.id)})")
        print(f"Каталог моделей {a.brand or '—'}: {len(models)}")
        if not any(b.id for b in brands):
            print("⚠️  Каталог av.by недоступен: кнопки марок берутся из запасного списка и из увиденных "
                  "объявлений, а «Поиск по параметрам» работает только среди свежих объявлений.")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
