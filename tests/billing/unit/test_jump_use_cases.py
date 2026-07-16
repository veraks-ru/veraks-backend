"""Use-cases выплат через Jump: реквизиты СБП, авто-dispatch, опрос статусов."""

from __future__ import annotations

import uuid

import pytest

from app.modules.billing.application.dto import Actor
from app.modules.billing.application.use_cases import (
    DispatchApprovedPayouts,
    DispatchPayout,
    GetMyPayoutRequisites,
    PollPayoutStatuses,
    RecordPayoutResult,
    UpsertMyPayoutRequisites,
)
from app.modules.billing.domain.entities import (
    PaymentProvider,
    Payout,
    PayoutRequisites,
    PayoutStatus,
)
from app.modules.billing.domain.errors import (
    BillingPermissionError,
    PayoutRequisitesMissingError,
)
from app.modules.billing.ports.gateways import PayoutStatusView
from app.modules.identity.domain.entities import UserRole
from tests.billing.conftest import FIXED_NOW
from tests.billing.fakes import (
    FakeAuditTrail,
    FakeClock,
    FakeNotifier,
    FakePayoutGateway,
    FakePayoutStatusProbe,
    InMemoryPayoutRepository,
    InMemoryPayoutRequisiteRepository,
)


def _requisites(user_id: uuid.UUID | None = None) -> PayoutRequisites:
    return PayoutRequisites(
        user_id=user_id or uuid.uuid4(),
        phone="+79001234567",
        sbp_bank_id="100000000004",
        last_name="Иванов",
        first_name="Пётр",
        middle_name="Сергеевич",
    )


def _payout(status: PayoutStatus = PayoutStatus.APPROVED, **over: object) -> Payout:
    params: dict[str, object] = {
        "user_id": uuid.uuid4(),
        "prize_fund_id": uuid.uuid4(),
        "amount_kopecks": 8_700,
        "created_by": uuid.uuid4(),
        "status": status,
    }
    if status is not PayoutStatus.PENDING:
        params["approved_by"] = uuid.uuid4()
    params.update(over)
    return Payout(**params)  # type: ignore[arg-type]


def _upsert_uc(repo: InMemoryPayoutRequisiteRepository, audit: FakeAuditTrail):
    return UpsertMyPayoutRequisites(
        requisites=repo, audit=audit, clock=FakeClock(FIXED_NOW)
    )


def _dispatch_uc(
    payouts: InMemoryPayoutRepository,
    requisites: InMemoryPayoutRequisiteRepository,
    gateway: FakePayoutGateway | None = None,
) -> DispatchPayout:
    return DispatchPayout(
        payouts=payouts,
        gateway=gateway or FakePayoutGateway(provider="jump"),
        requisites=requisites,
        audit=FakeAuditTrail(),
        clock=FakeClock(FIXED_NOW),
    )


# ── Реквизиты: upsert и чтение ─────────────────────────────────────────────


async def test_upsert_creates_then_updates_requisites() -> None:
    repo = InMemoryPayoutRequisiteRepository()
    audit = FakeAuditTrail()
    user_id = uuid.uuid4()
    uc = _upsert_uc(repo, audit)

    created = await uc.execute(
        user_id=user_id,
        phone="8 (900) 123-45-67",
        sbp_bank_id="100000000004",
        last_name="Иванов",
        first_name="Пётр",
        middle_name=None,
    )
    assert created.phone == "+79001234567"

    updated = await uc.execute(
        user_id=user_id,
        phone="+79007654321",
        sbp_bank_id="100000000111",
        last_name="Иванов",
        first_name="Пётр",
        middle_name="Сергеевич",
    )
    # Тот же пользователь — та же запись (id стабилен), реквизиты новые.
    assert updated.id == created.id
    assert updated.phone == "+79007654321"
    assert (await repo.get_by_user(user_id)).sbp_bank_id == "100000000111"


async def test_upsert_audit_masks_phone_and_omits_names() -> None:
    repo = InMemoryPayoutRequisiteRepository()
    audit = FakeAuditTrail()
    await _upsert_uc(repo, audit).execute(
        user_id=uuid.uuid4(),
        phone="+79001234567",
        sbp_bank_id="100000000004",
        last_name="Иванов",
        first_name="Пётр",
        middle_name=None,
    )
    entry = audit.records[-1]
    assert entry["action"] == "prize.requisites.updated"
    # ПДн в аудит не пишем: телефон только маской, ФИО — вовсе нет.
    dumped = str(entry)
    assert "+79001234567" not in dumped
    assert "Иванов" not in dumped
    assert entry["after"]["phone_mask"].endswith("4567")


async def test_get_my_requisites_returns_none_when_absent() -> None:
    repo = InMemoryPayoutRequisiteRepository()
    assert await GetMyPayoutRequisites(requisites=repo).execute(
        user_id=uuid.uuid4()
    ) is None


# ── DispatchPayout: реквизиты и системный актор ────────────────────────────


async def test_dispatch_passes_recipient_to_gateway() -> None:
    payouts = InMemoryPayoutRepository()
    requisites = InMemoryPayoutRequisiteRepository()
    payout = _payout()
    await payouts.add(payout)
    await requisites.upsert(_requisites(payout.user_id))
    gateway = FakePayoutGateway(provider="jump")
    admin = Actor(user_id=uuid.uuid4(), role=UserRole.ADMIN)

    saved = await _dispatch_uc(payouts, requisites, gateway).execute(
        actor=admin, payout_id=payout.id
    )

    assert saved.status is PayoutStatus.PROCESSING
    assert saved.provider is PaymentProvider.JUMP
    call = gateway.calls[0]
    assert call["recipient"].phone == "+79001234567"
    assert call["recipient"].sbp_bank_id == "100000000004"
    assert call["payout_id"] == payout.id


async def test_dispatch_without_requisites_fails_before_gateway_call() -> None:
    payouts = InMemoryPayoutRepository()
    requisites = InMemoryPayoutRequisiteRepository()
    payout = _payout()
    await payouts.add(payout)
    gateway = FakePayoutGateway(provider="jump")
    admin = Actor(user_id=uuid.uuid4(), role=UserRole.ADMIN)

    with pytest.raises(PayoutRequisitesMissingError):
        await _dispatch_uc(payouts, requisites, gateway).execute(
            actor=admin, payout_id=payout.id
        )
    # Внешний вызов не делался, статус не тронут — можно повторить позже.
    assert gateway.calls == []
    assert (await payouts.get_by_id(payout.id)).status is PayoutStatus.APPROVED


async def test_dispatch_as_system_skips_rbac_and_audits_system() -> None:
    payouts = InMemoryPayoutRepository()
    requisites = InMemoryPayoutRequisiteRepository()
    payout = _payout()
    await payouts.add(payout)
    await requisites.upsert(_requisites(payout.user_id))
    audit = FakeAuditTrail()
    uc = DispatchPayout(
        payouts=payouts,
        gateway=FakePayoutGateway(provider="jump"),
        requisites=requisites,
        audit=audit,
        clock=FakeClock(FIXED_NOW),
    )

    saved = await uc.execute(actor=None, payout_id=payout.id)

    assert saved.status is PayoutStatus.PROCESSING
    entry = audit.records[-1]
    assert entry["actor_id"] is None
    assert entry["actor_type"].value == "system"


async def test_dispatch_still_requires_admin_for_human_actor() -> None:
    payouts = InMemoryPayoutRepository()
    requisites = InMemoryPayoutRequisiteRepository()
    payout = _payout()
    await payouts.add(payout)
    await requisites.upsert(_requisites(payout.user_id))
    user = Actor(user_id=uuid.uuid4(), role=UserRole.USER)

    with pytest.raises(BillingPermissionError):
        await _dispatch_uc(payouts, requisites).execute(
            actor=user, payout_id=payout.id
        )


# ── DispatchApprovedPayouts: скан очереди на отправку ──────────────────────


async def test_list_dispatchable_returns_only_approved_in_fifo_order() -> None:
    payouts = InMemoryPayoutRepository()
    older = _payout()
    newer = _payout()
    newer.created_at = older.created_at.replace(year=older.created_at.year + 1)
    await payouts.add(newer)
    await payouts.add(older)
    await payouts.add(_payout(status=PayoutStatus.PENDING))
    await payouts.add(
        _payout(
            status=PayoutStatus.PROCESSING,
            provider=PaymentProvider.JUMP,
            provider_payout_id="1",
        )
    )

    ids = await DispatchApprovedPayouts(payouts=payouts).list_dispatchable_ids()

    assert ids == [older.id, newer.id]


# ── PollPayoutStatuses: опрос Jump до is_final ─────────────────────────────


def _processing_jump_payout(ref: str) -> Payout:
    return _payout(
        status=PayoutStatus.PROCESSING,
        provider=PaymentProvider.JUMP,
        provider_payout_id=ref,
    )


def _poll_uc(
    payouts: InMemoryPayoutRepository,
    probe: FakePayoutStatusProbe,
    notifier: FakeNotifier | None = None,
) -> PollPayoutStatuses:
    recorder = RecordPayoutResult(
        payouts=payouts, audit=FakeAuditTrail(), clock=FakeClock(FIXED_NOW)
    )
    return PollPayoutStatuses(
        payouts=payouts,
        probe=probe,
        recorder=recorder,
        notifier=notifier,
    )


async def test_poll_finalizes_paid_and_failed_and_skips_pending() -> None:
    payouts = InMemoryPayoutRepository()
    paid = _processing_jump_payout("jp-1")
    failed = _processing_jump_payout("jp-2")
    waiting = _processing_jump_payout("jp-3")
    for p in (paid, failed, waiting):
        await payouts.add(p)
    probe = FakePayoutStatusProbe()
    probe.statuses["jp-1"] = PayoutStatusView(status_id=1, is_final=True)
    probe.statuses["jp-2"] = PayoutStatusView(status_id=5, is_final=True)
    # 7 «Ожидает подтверждения» — штатный режим тестирования на бою.
    probe.statuses["jp-3"] = PayoutStatusView(status_id=7, is_final=False)

    finalized = await _poll_uc(payouts, probe).execute()

    assert finalized == 2
    assert (await payouts.get_by_id(paid.id)).status is PayoutStatus.PAID
    assert (await payouts.get_by_id(failed.id)).status is PayoutStatus.FAILED
    assert (await payouts.get_by_id(waiting.id)).status is PayoutStatus.PROCESSING


async def test_poll_ignores_other_providers() -> None:
    payouts = InMemoryPayoutRepository()
    foreign = _payout(
        status=PayoutStatus.PROCESSING,
        provider=PaymentProvider.YOOKASSA,
        provider_payout_id="po-1",
    )
    await payouts.add(foreign)
    probe = FakePayoutStatusProbe()  # пустой: любой опрос упал бы KeyError

    assert await _poll_uc(payouts, probe).execute() == 0
    assert (await payouts.get_by_id(foreign.id)).status is PayoutStatus.PROCESSING


async def test_poll_error_on_one_payout_does_not_stop_others() -> None:
    payouts = InMemoryPayoutRepository()
    broken = _processing_jump_payout("jp-err")
    healthy = _processing_jump_payout("jp-ok")
    await payouts.add(broken)
    await payouts.add(healthy)
    probe = FakePayoutStatusProbe()
    probe.errors.add("jp-err")
    probe.statuses["jp-ok"] = PayoutStatusView(status_id=1, is_final=True)

    finalized = await _poll_uc(payouts, probe).execute()

    assert finalized == 1
    assert (await payouts.get_by_id(healthy.id)).status is PayoutStatus.PAID
    assert (await payouts.get_by_id(broken.id)).status is PayoutStatus.PROCESSING


async def test_poll_notifies_winner_on_paid() -> None:
    payouts = InMemoryPayoutRepository()
    payout = _processing_jump_payout("jp-1")
    await payouts.add(payout)
    probe = FakePayoutStatusProbe()
    probe.statuses["jp-1"] = PayoutStatusView(status_id=1, is_final=True)
    notifier = FakeNotifier()

    await _poll_uc(payouts, probe, notifier).execute()

    emitted = notifier.emitted[-1]
    assert emitted["user_id"] == payout.user_id
    assert emitted["kind"] == "payout.paid"
