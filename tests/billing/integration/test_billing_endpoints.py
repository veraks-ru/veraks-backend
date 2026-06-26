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
    get_payout_repository,
    get_prize_fund_repository,
    get_season_directory,
    get_subscription_repository,
)
from app.modules.billing.domain import chart
from app.modules.billing.domain.ledger import LedgerAccount, LedgerType
from app.modules.identity.api.dependencies import get_current_user
from app.modules.identity.domain.entities import User, UserRole
from tests.billing.conftest import FIXED_NOW
from tests.billing.fakes import (
    FakeAuditTrail,
    FakeCheckoutGateway,
    FakeClock,
    FakePayoutGateway,
    FakeSeasonDirectory,
    InMemoryLedgerRepository,
    InMemoryPaymentRepository,
    InMemoryPayoutRepository,
    InMemoryPrizeFundRepository,
    InMemorySubscriptionRepository,
)

_SEED = [
    (LedgerType.OPERATIONS, chart.OPS_CASH_YOOKASSA),
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
    funds: InMemoryPrizeFundRepository
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
    audit = FakeAuditTrail()
    seasons = FakeSeasonDirectory()
    holder: dict = {"user": None}

    app = create_app()
    app.dependency_overrides[get_ledger_repository] = lambda: ledger
    app.dependency_overrides[get_subscription_repository] = lambda: subscriptions
    app.dependency_overrides[get_payment_repository] = lambda: payments
    app.dependency_overrides[get_prize_fund_repository] = lambda: funds
    app.dependency_overrides[get_payout_repository] = lambda: payouts
    app.dependency_overrides[get_audit_trail] = lambda: audit
    app.dependency_overrides[get_checkout_gateway] = lambda: FakeCheckoutGateway()
    app.dependency_overrides[get_payout_gateway] = lambda: FakePayoutGateway()
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
            client=client, ledger=ledger, funds=funds, seasons=seasons, holder=holder
        )
    finally:
        client.close()


def test_list_plans_returns_priced_tariffs(ctx: Ctx) -> None:
    """``GET /billing/plans`` отдаёт тарифы с ценами в копейках (публично)."""
    resp = ctx.client.get("/billing/plans")
    assert resp.status_code == 200, resp.text
    by_plan = {p["plan"]: p["price_kopecks"] for p in resp.json()["plans"]}
    assert by_plan["monthly"] == 49_000
    assert by_plan["annual"] == 490_000


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
