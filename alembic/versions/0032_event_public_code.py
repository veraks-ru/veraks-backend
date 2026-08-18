"""events: короткий публичный код для ссылок

Событие адресовалось UUID, и ссылка выглядела так::

    https://veraks.ru/events/6c2594ba-5ed1-42c1-b769-f5c2db36e67b

Такую не продиктуешь и не поставишь в пост — а ссылками на события делятся,
в этом весь смысл публичных прогнозов. ``public_code`` даёт короткую форму
из 11 символов base64url (8 случайных байт), как у ссылок YouTube.

Код случайный, а не производный от заголовка: заголовки правят, и разосланная
ссылка от этого ломаться не должна. UUID остаётся первичным ключом и работает
в ссылках по-прежнему — обе формы ведут на одно событие.

Существующим строкам коды раздаются здесь же, поэтому колонка заполняется в
три шага: добавить nullable, проставить значения, затем NOT NULL и UNIQUE.
"""

from __future__ import annotations

import secrets

import sqlalchemy as sa

from alembic import op

revision: str = "0032_event_public_code"
down_revision: str | None = "0031_season_planned_config"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column(
            "public_code",
            sa.Text(),
            nullable=True,
            comment="Короткий код для публичных ссылок (11 символов base64url).",
        ),
    )

    # Коды генерируем в Python, а не в SQL: pgcrypto в базе может быть не
    # установлен, а повторять здесь ровно тот же алфавит, что в домене
    # (secrets.token_urlsafe), надёжнее без промежуточного слоя.
    bind = op.get_bind()
    ids = bind.execute(sa.text("SELECT id FROM events")).scalars().all()
    for event_id in ids:
        bind.execute(
            sa.text("UPDATE events SET public_code = :code WHERE id = :id"),
            {"code": secrets.token_urlsafe(8), "id": event_id},
        )

    op.alter_column("events", "public_code", existing_type=sa.Text(), nullable=False)
    op.create_index("ix_events_public_code", "events", ["public_code"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_events_public_code", table_name="events")
    op.drop_column("events", "public_code")
