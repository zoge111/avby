"""Клиент av.by: лента новых объявлений, каталог марок/моделей, разовый поиск.

Источники ленты (пробуются по очереди, рабочий запоминается):
  api  — JSON API api.av.by (то, что использует сам сайт);
  html — страница cars.av.by/filter с данными в <script id="__NEXT_DATA__">.
Обе точки входа — неофициальные: форматы могут измениться, см. parsing.py и check_api.py.
Используйте умеренный интервал опроса и соблюдайте правила сайта.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

import aiohttp

from .matcher import matches
from .models import Advert, fold, norm
from .parsing import (
    MIN_DT, AvByError, extract_next_data, find_advert, find_adverts, looks_sorted_desc, normalize,
)

log = logging.getLogger(__name__)

API = "https://api.av.by"
WEB = "https://cars.av.by"
LIST_API = f"{API}/offer-types/cars/filters/main/init"
LIST_WEB = f"{WEB}/filter"
BRANDS_API = f"{API}/offer-types/cars/catalog/brand-items"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
JSON_HEADERS = {
    "User-Agent": _UA, "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9", "Origin": WEB, "Referer": WEB + "/",
}
HTML_HEADERS = {
    "User-Agent": _UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class AvByClient:
    def __init__(self, sort: str = "4", timeout: float = 20.0) -> None:
        self.sort = sort
        self.source: str | None = None       # какой источник отработал последним: api | html
        self.sorted_ok: bool | None = None   # похожа ли лента на «новые сверху» (проверка по первой странице)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._sem = asyncio.Semaphore(2)
        self._requests = 0
        self._errors = 0

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def take_counters(self) -> tuple[int, int]:
        """(запросов, ошибок) с прошлого вызова — для статистики."""
        r, e, self._requests, self._errors = self._requests, self._errors, 0, 0
        return r, e

    # --- HTTP ------------------------------------------------------------------------

    async def _get(self, url: str, params: dict[str, Any] | None = None, *, html: bool = False) -> str:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        headers = HTML_HEADERS if html else JSON_HEADERS
        delay, last = 2.0, "?"
        for attempt in range(3):
            self._requests += 1
            retry = True
            try:
                async with self._sem:
                    async with self._session.get(url, params=params, headers=headers) as r:
                        if r.status == 200:
                            return await r.text()
                        last = f"HTTP {r.status}"
                        retry = r.status in (429, 500, 502, 503, 504)
                        ra = r.headers.get("Retry-After", "")
                        if ra.isdigit():
                            delay = min(float(ra), 60.0)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last = type(e).__name__
            self._errors += 1
            if not retry or attempt == 2:
                break
            await asyncio.sleep(delay + random.random())
            delay *= 2
        raise AvByError(f"{last} ({url})")

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        text = await self._get(url, params)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise AvByError(f"не JSON ({url}): {e}") from e

    # --- лента -----------------------------------------------------------------------

    async def _raw_list(self, src: str, params: dict[str, Any]) -> list[dict]:
        if src == "api":
            data = await self.get_json(LIST_API, params)
        else:
            data = extract_next_data(await self._get(LIST_WEB, params, html=True))
        found = find_adverts(data)
        if found is None:
            raise AvByError("в ответе нет списка объявлений")
        return found

    async def fetch_page(self, page: int = 1, extra: dict[str, Any] | None = None) -> list[Advert]:
        """Страница ленты, новые сверху. extra — серверные фильтры (для разового поиска)."""
        params: dict[str, Any] = {"sort": self.sort, "page": page, **(extra or {})}
        order = ["api", "html"]
        if self.source in order:  # сначала то, что работало в прошлый раз
            order.remove(self.source)
            order.insert(0, self.source)
        problems: list[str] = []
        for src in order:
            try:
                raws = await self._raw_list(src, params)
            except AvByError as e:
                problems.append(f"{src}: {e}")
                continue
            ads = [a for a in map(normalize, raws) if a]
            if raws and not ads:
                problems.append(f"{src}: не удалось разобрать объявления")
                continue
            if page == 1 and not extra:
                self.sorted_ok = looks_sorted_desc(ads)
            ads.sort(key=lambda a: a.published_at or MIN_DT, reverse=True)
            self.source = src
            return ads
        raise AvByError("; ".join(problems))

    async def fetch_seller(self, url: str) -> tuple[str, str] | None:
        """(seller_id, seller_name) по ссылке на объявление av.by. None — продавца определить не удалось."""
        p = urlparse(url if "://" in url else "https://" + url)
        host = (p.hostname or "").lower()
        if host != "av.by" and not host.endswith(".av.by"):  # не ходим на сторонние адреса
            raise AvByError("ссылка должна вести на av.by")
        m = re.search(r"/(\d{6,})", p.path)
        if not m:
            return None
        ad_id = int(m.group(1))
        page = await self._get(f"https://{host}{p.path}", html=True)
        raw = find_advert(extract_next_data(page), ad_id)
        ad = normalize(raw) if raw else None
        if ad and (ad.seller_id or ad.seller_name):
            return ad.seller_id, ad.seller_name
        return None


# --- каталог марок и моделей (для кнопок мастера) ---------------------------------------

POPULAR = [
    "Audi", "BMW", "Volkswagen", "Mercedes-Benz", "Skoda", "Toyota", "Renault", "Opel", "Ford", "Peugeot",
    "Citroen", "Kia", "Hyundai", "Mazda", "Nissan", "Honda", "Volvo", "Geely", "Chery", "Lexus",
    "Mitsubishi", "Land Rover", "Porsche", "Seat",
]
FALLBACK_BRANDS = POPULAR + [
    "Alfa Romeo", "Chevrolet", "Chrysler", "Dacia", "Daewoo", "Dodge", "Fiat", "Haval", "Infiniti", "Jaguar",
    "Jeep", "Lada (ВАЗ)", "Mini", "Subaru", "Suzuki", "Tesla", "UAZ", "Zeekr", "Changan", "Exeed",
]
_POP = {norm(n): i for i, n in enumerate(POPULAR)}


@dataclass(slots=True)
class Item:
    id: str
    name: str


def _items(data: Any) -> list[Item]:
    """Терпимо разбирает список марок/моделей: [{"id":..,"name":..}] или {"items": [...]}."""
    if isinstance(data, dict):
        for k in ("items", "data", "brands", "models", "results"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
        else:
            lists = [v for v in data.values() if isinstance(v, list)]
            data = lists[0] if lists else []
    out: list[Item] = []
    for d in data if isinstance(data, list) else []:
        if isinstance(d, dict):
            name = d.get("name") or d.get("label") or d.get("title")
            if name:
                out.append(Item(str(d.get("id", "")), str(name).strip()))
        elif isinstance(d, str) and d.strip():
            out.append(Item("", d.strip()))
    return out


def _natural(name: str) -> list[Any]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", fold(name))]


class Catalog:
    """Марки/модели: из каталога av.by + из реально увиденных объявлений + запасной список."""

    TTL = 24 * 3600

    def __init__(self, client: AvByClient) -> None:
        self.client = client
        self._brands: list[Item] = []
        self._brands_at = -1e9
        self._models: dict[str, tuple[float, list[Item]]] = {}
        self._seen: dict[str, tuple[str, set[str]]] = {}  # norm(марка) -> (название, {модели})

    def observe(self, ads: Iterable[Advert]) -> None:
        """Запоминает марки/модели из свежих объявлений: список подсказок пополняется сам."""
        for a in ads:
            if not a.brand:
                continue
            _, models = self._seen.setdefault(norm(a.brand), (a.brand, set()))
            if a.model:
                models.add(a.model)

    async def brands(self) -> list[Item]:
        age = time.monotonic() - self._brands_at
        if (not self._brands and age > 600) or age > self.TTL:
            self._brands_at = time.monotonic()
            try:
                self._brands = _items(await self.client.get_json(BRANDS_API))
            except AvByError as e:
                log.warning("Каталог марок недоступен: %s", e)
        items = list(self._brands)
        have = {norm(i.name) for i in items}
        items += [Item("", name) for key, (name, _) in self._seen.items() if key not in have]
        if not items:
            items = [Item("", n) for n in FALLBACK_BRANDS]
        return sorted(items, key=lambda i: (_POP.get(norm(i.name), 999), fold(i.name)))

    async def models(self, brand: str) -> list[Item]:
        key = norm(brand)
        b = next((i for i in await self.brands() if norm(i.name) == key), None)
        items: list[Item] = []
        if b and b.id:
            at, cached = self._models.get(key, (-1e9, []))
            age = time.monotonic() - at
            if (not cached and age > 600) or age > self.TTL:
                try:
                    cached = _items(await self.client.get_json(f"{BRANDS_API}/{b.id}/models"))
                except AvByError as e:
                    log.warning("Каталог моделей %s недоступен: %s", brand, e)
                self._models[key] = (time.monotonic(), cached)
            items = list(cached)
        have = {norm(i.name) for i in items}
        extra = self._seen.get(key, ("", set()))[1]
        items += [Item("", m) for m in extra if norm(m) not in have]
        return sorted(items, key=lambda i: _natural(i.name))

    async def find_brand(self, name: str) -> Item | None:
        return next((i for i in await self.brands() if norm(i.name) == norm(name)), None)

    async def find_model(self, brand: str, model: str) -> Item | None:
        return next((i for i in await self.models(brand) if norm(i.name) == norm(model)), None)


# --- разовый поиск ----------------------------------------------------------------------

async def search_adverts(
    client: AvByClient, catalog: Catalog, f: dict[str, Any], pages: int = 3, limit: int = 10,
) -> list[Advert]:
    """«Поиск по параметрам»: серверные фильтры (если удалось построить) + обязательная локальная проверка.

    Локальный matches() гарантирует корректный результат, даже если параметры сервера не сработали
    (тогда просто ниже полнота: искать будем среди самых свежих объявлений).
    """
    params: dict[str, Any] = {}
    brand = f.get("brand")
    if brand:
        b = await catalog.find_brand(brand)
        if b and b.id:
            params["brands[0][brand]"] = b.id
            if f.get("model"):
                m = await catalog.find_model(brand, f["model"])
                if m and m.id:
                    params["brands[0][model]"] = m.id
    if f.get("year_from"):
        params["year[min]"] = f["year_from"]
    if f.get("year_to"):
        params["year[max]"] = f["year_to"]
    usd = f.get("currency", "USD") == "USD"
    if f.get("price_from") or f.get("price_to"):
        params["price_currency"] = 2 if usd else 1
        if f.get("price_from"):
            params["price_usd[min]" if usd else "price_byn[min]"] = f["price_from"]
        if f.get("price_to"):
            params["price_usd[max]" if usd else "price_byn[max]"] = f["price_to"]

    out: list[Advert] = []
    for page in range(1, pages + 1):
        ads = await client.fetch_page(page, params)
        if not ads:
            break
        catalog.observe(ads)
        out.extend(a for a in ads if matches(f, a))
        if len(out) >= limit:
            break
        await asyncio.sleep(0.5)
    return out[:limit]
