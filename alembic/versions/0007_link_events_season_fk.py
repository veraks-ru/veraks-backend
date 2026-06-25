"""events: отложенный FK events.season_id → seasons.id (с бэкфиллом orphan'ов)

Вынесено отдельной миграцией сознательно. До домена seasons сезонов не
существовало, поэтому ``events.season_id`` мог содержать «голые» UUID, не
указывающие ни на один реальный сезон. Навешивание FK без подготовки упало бы
целиком (и потянуло бы за собой откат всей миграции). Поэтому сначала
**верифицируем/бэкфиллим**: orphan-значения обнуляем (``NULL``), затем вешаем
FK. Кросс-доменное изменение не смешано с созданием таблиц (0005).

Revision ID: 0007_link_events_season_fk
Revises: 0006_add_ratings_qualified
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_link_events_season_fk"
down_revision: str | None = "0006_add_ratings_qualified"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK_NAME = "fk_events_season_id"


def upgrade() -> None:
    """Бэкфилл orphan season_id → NULL, затем FK events.season_id → seasons.id."""
    # Обнуляем ссылки на несуществующие сезоны, иначе создание FK упадёт.
    op.execute(
        """
        UPDATE events
        SET season_id = NULL
        WHERE season_id IS NOT NULL
          AND season_id NOT IN (SELECT id FROM seasons)
        """
    )
    op.create_foreign_key(
        _FK_NAME,
        source_table="events",
        referent_table="seasons",
        local_cols=["season_id"],
        remote_cols=["id"],
    )


def downgrade() -> None:
    """Снимает FK (season_id остаётся nullable UUID-колонкой, как до 0007)."""
    op.drop_constraint(_FK_NAME, "events", type_="foreignkey")
