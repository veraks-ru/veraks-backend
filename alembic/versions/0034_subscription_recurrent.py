"""billing: автопродление подписки (рекуррентные списания)

Подписка была разовой покупкой периода: человек платил, доступ открывался на
срок и молча заканчивался. Чтобы продлить, нужно было заново пройти оплату
руками — так теряется почти вся выручка второго периода.

``rebill_id`` — токен ТБанка, который приходит в уведомлении о первом
(родительском) платеже, если Init отправлен с ``Recurrent=Y``. Дальше списание
делается методом Charge без участия человека, карточные данные к нам не
попадают.

``auto_renew`` отделён от наличия токена намеренно: человек может отключить
продление, и тогда токен стирается, а оплаченный период остаётся за ним.
``renewal_attempts`` считает подряд идущие отказы банка — после лимита попытки
прекращаются, чтобы не долбить карту.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0034_subscription_recurrent"
down_revision: str | None = "0033_invites"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "rebill_id",
            sa.Text(),
            nullable=True,
            comment="Токен провайдера для списаний без участия человека (ТБанк: RebillId).",
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "auto_renew",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Продлевать ли подписку автоматически.",
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "renewal_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Подряд идущие неудачные списания; обнуляется успешной оплатой.",
        ),
    )
    # Существующие подписки остаются разовыми: согласия на периодические
    # списания эти люди не давали, и задним числом его не появляется.


def downgrade() -> None:
    op.drop_column("subscriptions", "renewal_attempts")
    op.drop_column("subscriptions", "auto_renew")
    op.drop_column("subscriptions", "rebill_id")
