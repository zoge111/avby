"""Модели данных и нормализация текста (только stdlib, без сети и aiogram)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

# Коды -> подписи для интерфейса. Коды хранятся в фильтрах мониторингов.
FUELS = {"petrol": "Бензин", "diesel": "Дизель", "electric": "Электро", "hybrid": "Гибрид"}
GEARBOXES = {"auto": "Автомат", "manual": "Механика"}
BODIES = {
    "sedan": "Седан", "suv": "Внедорожник", "hatchback": "Хэтчбек", "wagon": "Универсал",
    "coupe": "Купе", "minivan": "Минивэн", "cabrio": "Кабриолет", "pickup": "Пикап",
    "liftback": "Лифтбек",
}
# В ТЗ шесть пунктов; «Минская обл.» добавлена, иначе объявления области некуда отнести.
REGIONS = {
    "minsk": "Минск", "minsk_obl": "Минская обл.", "brest": "Брестская обл.",
    "vitebsk": "Витебская обл.", "gomel": "Гомельская обл.", "grodno": "Гродненская обл.",
    "mogilev": "Могилёвская обл.",
}


def fold(s: object) -> str:
    """Нижний регистр, ё→е, схлопнутые пробелы: для поиска подстрок."""
    return re.sub(r"\s+", " ", str(s or "").lower().replace("ё", "е")).strip()


def norm(s: object) -> str:
    """Ключ для сравнения названий: 'Mercedes-Benz' == 'mercedes benz'."""
    return re.sub(r"[^0-9a-zа-я]+", "", fold(s))


@dataclass(slots=True)
class Advert:
    """Объявление в том виде, в котором с ним работает остальной код."""

    id: int
    url: str
    brand: str = ""
    model: str = ""
    generation: str = ""
    year: int | None = None
    price_usd: int | None = None
    price_byn: int | None = None
    location: str = ""
    mileage_km: int | None = None
    engine_capacity: float | None = None
    fuel: str = ""        # petrol | diesel | electric | hybrid | ""
    fuel_raw: str = ""
    gearbox: str = ""     # auto | manual | ""
    gearbox_raw: str = ""
    body: str = ""        # ключ из BODIES | ""
    body_raw: str = ""
    description: str = ""
    seller_name: str = ""
    seller_id: str = ""
    photo: str = ""
    published_at: datetime | None = None

    @property
    def title(self) -> str:
        name = " ".join(p for p in (self.brand, self.model, self.generation) if p)
        return f"{name}, {self.year} г." if name and self.year else name


def parse_fuel(raw: object) -> str:
    s = fold(raw)
    if "гибрид" in s:
        return "hybrid"
    if "электро" in s:
        return "electric"
    if "дизел" in s:
        return "diesel"
    if "бензин" in s:
        return "petrol"
    return ""


def parse_gearbox(raw: object) -> str:
    s = fold(raw)
    if "механ" in s:
        return "manual"
    if any(k in s for k in ("автомат", "робот", "вариатор")):
        return "auto"
    return ""


_BODY_STEMS = (
    ("лифтбек", "liftback"), ("хэтчбек", "hatchback"), ("хетчбек", "hatchback"),
    ("универсал", "wagon"), ("седан", "sedan"), ("внедорожник", "suv"), ("кроссовер", "suv"),
    ("купе", "coupe"), ("минивэн", "minivan"), ("минивен", "minivan"), ("компактвэн", "minivan"),
    ("кабриолет", "cabrio"), ("родстер", "cabrio"), ("пикап", "pickup"),
)


def parse_body(raw: object) -> str:
    s = fold(raw)
    for stem, code in _BODY_STEMS:
        if stem in s:
            return code
    return ""


# --- регион по тексту «locationName» ---------------------------------------------------
# Часть объявлений содержит только город, поэтому для крупных городов есть словарь.
# Если регион определить не удалось, фильтр по региону объявление НЕ отсекает (лучше лишнее, чем пропуск).
_STEMS = {
    "brest": ("брест",), "vitebsk": ("витеб",), "gomel": ("гомел",),
    "grodno": ("гродн",), "mogilev": ("могил",),
}
_TOWNS = {
    "brest": "барановичи, пинск, кобрин, береза, ивацевичи, пружаны, ганцево, дрогичин, жабинка, каменец, "
             "малорита, столин, лунинец, ляховичи, иваново, давид-городок, белоозерск, микашевичи",
    "vitebsk": "орша, полоцк, новополоцк, поставы, глубокое, лепель, браслав, докшицы, бешенковичи, "
               "верхнедвинск, миоры, толочин, чашники, дубровно, сенно, лиозно, шумилино, ушачи",
    "gomel": "мозырь, жлобин, светлогорск, речица, калинковичи, рогачев, добруш, ельск, лельчицы, петриков, "
             "хойники, наровля, буда-кошелево, ветка, брагин, житковичи",
    "grodno": "лида, слоним, волковыск, сморгонь, новогрудок, островец, ошмяны, щучин, скидель, мосты, "
              "берестовица, вороново, дятлово, зельва, кореличи, свислочь, ивье",
    "mogilev": "бобруйск, горки, осиповичи, кричев, климовичи, шклов, быхов, белыничи, костюковичи, "
               "мстиславль, чаусы, дрибин, круглое, кировск, славгород, хотимск, чериков, глуск",
    "minsk_obl": "борисов, солигорск, молодечно, жодино, слуцк, дзержинск, марьина горка, вилейка, столбцы, "
                 "несвиж, смолевичи, фаниполь, заславль, логойск, крупки, клецк, червень, копыль, узда, "
                 "мядель, воложин, любань, старые дороги, березино, радошковичи, колодищи, ждановичи",
}
_TOWN2REGION = {t.strip(): reg for reg, line in _TOWNS.items() for t in line.split(",") if t.strip()}


def region_of(location: object) -> str | None:
    """Ключ региона из REGIONS по тексту местоположения или None, если не удалось определить."""
    loc = fold(location)
    if not loc:
        return None
    if "минская" in loc or "минский" in loc:
        return "minsk_obl"
    for key, stems in _STEMS.items():
        if any(s in loc for s in stems):
            return key
    parts = [re.sub(r"^(?:г|аг|д|п|гп|пгт)\.?\s+", "", p.strip()) for p in loc.split(",")]
    if "минск" in parts:
        return "minsk"
    for p in parts:
        if p in _TOWN2REGION:
            return _TOWN2REGION[p]
    return None
