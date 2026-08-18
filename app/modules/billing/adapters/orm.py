"""ORM-модели billing (SQLAlchemy 2.0).

Маппятся на доменные сущности явными ``to_domain``/``from_domain``.
Неизменяемость журнала (``ledger_transactions``/``ledger_entries``) и
раздельность касс гарантируются схемными триггерами (миграция ``0010``),
а не ORM.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.billing.domain.entities import (
    AccessGrant,
    Invite,
    Payment,
    PaymentProvider,
    PaymentPurpose,
    PaymentStatus,
    Payout,
    PayoutStatus,
    PrizeFund,
    PrizeFundStatus,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.modules.billing.domain.ledger import (
    EntryDirection,
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
    LedgerType,
    TransactionKind,
)


def _enum(py_enum: type, name: str) -> SAEnum:
    """Нативный Postgres enum со значениями в нижнем регистре."""
    return SAEnum(
        py_enum,
        name=name,
        values_callable=lambda enum: [member.value for member in enum],
    )


_ledger_type_enum = _enum(LedgerType, "ledger_type")
_entry_direction_enum = _enum(EntryDirection, "entry_direction")
_transaction_kind_enum = _enum(TransactionKind, "transaction_kind")
_subscription_plan_enum = _enum(SubscriptionPlan, "subscription_plan")
_subscription_status_enum = _enum(SubscriptionStatus, "subscription_status")
_payment_provider_enum = _enum(PaymentProvider, "payment_provider")
_payment_status_enum = _enum(PaymentStatus, "payment_status")
_payment_purpose_enum = _enum(PaymentPurpose, "payment_purpose")
_prize_fund_status_enum = _enum(PrizeFundStatus, "prize_fund_status")
_payout_status_enum = _enum(PayoutStatus, "payout_status")


# ── Леджер ────────────────────────────────────────────────────────────────


class LedgerAccountORM(Base):
    """Таблица ``ledger_accounts`` — план счетов с привязкой к кассе."""

    __tablename__ = "ledger_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    ledger_type: Mapped[LedgerType] = mapped_column(_ledger_type_enum, nullable=False)
    account_code: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="RUB")

    def to_domain(self) -> LedgerAccount:
        """ORM → доменная сущность."""
        return LedgerAccount(
            id=self.id,
            ledger_type=self.ledger_type,
            account_code=self.account_code,
            title=self.title,
            currency=self.currency,
        )

    @classmethod
    def from_domain(cls, account: LedgerAccount) -> LedgerAccountORM:
        """Доменная сущность → новая строка ORM."""
        return cls(
            id=account.id,
            ledger_type=account.ledger_type,
            account_code=account.account_code,
            title=account.title,
            currency=account.currency,
        )


class LedgerTransactionORM(Base):
    """Таблица ``ledger_transactions`` — единица проводки (append-only)."""

    __tablename__ = "ledger_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    ledger_type: Mapped[LedgerType] = mapped_column(_ledger_type_enum, nullable=False)
    kind: Mapped[TransactionKind] = mapped_column(
        _transaction_kind_enum, nullable=False
    )
    external_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    @classmethod
    def from_domain(cls, txn: LedgerTransaction) -> LedgerTransactionORM:
        """Доменная транзакция → новая строка ORM (без ног)."""
        return cls(
            id=txn.id,
            ledger_type=txn.ledger_type,
            kind=txn.kind,
            external_ref=txn.external_ref,
            description=txn.description,
            created_at=txn.created_at,
        )


class LedgerEntryORM(Base):
    """Таблица ``ledger_entries`` — ноги двойной записи (append-only)."""

    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_transactions.id"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_accounts.id"), nullable=False, index=True
    )
    direction: Mapped[EntryDirection] = mapped_column(
        _entry_direction_enum, nullable=False
    )
    amount_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    @classmethod
    def from_domain(
        cls, entry: LedgerEntry, *, transaction_id: uuid.UUID, created_at: datetime
    ) -> LedgerEntryORM:
        """Доменная нога → новая строка ORM."""
        return cls(
            id=entry.id,
            transaction_id=transaction_id,
            account_id=entry.account_id,
            direction=entry.direction,
            amount_kopecks=entry.amount_kopecks,
            created_at=created_at,
        )


# ── Подписки и платежи ────────────────────────────────────────────────────


class SubscriptionORM(Base):
    """Таблица ``subscriptions`` (операционная касса)."""

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    plan: Mapped[SubscriptionPlan] = mapped_column(
        _subscription_plan_enum, nullable=False
    )
    price_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[PaymentProvider] = mapped_column(
        _payment_provider_enum, nullable=False
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        _subscription_status_enum, nullable=False, index=True
    )
    provider_subscription_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_period_start: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    # Токен провайдера для автосписаний (ТБанк: RebillId).
    rebill_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_renew: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    renewal_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    def to_domain(self) -> Subscription:
        return Subscription(
            id=self.id,
            user_id=self.user_id,
            plan=self.plan,
            price_kopecks=self.price_kopecks,
            provider=self.provider,
            status=self.status,
            provider_subscription_id=self.provider_subscription_id,
            current_period_start=self.current_period_start,
            current_period_end=self.current_period_end,
            created_at=self.created_at,
            canceled_at=self.canceled_at,
            rebill_id=self.rebill_id,
            auto_renew=self.auto_renew,
            renewal_attempts=self.renewal_attempts,
        )

    @classmethod
    def from_domain(cls, sub: Subscription) -> SubscriptionORM:
        return cls(
            id=sub.id,
            user_id=sub.user_id,
            plan=sub.plan,
            price_kopecks=sub.price_kopecks,
            provider=sub.provider,
            status=sub.status,
            provider_subscription_id=sub.provider_subscription_id,
            current_period_start=sub.current_period_start,
            current_period_end=sub.current_period_end,
            created_at=sub.created_at,
            canceled_at=sub.canceled_at,
            rebill_id=sub.rebill_id,
            auto_renew=sub.auto_renew,
            renewal_attempts=sub.renewal_attempts,
        )


class PaymentORM(Base):
    """Таблица ``payments`` — приём средств (всегда операционная касса)."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=True
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        _payment_provider_enum, nullable=False
    )
    provider_payment_id: Mapped[str] = mapped_column(Text, nullable=False)
    amount_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purpose: Mapped[PaymentPurpose] = mapped_column(
        _payment_purpose_enum, nullable=False
    )
    status: Mapped[PaymentStatus] = mapped_column(
        _payment_status_enum, nullable=False, index=True
    )
    fiscal_receipt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ledger_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id"), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    def to_domain(self) -> Payment:
        return Payment(
            id=self.id,
            provider=self.provider,
            provider_payment_id=self.provider_payment_id,
            amount_kopecks=self.amount_kopecks,
            purpose=self.purpose,
            status=self.status,
            user_id=self.user_id,
            subscription_id=self.subscription_id,
            fiscal_receipt_id=self.fiscal_receipt_id,
            ledger_transaction_id=self.ledger_transaction_id,
            paid_at=self.paid_at,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, payment: Payment) -> PaymentORM:
        return cls(
            id=payment.id,
            provider=payment.provider,
            provider_payment_id=payment.provider_payment_id,
            amount_kopecks=payment.amount_kopecks,
            purpose=payment.purpose,
            status=payment.status,
            user_id=payment.user_id,
            subscription_id=payment.subscription_id,
            fiscal_receipt_id=payment.fiscal_receipt_id,
            ledger_transaction_id=payment.ledger_transaction_id,
            paid_at=payment.paid_at,
            created_at=payment.created_at,
        )


# ── Призовой фонд и выплаты ───────────────────────────────────────────────


class PrizeFundORM(Base):
    """Таблица ``prize_funds`` (призовая касса)."""

    __tablename__ = "prize_funds"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    season_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=True, index=True
    )
    sponsor_name: Mapped[str] = mapped_column(Text, nullable=False)
    sponsor_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sponsor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    committed_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deposited_kopecks: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    ledger_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_accounts.id"), nullable=False
    )
    status: Mapped[PrizeFundStatus] = mapped_column(
        _prize_fund_status_enum, nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    def to_domain(self) -> PrizeFund:
        return PrizeFund(
            id=self.id,
            sponsor_name=self.sponsor_name,
            ledger_account_id=self.ledger_account_id,
            committed_kopecks=self.committed_kopecks,
            season_id=self.season_id,
            sponsor_ref=self.sponsor_ref,
            sponsor_user_id=self.sponsor_user_id,
            deposited_kopecks=self.deposited_kopecks,
            status=self.status,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, fund: PrizeFund) -> PrizeFundORM:
        return cls(
            id=fund.id,
            season_id=fund.season_id,
            sponsor_name=fund.sponsor_name,
            sponsor_ref=fund.sponsor_ref,
            sponsor_user_id=fund.sponsor_user_id,
            committed_kopecks=fund.committed_kopecks,
            deposited_kopecks=fund.deposited_kopecks,
            ledger_account_id=fund.ledger_account_id,
            status=fund.status,
            created_at=fund.created_at,
        )


class PayoutORM(Base):
    """Таблица ``payouts`` — выплаты победителям (призовая касса)."""

    __tablename__ = "payouts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    prize_fund_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prize_funds.id"), nullable=False
    )
    season_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=True, index=True
    )
    amount_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_withheld_kopecks: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    status: Mapped[PayoutStatus] = mapped_column(
        _payout_status_enum, nullable=False, index=True
    )
    provider: Mapped[PaymentProvider | None] = mapped_column(
        _payment_provider_enum, nullable=True
    )
    provider_payout_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    ledger_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    def to_domain(self) -> Payout:
        return Payout(
            id=self.id,
            user_id=self.user_id,
            prize_fund_id=self.prize_fund_id,
            amount_kopecks=self.amount_kopecks,
            created_by=self.created_by,
            season_id=self.season_id,
            tax_withheld_kopecks=self.tax_withheld_kopecks,
            status=self.status,
            provider=self.provider,
            provider_payout_id=self.provider_payout_id,
            approved_by=self.approved_by,
            ledger_transaction_id=self.ledger_transaction_id,
            created_at=self.created_at,
            paid_at=self.paid_at,
        )

    @classmethod
    def from_domain(cls, payout: Payout) -> PayoutORM:
        return cls(
            id=payout.id,
            user_id=payout.user_id,
            prize_fund_id=payout.prize_fund_id,
            season_id=payout.season_id,
            amount_kopecks=payout.amount_kopecks,
            tax_withheld_kopecks=payout.tax_withheld_kopecks,
            status=payout.status,
            provider=payout.provider,
            provider_payout_id=payout.provider_payout_id,
            created_by=payout.created_by,
            approved_by=payout.approved_by,
            ledger_transaction_id=payout.ledger_transaction_id,
            created_at=payout.created_at,
            paid_at=payout.paid_at,
        )


class PayoutRequisitesORM(Base):
    """Таблица ``payout_requisites`` — реквизиты выплат пользователя (СБП).

    ПДн (телефон, ФИО) лежат шифрованными (Fernet); маппинг в доменную
    сущность делает репозиторий, которому передан ``FieldEncryptor`` — ORM
    о ключах не знает.
    """

    __tablename__ = "payout_requisites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    sbp_phone_enc: Mapped[bytes] = mapped_column(nullable=False)
    sbp_bank_id: Mapped[str] = mapped_column(Text, nullable=False)
    last_name_enc: Mapped[bytes] = mapped_column(nullable=False)
    first_name_enc: Mapped[bytes] = mapped_column(nullable=False)
    middle_name_enc: Mapped[bytes | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )


class InviteORM(Base):
    """Таблица ``invites`` — одноразовые пригласительные ссылки."""

    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    # NULL — доступ бессрочный.
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    redeemed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    def to_domain(self) -> Invite:
        """ORM → доменная сущность."""
        return Invite(
            id=self.id,
            code=self.code,
            created_by=self.created_by,
            duration_days=self.duration_days,
            note=self.note,
            redeemed_by=self.redeemed_by,
            redeemed_at=self.redeemed_at,
            revoked_at=self.revoked_at,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, invite: Invite) -> InviteORM:
        """Доменная сущность → новая ORM-строка."""
        return cls(
            id=invite.id,
            code=invite.code,
            created_by=invite.created_by,
            duration_days=invite.duration_days,
            note=invite.note,
            redeemed_by=invite.redeemed_by,
            redeemed_at=invite.redeemed_at,
            revoked_at=invite.revoked_at,
            created_at=invite.created_at,
        )


class AccessGrantORM(Base):
    """Таблица ``access_grants`` — доступ, выданный по приглашению."""

    __tablename__ = "access_grants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    invite_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invites.id"), nullable=False, unique=True
    )
    # NULL — бессрочно.
    expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    def to_domain(self) -> AccessGrant:
        """ORM → доменная сущность."""
        return AccessGrant(
            id=self.id,
            user_id=self.user_id,
            invite_id=self.invite_id,
            expires_at=self.expires_at,
            granted_at=self.granted_at,
        )

    @classmethod
    def from_domain(cls, grant: AccessGrant) -> AccessGrantORM:
        """Доменная сущность → новая ORM-строка."""
        return cls(
            id=grant.id,
            user_id=grant.user_id,
            invite_id=grant.invite_id,
            expires_at=grant.expires_at,
            granted_at=grant.granted_at,
        )
