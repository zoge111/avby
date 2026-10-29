"""Тесты ядра (без сети и без aiogram). Запуск: `pytest` или `python tests/test_core.py`."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from avbybot.db import Database  # noqa: E402
from avbybot.formatting import alert_text, filter_title, monitor_card  # noqa: E402
from avbybot.matcher import matches  # noqa: E402
from avbybot.models import Advert, region_of  # noqa: E402
from avbybot.monitor import run_cycle  # noqa: E402
from avbybot.parsing import (  # noqa: E402
    AvByError, extract_next_data, find_advert, find_adverts, looks_sorted_desc, normalize,
)

NOW = datetime.now(timezone.utc)


def raw_advert(ad_id=108117424, brand="Audi", model="A6", year=2019, usd=18500, byn=59000, published=None, **kw):
    d = {
        "id": ad_id,
        "publicUrl": f"https://cars.av.by/{brand.lower()}/{model.lower()}/{ad_id}",
        "publishedAt": (published or NOW).strftime("%Y-%m-%dT%H:%M:%S+0000"),
        "locationName": "Минск",
        "sellerName": "Автохаус «Север»",
        "sellerId": 777,
        "description": "Полноприводная, в идеальном состоянии, обслуживалась у дилера. " * 5,
        "price": {"byn": {"amount": byn, "currency": "BYN"}, "usd": {"amount": usd, "currency": "USD"}},
        "photos": [{"big": {"url": "https://avcdn.av.by/advertbig/x.jpg"}}],
        "properties": [
            {"name": "brand", "value": brand}, {"name": "model", "value": model},
            {"name": "generation", "value": "C8"}, {"name": "year", "value": year},
            {"name": "mileage_km", "value": 145000}, {"name": "engine_capacity", "value": "2.0"},
            {"name": "engine_type", "value": "дизель"}, {"name": "transmission_type", "value": "автоматическая"},
            {"name": "body_type", "value": "седан"},
        ],
    }
    d.update(kw)
    return d


def adv(**kw) -> Advert:
    return normalize(raw_advert(**kw))


# --- разбор ----------------------------------------------------------------------------

def test_normalize_fields():
    a = adv()
    assert (a.id, a.brand, a.model, a.year) == (108117424, "Audi", "A6", 2019)
    assert (a.price_usd, a.price_byn, a.mileage_km, a.engine_capacity) == (18500, 59000, 145000, 2.0)
    assert (a.fuel, a.gearbox, a.body) == ("diesel", "auto", "sedan")
    assert a.seller_id == "777" and a.photo.endswith("x.jpg") and a.published_at is not None
    assert normalize({"no": "id"}) is None and normalize("x") is None


def test_alert_format_matches_spec():
    text = alert_text(adv())
    assert "🚗 <b>Найдено новое объявление!</b>" in text
    assert '<b><a href="https://cars.av.by/audi/a6/108117424">Audi A6 C8, 2019 г.</a></b>' in text
    assert "💰 <b>18 500 USD</b> (~59 000 BYN)" in text
    assert "📍 Минск | ⚙️ 2.0 л, Дизель, Автомат" in text
    assert "🛣 Пробег: 145 000 км" in text
    assert "📝 <i>Описание:</i> Полноприводная" in text and "…" in text
    assert text.rstrip().endswith('🔗 <a href="https://cars.av.by/audi/a6/108117424">Открыть на av.by</a>')
    assert len(text) < 1024  # влезает в подпись к фото


def test_alert_escapes_html():
    a = adv(description="<script>alert(1)</script> & co")
    assert "<script>" not in alert_text(a) and "&lt;script&gt;" in alert_text(a)


def test_find_adverts_in_next_data_and_api_shapes():
    ads = [raw_advert(1), raw_advert(2)]
    page = '<html><script id="__NEXT_DATA__" type="application/json">%s</script></html>' % json.dumps(
        {"props": {"initialState": {"filter": {"main": {"adverts": ads}}}}}
    )
    assert [a["id"] for a in find_adverts(extract_next_data(page))] == [1, 2]
    assert [a["id"] for a in find_adverts({"adverts": ads, "count": 2})] == [1, 2]
    assert find_adverts({"x": [{"id": 5, "publicUrl": "u"}]}) is not None   # список без ключа adverts
    assert find_adverts({"adverts": []}) == []                               # пустая лента — это не ошибка
    assert find_adverts({"hello": "world"}) is None                           # заглушка вместо ленты
    assert find_advert({"a": {"b": [raw_advert(42)]}}, 42)["id"] == 42
    try:
        extract_next_data("<html>captcha</html>")
    except AvByError:
        pass
    else:
        raise AssertionError("ожидалась AvByError")


def test_sorted_detection():
    base = NOW
    desc = [adv(ad_id=i, published=base - timedelta(minutes=i)) for i in range(1, 9)]
    assert looks_sorted_desc(desc) is True
    assert looks_sorted_desc(list(reversed(desc))) is False
    assert looks_sorted_desc(desc[:3]) is None


# --- матчинг ---------------------------------------------------------------------------

def test_matcher_basic_filters():
    a = adv()
    assert matches({}, a)
    assert matches({"brand": "audi", "model": "a6"}, a)
    assert not matches({"brand": "BMW"}, a)
    assert not matches({"brand": "Audi", "model": "A4"}, a)
    assert matches({"year_from": 2015, "year_to": 2024}, a) and not matches({"year_from": 2020}, a)
    assert matches({"currency": "USD", "price_to": 20000}, a) and not matches({"currency": "USD", "price_to": 18000}, a)
    assert matches({"currency": "BYN", "price_from": 50000, "price_to": 60000}, a)
    assert matches({"fuel": ["diesel", "hybrid"], "gearbox": ["auto"], "body": ["sedan", "wagon"]}, a)
    assert not matches({"fuel": ["petrol"]}, a) and not matches({"gearbox": ["manual"]}, a)


def test_matcher_stopwords_and_region():
    a = adv(description="Состояние отличное, после ДТП не был, БИТЫЙ не бывал")
    assert not matches({"stopwords": ["битый"]}, a)       # регистр не важен
    assert not matches({"stopwords": ["после дтп"]}, a)   # фразы из нескольких слов
    assert matches({"stopwords": ["под разбор"]}, a)
    assert matches({"region": "minsk"}, a) and not matches({"region": "brest"}, a)
    unknown = adv(locationName="Какая-то деревня")
    assert matches({"region": "brest"}, unknown)          # регион не определён — не отсекаем


def test_region_of():
    assert region_of("Минск") == "minsk" and region_of("г. Минск") == "minsk"
    assert region_of("Минская обл.") == "minsk_obl" and region_of("Борисов") == "minsk_obl"
    assert region_of("Брестская обл., Пинск") == "brest" and region_of("Барановичи") == "brest"
    assert region_of("Гомель") == "gomel" and region_of("Гродно") == "grodno"
    assert region_of("Могилёв") == "mogilev" and region_of("Витебск") == "vitebsk"
    assert region_of("") is None and region_of("Неизвестно") is None


def test_matcher_seller():
    a = adv()
    assert matches({"seller_id": "777"}, a) and not matches({"seller_id": "1"}, a)
    assert matches({"seller_name": "автохаус север"}, a) and not matches({"seller_name": "Другой"}, a)
    no_id = adv(sellerId=None)
    assert matches({"seller_id": "777", "seller_name": "Автохаус «Север»"}, no_id)  # нет id -> по имени


def test_titles_and_cards():
    f = {"brand": "Audi", "model": "A6", "year_from": 2015, "year_to": 2024, "currency": "USD",
         "price_to": 20000, "region": "minsk", "fuel": ["diesel"], "stopwords": ["битый"]}
    assert filter_title(f) == "Audi A6 · 2015–2024 · до 20 000 USD · Минск"
    card = monitor_card({"id": 7, "is_active": 1, "filters": f, "created_at": int(time.time())})
    for part in ("Мониторинг #7", "Audi A6", "2015–2024", "до 20 000 USD", "Дизель", "битый", "🟢"):
        assert part in card, part


# --- БД --------------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def test_database_flow():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(os.path.join(tmp, "t.sqlite3"))
            await db.connect()
            await db.upsert_user(1, "u", "User")
            assert not Database.is_premium(await db.get_user(1))
            await db.set_premium(1, int(time.time()) + 100)
            assert Database.is_premium(await db.get_user(1))

            m1 = await db.add_monitor(1, "filter", "A", {"brand": "Audi"})
            m2 = await db.add_monitor(1, "filter", "B", {"brand": "BMW"})
            assert await db.count_active(1) == 2
            await db.set_active(1, m2, False)
            assert await db.count_active(1) == 1
            assert (await db.get_monitor(1, m1))["filters"] == {"brand": "Audi"}
            assert await db.get_monitor(2, m1) is None            # чужой мониторинг недоступен
            assert await db.stop_all(1) == 1 and await db.count_active(1) == 0

            await db.set_active(1, m1, True)
            assert [m["id"] for m in await db.active_monitors()] == [m1]
            await db.mark_blocked(1)
            assert await db.active_monitors() == [] and await db.all_user_ids() == []
            await db.upsert_user(1, "u", "User")                   # вернулся — разблокирован
            assert len(await db.active_monitors()) == 1

            assert not await db.has_seen()
            await db.mark_seen([10, 11, 12])
            await db.mark_seen([12, 13])                           # повторная вставка не ломает
            assert await db.has_seen() and await db.known_ids([9, 10, 13]) == {10, 13}
            assert await db.claim(m1, 10) is True and await db.claim(m1, 10) is False

            await db.incr("x", 2); await db.incr("x", 3); await db.set_counter("y", 9)
            st = await db.stats()
            assert st["users"] == 1 and st["premium"] == 1 and st["monitors"] == 2 and st["active"] == 1
            assert st["counters"] == {"x": 5, "y": 9}

            assert await db.delete_monitor(1, m1) and not await db.delete_monitor(1, m1)
            await db.cleanup(seen_days=0, sent_days=0)
            await db.close()
    run(go())


# --- цикл мониторинга ------------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.pages: list[list[Advert]] = [[]]
        self.sorted_ok = True

    async def fetch_page(self, page=1, extra=None):
        return list(self.pages[page - 1]) if page <= len(self.pages) else []

    def take_counters(self):
        return 1, 0


def test_monitor_cycle():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(os.path.join(tmp, "t.sqlite3"))
            await db.connect()
            client = FakeClient()
            catalog = SimpleNamespace(observe=lambda ads: None)
            cfg = SimpleNamespace(max_pages=2)
            app = SimpleNamespace(db=db, client=client, catalog=catalog, cfg=cfg)
            sent: list[tuple[int, int]] = []

            async def send(chat_id, ad):
                sent.append((chat_id, ad.id))
                return True

            old = [adv(ad_id=100 + i, published=NOW - timedelta(hours=1, minutes=i)) for i in range(3)]
            client.pages = [old]
            await db.upsert_user(1, "a", "A"); await db.upsert_user(2, "b", "B")
            await db.add_monitor(1, "filter", "audi", {"brand": "Audi"})
            await db.add_monitor(2, "filter", "bmw", {"brand": "BMW"})

            # 1) первый запуск: только запоминаем ленту
            assert await run_cycle(app, send) == 0 and sent == []
            assert await db.known_ids([100, 101, 102]) == {100, 101, 102}

            # 2) появились новые: Audi (подходит 1-му), BMW (подходит 2-му), Opel (никому)
            new_audi = adv(ad_id=200, published=NOW + timedelta(seconds=5))
            new_bmw = adv(ad_id=201, brand="BMW", model="X5", published=NOW + timedelta(seconds=6))
            new_opel = adv(ad_id=202, brand="Opel", model="Astra", published=NOW + timedelta(seconds=7))
            client.pages = [[new_opel, new_bmw, new_audi] + old]
            assert await run_cycle(app, send) == 2
            assert sorted(sent) == [(1, 200), (2, 201)]

            # 3) повторный проход тех же объявлений — без дублей
            assert await run_cycle(app, send) == 0 and len(sent) == 2

            # 4) пауза: мониторинг не уведомляет, а объявление всё равно запоминается
            await db.stop_all(1)
            client.pages = [[adv(ad_id=203, published=NOW + timedelta(seconds=8))] + [new_opel, new_bmw, new_audi] + old]
            assert await run_cycle(app, send) == 0
            await db.set_active(1, 1, True)
            assert await run_cycle(app, send) == 0                  # 203 уже «видели» — задним числом не шлём

            # 5) объявление, опубликованное ДО создания мониторинга, не уведомляет
            await db.add_monitor(1, "filter", "audi-new", {"brand": "Audi"})
            stale = adv(ad_id=204, published=NOW - timedelta(days=2))
            client.pages = [[stale, adv(ad_id=205, published=NOW + timedelta(seconds=9))]]
            before = len(sent)
            await run_cycle(app, send)
            assert [s for s in sent[before:]] == [(1, 205)]         # 205 — один раз, несмотря на 2 монитора Audi

            # 6) заблокировавший бота не получает уведомлений
            await db.mark_blocked(2)
            client.pages = [[adv(ad_id=206, brand="BMW", model="X3", published=NOW + timedelta(seconds=10))]]
            before = len(sent)
            await run_cycle(app, send)
            assert len(sent) == before
            await db.close()
    run(go())


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✓ {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                print(f"✗ {name}: {e!r}")
                traceback.print_exc()
    print("\nВСЕ ТЕСТЫ ПРОШЛИ" if not failed else f"\nПРОВАЛЕНО: {failed}")
    sys.exit(1 if failed else 0)
