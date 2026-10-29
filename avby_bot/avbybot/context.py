"""Контейнер зависимостей, который aiogram подставляет в хендлеры (параметр `app`)."""
from __future__ import annotations

from dataclasses import dataclass

from .avby import AvByClient, Catalog
from .config import Config
from .db import Database
from .subscription import SubscriptionGate


@dataclass
class App:
    cfg: Config
    db: Database
    client: AvByClient
    catalog: Catalog
    gate: SubscriptionGate

    async def check_limit(self, user_id: int) -> tuple[bool, int | None]:
        """(можно ли запустить ещё мониторинг, лимит). Лимит None = безлимит (Premium/админ)."""
        if user_id in self.cfg.admin_ids or Database.is_premium(await self.db.get_user(user_id)):
            return True, None
        limit = self.cfg.free_limit
        return await self.db.count_active(user_id) < limit, limit

    def limit_text(self, limit: int | None) -> str:
        contact = f" Напишите {self.cfg.support}." if self.cfg.support else ""
        return (
            f"🔒 На бесплатном тарифе можно держать активными не более <b>{limit}</b> мониторингов.\n\n"
            f"Поставьте один из них на паузу, удалите его или оформите 💎 <b>Premium</b> (без ограничений).{contact}"
        )
