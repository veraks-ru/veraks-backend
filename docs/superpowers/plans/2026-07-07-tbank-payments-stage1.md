# ТБанк эквайринг (этап 1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Принимать платежи за подписку через ТБанк (hosted-форма банка, nonPCI) и пройти 5 тестов сертификации ТБанка (успех/отказ/возврат/чек/чек-возврата) на демо-кластере veraks.ru.

**Architecture:** Реализуем существующий порт `SubscriptionCheckoutGateway` адаптером ТБанк (`Init` → `PaymentURL`), приём оплаты — через новый вебхук `/webhooks/payments/tbank` (проверка `Token`) → существующий `RecordSubscriptionPayment`. Возврат — новый `PaymentRefundGateway` (`/v2/Cancel`) + use-case со сторно-проводкой в кассе OPERATIONS. Выбор адаптера — в composition root по `settings.tbank.enabled`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, pydantic-settings, httpx (уже в зависимостях, `>=0.27`), ARQ, Alembic, pytest/mypy/ruff.

## Global Constraints

- Деньги — только `amount_kopecks: int`, никогда float. `Amount` в ТБанк — копейки.
- Провайдер платежа — `PaymentProvider.TBANK = "tbank"` (уже есть в домене и enum БД).
- Кассы разделены схемой (триггеры): проводка подписки/возврата — целиком в кассе OPERATIONS.
- `Token` ТБанк: только корневые скалярные поля + `{"Password": …}` → сортировка по ключу → конкатенация значений → SHA-256 (UTF-8) hex. `Receipt`/`DATA`/вложенное — исключаются.
- Вебхук отвечает строго `HTTP 200` телом `OK`; приём идемпотентен (по `(provider, provider_payment_id)`, есть `UNIQUE` в БД).
- Секреты (`TBANK_PASSWORD`, `TBANK_TERMINAL_KEY`) — только в env/K8s-секрете, НЕ в git.
- Путь вебхука — `/webhooks/payments/tbank` (без префикса `/billing`, как у yookassa-вебхука).
- Комментарии/докстринги — на русском (конвенция репозитория).
- Внешние HTTP-адаптеры инъектят `httpx.AsyncClient` (паттерн `identity/adapters/esia_gateway.py`).
- После каждой задачи: `pytest`, `mypy app`, `ruff check app tests` — чисто.

---

## File Structure

- `app/config.py` — **modify**: `TBankSettings` + URL-базы `public_web_base`/`public_api_base` + валидатор.
- `app/modules/billing/domain/tbank_signing.py` — **create**: `make_token`/`verify_token`.
- `app/modules/billing/domain/chart.py` — **modify**: `OPS_CASH_TBANK`.
- `app/modules/billing/domain/ledger.py` — **modify**: `TransactionKind.SUBSCRIPTION_REFUND` (→ OPERATIONS).
- `app/modules/billing/domain/receipt.py` — **create**: сборка объекта `Receipt` (54-ФЗ).
- `app/modules/billing/ports/gateways.py` — **modify**: `PaymentRefundGateway`, `RefundResult`.
- `app/modules/billing/adapters/tbank_gateway.py` — **create**: `TBankGateway` (checkout `Init` + refund `Cancel`).
- `app/modules/billing/application/use_cases.py` — **modify**: выбор кэш-счёта по провайдеру в `RecordSubscriptionPayment`; новый `RefundSubscriptionPayment`.
- `app/modules/billing/api/schemas.py` — **modify**: `TBankNotification`, `RefundResponse`.
- `app/modules/billing/api/router.py` — **modify**: вебхук `/webhooks/payments/tbank`; админ-эндпоинт возврата.
- `app/modules/billing/api/dependencies.py` — **modify**: HTTP-клиент, выбор адаптера, `verify_tbank_webhook`, wiring refund.
- `alembic/versions/00XX_tbank_ops_account.py` — **create**: сид счёта `ops:cash:tbank`; partial UNIQUE на refund `external_ref`.
- `tests/billing/unit/test_tbank_signing.py`, `.../test_tbank_gateway.py`, `.../test_refund.py`, `tests/billing/integration/test_tbank_webhook.py` — **create**.
- `web/dev/...` — не трогаем (фронт уже редиректит на `confirmation_url`).
- `../infra/helm/veraks/{values.yaml,templates/config.yaml,templates/secret.yaml}` — **modify** (в задаче деплоя): `TBANK_*`.

---

## Task 1: Конфиг TBankSettings + URL-базы

**Files:**
- Modify: `app/config.py`
- Test: `tests/billing/unit/test_config_tbank.py`

**Interfaces:**
- Produces: `settings.tbank.{terminal_key,password,api_base_url,taxation,enabled}`, `settings.public_web_base`, `settings.public_api_base`.

- [ ] **Step 1: Тест загрузки настроек**

```python
# tests/billing/unit/test_config_tbank.py
from app.config import TBankSettings

def test_tbank_settings_defaults_and_env(monkeypatch):
    monkeypatch.setenv("TBANK_TERMINAL_KEY", "1783427792728DEMO")
    monkeypatch.setenv("TBANK_PASSWORD", "secret")
    s = TBankSettings()
    assert s.terminal_key == "1783427792728DEMO"
    assert s.password == "secret"
    assert s.api_base_url == "https://securepay.tinkoff.ru/v2"
    assert s.taxation == "usn_income"
```

- [ ] **Step 2: Запустить — падает** — `Run: pytest tests/billing/unit/test_config_tbank.py -v` → FAIL (нет `TBankSettings`).

- [ ] **Step 3: Реализация** — в `app/config.py` рядом с `BillingSettings`:

```python
class TBankSettings(BaseSettings):
    """Эквайринг ТБанк (приём платежей). Секреты — из env/секрета, не в git."""
    model_config = SettingsConfigDict(env_prefix="TBANK_", extra="ignore")
    enabled: bool = False
    terminal_key: str = ""
    password: str = ""
    api_base_url: str = "https://securepay.tinkoff.ru/v2"
    taxation: str = "usn_income"  # СНО для чека 54-ФЗ (ИП на УСН «доходы»)
```

В корневых `Settings` добавить поля (рядом с `billing`, `webhooks`):

```python
    tbank: TBankSettings = Field(default_factory=TBankSettings)
    public_web_base: str = "https://veraks.ru"     # для Success/Fail URL
    public_api_base: str = "https://api.veraks.ru"  # для NotificationURL
```

В fail-closed валидаторе (не-`local`): если `tbank.enabled`, требовать непустые `tbank.terminal_key` и `tbank.password` (по образцу проверки webhook-секретов).

- [ ] **Step 4: Запустить — проходит** — `Run: pytest tests/billing/unit/test_config_tbank.py -v` → PASS.

- [ ] **Step 5: .env.example** — добавить строки: `TBANK_ENABLED=false`, `# TBANK_TERMINAL_KEY=…DEMO`, `# TBANK_PASSWORD=…`.

- [ ] **Step 6: Commit** — `git add app/config.py tests/billing/unit/test_config_tbank.py .env.example && git commit -m "feat(billing): настройки эквайринга ТБанк (TBankSettings)"`

---

## Task 2: Подпись Token (домен)

**Files:**
- Create: `app/modules/billing/domain/tbank_signing.py`
- Test: `tests/billing/unit/test_tbank_signing.py`

**Interfaces:**
- Produces: `make_token(params: Mapping[str, object], password: str) -> str`; `verify_token(payload: Mapping[str, object], password: str) -> bool`.

- [ ] **Step 1: Тесты (алгоритм + эталон)**

```python
# tests/billing/unit/test_tbank_signing.py
import hashlib
from app.modules.billing.domain.tbank_signing import make_token, verify_token

def _expected(*, amount, description, order_id, password, terminal_key):
    # Отсортированные по ключу значения: Amount, Description, OrderId, Password, TerminalKey
    raw = f"{amount}{description}{order_id}{password}{terminal_key}"
    return hashlib.sha256(raw.encode()).hexdigest()

def test_make_token_sorts_scalars_and_appends_password():
    params = {
        "TerminalKey": "T", "Amount": 100000, "OrderId": "o-1",
        "Description": "Подписка", "Receipt": {"x": 1}, "DATA": {"y": 2},
    }
    token = make_token(params, "pass")
    assert token == _expected(
        amount=100000, description="Подписка", order_id="o-1",
        password="pass", terminal_key="T",
    )

def test_make_token_excludes_token_receipt_data_and_nested():
    a = make_token({"TerminalKey": "T", "Amount": 1, "Token": "old"}, "p")
    b = make_token({"TerminalKey": "T", "Amount": 1}, "p")
    assert a == b

def test_make_token_bool_lowercased():
    t = make_token({"TerminalKey": "T", "Recurrent": True}, "p")
    raw = f"{'true'}p{'T'}"  # ключи: Recurrent, Password, TerminalKey → sorted
    # порядок sorted по ключу: Password, Recurrent, TerminalKey
    import hashlib as h
    assert t == h.sha256(f"p{'true'}{'T'}".encode()).hexdigest()

def test_verify_token_roundtrip():
    payload = {"TerminalKey": "T", "OrderId": "o", "Success": True,
               "Status": "CONFIRMED", "PaymentId": "42", "Amount": 100000}
    payload["Token"] = make_token(payload, "p")
    assert verify_token(payload, "p") is True
    payload["Amount"] = 999
    assert verify_token(payload, "p") is False

def test_verify_token_missing_token_is_false():
    assert verify_token({"TerminalKey": "T"}, "p") is False
```

- [ ] **Step 2: Запустить — падает** — `Run: pytest tests/billing/unit/test_tbank_signing.py -v` → FAIL (модуля нет).

- [ ] **Step 3: Реализация**

```python
# app/modules/billing/domain/tbank_signing.py
"""Подпись запросов и проверка уведомлений ТБанк (Token, SHA-256).

Алгоритм (developer.tbank.ru/eacq/intro/developer/token): берём только скалярные
поля корневого объекта (вложенные объекты/массивы — Receipt, DATA, Shops —
исключаются), добавляем пару Password, сортируем по ключу, конкатенируем значения
без разделителей, SHA-256 (UTF-8) → hex.
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

_EXCLUDED = frozenset({"Token", "Receipt", "DATA", "Shops", "Receipts"})


def _stringify(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _digest(params: Mapping[str, object], password: str) -> str:
    scalar = {
        k: v
        for k, v in params.items()
        if k not in _EXCLUDED and not isinstance(v, (dict, list, tuple))
    }
    scalar["Password"] = password
    concatenated = "".join(_stringify(scalar[k]) for k in sorted(scalar))
    return hashlib.sha256(concatenated.encode("utf-8")).hexdigest()


def make_token(params: Mapping[str, object], password: str) -> str:
    """Подпись исходящего запроса (Init/Cancel и т.п.)."""
    return _digest(params, password)


def verify_token(payload: Mapping[str, object], password: str) -> bool:
    """Проверка Token входящего уведомления (constant-time)."""
    provided = payload.get("Token")
    if not isinstance(provided, str) or not provided:
        return False
    expected = _digest(payload, password)
    return hmac.compare_digest(expected, provided)
```

- [ ] **Step 4: Запустить — проходит** — `Run: pytest tests/billing/unit/test_tbank_signing.py -v` → PASS. Затем `mypy app` / `ruff check app tests`.

- [ ] **Step 5: Commit** — `git commit -am "feat(billing): подпись Token ТБанк (make_token/verify_token)"`

---

## Task 3: Счёт ops:cash:tbank + вид SUBSCRIPTION_REFUND + миграция

**Files:**
- Modify: `app/modules/billing/domain/chart.py`, `app/modules/billing/domain/ledger.py`
- Create: `alembic/versions/00XX_tbank_ops_account.py`
- Test: `tests/billing/unit/test_ledger_posting.py` (дополнить)

**Interfaces:**
- Produces: `chart.OPS_CASH_TBANK = "ops:cash:tbank"`; `TransactionKind.SUBSCRIPTION_REFUND` (→ `LedgerType.OPERATIONS`).

- [ ] **Step 1: Тест** — вид SUBSCRIPTION_REFUND привязан к OPERATIONS; проводка возврата (дебет выручки, кредит `ops:cash:tbank`) валидна.

```python
def test_subscription_refund_kind_is_operations():
    from app.modules.billing.domain.ledger import TransactionKind, ledger_of
    assert ledger_of(TransactionKind.SUBSCRIPTION_REFUND).value == "operations"
```
(Если хелпера `ledger_of` нет — проверить через существующий механизм `_KIND_LEDGER`.)

- [ ] **Step 2: Запустить — падает.**

- [ ] **Step 3: Реализация** — в `chart.py`: `OPS_CASH_TBANK = "ops:cash:tbank"`. В `ledger.py`: добавить в `TransactionKind` значение `SUBSCRIPTION_REFUND = "subscription_refund"` и в `_KIND_LEDGER` пару `SUBSCRIPTION_REFUND: LedgerType.OPERATIONS`.

- [ ] **Step 4: Миграция** — `alembic revision -m "tbank ops account"`; в `upgrade()`: `INSERT INTO ledger_accounts(account_code, ledger_type, ...) VALUES ('ops:cash:tbank','operations',...)` (по образцу `_SEED_ACCOUNTS` из 0010); добавить `ALTER TYPE transaction_kind ADD VALUE IF NOT EXISTS 'subscription_refund'` (autocommit-блок, как в 0012); partial UNIQUE `uq_ledger_txn_sub_refund_ref ON ledger_transactions(external_ref) WHERE kind='subscription_refund'`. В `downgrade()` — удалить счёт и индекс (enum-значение не откатываем).

- [ ] **Step 5: Прогнать миграцию на тест-БД / проверить** — `Run: alembic upgrade head` (в e2e-окружении) или юнит на наличие константы.

- [ ] **Step 6: Commit** — `git commit -am "feat(billing): счёт ops:cash:tbank и вид проводки subscription_refund"`

---

## Task 4: RecordSubscriptionPayment — кэш-счёт по провайдеру

**Files:**
- Modify: `app/modules/billing/application/use_cases.py` (RecordSubscriptionPayment)
- Test: `tests/billing/unit/test_use_cases.py` (дополнить)

**Interfaces:**
- Consumes: `chart.OPS_CASH_TBANK`, `chart.OPS_CASH_YOOKASSA`.

- [ ] **Step 1: Тест** — платёж с `provider=TBANK` проводится в дебет `ops:cash:tbank`.

```python
async def test_record_tbank_payment_posts_to_tbank_cash(stand):
    sub = await stand.start_subscription(plan=SubscriptionPlan.MONTHLY)
    await stand.record_payment(
        provider=PaymentProvider.TBANK, provider_payment_id="tb-1",
        amount_kopecks=sub.price_kopecks, subscription_id=sub.id,
    )
    assert await stand.ledger.balance("ops:cash:tbank") == sub.price_kopecks
```

- [ ] **Step 2: Запустить — падает** (счёт не тот).

- [ ] **Step 3: Реализация** — в `RecordSubscriptionPayment.execute` заменить жёсткий `chart.OPS_CASH_YOOKASSA` на выбор по провайдеру:

```python
_CASH_ACCOUNT: dict[PaymentProvider, str] = {
    PaymentProvider.YOOKASSA: chart.OPS_CASH_YOOKASSA,
    PaymentProvider.TBANK: chart.OPS_CASH_TBANK,
}
...
cash = await self._ledger.account(_CASH_ACCOUNT[provider])
```

- [ ] **Step 4: Запустить — проходит.** Прогнать весь `tests/billing`.

- [ ] **Step 5: Commit** — `git commit -am "feat(billing): проводка приёма — кэш-счёт по провайдеру"`

---

## Task 5: Порт возврата (PaymentRefundGateway)

**Files:**
- Modify: `app/modules/billing/ports/gateways.py`

**Interfaces:**
- Produces: `RefundResult(provider_payment_id: str, status: str)`; `PaymentRefundGateway.cancel_payment(*, provider_payment_id: str, amount_kopecks: int, receipt: dict | None) -> RefundResult`.

- [ ] **Step 1: Реализация** (интерфейс — тестируется через адаптер в Task 6):

```python
@dataclass(frozen=True, slots=True)
class RefundResult:
    """Результат возврата у провайдера."""
    provider_payment_id: str
    status: str

@runtime_checkable
class PaymentRefundGateway(Protocol):
    """Возврат/отмена платежа у провайдера (операционка)."""
    async def cancel_payment(
        self, *, provider_payment_id: str, amount_kopecks: int, receipt: dict | None,
    ) -> RefundResult:
        ...
```

- [ ] **Step 2: mypy/ruff чисто** — `Run: mypy app && ruff check app`.

- [ ] **Step 3: Commit** — `git commit -am "feat(billing): порт возврата платежа (PaymentRefundGateway)"`

---

## Task 6: Адаптер ТБанк (Init + Cancel + Receipt)

**Files:**
- Create: `app/modules/billing/adapters/tbank_gateway.py`
- Create: `app/modules/billing/domain/receipt.py`
- Test: `tests/billing/unit/test_tbank_gateway.py`

**Interfaces:**
- Consumes: `TBankSettings`, `make_token`, `SubscriptionCheckoutGateway`, `PaymentRefundGateway`, `CheckoutIntent`, `RefundResult`, `httpx.AsyncClient`.
- Produces: `TBankGateway(settings, client, *, notification_url, success_url, fail_url)` реализует оба протокола; `build_receipt(*, description, amount_kopecks, taxation, email, phone) -> dict`.

- [ ] **Step 1: Тесты (мок httpx)** — Init формирует корректное тело+Token и возвращает PaymentURL; ошибка Init → PaymentGatewayError; Cancel шлёт PaymentId+Amount+Token.

```python
# tests/billing/unit/test_tbank_gateway.py
import httpx, pytest, uuid
from app.config import TBankSettings
from app.modules.billing.adapters.tbank_gateway import TBankGateway
from app.modules.billing.domain.tbank_signing import make_token

def _client(handler): return httpx.AsyncClient(transport=httpx.MockTransport(handler))

def _settings(): return TBankSettings(enabled=True, terminal_key="TDEMO", password="p",
                                      api_base_url="https://pay.test/v2")

async def test_init_builds_request_and_returns_payment_url():
    captured = {}
    def handler(req: httpx.Request) -> httpx.Response:
        import json; body = json.loads(req.content); captured.update(body)
        return httpx.Response(200, json={"Success": True, "Status": "NEW",
            "PaymentId": "900", "PaymentURL": "https://pay.test/form/900"})
    sub_id = uuid.uuid4()
    gw = TBankGateway(_settings(), _client(handler),
        notification_url="https://api.veraks.ru/webhooks/payments/tbank",
        success_url="https://veraks.ru/account", fail_url="https://veraks.ru/account")
    intent = await gw.create_checkout(subscription_id=sub_id, amount_kopecks=99000,
                                      description="Подписка monthly")
    assert intent.confirmation_url == "https://pay.test/form/900"
    assert intent.provider_subscription_id == "900"
    assert captured["Amount"] == 99000
    assert captured["OrderId"] == str(sub_id)
    assert captured["TerminalKey"] == "TDEMO"
    # Token корректен для скалярных полей
    assert captured["Token"] == make_token(captured, "p")

async def test_init_failure_raises():
    def handler(req): return httpx.Response(200, json={"Success": False,
        "ErrorCode": "1", "Message": "Отказ"})
    gw = TBankGateway(_settings(), _client(handler), notification_url="n",
                      success_url="s", fail_url="f")
    from app.modules.billing.domain.errors import PaymentGatewayError
    with pytest.raises(PaymentGatewayError):
        await gw.create_checkout(subscription_id=uuid.uuid4(), amount_kopecks=1,
                                 description="x")

async def test_cancel_sends_payment_id_and_amount():
    captured = {}
    def handler(req):
        import json; captured.update(json.loads(req.content))
        return httpx.Response(200, json={"Success": True, "Status": "REFUNDED",
            "PaymentId": "900"})
    gw = TBankGateway(_settings(), _client(handler), notification_url="n",
                      success_url="s", fail_url="f")
    res = await gw.cancel_payment(provider_payment_id="900", amount_kopecks=99000,
                                  receipt=None)
    assert res.status == "REFUNDED"
    assert captured["PaymentId"] == "900"
    assert captured["Amount"] == 99000
    assert captured["Token"] == make_token(captured, "p")
```

- [ ] **Step 2: Запустить — падает** (адаптера нет).

- [ ] **Step 3: Receipt-билдер** — `app/modules/billing/domain/receipt.py`:

```python
"""Сборка объекта Receipt для чека 54-ФЗ (ТБанк)."""
from __future__ import annotations

def build_receipt(*, description: str, amount_kopecks: int, taxation: str,
                  email: str | None, phone: str | None) -> dict:
    contact: dict[str, str] = {}
    if email: contact["Email"] = email
    if phone: contact["Phone"] = phone
    return {
        **contact,
        "Taxation": taxation,
        "Items": [{
            "Name": description[:128],
            "Price": amount_kopecks,
            "Quantity": 1,
            "Amount": amount_kopecks,
            "Tax": "none",                 # ИП на УСН, без НДС
            "PaymentMethod": "full_payment",
            "PaymentObject": "service",
        }],
    }
```

- [ ] **Step 4: Адаптер** — `app/modules/billing/adapters/tbank_gateway.py`:

```python
"""Адаптер эквайринга ТБанк: Init (создание платежа) и Cancel (возврат).
Hosted-форма банка (nonPCI): backend вызывает Init → PaymentURL → фронт редиректит.
"""
from __future__ import annotations

import uuid
import httpx

from app.config import TBankSettings
from app.modules.billing.domain.errors import PaymentGatewayError
from app.modules.billing.domain.tbank_signing import make_token
from app.modules.billing.ports.gateways import CheckoutIntent, RefundResult


class TBankGateway:
    """Реализует SubscriptionCheckoutGateway и PaymentRefundGateway для ТБанк."""

    def __init__(self, settings: TBankSettings, client: httpx.AsyncClient, *,
                 notification_url: str, success_url: str, fail_url: str) -> None:
        self._s = settings
        self._client = client
        self._notification_url = notification_url
        self._success_url = success_url
        self._fail_url = fail_url

    async def _post(self, method: str, payload: dict) -> dict:
        payload = {**payload, "Token": make_token(payload, self._s.password)}
        try:
            resp = await self._client.post(f"{self._s.api_base_url}/{method}", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaymentGatewayError(f"ТБанк {method}: сетевая ошибка: {exc}") from exc
        if not data.get("Success", False):
            raise PaymentGatewayError(
                f"ТБанк {method}: {data.get('ErrorCode')} {data.get('Message')}"
            )
        return data

    async def create_checkout(self, *, subscription_id: uuid.UUID,
                              amount_kopecks: int, description: str) -> CheckoutIntent:
        payload: dict = {
            "TerminalKey": self._s.terminal_key,
            "Amount": amount_kopecks,
            "OrderId": str(subscription_id),
            "Description": description[:140],
            "PayType": "O",
            "NotificationURL": self._notification_url,
            "SuccessURL": self._success_url,
            "FailURL": self._fail_url,
        }
        # Receipt добавляется на этапе wiring, если известен контакт плательщика.
        data = await self._post("Init", payload)
        return CheckoutIntent(
            confirmation_url=str(data["PaymentURL"]),
            provider_subscription_id=str(data["PaymentId"]),
        )

    async def cancel_payment(self, *, provider_payment_id: str, amount_kopecks: int,
                             receipt: dict | None) -> RefundResult:
        payload: dict = {
            "TerminalKey": self._s.terminal_key,
            "PaymentId": provider_payment_id,
            "Amount": amount_kopecks,
        }
        if receipt is not None:
            payload["Receipt"] = receipt
        data = await self._post("Cancel", payload)
        return RefundResult(provider_payment_id=str(data["PaymentId"]),
                            status=str(data.get("Status", "REFUNDED")))
```

> Если `PaymentGatewayError` ещё нет в `domain/errors.py` — добавить как подкласс `BillingError` и замапить в `app/main.py` `_ERROR_STATUS` на 502.

- [ ] **Step 5: Запустить — проходит** — `Run: pytest tests/billing/unit/test_tbank_gateway.py -v`. `mypy app && ruff check app tests`.

- [ ] **Step 6: Commit** — `git commit -am "feat(billing): адаптер ТБанк — Init + Cancel + Receipt"`

---

## Task 7: Use-case возврата (RefundSubscriptionPayment)

**Files:**
- Modify: `app/modules/billing/application/use_cases.py`
- Test: `tests/billing/unit/test_refund.py`

**Interfaces:**
- Consumes: `PaymentRefundGateway`, репозиторий платежей, леджер, аудит, clock, `chart.OPS_CASH_TBANK`, `chart.OPS_REVENUE_SUBSCRIPTIONS`, `TransactionKind.SUBSCRIPTION_REFUND`.
- Produces: `RefundSubscriptionPayment.execute(*, payment_id: uuid.UUID, actor_id: uuid.UUID) -> Payment`.

- [ ] **Step 1: Тест** — возврат успешного платежа: сторно-проводка (дебет выручки, кредит `ops:cash:tbank`), статус `REFUNDED`, повторный возврат запрещён.

```python
async def test_refund_reverses_operations_and_marks_refunded(stand):
    sub = await stand.start_subscription(plan=SubscriptionPlan.MONTHLY)
    pay = await stand.record_payment(provider=PaymentProvider.TBANK,
        provider_payment_id="tb-9", amount_kopecks=sub.price_kopecks, subscription_id=sub.id)
    refunded = await stand.refund_payment(payment_id=pay.id, actor_id=stand.admin_id)
    assert refunded.status == PaymentStatus.REFUNDED
    assert await stand.ledger.balance("ops:cash:tbank") == 0  # приход и сторно
    with pytest.raises(Exception):
        await stand.refund_payment(payment_id=pay.id, actor_id=stand.admin_id)
```

- [ ] **Step 2: Запустить — падает.**

- [ ] **Step 3: Реализация** — класс `RefundSubscriptionPayment` в `use_cases.py`:
  1. Загрузить `Payment` по id; проверить `provider==TBANK`, `status==SUCCEEDED` (иначе доменная ошибка).
  2. Собрать `receipt` возврата (`build_receipt(...)`, `PaymentMethod`/`PaymentObject` те же) — если фискализация включена.
  3. `refund = await self._gateway.cancel_payment(provider_payment_id=payment.provider_payment_id, amount_kopecks=payment.amount_kopecks, receipt=receipt)`.
  4. Проводка `TransactionKind.SUBSCRIPTION_REFUND`: дебет `ops:revenue:subscriptions`, кредит `ops:cash:tbank`, `external_ref=f"{payment.provider_payment_id}:refund"`.
  5. `payment.status = PaymentStatus.REFUNDED`; сохранить; аудит `subscription.payment.refunded`.

- [ ] **Step 4: Запустить — проходит.** Прогнать `tests/billing`.

- [ ] **Step 5: Commit** — `git commit -am "feat(billing): возврат платежа ТБанк со сторно-проводкой"`

---

## Task 8: Вебхук ТБанк + проверка Token

**Files:**
- Modify: `app/modules/billing/api/schemas.py`, `app/modules/billing/api/router.py`, `app/modules/billing/api/dependencies.py`
- Test: `tests/billing/integration/test_tbank_webhook.py`

**Interfaces:**
- Consumes: `verify_token`, `RecordSubscriptionPayment`, `settings.tbank.password`.
- Produces: `POST /webhooks/payments/tbank` → `PlainTextResponse("OK")`.

- [ ] **Step 1: Интеграционные тесты** — CONFIRMED активирует подписку и возвращает `OK`; REJECTED не активирует; битый Token → 401; повтор идемпотентен.

```python
# tests/billing/integration/test_tbank_webhook.py
def _notif(password, **fields):
    from app.modules.billing.domain.tbank_signing import make_token
    body = {"TerminalKey": "TDEMO", **fields}
    body["Token"] = make_token(body, password)
    return body

def test_tbank_webhook_confirmed_activates_and_returns_ok(client, sub_id, price):
    body = _notif("p", OrderId=str(sub_id), Success=True, Status="CONFIRMED",
                  PaymentId="tb-1", Amount=price)
    r = client.post("/webhooks/payments/tbank", json=body)
    assert r.status_code == 200 and r.text == "OK"
    # подписка активна (через GET /billing/subscriptions/me)

def test_tbank_webhook_rejected_does_not_activate(client, sub_id, price):
    body = _notif("p", OrderId=str(sub_id), Success=False, Status="REJECTED",
                  PaymentId="tb-2", Amount=price)
    r = client.post("/webhooks/payments/tbank", json=body)
    assert r.status_code == 200 and r.text == "OK"

def test_tbank_webhook_bad_token_401(client, sub_id, price):
    body = {"TerminalKey": "TDEMO", "OrderId": str(sub_id), "Success": True,
            "Status": "CONFIRMED", "PaymentId": "tb-3", "Amount": price, "Token": "bad"}
    assert client.post("/webhooks/payments/tbank", json=body).status_code == 401
```

- [ ] **Step 2: Запустить — падает.**

- [ ] **Step 3: Схема** — `TBankNotification` в `schemas.py` (поля из доки, `extra="allow"` для запаса):

```python
class TBankNotification(BaseModel):
    model_config = ConfigDict(extra="allow")
    TerminalKey: str
    OrderId: str
    Success: bool
    Status: str
    PaymentId: str | int
    Amount: int
    ErrorCode: str | None = None
    RebillId: str | int | None = None
    Token: str
```

- [ ] **Step 4: Верификатор** — в `dependencies.py`: прочитать сырое тело (JSON или form), проверить `verify_token(payload, settings.tbank.password)`; провал → 401. (Тело парсим и как JSON, и как form-urlencoded.)

- [ ] **Step 5: Эндпоинт** — в `router.py`:

```python
@router.post("/webhooks/payments/tbank", summary="Вебхук ТБанк (приём платежа)")
async def tbank_payment_webhook(
    payload: Annotated[dict, Depends(verified_tbank_payload)],
    uc: Annotated[RecordSubscriptionPayment, Depends(get_record_subscription_payment)],
) -> PlainTextResponse:
    status = str(payload.get("Status"))
    if status in {"CONFIRMED", "AUTHORIZED"} and payload.get("Success"):
        await uc.execute(
            provider=PaymentProvider.TBANK,
            provider_payment_id=str(payload["PaymentId"]),
            amount_kopecks=int(payload["Amount"]),
            subscription_id=uuid.UUID(str(payload["OrderId"])),
        )
    # REJECTED/CANCELED/DEADLINE_EXPIRED — подписку не активируем.
    return PlainTextResponse("OK")
```

- [ ] **Step 6: Запустить — проходит.** `mypy app && ruff check app tests`.

- [ ] **Step 7: Commit** — `git commit -am "feat(billing): вебхук приёма платежа ТБанк (Token, OK)"`

---

## Task 9: Composition root — выбор адаптера + эндпоинт возврата

**Files:**
- Modify: `app/modules/billing/api/dependencies.py`, `app/modules/billing/api/router.py`, `app/modules/billing/api/schemas.py`
- Test: `tests/billing/integration/test_billing_endpoints.py` (дополнить)

**Interfaces:**
- Consumes: `TBankGateway`, `get_http_client`, `RefundSubscriptionPayment`.
- Produces: `get_checkout_gateway` возвращает `TBankGateway` при `tbank.enabled`; `POST /billing/payments/{payment_id}/refund` (admin).

- [ ] **Step 1: HTTP-клиент** — добавить в billing `dependencies.py` провайдер `get_http_client` (по образцу identity) или переиспользовать общий.

- [ ] **Step 2: Выбор checkout-адаптера**:

```python
def get_checkout_gateway(settings: SettingsDep,
                         client: Annotated[httpx.AsyncClient, Depends(get_http_client)]):
    if settings.app_env == "local":
        return LocalSubscriptionCheckoutGateway()
    if settings.tbank.enabled:
        return TBankGateway(settings.tbank, client,
            notification_url=f"{settings.public_api_base}/webhooks/payments/tbank",
            success_url=f"{settings.public_web_base}/account",
            fail_url=f"{settings.public_web_base}/account")
    return YookassaSubscriptionCheckoutGateway()
```

- [ ] **Step 3: Refund-провайдеры** — `get_refund_gateway` (тот же `TBankGateway`) и `get_refund_subscription_payment` (собрать use-case, admin-guard по RBAC как в payouts).

- [ ] **Step 4: Эндпоинт возврата** (admin) в `router.py`:

```python
@router.post("/billing/payments/{payment_id}/refund",
             dependencies=[Depends(require_admin)])
async def refund_payment(payment_id: uuid.UUID,
    actor: CurrentUserDep,
    uc: Annotated[RefundSubscriptionPayment, Depends(get_refund_subscription_payment)],
) -> RefundResponse:
    payment = await uc.execute(payment_id=payment_id, actor_id=actor.id)
    return RefundResponse.from_domain(payment)
```

- [ ] **Step 5: Тест интеграции** — при `tbank.enabled=True` (override settings) `POST /billing/subscriptions` дергает Init (мок httpx через override клиента) и возвращает `confirmation_url`; refund-эндпоинт доступен только admin.

- [ ] **Step 6: Запустить — проходит.** Полный `pytest`, `mypy app`, `ruff check app tests`.

- [ ] **Step 7: Commit** — `git commit -am "feat(billing): выбор адаптера ТБанк и эндпоинт возврата (admin)"`

---

## Task 10: Деплой-конфиг (infra) + секреты

**Files:**
- Modify (репо veraks-infra): `helm/veraks/values.yaml`, `helm/veraks/templates/config.yaml`, `helm/veraks/templates/secret.yaml`, `.github/workflows/deploy.yml`

- [ ] **Step 1: ConfigMap** — в `config.yaml` добавить `TBANK_ENABLED: "true"`, `TBANK_API_BASE_URL`, `TBANK_TAXATION: "usn_income"`, `PUBLIC_WEB_BASE: "https://veraks.ru"`, `PUBLIC_API_BASE: "https://api.veraks.ru"`.

- [ ] **Step 2: Secret** — в `secret.yaml` добавить `TBANK_TERMINAL_KEY`, `TBANK_PASSWORD` из `.Values.secrets.tbank*`; в `values.yaml` — плейсхолдеры; в deploy-workflow — `--set secrets.tbankTerminalKey=${{ secrets.VERAKS_TBANK_TERMINAL_KEY }}` и пароль. Тестовые креды завести как org/repo-секреты (значения — из кабинета, НЕ в git).

- [ ] **Step 3: Деплой** — собрать бэкенд-образ, задеплоить; проверить, что бэкенд поднялся и `/webhooks/payments/tbank` отвечает 401 на пустой запрос (эндпоинт есть).

- [ ] **Step 4: Прогон 5 тестов ТБанка** — на veraks.ru: оплата картой `4300 0000 0000 0777` (Тест 1), отказ (Тест 2), возврат через admin-эндпоинт (Тест 3), чек `4000 0000 0000 0101` (Тест 7), чек возврата (Тест 8) → «Проверить» в кабинете зелёное.

- [ ] **Step 5: Commit (infra)** — `git commit -am "deploy: конфиг и секреты эквайринга ТБанк"`

---

## Self-Review

- **Покрытие спека:** конфиг (T1), Token (T2), счёт/вид/миграция (T3), проводка по провайдеру (T4), порт возврата (T5), адаптер Init+Cancel+Receipt (T6), use-case возврата (T7), вебхук (T8), wiring+refund-эндпоинт (T9), деплой+сертификация (T10). Рекуррент и выплаты — вне этапа 1 (по спеку). ✓
- **Плейсхолдеры:** номер миграции `00XX` — назначается `alembic revision`; карты Теста 2/8 и точный формат `Cancel`/`Receipt` — берутся из кабинета/доки при прогоне (отмечено). Иных TODO нет.
- **Согласованность типов:** `CheckoutIntent(confirmation_url, provider_subscription_id)` и `RefundResult(provider_payment_id, status)` едины в T5/T6/T7; `make_token/verify_token` — в T2/T6/T8; `OPS_CASH_TBANK` — в T3/T4/T7.
