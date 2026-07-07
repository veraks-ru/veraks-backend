# Интеграция приёма платежей ТБанк (эквайринг) — дизайн

Дата: 2026-07-07. Область: домен `billing`. Провайдер: ТБанк (эквайринг «Универсальное
подключение», API v2, база `https://securepay.tinkoff.ru/v2`). Тестовый терминал — DEMO.

## 1. Цель и критерий готовности

Подключить реальный приём платежей за подписку через ТБанк вместо заглушки `local://` и
пройти сертификацию ТБанка (обязательна, чтобы «начать принимать оплату»):

- **Тест 1** — успешная оплата (карта `4300 0000 0000 0777`).
- **Тест 2** — неуспешная оплата (карта-отказ; наш вебхук корректно обрабатывает `REJECTED`).
- **Тест 3** — возврат (метод `/v2/Cancel`).
- **Тест 7** — формирование чека 54-ФЗ (объект `Receipt` в `Init`, карта `4000 0000 0000 0101`).
- **Тест 8** — чек возврата (Receipt при `/v2/Cancel`).

Критерий готовности этапа 1: на `veraks.ru` (демо-кластер, тестовый терминал) все пять
тестов проходят, кнопка «Проверить» в кабинете ТБанка зелёная; подписка активируется по
вебхуку; `mypy`/`ruff`/`pytest` чистые.

## 2. Что уже есть (не трогаем без нужды)

Гексагональная нарезка `app/modules/billing/{domain,ports,application,adapters,api}`.

- Порт `SubscriptionCheckoutGateway.create_checkout(subscription_id, amount_kopecks,
  description) -> CheckoutIntent(confirmation_url, provider_subscription_id)` — под него
  пишем адаптер ТБанк.
- Use-case `RecordSubscriptionPayment` (приём вебхука → касса OPERATIONS): идемпотентность
  по `(provider, provider_payment_id)` (дублируется `UNIQUE` в БД), сверка `amount==price`,
  проводка `SUBSCRIPTION_PAYMENT`, активация подписки. Переиспользуем как есть.
- Провайдер `PaymentProvider.TBANK = "tbank"` уже объявлен в домене и в enum БД.
- Фронт: `POST /billing/subscriptions {plan}` → редирект браузера на `confirmation_url`.
  Контракт совпадает с hosted-страницей ТБанк — **фронт не меняем**.
- Чистая доменная функция подписи вебхука `domain/webhooks.py` (HMAC) — для ТБанк не
  подходит (там `Token` = SHA-256 полей), пишем отдельную.

## 3. Объём этапа 1 (сертификация) и что позже

**В этапе 1:** конфиг, подпись `Token`, адаптер `Init` (с `Receipt`), вебхук ТБанк,
возврат (`Cancel` + чек возврата), проводки, wiring, тесты.

**Не в этапе 1 (отдельные под-проекты, свои спеки):**
- Этап 2 — автопродление (рекуррент): `Recurrent=Y`+`CustomerKey`, хранение `RebillId`
  (миграция в `subscriptions`), ARQ-задача списания `/v2/Charge` перед концом периода,
  переход в `past_due` при отказе.
- Этап 3 — выплаты призов через ТБанк: реализация `PayoutGateway.send_payout` через API
  массовых выплат (отдельный продукт/креды), вебхук результата, касса PRIZE.

## 4. Компоненты этапа 1

### 4.1. Конфиг — `app/config.py`
Новая группа `TBankSettings` (префикс `TBANK_`):
- `terminal_key: str` — Terminal Key.
- `password: str` — пароль терминала (секрет).
- `api_base_url: str = "https://securepay.tinkoff.ru/v2"`.
- `taxation: str = "usn_income"` — СНО для чека (ИП на УСН «доходы»).
- `enabled: bool` — включён ли ТБанк (иначе поведение как сейчас).

Fail-closed валидатор: вне `local` при `enabled` требуются непустые `terminal_key`/`password`.
Success/Fail/Notification URL — из `app.public_web_base`/`public_api_base` (или новые
настройки), по умолчанию `https://veraks.ru` и `https://api.veraks.ru`.

### 4.2. Подпись Token — `app/modules/billing/domain/tbank_signing.py` (чистый модуль)
- `make_token(params: Mapping[str, str|int|bool], password: str) -> str`: берём **только
  скалярные** поля корня (исключая `Token`, `Receipt`, `DATA`, `Shops`, `Receipts`),
  добавляем `{"Password": password}`, сортируем по ключу, конкатенируем значения
  (bool → `"true"/"false"`), `sha256(...).hexdigest()`.
- `verify_token(payload: Mapping, password: str) -> bool`: пересчёт `make_token` по всем
  скалярным полям уведомления (кроме `Token`) и constant-time сравнение с `payload["Token"]`.
- Юнит-тесты по эталонному примеру из документации ТБанк.

### 4.3. Адаптер — `app/modules/billing/adapters/tbank_gateway.py`
`TBankSubscriptionCheckoutGateway` реализует `SubscriptionCheckoutGateway`:
- `create_checkout(...)`: формирует `Init`:
  - `TerminalKey`, `Amount` (копейки), `OrderId` (см. 5.1), `Description`,
    `NotificationURL`, `SuccessURL`, `FailURL`, `Receipt` (см. 4.6), `Token`.
  - `POST {api_base}/Init` (httpx, таймаут, повтор на сетевые ошибки).
  - Успех (`Success=true`, `Status=NEW`) → `CheckoutIntent(confirmation_url=PaymentURL,
    provider_subscription_id=str(PaymentId))`.
  - Ошибка (`Success=false`) → доменная `PaymentGatewayError(ErrorCode, Message)` →
    маппится в HTTP в `app/main.py`.
- Метод возврата (не из порта checkout — отдельный интерфейс `PaymentRefundGateway`):
  `cancel_payment(provider_payment_id, amount_kopecks, receipt) -> RefundResult`:
  `POST {api_base}/Cancel` c `TerminalKey`, `PaymentId`, `Amount`, `Receipt`, `Token`.

HTTP-клиент — общий `httpx.AsyncClient` (внедряется, чтобы мокать в тестах).

### 4.4. Вебхук — `app/modules/billing/api/router.py` + `schemas.py`
Новый эндпоинт `POST /billing/webhooks/payments/tbank`:
- Схема тела `TBankNotification` (по формату ТБанк: `TerminalKey, OrderId, Success, Status,
  PaymentId, ErrorCode, Amount, RebillId?, CardId?, Token, ...`).
- Верификация: `verify_token(payload, settings.tbank.password)` (не HMAC/`x-signature`).
  Провал → 401.
- Маршрутизация по `Status`:
  - `CONFIRMED` (и `AUTHORIZED` для одностадийного) → `RecordSubscriptionPayment(
    provider=TBANK, provider_payment_id=str(PaymentId), amount_kopecks=Amount,
    subscription_id=<из OrderId>)` → подписка активна.
  - `REJECTED`/`DEADLINE_EXPIRED`/`CANCELED` → фиксируем неуспех (лог/аудит), подписку не
    активируем (Тест 2).
  - `REFUNDED`/`PARTIAL_REFUNDED` → отметить платёж/подписку (см. 4.5).
- Ответ телом **`OK`** (text/plain, 200) — иначе ТБанк повторяет уведомление.

### 4.5. Возврат — use-case `RefundSubscriptionPayment` (`application/use_cases.py`)
Инициатор — админ (RBAC), поскольку это движение денег. Поток:
1. Найти `Payment` по id; проверить, что это успешный платёж ТБанк.
2. `PaymentRefundGateway.cancel_payment(provider_payment_id, amount, receipt_возврата)`.
3. Проводка-сторно в кассе OPERATIONS: дебет `ops:revenue:subscriptions`, кредит
   `ops:cash:tbank` на сумму возврата (обратна платежу), `kind=SUBSCRIPTION_REFUND`
   (новый вид, привязан к OPERATIONS), `external_ref=provider_payment_id+":refund"`.
4. `Payment.status = REFUNDED`; при полном возврате подписку → `canceled` (или
   пропорционально, если частичный — вне этапа 1, только полный возврат).
Идемпотентность: повторный возврат того же платежа запрещён (проверка статуса + возможно
partial UNIQUE на `external_ref` для refund, по аналогии с prize-payout guard).

### 4.6. Чек 54-ФЗ (Receipt)
Строим `Receipt` в `Init` и в `Cancel`:
- `Taxation = settings.tbank.taxation` (`usn_income`).
- `Email` или `Phone` плательщика (из профиля/ЕСИА; на этапе теста — заглушка/настройка).
- `Items`: одна позиция «Подписка Веракс, тариф <plan>», `Price=Amount`, `Quantity=1`,
  `Amount=Amount`, `Tax="none"` (ИП на УСН без НДС), `PaymentMethod="full_payment"`,
  `PaymentObject="service"`.
- Реально фискализируется, когда к терминалу привязана онлайн-касса; на DEMO проходит
  Тест 7/8.

### 4.7. План счетов и проводки — `domain/chart.py`, `domain/ledger.py`
- Новый счёт `OPS_CASH_TBANK = "ops:cash:tbank"` (касса OPERATIONS) — сид-миграцией.
- Платёж подписки ТБанк: дебет `ops:cash:tbank`, кредит `ops:revenue:subscriptions`
  (сейчас захардкожен `ops:cash:yookassa` — выбираем счёт по провайдеру платежа).
- Новый `TransactionKind.SUBSCRIPTION_REFUND → OPERATIONS`.

### 4.8. Composition root — `app/modules/billing/api/dependencies.py`
- `get_checkout_gateway`: `local` → заглушка; иначе, если `settings.tbank.enabled` →
  `TBankSubscriptionCheckoutGateway`; иначе прежнее поведение.
- Новый провайдер `get_refund_gateway` / `get_refund_subscription_payment`.
- Верификатор вебхука ТБанк — отдельная зависимость (Token, не HMAC).

## 5. Ключевые решения

### 5.1. OrderId ↔ подписка
`OrderId = str(subscription_id)` (подписка `incomplete`, ожидается один первый платёж).
Вебхук парсит `OrderId` как UUID подписки и передаёт в `RecordSubscriptionPayment`.
Идемпотентность приёма — по `(tbank, PaymentId)`. (Для рекуррента на этапе 2 OrderId станет
`{subscription_id}-{ts}`.)

### 5.2. Одностадийная оплата
`PayType` по умолчанию (одностадийная): платёж сразу `CONFIRMED`, отдельный `Confirm` не
нужен. «Возврат» = полный `Cancel` подтверждённого платежа. Двухстадийную не вводим.

### 5.3. Сумма и подписка
Сумму берём из `subscription.price_kopecks` (карта тарифов из `BillingSettings`), в `Init`
и в проверке вебхука сверяем — защита от подмены (существующая `InvalidAmountError`).

### 5.4. Безопасность
Пароль терминала — только в K8s-секрете `veraks-secrets` (`TBANK_PASSWORD`), не в git.
Тестовые креды туда же (`TBANK_TERMINAL_KEY=…DEMO`). Вебхук проверяет `Token`; тело `OK`.

## 6. Изменяемые/новые файлы

- `app/config.py` (+`TBankSettings`, URL-базы, валидатор).
- `app/modules/billing/domain/tbank_signing.py` (новый).
- `app/modules/billing/domain/chart.py` (+`OPS_CASH_TBANK`), `domain/ledger.py`
  (+`SUBSCRIPTION_REFUND`).
- `app/modules/billing/ports/gateways.py` (+`PaymentRefundGateway`, `RefundResult`).
- `app/modules/billing/adapters/tbank_gateway.py` (новый: checkout + refund).
- `app/modules/billing/application/use_cases.py` (выбор кэш-счёта по провайдеру;
  `RefundSubscriptionPayment`).
- `app/modules/billing/api/{router,schemas,dependencies}.py` (вебхук ТБанк, refund-эндпоинт,
  wiring, верификатор).
- `alembic/versions/00XX_tbank_ops_account.py` (сид `ops:cash:tbank`; refund guard).
- `tests/billing/` (подпись Token; Init через мок httpx; вебхук CONFIRMED/REJECTED; refund;
  проводки/идемпотентность).

## 7. Тестирование

- Юнит: `make_token`/`verify_token` (эталон из докидоки); адаптер `Init`/`Cancel` с мок-httpx;
  вебхук (валидный/битый Token, CONFIRMED→активация, REJECTED→без активации, идемпотентность);
  refund (сторно-проводка, статус, запрет повторного).
- Ручной прогон на демо-кластере (тестовый терминал) — пять тестов ТБанка.

## 8. Что нужно от человека

- В кабинете ТБанк (при необходимости) включить уведомления «По протоколу HTTP» — но URL мы
  передаём в `Init` (`NotificationURL`), так что, вероятно, ручная настройка не нужна.
- Email/телефон плательщика для чека: на этапе теста — из настройки/заглушки; в проде — из
  профиля пользователя.

## 9. Открытые вопросы (уточнить при реализации)

- Точный формат тела уведомления и набор скалярных полей для `Token` — сверить по
  `developer.tbank.ru` (endpoint уведомлений).
- Карта-отказ для Теста 2 и карта возврата для Теста 8 — взять из кабинета при прогоне.
- Префикс billing-роутера (`/billing`) — подтвердить для точного `NotificationURL`.
