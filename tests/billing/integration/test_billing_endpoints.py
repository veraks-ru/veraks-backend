"""Интеграционные тесты HTTP-эндпоинтов billing.

Поднимают реальное приложение, но I/O-порты и аутентификацию подменяют
фейками через ``dependency_overrides``. Один набор фейков переживает все
запросы; активный пользователь переключается мутабельным холдером.

БД-инварианты (триггеры раздельности касс/баланса/append-only, UNIQUE) —
отдельно e2e против Postgres.

TODO(billing-infra): e2e против реального Postgres (testcontainers) для
триггеров ``enforce_ledger_separation``/``enforce_transaction_balanced`` и
append-only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.billing.api.dependencies import (
    get_audit_trail,
    get_checkout_gateway,
    get_clock,
    get_ledger_repository,
    get_payment_repository,
    get_payout_gateway,
    get_payout_notifier,
    get_payout_repository,
    get_payout_requisite_repository,
    get_prize_fund_repository,
    get_refund_gateway,
    get_season_directory,
    get_subscription_repository,
)
from app.modules.billing.domain import chart
from app.modules.billing.domain.entities import PaymentProvider, PayoutRequisites
from app.modules.billing.domain.ledger import LedgerAccount, LedgerType
from app.modules.billing.domain.tbank_signing import make_token
from app.modules.identity.api.dependencies import get_current_user
from app.modules.identity.domain.entities import User, UserRole
from tests.billing.conftest import FIXED_NOW
from tests.billing.fakes import (
    FakeAuditTrail,
    FakeCheckoutGateway,
    FakeClock,
    FakeNotifier,
    FakePayoutGateway,
    FakeRefundGateway,
    FakeSeasonDirectory,
    InMemoryLedgerRepository,
    InMemoryPaymentRepository,
    InMemoryPayoutRepository,
    InMemoryPayoutRequisiteRepository,
    InMemoryPrizeFundRepository,
    InMemorySubscriptionRepository,
)

_SEED = [
    (LedgerType.OPERATIONS, chart.OPS_CASH_YOOKASSA),
    (LedgerType.OPERATIONS, chart.OPS_CASH_TBANK),
    (LedgerType.OPERATIONS, chart.OPS_REVENUE_SUBSCRIPTIONS),
    (LedgerType.PRIZE, chart.PRIZE_CASH_SPONSOR),
    (LedgerType.PRIZE, chart.PRIZE_PAYABLE_WINNERS),
    (LedgerType.PRIZE, chart.PRIZE_TAX_WITHHELD),
]


def _user(role: UserRole) -> User:
    return User(
        esia_oid=f"oid-{uuid.uuid4()}",
        snils_hash=f"hash-{uuid.uuid4()}",
        username=f"user-{uuid.uuid4().hex[:8]}",
        display_name="Тест",
        real_name_enc=None,
        role=role,
    )


@dataclass
class Ctx:
    client: TestClient
    ledger: InMemoryLedgerRepository
    payments: InMemoryPaymentRepository
    funds: InMemoryPrizeFundRepository
    requisites: InMemoryPayoutRequisiteRepository
    seasons: FakeSeasonDirectory
    holder: dict


@pytest.fixture
def ctx():
    ledger = InMemoryLedgerRepository()
    for ltype, code in _SEED:
        ledger.seed_account(
            LedgerAccount(ledger_type=ltype, account_code=code, title=code)
        )
    subscriptions = InMemorySubscriptionRepository()
    payments = InMemoryPaymentRepository()
    funds = InMemoryPrizeFundRepository()
    payouts = InMemoryPayoutRepository()
    requisites = InMemoryPayoutRequisiteRepository()
    audit = FakeAuditTrail()
    seasons = FakeSeasonDirectory()
    holder: dict = {"user": None}

    app = create_app()
    app.dependency_overrides[get_ledger_repository] = lambda: ledger
    app.dependency_overrides[get_subscription_repository] = lambda: subscriptions
    app.dependency_overrides[get_payment_repository] = lambda: payments
    app.dependency_overrides[get_prize_fund_repository] = lambda: funds
    app.dependency_overrides[get_payout_repository] = lambda: payouts
    app.dependency_overrides[get_payout_requisite_repository] = lambda: requisites
    app.dependency_overrides[get_audit_trail] = lambda: audit
    app.dependency_overrides[get_checkout_gateway] = lambda: FakeCheckoutGateway()
    app.dependency_overrides[get_refund_gateway] = lambda: FakeRefundGateway()
    app.dependency_overrides[get_payout_gateway] = lambda: FakePayoutGateway()
    app.dependency_overrides[get_payout_notifier] = lambda: FakeNotifier()
    app.dependency_overrides[get_season_directory] = lambda: seasons
    app.dependency_overrides[get_clock] = lambda: FakeClock(FIXED_NOW)

    def _current_user() -> User:
        user = holder["user"]
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return user

    app.dependency_overrides[get_current_user] = _current_user

    client = TestClient(app)
    try:
        yield Ctx(
            client=client, ledger=ledger, payments=payments, funds=funds,
            requisites=requisites, seasons=seasons, holder=holder
        )
    finally:
        client.close()


def test_list_plans_returns_priced_tariffs(ctx: Ctx) -> None:
    """``GET /billing/plans`` отдаёт тарифы с ценами в копейках (публично)."""
    resp = ctx.client.get("/billing/plans")
    assert resp.status_code == 200, resp.text
    by_plan = {p["plan"]: p["price_kopecks"] for p in resp.json()["plans"]}
    # Совпадает с дефолтами BillingSettings и статикой фронта (lib/pricing.ts).
    assert by_plan["monthly"] == 99_000
    assert by_plan["annual"] == 499_000


def test_my_subscription_returns_latest(ctx: Ctx) -> None:
    """``GET /billing/subscriptions/me`` возвращает свою подписку после оформления."""
    user = _user(UserRole.USER)
    ctx.holder["user"] = user

    start = ctx.client.post(
        "/billing/subscriptions", json={"plan": "monthly", "provider": "yookassa"}
    )
    assert start.status_code == 201, start.text

    mine = ctx.client.get("/billing/subscriptions/me")
    assert mine.status_code == 200, mine.text
    assert mine.json()["plan"] == "monthly"
    assert mine.json()["user_id"] == str(user.id)


def test_my_subscription_404_when_absent(ctx: Ctx) -> None:
    """Без подписки — 404 (доменная ошибка маппится централизованно)."""
    ctx.holder["user"] = _user(UserRole.USER)
    resp = ctx.client.get("/billing/subscriptions/me")
    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_my_subscription_requires_auth(ctx: Ctx) -> None:
    """Чтение своей подписки требует аутентификации."""
    resp = ctx.client.get("/billing/subscriptions/me")
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_payment_webhook_posts_operations(ctx: Ctx) -> None:
    """Вебхук приёма платежа не требует авторизации и проводит в OPERATIONS."""
    resp = ctx.client.post(
        "/webhooks/payments/yookassa",
        json={
            "provider": "yookassa",
            "provider_payment_id": "pay-int-1",
            "amount_kopecks": 49_000,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["ledger_transaction_id"] is not None
    cash = ctx.ledger.transactions[0]
    assert cash.ledger_type is LedgerType.OPERATIONS


def test_payment_webhook_idempotent(ctx: Ctx) -> None:
    payload = {
        "provider": "yookassa",
        "provider_payment_id": "pay-int-dup",
        "amount_kopecks": 49_000,
    }
    first = ctx.client.post("/webhooks/payments/yookassa", json=payload).json()
    second = ctx.client.post("/webhooks/payments/yookassa", json=payload).json()
    assert first["id"] == second["id"]
    assert len(ctx.ledger.transactions) == 1


def test_prize_payout_maker_checker_flow(ctx: Ctx) -> None:
    maker = _user(UserRole.ADMIN)
    checker = _user(UserRole.ADMIN)

    # admin заводит фонд
    ctx.holder["user"] = maker
    fund_resp = ctx.client.post(
        "/admin/prize-funds",
        json={"sponsor_name": "Acme", "committed_kopecks": 1_000_000},
    )
    assert fund_resp.status_code == 201, fund_resp.text
    fund_id = fund_resp.json()["id"]

    # депозит спонсора
    dep = ctx.client.post(
        f"/admin/prize-funds/{fund_id}/deposit", json={"amount_kopecks": 1_000_000}
    )
    assert dep.status_code == 200, dep.text

    # maker создаёт выплату
    created = ctx.client.post(
        "/admin/payouts",
        json={
            "user_id": str(uuid.uuid4()),
            "prize_fund_id": fund_id,
            "amount_kopecks": 8_700,
            "tax_withheld_kopecks": 1_300,
        },
    )
    assert created.status_code == 201, created.text
    payout_id = created.json()["id"]
    assert created.json()["status"] == "pending"

    # maker не может подтвердить свою выплату (maker-checker → 403)
    self_approve = ctx.client.post(f"/admin/payouts/{payout_id}/approve")
    assert self_approve.status_code == status.HTTP_403_FORBIDDEN

    # checker (другой admin) подтверждает
    ctx.holder["user"] = checker
    approved = ctx.client.post(f"/admin/payouts/{payout_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    # публичный фонд показывает уменьшившееся сальдо
    public = ctx.client.get(f"/prize-funds/{fund_id}")
    assert public.status_code == 200
    assert public.json()["balance_kopecks"] == 1_000_000 - 10_000


def test_my_payouts_returns_own(ctx: Ctx) -> None:
    """GET /users/me/payouts — пользователь видит начисленные ему выплаты."""
    winner = _user(UserRole.USER)
    admin = _user(UserRole.ADMIN)

    ctx.holder["user"] = admin
    fund_id = ctx.client.post(
        "/admin/prize-funds",
        json={"sponsor_name": "Acme", "committed_kopecks": 1_000_000},
    ).json()["id"]
    ctx.client.post(
        f"/admin/prize-funds/{fund_id}/deposit", json={"amount_kopecks": 1_000_000}
    )
    ctx.client.post(
        "/admin/payouts",
        json={
            "user_id": str(winner.id),
            "prize_fund_id": fund_id,
            "amount_kopecks": 7_000,
        },
    )

    ctx.holder["user"] = winner
    mine = ctx.client.get("/users/me/payouts")
    assert mine.status_code == 200, mine.text
    assert len(mine.json()) == 1
    assert mine.json()[0]["user_id"] == str(winner.id)

    # Другой пользователь своих выплат не имеет.
    ctx.holder["user"] = _user(UserRole.USER)
    assert ctx.client.get("/users/me/payouts").json() == []


def test_my_payouts_requires_auth(ctx: Ctx) -> None:
    assert ctx.client.get("/users/me/payouts").status_code == status.HTTP_401_UNAUTHORIZED


def test_payout_webhook_rejects_bad_signature_when_secret_set() -> None:
    """С заданным секретом вебхук без/с неверной подписью → 401 (до use-case)."""
    from app.config import WebhookSettings, get_settings

    base = get_settings()
    with_secret = base.model_copy(
        update={"webhooks": WebhookSettings(yookassa_payout_secret="s3cr3t")}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: with_secret
    with TestClient(app) as client:
        body = {
            "provider": "yookassa",
            "provider_payout_id": "po-x",
            "succeeded": True,
        }
        # Без подписи — отклонено до use-case (не доходит до БД).
        assert client.post("/webhooks/payouts/yookassa", json=body).status_code == 401
        # С неверной подписью — тоже 401.
        bad = client.post(
            "/webhooks/payouts/yookassa", json=body, headers={"X-Signature": "nope"}
        )
        assert bad.status_code == 401


def test_payout_dispatch_and_webhook_lifecycle(ctx: Ctx) -> None:
    """approve → dispatch (processing) → вебхук (paid): полный жизненный цикл."""
    maker = _user(UserRole.ADMIN)
    checker = _user(UserRole.ADMIN)

    winner_id = uuid.uuid4()
    ctx.holder["user"] = maker
    fund_id = ctx.client.post(
        "/admin/prize-funds",
        json={"sponsor_name": "Acme", "committed_kopecks": 1_000_000},
    ).json()["id"]
    ctx.client.post(
        f"/admin/prize-funds/{fund_id}/deposit", json={"amount_kopecks": 1_000_000}
    )
    payout_id = ctx.client.post(
        "/admin/payouts",
        json={
            "user_id": str(winner_id),
            "prize_fund_id": fund_id,
            "amount_kopecks": 8_700,
        },
    ).json()["id"]
    # У победителя заполнены реквизиты СБП — иначе dispatch вернёт 409.
    await_seed = PayoutRequisites(
        user_id=winner_id,
        phone="+79001234567",
        sbp_bank_id="100000000004",
        last_name="Иванов",
        first_name="Пётр",
    )
    ctx.requisites.items[winner_id] = await_seed

    # checker подтверждает
    ctx.holder["user"] = checker
    assert (
        ctx.client.post(f"/admin/payouts/{payout_id}/approve").json()["status"]
        == "approved"
    )

    # admin отправляет провайдеру → processing
    dispatched = ctx.client.post(f"/admin/payouts/{payout_id}/dispatch")
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["status"] == "processing"

    # вебхук провайдера (без авторизации) → paid
    ctx.holder["user"] = None
    webhook = ctx.client.post(
        "/webhooks/payouts/yookassa",
        json={
            "provider": "yookassa",
            "provider_payout_id": f"po-{payout_id}",
            "succeeded": True,
        },
    )
    assert webhook.status_code == 200, webhook.text
    assert webhook.json()["status"] == "paid"


def test_list_payouts_admin_only(ctx: Ctx) -> None:
    """GET /admin/payouts: admin видит список; обычный пользователь — 403."""
    maker = _user(UserRole.ADMIN)
    ctx.holder["user"] = maker
    fund_id = ctx.client.post(
        "/admin/prize-funds",
        json={"sponsor_name": "Acme", "committed_kopecks": 1_000_000},
    ).json()["id"]
    ctx.client.post(
        f"/admin/prize-funds/{fund_id}/deposit", json={"amount_kopecks": 1_000_000}
    )
    ctx.client.post(
        "/admin/payouts",
        json={
            "user_id": str(uuid.uuid4()),
            "prize_fund_id": fund_id,
            "amount_kopecks": 5_000,
        },
    )

    listed = ctx.client.get("/admin/payouts")
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1

    ctx.holder["user"] = _user(UserRole.USER)
    forbidden = ctx.client.get("/admin/payouts")
    assert forbidden.status_code == status.HTTP_403_FORBIDDEN


def test_season_prize_fund_public_transparency(ctx: Ctx) -> None:
    """GET /seasons/{slug}/prize-fund — публичная прозрачность фонда по сезону."""
    season_id = uuid.uuid4()
    ctx.seasons.set("2026q1", season_id)

    admin = _user(UserRole.ADMIN)
    ctx.holder["user"] = admin
    fund_resp = ctx.client.post(
        "/admin/prize-funds",
        json={
            "sponsor_name": "Acme",
            "committed_kopecks": 1_000_000,
            "season_id": str(season_id),
        },
    )
    assert fund_resp.status_code == 201, fund_resp.text
    fund_id = fund_resp.json()["id"]
    ctx.client.post(
        f"/admin/prize-funds/{fund_id}/deposit", json={"amount_kopecks": 1_000_000}
    )

    # Публично (без авторизации).
    ctx.holder["user"] = None
    resp = ctx.client.get("/seasons/2026q1/prize-fund")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["season_slug"] == "2026q1"
    assert len(body["funds"]) == 1
    assert body["funds"][0]["balance_kopecks"] == 1_000_000


def test_season_prize_fund_unknown_season_404(ctx: Ctx) -> None:
    resp = ctx.client.get("/seasons/nope/prize-fund")
    assert resp.status_code == 404


def test_admin_endpoint_requires_auth(ctx: Ctx) -> None:
    ctx.holder["user"] = None
    resp = ctx.client.post(
        "/admin/prize-funds",
        json={"sponsor_name": "Acme", "committed_kopecks": 1},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_non_admin_cannot_create_fund(ctx: Ctx) -> None:
    ctx.holder["user"] = _user(UserRole.USER)
    resp = ctx.client.post(
        "/admin/prize-funds",
        json={"sponsor_name": "Acme", "committed_kopecks": 1},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN


# ── Вебхук ТБанк ──────────────────────────────────────────────────────────


def _tbank_notif(password: str, **fields) -> dict:
    body = {"TerminalKey": "TDEMO", **fields}
    body["Token"] = make_token(body, password)
    return body


def _new_tbank_subscription(ctx: Ctx) -> str:
    ctx.holder["user"] = _user(UserRole.USER)
    start = ctx.client.post("/billing/subscriptions", json={"plan": "monthly"})
    assert start.status_code == 201, start.text
    return start.json()["subscription"]["id"]


def test_tbank_webhook_confirmed_records_payment(ctx: Ctx) -> None:
    """CONFIRMED → идемпотентный приём в OPERATIONS, ответ телом OK."""
    sub_id = _new_tbank_subscription(ctx)
    notif = _tbank_notif(
        "", OrderId=sub_id, Success=True, Status="CONFIRMED",
        PaymentId="tb-1", Amount=99_000,
    )

    resp = ctx.client.post("/webhooks/payments/tbank", json=notif)

    assert resp.status_code == 200, resp.text
    assert resp.text == "OK"
    assert len(ctx.payments.items) == 1
    assert ctx.payments.items[0].provider is PaymentProvider.TBANK
    # Повтор идемпотентен: второй платёж не создаётся.
    assert ctx.client.post("/webhooks/payments/tbank", json=notif).text == "OK"
    assert len(ctx.payments.items) == 1


def test_tbank_webhook_rejected_does_not_record(ctx: Ctx) -> None:
    """REJECTED → OK, но платёж не проводится (Тест 2 — неуспешная оплата)."""
    sub_id = _new_tbank_subscription(ctx)
    notif = _tbank_notif(
        "", OrderId=sub_id, Success=False, Status="REJECTED",
        PaymentId="tb-2", Amount=99_000,
    )

    resp = ctx.client.post("/webhooks/payments/tbank", json=notif)

    assert resp.status_code == 200 and resp.text == "OK"
    assert ctx.payments.items == []


def test_tbank_webhook_bad_token_401() -> None:
    """С заданным паролем терминала неверный Token → 401 (до use-case)."""
    from app.config import TBankSettings, get_settings

    base = get_settings()
    with_tbank = base.model_copy(
        update={"tbank": TBankSettings(enabled=False, password="realpass")}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: with_tbank
    with TestClient(app) as client:
        body = {
            "TerminalKey": "TDEMO", "OrderId": str(uuid.uuid4()),
            "Success": True, "Status": "CONFIRMED", "PaymentId": "tb-3",
            "Amount": 99_000, "Token": "bad",
        }
        assert client.post("/webhooks/payments/tbank", json=body).status_code == 401


def _tbank_paid(ctx: Ctx, ref: str) -> str:
    sub_id = _new_tbank_subscription(ctx)
    ctx.client.post(
        "/webhooks/payments/tbank",
        json=_tbank_notif(
            "", OrderId=sub_id, Success=True, Status="CONFIRMED",
            PaymentId=ref, Amount=99_000,
        ),
    )
    return str(ctx.payments.items[0].id)


def test_refund_endpoint_admin_refunds(ctx: Ctx) -> None:
    """Админ возвращает платёж ТБанк → статус refunded (Тест 3 — возврат)."""
    payment_id = _tbank_paid(ctx, "tb-r1")

    ctx.holder["user"] = _user(UserRole.ADMIN)
    resp = ctx.client.post(f"/billing/payments/{payment_id}/refund")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "refunded"


def test_refund_endpoint_forbidden_for_user(ctx: Ctx) -> None:
    """Обычный пользователь не может вернуть платёж → 403."""
    payment_id = _tbank_paid(ctx, "tb-r2")  # роль остаётся USER

    resp = ctx.client.post(f"/billing/payments/{payment_id}/refund")

    assert resp.status_code == status.HTTP_403_FORBIDDEN


# ── Реквизиты выплат (СБП) и авто-dispatch Jump ────────────────────────────


_REQUISITES_BODY = {
    "sbp_phone": "8 (900) 123-45-67",
    "sbp_bank_id": "100000000004",
    "last_name": "Иванов",
    "first_name": "Пётр",
    "middle_name": None,
}


def test_requisites_require_auth(ctx: Ctx) -> None:
    """Реквизиты выплат — только для владельца сессии (401 без входа)."""
    assert (
        ctx.client.get("/users/me/payout-requisites").status_code
        == status.HTTP_401_UNAUTHORIZED
    )
    assert (
        ctx.client.put(
            "/users/me/payout-requisites", json=_REQUISITES_BODY
        ).status_code
        == status.HTTP_401_UNAUTHORIZED
    )


def test_requisites_404_until_saved_then_roundtrip(ctx: Ctx) -> None:
    """PUT сохраняет реквизиты (телефон нормализуется), GET возвращает их."""
    ctx.holder["user"] = _user(UserRole.USER)

    assert (
        ctx.client.get("/users/me/payout-requisites").status_code
        == status.HTTP_404_NOT_FOUND
    )

    saved = ctx.client.put("/users/me/payout-requisites", json=_REQUISITES_BODY)
    assert saved.status_code == 200, saved.text
    assert saved.json()["sbp_phone"] == "+79001234567"

    fetched = ctx.client.get("/users/me/payout-requisites")
    assert fetched.status_code == 200
    assert fetched.json()["sbp_bank_id"] == "100000000004"
    assert fetched.json()["last_name"] == "Иванов"

    # Повторный PUT обновляет запись, а не плодит вторую.
    updated = ctx.client.put(
        "/users/me/payout-requisites",
        json={**_REQUISITES_BODY, "sbp_bank_id": "100000000111"},
    )
    assert updated.status_code == 200
    assert updated.json()["sbp_bank_id"] == "100000000111"
    assert updated.json()["id"] == saved.json()["id"]


def test_requisites_invalid_phone_rejected(ctx: Ctx) -> None:
    """Мусорный телефон СБП → 422 с доменной ошибкой."""
    ctx.holder["user"] = _user(UserRole.USER)
    resp = ctx.client.put(
        "/users/me/payout-requisites",
        json={**_REQUISITES_BODY, "sbp_phone": "не телефон"},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_dispatch_without_requisites_conflicts(ctx: Ctx) -> None:
    """Dispatch выплаты получателю без реквизитов → 409, статус не меняется."""
    maker = _user(UserRole.ADMIN)
    checker = _user(UserRole.ADMIN)

    ctx.holder["user"] = maker
    fund_id = ctx.client.post(
        "/admin/prize-funds",
        json={"sponsor_name": "Acme", "committed_kopecks": 1_000_000},
    ).json()["id"]
    ctx.client.post(
        f"/admin/prize-funds/{fund_id}/deposit", json={"amount_kopecks": 1_000_000}
    )
    payout_id = ctx.client.post(
        "/admin/payouts",
        json={
            "user_id": str(uuid.uuid4()),
            "prize_fund_id": fund_id,
            "amount_kopecks": 8_700,
        },
    ).json()["id"]
    ctx.holder["user"] = checker
    ctx.client.post(f"/admin/payouts/{payout_id}/approve")

    resp = ctx.client.post(f"/admin/payouts/{payout_id}/dispatch")

    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    listed = ctx.client.get("/admin/payouts").json()
    assert [p["status"] for p in listed if p["id"] == payout_id] == ["approved"]


def test_payout_response_exposes_provider_fields(ctx: Ctx) -> None:
    """Проекция выплаты содержит провайдера и его ссылку (нужно админке)."""
    maker = _user(UserRole.ADMIN)
    ctx.holder["user"] = maker
    fund_id = ctx.client.post(
        "/admin/prize-funds",
        json={"sponsor_name": "Acme", "committed_kopecks": 1_000_000},
    ).json()["id"]
    payout = ctx.client.post(
        "/admin/payouts",
        json={
            "user_id": str(uuid.uuid4()),
            "prize_fund_id": fund_id,
            "amount_kopecks": 100,
        },
    ).json()
    assert payout["provider"] is None
    assert payout["provider_payout_id"] is None
    assert payout["paid_at"] is None
    assert payout["created_at"] is not None
