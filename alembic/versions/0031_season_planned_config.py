"""seasons: планируемые правила лиги, задаваемые до активации

Правила сезона (``league_config``) морозятся в момент активации и дальше
неизменны — так требует ст. 1058 ГК. Проблема в том, что активация не всегда
ручная: воркер поднимает ``upcoming`` сезон, у которого наступил ``starts_at``,
и берёт ``LeagueConfig.default()``. Сезон, заведённый с датой старта в прошлом,
активируется через минуты после создания, а заведённые на год вперёд — каждый
в свой срок, и все с боевыми дефолтами. Выбрать пороги человек физически не
успевает, хотя форма для этого есть.

``planned_league_config`` разрывает эту связку: пороги задаются заранее, при
создании или правке сезона, и автоактивация замораживает именно их. NULL —
прежнее поведение (дефолты), так что существующие сезоны не меняются.

Отдельная колонка, а не переиспользование ``league_config``: у них разный
смысл и разные права на изменение. Планируемые правятся свободно до активации,
замороженные — уже нет; сливать их в одно поле значит терять эту границу.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0031_season_planned_config"
down_revision: str | None = "0030_email_login"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "seasons",
        sa.Column(
            "planned_league_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Правила, которые заморозит активация (в т.ч. автоматическая). "
                "NULL — взять дефолты scoring."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("seasons", "planned_league_config")
